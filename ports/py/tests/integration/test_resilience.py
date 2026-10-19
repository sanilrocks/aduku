"""One unmergeable entry must never make a document unreadable (review: admission and derivation hardening)."""

import math

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.admission import check_request
from aduku.engine.derive import derive
from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.ion_codec import Annotated, FrozenStruct, to_canonical
from aduku.schema.loader import load_schema
from aduku.storage.base import RequestRecord, Storage
from tests.engine_helpers import FIELDS
from tests.helpers import make_entry
from tests.service_helpers import ALICE, ROOT, make_service, put, schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
SCHEMA = load_schema(schema_text(fields=FIELDS + ', "/f": {type: float, merge: max}'))


def codes(reasons: list) -> list[ErrorCode]:  # type: ignore[type-arg]
    return [r.code for r in reasons]


# -- admission checks that a rule can really merge the value, children included ------------------------------------------


def test_a_value_the_rule_cannot_order_is_refused_at_admission() -> None:
    assert codes(check_request(SCHEMA, [put("/f", math.nan)], ALICE, False, BEHAVIORS)) == [ErrorCode.TYPE_MISMATCH]
    assert check_request(SCHEMA, [put("/f", 1.5)], ALICE, False, BEHAVIORS) == []


def test_the_children_of_a_struct_value_are_checked_against_their_own_fields() -> None:
    bad = FrozenStruct({"n": "not a number"})
    reasons = check_request(SCHEMA, [put("/address", bad)], ALICE, False, BEHAVIORS)
    assert codes(reasons) == [ErrorCode.TYPE_MISMATCH] and "/address/n" in reasons[0].message
    assert "not a number" not in reasons[0].message
    deep = FrozenStruct({"geo": FrozenStruct({"lat": "north"})})
    assert codes(check_request(SCHEMA, [put("/address", deep)], ALICE, False, BEHAVIORS)) == [ErrorCode.TYPE_MISMATCH]
    ok = FrozenStruct({"n": 3, "city": "Paris", "geo": FrozenStruct({"lat": 1})})
    assert check_request(SCHEMA, [put("/address", ok)], ALICE, False, BEHAVIORS) == []
    unlisted = FrozenStruct({"unlisted": "anything"})  # a child the schema does not declare is as unknown as its path
    assert codes(check_request(SCHEMA, [put("/address", unlisted)], ALICE, False, BEHAVIORS)) == [
        ErrorCode.UNKNOWN_PATH
    ]


def test_the_service_applies_these_checks(backend: Storage) -> None:
    service = make_service(backend, schema=schema_text(fields=FIELDS + ', "/f": {type: float, merge: max}'))
    with pytest.raises(RejectedError):
        service.append("d1", [put("/f", math.nan)], contributor=ALICE)
    with pytest.raises(RejectedError):
        service.append("d1", [put("/address", FrozenStruct({"n": "x"}))], contributor=ALICE)
    assert service.get("d1").document == FrozenStruct()


# -- derivation skips what it cannot merge, atomically, and says so ------------------------------------------------------


def entry(seq, tier, path, value, op="set"):  # type: ignore[no-untyped-def]
    return make_entry(seq=seq, request_id=f"r{seq}", tier=tier, path=path, op=op, value=value)


def test_derivation_survives_an_entry_that_cannot_be_merged() -> None:
    entries = [entry(1, "user", "/f", 1.0), entry(2, "user", "/f", math.nan), entry(3, "user", "/name", "kept")]
    derived = derive(SCHEMA, BEHAVIORS, entries)
    assert to_canonical(derived.document) == '{f:1.0e0,name:"kept"}'.replace("1.0e0", "1e0")
    assert derived.state.skipped == (2,)


def test_a_skipped_entry_leaves_no_trace_of_its_other_writes() -> None:
    entries = [
        entry(1, "user", "/address/n", 5),
        entry(2, "user", "/address", FrozenStruct({"zip": "z", "n": "x"})),  # n: 5 vs "x" cannot merge
        entry(3, "user", "/address/city", "Paris"),
    ]
    derived = derive(SCHEMA, BEHAVIORS, entries)
    assert derived.document["address"] == FrozenStruct({"n": 5, "city": "Paris"})  # no zip: the whole entry was skipped
    assert derived.state.skipped == (2,)


def test_entries_of_a_tier_the_schema_no_longer_has_are_skipped() -> None:
    entries = [entry(1, "legacy", "/name", "old"), entry(2, "user", "/name", "new")]
    derived = derive(SCHEMA, BEHAVIORS, entries)
    assert derived.document["name"] == "new" and derived.state.skipped == (1,)


