"""Feature 003, SC-001: the core of a document does not depend on the derived store, and the plan does not depend on the
order fields were declared in."""

from __future__ import annotations

import random
import tempfile
from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.deferred import plan
from aduku.ion_codec import FrozenStruct, parse, to_canonical
from aduku.model import DerivedRecord, EntryInput
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.deferred_helpers import PLAIN, build, deferred
from tests.service_helpers import ALICE, make_service, schema_text

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
FIELDS = {
    "/once": deferred(hook="fx.score@1"),
    "/fetched": deferred(
        hook="fx.score@1", refresh="per_fetch", ttl=chr(34) + "5m" + chr(34), on_failure="serve_stale"
    ),
    "/later": deferred(
        hook="fx.score@1", refresh="scheduled::{every: " + chr(34) + "1h" + chr(34) + "}", on_failure="absent"
    ),
}
DEFERRED_KEYS = {"once", "fetched", "later"}
WORDS = st.sampled_from(["a", "Bo", "cat", "Dee Dee"])
WRITES = st.lists(
    st.tuples(st.sampled_from(["/name", "/country", "/currency"]), st.sampled_from(["set", "delete"]), WORDS),
    min_size=1,
    max_size=8,
)


@st.composite
def stores(draw):  # type: ignore[no-untyped-def]
    """Arbitrary derived-store content: some fields with a record of any status and age, some with none."""
    records = []
    for path in FIELDS:
        if draw(st.booleans()):
            has_value = draw(st.booleans())
            records.append(
                DerivedRecord(
                    doc="d1",
                    field=path,
                    value=draw(st.sampled_from(["1", "2", "7"])) if has_value else None,
                    has_value=has_value,
                    as_of=NOW if has_value else None,
                    input_fingerprint=draw(st.sampled_from(["f" * 64, "0" * 64])),
                    hook=draw(st.sampled_from(["fx.score@1", "other@1"])),
                    shape=draw(st.sampled_from(["s" * 64, "t" * 64])),
                    schema_version=1,
                    status=draw(st.sampled_from(["ok", "stale", "error"])),
                    error=draw(st.sampled_from([None, "timeout", "bad_value"])),
                    attempts=draw(st.integers(1, 3)),
                    last_attempt=NOW,
                    invalidated=draw(st.booleans()),
                )
            )
    return records


def core(document: object) -> str:
    assert isinstance(document, FrozenStruct)
    return to_canonical(FrozenStruct({k: v for k, v in document.items() if k not in DEFERRED_KEYS}))


def check(storage: Storage, writes, content) -> None:  # type: ignore[no-untyped-def]
    built = build(storage, ", ".join(f'"{p}": {d}' for p, d in FIELDS.items()), sources=["fx.score@1"])
    reference = make_service(MemoryStorage(), schema=schema_text(fields=PLAIN))
    for service, doc in ((built.service, "d1"), (reference, "d1")):
        for path, op, value in writes:
            service.append(doc, [EntryInput("user", path, op, value if op == "set" else None)], contributor=ALICE)  # type: ignore[arg-type]
    with storage.transaction() as tx:
        tx.put_derived(content)  # whatever the store holds
    shown = built.service.get("d1")
    expected = reference.get("d1")
    assert core(shown.document) == core(expected.document)  # the merged document and its computed fields
    assert shown.model_errors == expected.model_errors
    calls = built.calls("fx.score@1")
    report = built.service.rebuild("d1")
    assert report.identical and built.calls("fx.score@1") == calls  # a rebuild makes no source call
    with storage.transaction() as tx:
        tx.delete_derived()
    assert core(built.service.get("d1").document) == core(expected.document)  # nor does emptying the store


@given(WRITES, stores())
def test_the_core_is_the_same_for_any_content_of_the_store_in_memory(writes, content) -> None:  # type: ignore[no-untyped-def]
    check(MemoryStorage(), writes, content)


@given(WRITES, stores())
def test_the_core_is_the_same_for_any_content_of_the_store_in_sqlite(writes, content) -> None:  # type: ignore[no-untyped-def]
    with tempfile.TemporaryDirectory() as folder:
        storage = SqliteStorage(f"{folder}/p.db")
        try:
            check(storage, writes, content)
        finally:
            storage.close()


@given(st.randoms(use_true_random=False), stores())
def test_the_plan_is_the_same_for_any_declaration_order(rng: random.Random, content) -> None:  # type: ignore[no-untyped-def]
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_hook("fx.score", 1, lambda inputs, timeout: 1)
    names = list(FIELDS)
    other = names[:]
    rng.shuffle(other)

    def schema(order):  # type: ignore[no-untyped-def]
        body = ", ".join(f'"{p}": {FIELDS[p]}' for p in order)
        text = (
            'schema::{id: "t", version: 1, tiers: [user, admin, backfill], '
            f'fields: {{"/name": {{type: string, merge: lww}}, "/currency": {{type: string, merge: lww}}, {body}}}}}'
        )
        return load_schema(text, behaviors)

    document = parse('{name: "a", currency: "EUR"}')
    records = {r.field: r for r in content}
    one, two = plan(schema(names), document, records, NOW), plan(schema(other), document, records, NOW)
    assert [(f.path, f.status, f.action, f.include) for f in one.fields] == [
        (f.path, f.status, f.action, f.include) for f in two.fields
    ]
