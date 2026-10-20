"""Feature 005 (research R-9): `explain` over a compacted ledger names synthetic entries and agrees with the old answer."""

from datetime import UTC, datetime
from itertools import count

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.compaction import Observed, compacted_ledger, differences, id_map, plan
from aduku.engine.derive import prepare
from aduku.engine.explain import explain
from aduku.schema.loader import load_schema
from tests.helpers import make_entry
from tests.service_helpers import schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
SCHEMA = load_schema(
    schema_text(
        fields='"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}, "/tags": {type: "list<string>", merge: union}'
    ),
    BEHAVIORS,
)
READY = prepare(SCHEMA, BEHAVIORS)
NOW = datetime(2026, 2, 1, tzinfo=UTC)


def e(seq: int, tier: str, path: str, value: object):  # noqa: ANN201
    return make_entry("d", seq, request_id=f"r{seq}", tier=tier, path=path, value=value)


def compact(entries, through):  # type: ignore[no-untyped-def]
    ids = count(1)
    result = plan(
        entries,
        SCHEMA,
        BEHAVIORS,
        READY,
        doc="d",
        through_seq=through,
        compaction_id="c1",
        new_id=lambda: f"s{next(ids)}",
        created=NOW,
        policy="p",
        base_errors="",
        base_skipped=(),
    )
    assert result is not None
    return result, compacted_ledger(result, entries)


def why(entries, path):  # type: ignore[no-untyped-def]
    return explain(SCHEMA, BEHAVIORS, READY, entries, path, ())


def test_a_winner_inside_the_range_is_named_by_its_synthetic_entry() -> None:
    entries = [
        e(1, "user", "/name", "a"),
        e(2, "user", "/name", "b"),
        e(3, "user", "/name", "c"),
        e(4, "admin", "/age", 9),
    ]
    result, after = compact(entries, 3)
    before = why(entries, "/name")
    now = why(after, "/name")
    assert before.winner == "e-d-3" and now.winner == result.synthetic[0].id and now.value == before.value == "c"
    assert now.inputs == (now.winner,) and now.shadowed == ()  # the one synthetic entry is the input


def test_an_entry_beyond_the_watermark_keeps_its_own_name_and_shadows_the_synthetic_one() -> None:
    entries = [e(1, "user", "/name", "a"), e(2, "user", "/name", "b"), e(3, "user", "/name", "c")]
    result, after = compact(entries, 2)
    now = why(after, "/name")
    assert now.winner == "e-d-3" and now.inputs == ("e-d-3",) and now.shadowed == (result.synthetic[0].id,)


def test_the_comparison_accepts_every_touched_path_of_a_mixed_history() -> None:
    entries = [
        e(1, "backfill", "/age", 3),
        e(2, "user", "/name", "a"),
        e(3, "user", "/tags", ("x",)),
        e(4, "admin", "/tags", ("x", "y")),
        e(5, "user", "/age", 8),
        e(6, "user", "/name", "b"),
        e(7, "admin", "/age", 2),
    ]
    result, after = compact(entries, 6)
    mapping = id_map(result)
    for path in ("/age", "/name", "/tags"):
        pre = Observed("{}", (), (), (), {path: why(entries, path)})
        post = Observed("{}", (), (), (), {path: why(after, path)})
        assert differences(pre, post, mapping) == [], path


def test_a_union_with_a_redundant_source_still_agrees() -> None:
    entries = [e(1, "user", "/tags", ("x",)), e(2, "user", "/tags", ("x", "y")), e(3, "admin", "/tags", ("y",))]
    result, after = compact(entries, 2)
    pre = Observed("{}", (), (), (), {"/tags": why(entries, "/tags")})
    post = Observed("{}", (), (), (), {"/tags": why(after, "/tags")})
    assert differences(pre, post, id_map(result)) == []


def test_a_path_only_a_synthetic_entry_touches_is_found() -> None:
    entries = [e(1, "user", "/name", "a"), e(2, "user", "/age", 1)]
    result, after = compact(entries, 2)
    assert why(after, "/age").winner == result.synthetic[0].id and why(after, "/tags").winner is None