def test_get_reports_what_was_skipped_and_still_works(backend: Storage) -> None:
    service = make_service(backend)
    with backend.transaction() as tx:  # a ledger written under an older schema, with a tier this one lacks
        tx.append_request(
            RequestRecord("old", "d1", "x"),
            [make_entry(seq=1, request_id="old", tier="legacy", path="/name", value="ancient")],
        )
    service.append("d1", [put("/name", "fresh")], contributor=ALICE)
    result = service.get("d1")
    assert result.document["name"] == "fresh" and result.skipped == (1,)
    service._views.clear()
    assert service.get("d1").skipped == (1,)  # also from the stored view
    service._views.clear()
    service._states.clear()
    with backend.transaction() as tx:
        tx.drop_view("d1")
    assert service.get("d1").skipped == (1,) and service.get("d1").document["name"] == "fresh"  # and from the ledger
    with pytest.raises(SchemaError):  # a service started now refuses the schema: it drops a tier the ledger uses
        make_service(backend)


# -- the in-memory state is bounded --------------------------------------------------------------------------------------


def test_only_the_most_recently_used_documents_keep_an_in_memory_state(backend: Storage) -> None:
    service = make_service(backend)
    service.state_cache_size = 2
    for doc in ("d1", "d2", "d3"):
        service.append(doc, [put()], contributor=ALICE)
    assert len(service._states) == 2 and "d1" not in service._states
    assert service.get("d1").document["name"] == "Alice"  # an evicted document is simply derived again
    assert len(service._states) == 2


# -- an append uses one write transaction and stores the view in it ------------------------------------------------------


class Counting:
    def __init__(self, inner: Storage) -> None:
        self.inner, self.writes, self.reads = inner, 0, 0

    def transaction(self):  # type: ignore[no-untyped-def]
        self.writes += 1
        return self.inner.transaction()

    def read(self):  # type: ignore[no-untyped-def]
        self.reads += 1
        return self.inner.read()

    def audit_backend(self):  # type: ignore[no-untyped-def]
        return self.inner.audit_backend()

    def close(self) -> None:
        self.inner.close()


def test_an_append_is_one_write_transaction_and_carries_its_view(backend: Storage) -> None:
    counting = Counting(backend)
    service = make_service(counting)  # type: ignore[arg-type]
    counting.reads = counting.writes = 0  # starting up reads the tiers in use, to check the active schema
    result = service.append("d1", [put("/name", "A"), put("/age", 4)], contributor=ALICE)
    # one read to warm the in-memory state outside the write lock (first use only), then one write transaction
    assert (counting.writes, counting.reads) == (1, 1)
    assert result.view is not None and result.view.document["name"] == "A"
    service.append("d1", [put("/age", 5)], contributor=ALICE)
    assert (counting.writes, counting.reads) == (2, 1)  # a warm service reads nothing before writing
    service.get("d1")
    assert (counting.writes, counting.reads) == (2, 2)  # the view stored by the append is served


class Flaky:
    """Fails the first time a view is stored, as a disk error would."""

    def __init__(self, inner: Storage) -> None:
        self.inner, self.failed = inner, False

    def transaction(self):  # type: ignore[no-untyped-def]
        from contextlib import contextmanager

        @contextmanager
        def wrapped():  # type: ignore[no-untyped-def]
            with self.inner.transaction() as tx:
                yield _FlakyTx(tx, self)

        return wrapped()

    def read(self):  # type: ignore[no-untyped-def]
        return self.inner.read()

    def audit_backend(self):  # type: ignore[no-untyped-def]
        return self.inner.audit_backend()

    def close(self) -> None:
        self.inner.close()


class _FlakyTx:
    def __init__(self, tx, owner) -> None:  # type: ignore[no-untyped-def]
        self._tx, self._owner = tx, owner

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._tx, name)

    def put_view(self, view) -> None:  # type: ignore[no-untyped-def]
        if not self._owner.failed:
            self._owner.failed = True
            from aduku.errors import StorageError

            raise StorageError("disk full")
        self._tx.put_view(view)


def test_a_failure_after_deriving_leaves_no_trace_in_the_cache_or_the_ledger(backend: Storage) -> None:
    from aduku.errors import StorageError

    service = make_service(Flaky(backend))  # type: ignore[arg-type]
    with pytest.raises(StorageError):
        service.append("d1", [put("/name", "ghost")], contributor=ALICE)
    assert service.list_entries("d1") == [] and "d1" not in service._states
    service.append("d1", [put("/name", "real")], contributor=ALICE)
    assert service.get("d1").document == FrozenStruct({"name": "real", "displayName": Annotated(("computed",), "REAL")})
    assert ROOT  # keep the shared import used
