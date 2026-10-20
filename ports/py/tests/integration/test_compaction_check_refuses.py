"""Feature 005 (FR-023): a compaction that would change an answer, or lose a key, is refused and nothing changes."""

import pytest

import aduku.engine.compaction as compaction_engine
from aduku.engine.layer_codec import decode_layer, encode_layer
from aduku.errors import ErrorCode, RejectedError
from aduku.model import CompactionPlan
from aduku.storage.base import Storage
from tests.service_helpers import ALICE, FIELDS, JOB, make_service, put, schema_text

SCHEMA = (
    FIELDS.replace(
        '"/age": {type: int, merge: max}', '"/age": {type: int, merge: max, validate: [range::{min: 0, max: 50}]}'
    )
    + ', "/n": {type: int, merge: "acme.add@1"}'
)


def build(backend: Storage):  # type: ignore[no-untyped-def]
    db = make_service(backend, schema=schema_text(fields=SCHEMA))
    db.append("d", [put("/name", "ann"), put("/age", 3)], contributor=ALICE)
    db.append("d", [put("/age", 99, "backfill"), put("/tags", ("x",), "backfill")], contributor=JOB, lenient=True)
    db.append("d", [put("/n", 1, idem_key="K1"), put("/age", 9)], contributor=ALICE)
    return db


def snapshot(backend: Storage):  # type: ignore[no-untyped-def]
    with backend.read() as tx:
        return (
            tx.entries("d"),
            tx.archived_entries("d"),
            tx.compactions("d"),
            tx.epoch("d"),
            tx.idem_key_seen("d", "K1"),
        )


def tampered(monkeypatch: pytest.MonkeyPatch, change) -> None:  # type: ignore[no-untyped-def]
    real = compaction_engine.plan

    def wrong(*args, **kwargs):  # type: ignore[no-untyped-def]
        return change(real(*args, **kwargs))

    monkeypatch.setattr(compaction_engine, "plan", wrong)


def with_layer(compaction: CompactionPlan, tier: str, edit):  # type: ignore[no-untyped-def]
    synthetic = []
    for entry in compaction.synthetic:
        if entry.tier == tier:
            entry = type(entry)(**{**entry.__dict__, "value": encode_layer(edit(decode_layer(entry.value)))})  # type: ignore[arg-type]
        synthetic.append(entry)
    return CompactionPlan(**{**compaction.__dict__, "synthetic": tuple(synthetic)})


def change_a_value(compaction: CompactionPlan) -> CompactionPlan:
    return with_layer(compaction, "user", lambda layer: {p: ("hacked" if v == "ann" else v) for p, v in layer.items()})


def drop_a_path(compaction: CompactionPlan) -> CompactionPlan:
    return with_layer(compaction, "backfill", lambda layer: {p: v for p, v in layer.items() if p[0].name != "tags"})


def drop_the_keys(compaction: CompactionPlan) -> CompactionPlan:
    return CompactionPlan(**{**compaction.__dict__, "retired_keys": ()})


def drop_the_base_errors(compaction: CompactionPlan) -> CompactionPlan:
    record = type(compaction.record)(**{**compaction.record.__dict__, "base_errors": "[]"})
    return CompactionPlan(**{**compaction.__dict__, "record": record})


def drop_sources(compaction: CompactionPlan) -> CompactionPlan:
    return CompactionPlan(**{**compaction.__dict__, "sources": compaction.sources[:-1]})


@pytest.mark.parametrize("change", [change_a_value, drop_a_path, drop_the_keys, drop_the_base_errors, drop_sources])
def test_a_wrong_compaction_is_refused_with_the_first_differences_and_changes_nothing(
    backend: Storage, monkeypatch: pytest.MonkeyPatch, change
) -> None:  # type: ignore[no-untyped-def]
    db = build(backend)
    before = snapshot(backend)
    assert db.get("d").model_errors  # the lenient write left a model error whose attribution must be kept
    tampered(monkeypatch, change)
    with pytest.raises(RejectedError) as caught:
        db.compact("d", through_seq=db.last_seq("d"))
    assert caught.value.code is ErrorCode.COMPACTION_CHECK_FAILED
    assert 1 <= len(caught.value.reasons) <= 5 and all(r.message for r in caught.value.reasons)
    assert snapshot(backend) == before


def test_the_honest_compaction_passes_the_same_check(backend: Storage) -> None:
    db = build(backend)
    report = db.compact("d", through_seq=db.last_seq("d"))
    assert report.checked_paths >= 4 and db.get("d").model_errors
    (error,) = db.get("d").model_errors
    assert error.introduced_by_request is not None and error.path_scope == "/age"
