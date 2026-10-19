"""Feature 002, research R-15: properties of computed fields (SC-003, SC-004)."""

from __future__ import annotations

import random
import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.compute import compute_fields
from aduku.ion_codec import FrozenStruct, to_canonical
from aduku.model import EntryInput
from aduku.schema.loader import load_schema
from aduku.storage.base import SequentialIds
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, make_service

BEHAVIORS = BehaviorRegistry.with_builtins()
INPUTS = ["/name", "/nickname"]
WORDS = st.sampled_from(["", "a", "Bo", "cat", "Dee Dee"])


@st.composite
def fields(draw):  # type: ignore[no-untyped-def]
    """Computed fields f0..fn; each reads inputs or earlier fields, so the graph is acyclic by construction."""
    count = draw(st.integers(min_value=1, max_value=6))
    decls: list[tuple[str, str, list[str]]] = []
    for index in range(count):
        pool = INPUTS + [f"/f{n}" for n in range(index)]
        macro = draw(st.sampled_from(["upper", "lower", "trim", "concat", "coalesce"]))
        arity = 1 if macro in ("upper", "lower", "trim") else draw(st.integers(min_value=1, max_value=3))
        deps = [draw(st.sampled_from(pool)) for _ in range(arity)]
        decls.append((f"/f{index}", macro, deps))
    return decls


def schema_text(decls, order, extra: str = "") -> str:  # type: ignore[no-untyped-def]
    parts = ['"/name": {type: string, merge: lww}', '"/nickname": {type: string, merge: lww}']
    for index in order:
        path, macro, deps = decls[index]
        args = ", ".join(f'"{d}"' for d in deps)
        parts.append(f'"{path}": {{type: string, computed: {{macro: {macro}, args: [{args}]}}}}')
    return 'schema::{id: "p", version: 1, tiers: [user], ' + extra + " fields: {" + ", ".join(parts) + "}}"


def documents():  # type: ignore[no-untyped-def]
    value = st.one_of(st.none(), WORDS)
    return st.fixed_dictionaries({"name": value, "nickname": value}).map(
        lambda d: FrozenStruct({k: v for k, v in d.items() if v is not None})
    )


def dependents(decls, source: str) -> set[str]:  # type: ignore[no-untyped-def]
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for path, _, deps in decls:
            if path not in found and any(d == source or d in found for d in deps):
                found.add(path)
                changed = True
    return found


@given(fields(), documents(), st.randoms(use_true_random=False))
def test_declaration_order_never_changes_the_document(decls, document, rng: random.Random) -> None:  # type: ignore[no-untyped-def]
    order = list(range(len(decls)))
    shuffled = order[:]
    rng.shuffle(shuffled)
    a = load_schema(schema_text(decls, order), BEHAVIORS)
    b = load_schema(schema_text(decls, shuffled), BEHAVIORS)
    one, two = compute_fields(a, BEHAVIORS, document).document, compute_fields(b, BEHAVIORS, document).document
    assert to_canonical(one) == to_canonical(two)


@given(fields(), documents(), documents())
def test_a_value_changes_only_when_one_of_its_dependencies_changes(decls, first, second) -> None:  # type: ignore[no-untyped-def]
    """Two documents that differ only in /nickname: every field not downstream of /nickname is identical."""
    base = {k: v for k, v in first.items() if k != "nickname"}
    left = FrozenStruct({**base, **({"nickname": first["nickname"]} if "nickname" in first else {})})
    right = FrozenStruct({**base, **({"nickname": second["nickname"]} if "nickname" in second else {})})
    schema = load_schema(schema_text(decls, range(len(decls))), BEHAVIORS)
    a, b = compute_fields(schema, BEHAVIORS, left).document, compute_fields(schema, BEHAVIORS, right).document
    downstream = dependents(decls, "/nickname")
    for path, _, _ in decls:
        if path not in downstream:
            assert a[path[1:]] == b[path[1:]], path


ACTIONS = st.lists(
    st.tuples(st.sampled_from(["/name", "/nickname"]), st.sampled_from(["set", "delete"]), WORDS),
    min_size=1,
    max_size=8,
)


def _agree(decls, actions, storage) -> None:  # type: ignore[no-untyped-def]
    text = schema_text(decls, range(len(decls))).replace("tiers: [user]", "tiers: [backfill, user, admin]")
    service = make_service(storage, schema=text)
    for path, op, value in actions:
        service.append("d1", [EntryInput("user", path, op, value if op == "set" else None)], contributor=ALICE)  # type: ignore[arg-type]
    warm = to_canonical(service.get("d1").document)
    with storage.transaction() as tx:
        tx.drop_view("d1")
    cold = make_service(storage, schema=text, ids=SequentialIds("c"))
    assert to_canonical(cold.get("d1").document) == warm
    assert cold.rebuild("d1").identical


@settings(deadline=None)
@given(fields(), ACTIONS)
def test_scratch_cache_and_rebuild_agree(decls, actions) -> None:  # type: ignore[no-untyped-def]
    _agree(decls, actions, MemoryStorage())


@settings(deadline=None)
@given(fields(), ACTIONS)
def test_scratch_cache_and_rebuild_agree_on_sqlite_too(decls, actions) -> None:  # type: ignore[no-untyped-def]
    with tempfile.TemporaryDirectory() as folder:
        storage = SqliteStorage(f"{folder}/p.db")
        try:
            _agree(decls, actions, storage)
        finally:
            storage.close()
