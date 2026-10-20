"""Feature 005: the new stable error codes and the history-compacted exception."""

from aduku.errors import ErrorCode, HistoryCompactedError, RejectedError


def test_new_codes_exist_with_their_names() -> None:
    for name in (
        "BAD_VALID_TIME",
        "AS_OF_AMBIGUOUS",
        "HISTORY_COMPACTED",
        "MIXED_SCHEMA_RANGE",
        "IDEM_KEY_MISSING",
        "COMPACTION_CHECK_FAILED",
        "LEDGER_CHANGED",
        "PURGE_REASON_REQUIRED",
        "UNKNOWN_ENTRY",
    ):
        assert ErrorCode(name).value == name


def test_history_compacted_carries_the_earliest_answerable_seq() -> None:
    error = HistoryCompactedError("history before seq 40 is compacted", earliest_seq=40)
    assert isinstance(error, RejectedError)
    assert error.code is ErrorCode.HISTORY_COMPACTED
    assert error.earliest_seq == 40
