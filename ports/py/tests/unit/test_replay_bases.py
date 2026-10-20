"""Feature 005 (research R-8, R-10): a replay of a compacted ledger attributes model errors exactly as a replay of the originals."""

from datetime import UTC, datetime, timedelta
from itertools import count

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.compaction import compacted_ledger, plan
from aduku.engine.derive import prepare
from aduku.engine.model_errors import Base, replay
from aduku.schema.loader import load_schema
from tests.fixtures import register_fixtures
from tests.helpers import T0, make_entry
from tests.integration.test_model_errors import FIELDS
from tests.integration.test_version_change import text
from tests.service_helpers import schema_text

BEHAVIORS = BehaviorRegistry.with_builtins()
register_fixtures(BEHAVIORS)
V1 = load_schema(text(1), BEHAVIORS)
V2 = load_schema(text(2, "max_len::{n: 2}"), BEHAVIORS)  # a version that adds a rule (the same folding)
SCHEMAS = {1: V1, 2: V2}
PREPARED = {v: prepare(s, BEHAVIORS) for v, s in SCHEMAS.items()}
NOW = datetime(2026, 2, 1, tzinfo=UTC)
_ = (FIELDS, schema_text)


def entry(seq: int, request: str, tier: str, path: str, value: object, version: int = 1):  # noqa: ANN201
    return make_entry(
        "d",
        seq,
        request_id=request,
        tier=tier,
        path=path,
        value=value,
        schema_version=version,
        tx_time=T0 + timedelta(seconds=seq),
        lenient=True,
    )


def history():  # noqa: ANN201
    return [
        entry(1, "r1", "backfill", "/country", "US"),
        entry(2, "r1", "backfill", "/spouses", ("A", "B")),  # introduces max_spouses_by_country
        entry(3, "r2", "user", "/age", 200),  # introduces range at /age
        entry(4, "r3", "user", "/name", "Bob"),
        entry(5, "r4", "admin", "/spouses", ("A",)),  # resolves the spouses error
        entry(6, "r5", "user", "/name", "Roberto", version=2),  # a version change after the range: max_len 2 now fails
    ]


def run(entries, bases=None, read=V2):  # type: ignore[no-untyped-def]
    return replay(read, SCHEMAS.__getitem__, BEHAVIORS, lambda s: PREPARED[s.version], entries, bases=bases).final


def compact_through(entries, through):  # type: ignore[no-untyped-def]
    before = run([e for e in entries if e.seq <= through], read=V1)
    ids = count(1)
    result = plan(
        entries,
        V1,
        BEHAVIORS,
        PREPARED[1],
        doc="d",
        through_seq=through,
        compaction_id="c1",
        new_id=lambda: f"s{next(ids)}",
        created=NOW,
        policy="p",
        base_errors="",
        base_skipped=before.state.skipped,
    )
    assert result is not None
    return compacted_ledger(result, entries), {"c1": Base(before.errors, before.state.skipped)}


def seen(ext):  # type: ignore[no-untyped-def]
    return (
        [(e.rule_id, e.path_scope, e.introduced_by_request, e.introduced_by_schema_version) for e in ext.errors],
        ext.state.skipped,
        ext.conflicts,
        ext.document,
    )


def test_errors_keep_who_introduced_them_after_a_compaction_with_a_later_version_change() -> None:
    entries = history()
    compacted, bases = compact_through(entries, 3)
    assert [e.seq for e in compacted] == [2, 3, 4, 5, 6][-5:] or len(compacted) < len(entries)
    assert seen(run(compacted, bases)) == seen(run(entries))


def test_without_the_base_everything_would_be_attributed_to_the_compaction() -> None:
    entries = history()
    compacted, _ = compact_through(entries, 3)
    assert seen(run(compacted, {}))[0] != seen(run(entries))[0]  # the base is what keeps the attribution


def test_every_watermark_gives_the_same_replay() -> None:
    entries = history()
    for through in (2, 3, 4, 5):
        compacted, bases = compact_through(entries, through)
        assert seen(run(compacted, bases)) == seen(run(entries)), through


def test_skipped_seqs_of_the_range_come_back_through_the_base() -> None:
    entries = history()
    compacted, bases = compact_through(entries, 3)
    bases = {"c1": Base(bases["c1"].errors, (1,))}  # pretend entry 1 was unusable when the range was folded
    assert 1 in run(compacted, bases).state.skipped
