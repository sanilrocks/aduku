"""Deep nesting, shared contexts, linear resets, deduplicated skips, honest retries, one atomic unit, unknown offsets."""

import time

import pytest

from aduku.behaviors.merge import SchemaMergeContext
from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import derive
from aduku.engine.merge_layers import apply_layer
from aduku.errors import ErrorCode, RejectedError, StorageError
from aduku.ion_codec import TOMBSTONE, FrozenStruct, IonError, normalize, or_placeholder, parse, to_canonical
from aduku.paths import Segment
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from aduku.storage.sqlite import SqliteStorage
from tests.helpers import make_entry
from tests.service_helpers import ALICE, FIELDS, make_service, put, schema_text

AUDIT = "rejection_audit: {enabled: true, mode: full, retention: {max_entries: 20}},"
BLOB_SCHEMA = schema_text(audit=AUDIT, fields='"/blob": {type: any, merge: lww}')
BEHAVIORS = BehaviorRegistry.with_builtins()


def nest(depth: int) -> tuple:  # type: ignore[type-arg]
    value: tuple = ("x",)  # type: ignore[type-arg]
    for _ in range(depth):
        value = (value,)
    return value


# -- deep nesting is a rejection, never a crash ------------------------------------------------------------------------


@pytest.mark.parametrize("request_key", [None, "K"])
@pytest.mark.parametrize("where", ["value", "meta"])
def test_deeply_nested_values_are_rejected_cleanly(backend: Storage, request_key: str | None, where: str) -> None:
    service = make_service(backend, schema=BLOB_SCHEMA)
    entry = put("/blob", nest(3000)) if where == "value" else put("/blob", 1, meta=nest(3000))
    with pytest.raises(RejectedError) as caught:
        service.append("d1", [entry], contributor=ALICE, request_key=request_key)
    assert caught.value.code is ErrorCode.TYPE_MISMATCH
    assert service.list_entries("d1") == [] and len(service.list_rejections("d1")) == 1  # rejected and audited


def test_reasonable_nesting_is_accepted_and_reads_back(backend: Storage) -> None:
    service = make_service(backend, schema=BLOB_SCHEMA)
    service.append("d1", [put("/blob", nest(40))], contributor=ALICE)
    assert service.get("d1").document["blob"] == nest(40)


def test_helpers_survive_any_depth() -> None:
    assert or_placeholder(nest(3000)).startswith("<")  # type: ignore[union-attr]
    with pytest.raises(IonError):
        parse("[" * 3000 + "]" * 3000)


# -- the shared context is used for admission --------------------------------------------------------------------------


def test_admission_reuses_the_services_merge_context(monkeypatch: pytest.MonkeyPatch, backend: Storage) -> None:
    import aduku.engine.admission as admission

    built: list[int] = []

    class Counting(SchemaMergeContext):
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            built.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(admission, "SchemaMergeContext", Counting)
    service = make_service(backend)
    for index in range(10):
        service.append("d1", [put("/age", index)], contributor=ALICE)
    assert built == []


# -- resets over a large layer are linear ------------------------------------------------------------------------------


def test_many_resets_over_a_large_layer_stay_fast_and_correct() -> None:
    ctx = SchemaMergeContext(load_schema(schema_text()), BEHAVIORS)
    acc = {(Segment(f"a{n}"), Segment("x")): n for n in range(20000)}
    layer = {(Segment(f"a{n}"),): TOMBSTONE for n in range(300)} | {(Segment(f"r{n}"),): TOMBSTONE for n in range(300)}
    started = time.perf_counter()
    apply_layer(acc, layer, ctx)
    assert time.perf_counter() - started < 1.0
    assert (Segment("a5"), Segment("x")) not in acc and (Segment("a5"),) in acc  # wiped beneath, marker kept
    assert (Segment("a500"), Segment("x")) in acc  # not under a reset
    assert len(acc) == 20000 - 300 + 600


# -- skipped lists an entry once ----------------------------------------------------------------------------------------


def test_an_entry_is_skipped_once_even_if_it_fails_twice() -> None:
    fields = (
        FIELDS
        + ', "/profile": {type: struct, merge: struct}, "/profile/n": {type: int, merge: max},'
        + ' "/profile/score": {type: int, computed: {macro: sum, args: ["/age"]}}'
    )
    schema = load_schema(schema_text(fields=fields))
    entries = [
        make_entry(seq=1, request_id="a", path="/profile/n", value=5),
        # score is derived here (shadowed) and n cannot merge a string into the 5 (unmergeable): two reasons, one entry
        make_entry(seq=2, request_id="b", path="/profile", value=FrozenStruct({"score": 1, "n": "x"})),
    ]
    derived = derive(schema, BEHAVIORS, entries)
    assert derived.state.skipped == (2,) and derived.document["profile"] == FrozenStruct({"n": 5})


# -- a retry does not hide a failed read -------------------------------------------------------------------------------


def test_a_retry_whose_view_cannot_be_read_says_so(monkeypatch: pytest.MonkeyPatch, backend: Storage) -> None:
    service = make_service(backend)
    service.append("d1", [put()], contributor=ALICE, request_key="K")

    def failing(doc: str, schema: object):  # type: ignore[no-untyped-def]
        raise StorageError("disk error")

    monkeypatch.setattr(service, "_core", failing)  # the view of a retry is the merged document (never a source call)
    with pytest.raises(StorageError):
        service.append("d1", [put()], contributor=ALICE, request_key="K")
    assert len(service.list_entries("d1")) == 1  # nothing was written by the retry


# -- one atomic unit on SQLite -----------------------------------------------------------------------------------------


def test_sqlite_has_one_public_atomic_unit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = SqliteStorage(str(tmp_path / "unit.db"))

    def rows() -> int:
        return storage.connection.execute("SELECT COUNT(*) FROM audit_meta").fetchone()[0]

    with pytest.raises(RuntimeError):
        with storage.atomic():
            storage.connection.execute("INSERT INTO audit_meta (doc, pruned_count) VALUES ('x', 1)")
            raise RuntimeError("roll back")
    assert rows() == 0
    with storage.atomic():  # nested blocks join the outer one
        with storage.atomic():
            storage.connection.execute("INSERT INTO audit_meta (doc, pruned_count) VALUES ('y', 1)")
    assert rows() == 1
    with pytest.raises(RuntimeError):  # the audit backend uses the same unit
        with storage.audit_backend().atomic():
            storage.audit_backend().add_pruned("z", 5)
            raise RuntimeError("roll back")
    assert rows() == 1
    storage.close()


# -- an unknown offset is not UTC ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("2020-01-01T00:00-00:00", "2020-01-01T00:00-00:00"),
        ("2020-01-01T00:00:00-00:00", "2020-01-01T00:00:00-00:00"),
        ("2020-01-01T00:00:00.123456789-00:00", "2020-01-01T00:00:00.123456789-00:00"),
        ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"),
        ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00Z"),
        ("2020-01-01T05:30:00+05:30", "2020-01-01T00:00:00Z"),
        ("2020-01-01", "2020-01-01T"),
    ],
)
def test_unknown_offsets_stay_unknown(text: str, canonical: str) -> None:
    assert to_canonical(parse(text)) == canonical
    assert to_canonical(normalize(parse(text))) == canonical
