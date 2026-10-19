"""Feature 003, research R-8: when a scheduled deferred field is next due. Pure: times are arguments."""

from datetime import UTC, datetime, timedelta

import pytest

from aduku.engine.schedule import ScheduleError, next_fire, parse_schedule


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def cron(expression: str):  # type: ignore[no-untyped-def]
    return parse_schedule(cron=expression)


def test_an_interval_fires_one_interval_after_the_last_time() -> None:
    assert next_fire(parse_schedule(every=3600), at("2026-10-19T12:30:00")) == at("2026-10-19T13:30:00")
    assert next_fire(parse_schedule(every=1.5), at("2026-10-19T12:00:00")) == at("2026-10-19T12:00:01.500000")


@pytest.mark.parametrize("expression, after, expected", [
    ("0 * * * *", "2026-10-19T12:30:00", "2026-10-19T13:00:00"),
    ("0 * * * *", "2026-10-19T13:00:00", "2026-10-19T14:00:00"),  # strictly after
    ("*/15 * * * *", "2026-10-19T12:01:00", "2026-10-19T12:15:00"),
    ("*/15 * * * *", "2026-10-19T12:46:30", "2026-10-19T13:00:00"),
    ("30 2 * * *", "2026-10-19T03:00:00", "2026-10-20T02:30:00"),
    ("0 0 1 * *", "2026-01-31T10:00:00", "2026-02-01T00:00:00"),
    ("0 0 1 1 *", "2026-10-19T10:00:00", "2027-01-01T00:00:00"),
    ("0 0 29 2 *", "2025-03-01T00:00:00", "2028-02-29T00:00:00"),  # only a leap day
    ("0 9 * * 1", "2026-10-18T12:00:00", "2026-10-19T09:00:00"),  # the next Monday
    ("0 9 * * 7", "2026-10-19T12:00:00", "2026-10-25T09:00:00"),  # 7 is Sunday
    ("0 9 * * 0", "2026-10-19T12:00:00", "2026-10-25T09:00:00"),
    ("5,35 8-10/2 * * *", "2026-10-19T08:06:00", "2026-10-19T08:35:00"),
    ("5,35 8-10/2 * * *", "2026-10-19T08:36:00", "2026-10-19T10:05:00"),
    ("0 0 13 * 5", "2026-10-14T00:00:00", "2026-10-16T00:00:00"),  # day of month and day of week: either one
    ("0 0 13 * 5", "2026-10-17T00:00:00", "2026-10-23T00:00:00"),
])  # fmt: skip
def test_a_cron_expression_fires_at_the_next_matching_minute(expression: str, after: str, expected: str) -> None:
    assert next_fire(cron(expression), at(after)) == at(expected)


def test_the_result_is_in_utc_and_comes_after_the_argument() -> None:
    moment = at("2026-10-19T12:00:00.250000")
    fired = next_fire(cron("* * * * *"), moment)
    assert fired == at("2026-10-19T12:01:00") and fired.utcoffset() == timedelta(0)


@pytest.mark.parametrize("expression", [
    "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * 32 * *", "* * * 13 *", "* * * * 8",
    "0 0 31 2 *", "0 0 30 2 *", "a * * * *", "*/0 * * * *", "5-1 * * * *", "", "1,,2 * * * *", "-1 * * * *",
])  # fmt: skip
def test_a_malformed_cron_expression_is_refused(expression: str) -> None:
    with pytest.raises(ScheduleError):
        cron(expression)


def test_exactly_one_of_every_and_cron_and_an_interval_of_at_least_a_second() -> None:
    for kwargs in ({}, {"every": 60, "cron": "* * * * *"}, {"every": 0.5}, {"every": 0}, {"every": -5}):
        with pytest.raises(ScheduleError):
            parse_schedule(**kwargs)  # type: ignore[arg-type]


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValueError):
        next_fire(parse_schedule(every=60), datetime(2026, 10, 19, 12, 0))


def test_a_day_field_that_starts_with_a_star_is_not_restricted_so_both_day_fields_must_match() -> None:
    # `*/2` in the day-of-month field is a "star" field in cron: it ANDs with the weekday instead of OR-ing with it
    assert next_fire(cron("0 0 */2 * 1"), at("2026-10-19T00:00:00")) == at(
        "2026-11-09T00:00:00"
    )  # a Monday on an odd day
    assert next_fire(cron("0 0 * * */3"), at("2026-10-19T00:00:00")) == at("2026-10-21T00:00:00")  # still plain steps
