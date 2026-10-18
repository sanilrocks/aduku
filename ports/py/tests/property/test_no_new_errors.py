"""Principle X and SPEC 16 item 9: unflagged requests never add a model error; permitted flagged ones always succeed;
schema errors always reject, flagged or not."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aduku.errors import ErrorCode, RejectedError
from aduku.model import EntryInput
from tests.integration.test_model_errors import SCHEMA
from tests.service_helpers import ALICE, JOB, make_service

NAMES = st.sampled_from(["", "Pat", "x" * 25])  # "" and short names are fine; 25 characters breaks max_len 20
AGES = st.sampled_from([-5, 0, 30, 200])
SPOUSES = st.lists(st.sampled_from(["A", "B"]), max_size=3).map(tuple)
COUNTRIES = st.sampled_from(["US", "FR"])

ENTRY = st.one_of(
    st.builds(lambda v: ("/name", v), NAMES),
    st.builds(lambda v: ("/age", v), AGES),
    st.builds(lambda v: ("/spouses", v), SPOUSES),
    st.builds(lambda v: ("/country", v), COUNTRIES),
)
BAD = st.sampled_from([("/nope", 1), ("/age", "text"), ("/name", 5)])  # schema errors
REQUEST = st.tuples(
    st.booleans(), st.lists(ENTRY, min_size=1, max_size=3), st.booleans()
)  # (as backfill, entries, flag)


def identities(service) -> set[tuple[str, str]]:  # type: ignore[no-untyped-def]
    return {(e.rule_id, e.path_scope) for e in service.get("d1").model_errors}


@given(st.lists(REQUEST, min_size=1, max_size=8))
@settings(max_examples=60)
def test_the_rules_hold_over_any_sequence_of_requests(requests) -> None:  # type: ignore[no-untyped-def]
    service = make_service(schema=SCHEMA)
    for as_backfill, entries, flag in requests:
        who, tier = (JOB, "backfill") if as_backfill else (ALICE, "user")
        inputs = [EntryInput(tier, path, "set", value) for path, value in entries]
        before = identities(service)
        try:
            service.append("d1", inputs, contributor=who, lenient=flag)
        except RejectedError as rejection:
            # the only reasons to refuse well-formed requests: the flag is not permitted, or an error would be added
            allowed = {ErrorCode.LENIENT_NOT_PERMITTED, ErrorCode.MODEL_ERROR_INTRODUCED, ErrorCode.VALIDATION_FAILED}
            assert rejection.code in allowed
            if rejection.code is ErrorCode.LENIENT_NOT_PERMITTED:
                assert flag and not as_backfill  # only a writer the schema does not permit is refused the flag
            else:
                assert not (flag and as_backfill)  # a permitted flagged request is never refused for validation
            assert identities(service) == before  # a rejected request changes nothing
            continue
        after = identities(service)
        if not (flag and as_backfill):
            assert after <= before  # unflagged requests never add a model error


@given(st.sampled_from([True, False]), BAD, st.lists(ENTRY, max_size=2))
@settings(max_examples=40)
def test_a_schema_error_rejects_whatever_the_flag_says(flag, bad, extra) -> None:  # type: ignore[no-untyped-def]
    service = make_service(schema=SCHEMA)
    inputs = [EntryInput("backfill", path, "set", value) for path, value in [*extra, bad]]
    with pytest.raises(RejectedError) as caught:
        service.append("d1", inputs, contributor=JOB, lenient=flag)
    assert caught.value.code in {ErrorCode.UNKNOWN_PATH, ErrorCode.TYPE_MISMATCH}
    assert service.list_entries("d1") == []
