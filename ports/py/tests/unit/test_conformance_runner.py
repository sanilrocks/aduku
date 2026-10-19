"""The runner can read the case format from contracts/conformance-vectors.md."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.behaviors.validate import Subject
from aduku.errors import ErrorCode, RejectedError
from aduku.ion_codec import FrozenStruct
from aduku.model import AppendResult
from tests.conformance_runner import build_service, load_case, run_case
from tests.fixtures import max_spouses_by_country, register_fixtures

CASE = """case::{
  name: "sample",
  schema: schema::{id: "x", version: 1, tiers: [a, b], fields: {"/f": {type: string, merge: lww}}},
  fixtures: ["acme.max_spouses_by_country@1"],
  steps: [
    { append: { doc: "d1", contributor: {id: "job", tiers: [a]}, entries: [{tier: a, path: "/f", value: "v"}] },
      expect: { accepted: [{seq: 1}] } },
    { get: "d1", expect: { document: '{f:"v"}', model_errors: [] } },
    { activate_schema: 2 },
    { rebuild: "d1", expect: { identical: true } }
  ]
}"""


def test_case_is_parsed() -> None:
    case = load_case(CASE)
    assert case.name == "sample"
    assert [step.kind for step in case.steps] == ["append", "get", "activate_schema", "rebuild"]
    assert len(case.schemas) == 1 and case.schemas[0].startswith("schema::")
    assert case.fixtures == ("acme.max_spouses_by_country@1",)


def test_unknown_step_is_an_error() -> None:
    with pytest.raises(ValueError):
        load_case('case::{name: "x", steps: [{frobnicate: 1}]}')


def test_fixture_validator_and_registration() -> None:
    from aduku.ion_codec import parse

    ok = parse('{country:"US", spouses:["A"]}')
    bad = parse('{country:"US", spouses:["A","B"]}')
    other = parse('{country:"FR", spouses:["A","B"]}')
    assert max_spouses_by_country(Subject(ok, None, ok, FrozenStruct())) == []  # type: ignore[arg-type]
    assert max_spouses_by_country(Subject(other, None, other, FrozenStruct())) == []  # type: ignore[arg-type]
    assert [v.rule_id for v in max_spouses_by_country(Subject(bad, None, bad, FrozenStruct()))] == [  # type: ignore[arg-type]
        "max_spouses_by_country"
    ]
    registry = BehaviorRegistry()
    register_fixtures(registry)
    register_fixtures(registry)  # idempotent
    assert registry.get("acme.max_spouses_by_country", 1) is not None


# --- the runner must fail when expectations are not met --------------------------------------------------------------


class _Service:
    def __init__(
        self, rejection: RejectedError | None = None, entries: tuple[tuple[str, int], ...] = (("e1", 1),)
    ) -> None:
        self.rejection = rejection
        self.entries = entries

    def append(self, doc: str, entries: object, **kwargs: object) -> AppendResult:
        if self.rejection is not None:
            raise self.rejection
        return AppendResult(entries=self.entries, request_id="r1")


def _append_case(expect: str) -> str:
    return (
        'case::{name: "x", steps: [{append: {doc: "d1", contributor: {id: "j", tiers: [a]},'
        f' entries: [{{tier: a, path: "/f", value: "v"}}]}}, expect: {expect}}}]}}'
    )


def test_expected_rejection_but_accepted_fails() -> None:
    case = load_case(_append_case("{rejected: {code: FIELD_NOT_WRITABLE}}"))
    with pytest.raises(AssertionError):
        run_case(case, _Service())


def test_expected_rejection_with_right_code_passes() -> None:
    case = load_case(_append_case("{rejected: {code: FIELD_NOT_WRITABLE}}"))
    run_case(case, _Service(RejectedError(ErrorCode.FIELD_NOT_WRITABLE)))


def test_wrong_rejection_code_fails() -> None:
    case = load_case(_append_case("{rejected: {code: FIELD_NOT_WRITABLE}}"))
    with pytest.raises(AssertionError):
        run_case(case, _Service(RejectedError(ErrorCode.UNKNOWN_PATH)))


def test_unexpected_rejection_fails() -> None:
    case = load_case(_append_case("{accepted: [{seq: 1}]}"))
    with pytest.raises(AssertionError):
        run_case(case, _Service(RejectedError(ErrorCode.UNKNOWN_PATH)))


def test_accepted_sequence_numbers_must_match() -> None:
    case = load_case(_append_case("{accepted: [{seq: 2}]}"))
    with pytest.raises(AssertionError):
        run_case(case, _Service())
    run_case(load_case(_append_case("{accepted: [{seq: 1}]}")), _Service())


def test_accepted_count_must_match() -> None:
    case = load_case(_append_case("{accepted: [{seq: 1}, {seq: 2}]}"))
    with pytest.raises(AssertionError):
        run_case(case, _Service())
    with pytest.raises(AssertionError):
        run_case(load_case(_append_case("{accepted: []}")), _Service())


def test_no_expectation_accepts_any_success() -> None:
    run_case(load_case(_append_case("{}")), _Service())


# -- feature 003 steps ------------------------------------------------------------------------------------------------


def test_the_new_steps_are_parsed() -> None:
    case = load_case(
        'case::{name: "n", schema: schema::{id: "t", version: 1, tiers: [a], fields: {}}, steps: ['
        '{clock: {advance: "10m"}}, {source_calls: "fx.score@1", expect: {count: 1}},'
        '{invalidate: {doc: "d1", field: "/x", contributor: {id: "u", tiers: [a]}}, expect: {marked: 1}},'
        '{notify: {topic: "t"}, expect: {marked: 0}},'
        '{scheduler_run: {hook: "fx.rate@1", contributor: {id: "u", tiers: [a]}}, expect: {calls: 1}},'
        '{get: {doc: "d1", consistency: fresh, include: [pending]}, expect: {}}]}'
    )
    assert [step.kind for step in case.steps] == [
        "clock", "source_calls", "invalidate", "notify", "scheduler_run", "get",
    ]  # fmt: skip


def test_the_clock_step_moves_the_manual_clock() -> None:
    from datetime import timedelta

    case = load_case(
        'case::{name: "n", clock: {start: 2026-10-19T12:00:00Z, step_seconds: 0},'
        ' schema: schema::{id: "t", version: 1, tiers: [a], fields: {}},'
        ' steps: [{clock: {advance: "90m"}}]}'
    )
    service = build_service(case)
    before = service.clock.now()
    run_case(case, service)
    assert service.clock.now() - before == timedelta(minutes=90)


def test_a_clock_step_needs_the_manual_clock() -> None:
    import pytest

    case = load_case(
        'case::{name: "n", schema: schema::{id: "t", version: 1, tiers: [a], fields: {}},'
        ' steps: [{clock: {advance: "1s"}}]}'
    )

    class Plain:
        clock = object()

    with pytest.raises(AssertionError, match="manual clock"):
        run_case(case, Plain())
