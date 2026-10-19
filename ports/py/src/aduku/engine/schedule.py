"""When a scheduled deferred field is next due (research R-8). Pure: the time is always an argument.

A schedule is an interval (`every` seconds, at least one) or a standard five-field cron expression (minute, hour, day of
month, month, day of week) with `*`, lists, ranges and steps, read in UTC. When both day fields are restricted a day
matches if either does, as in cron. A day of week of 7 is Sunday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

__all__ = ["Schedule", "ScheduleError", "next_fire", "parse_schedule"]

MIN_INTERVAL = 1.0
_SEARCH_DAYS = 366 * 8  # a leap day on a given weekday can be eight years away; 29 Feb alone at most four


class ScheduleError(ValueError):
    """A schedule is malformed: the message names the problem, never a value from a document."""


@dataclass(frozen=True)
class Schedule:
    every: float | None = None
    minutes: frozenset[int] = frozenset()
    hours: frozenset[int] = frozenset()
    days: frozenset[int] = frozenset()  # day of month
    months: frozenset[int] = frozenset()
    weekdays: frozenset[int] = frozenset()  # 0 is Sunday
    days_any: bool = True  # the day-of-month field was `*`
    weekdays_any: bool = True  # the day-of-week field was `*`


def parse_schedule(*, every: float | None = None, cron: str | None = None) -> Schedule:
    """A schedule from exactly one of `every` (seconds, at least 1) or `cron`."""
    if (every is None) == (cron is None):
        raise ScheduleError("a schedule has exactly one of `every` and `cron`")
    if every is not None:
        if every < MIN_INTERVAL:
            raise ScheduleError(f"an interval is at least {MIN_INTERVAL:g} second")
        return Schedule(every=float(every))
    assert cron is not None
    fields = cron.split()
    if len(fields) != 5:
        raise ScheduleError("a cron expression has five fields: minute hour day-of-month month day-of-week")
    minutes = _field(fields[0], 0, 59)
    hours = _field(fields[1], 0, 23)
    days = _field(fields[2], 1, 31)
    months = _field(fields[3], 1, 12)
    weekdays = frozenset(day % 7 for day in _field(fields[4], 0, 7))
    most = {2: 29, 4: 30, 6: 30, 9: 30, 11: 30}
    if (
        fields[4].startswith("*")
        and not fields[2].startswith("*")
        and not any(d <= most.get(m, 31) for m in months for d in days)
    ):
        raise ScheduleError("the cron expression never fires: no month has those days")
    return Schedule(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        days_any=fields[2].startswith("*"),  # as in cron, a field that starts with `*` (even `*/2`) is no restriction
        weekdays_any=fields[4].startswith("*"),
    )


def _field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    if not text:
        raise ScheduleError("a cron field is empty")
    for part in text.split(","):
        values.update(_part(part, low, high))
    return frozenset(values)


def _part(part: str, low: int, high: int) -> range:
    step = 1
    if "/" in part:
        part, _, step_text = part.partition("/")
        step = _number(step_text, 1, high)
    if part == "*":
        first, last = low, high
    elif "-" in part:
        a, _, b = part.partition("-")
        first, last = _number(a, low, high), _number(b, low, high)
        if first > last:
            raise ScheduleError("a cron range runs from a smaller to a larger number")
    else:
        first = _number(part, low, high)
        last = high if step != 1 else first  # `5/10` is cron shorthand for 5-max/10
    return range(first, last + 1, step)


def _number(text: str, low: int, high: int) -> int:
    if not text.isdigit():
        raise ScheduleError("a cron field holds numbers, `*`, `,`, `-` and `/`")
    number = int(text)
    if not low <= number <= high:
        raise ScheduleError(f"a cron number must be from {low} to {high}")
    return number


def next_fire(schedule: Schedule, after: datetime) -> datetime:
    """The first time strictly after `after` that the schedule fires, in UTC."""
    if after.tzinfo is None:
        raise ValueError("next_fire takes a time with a time zone")
    after = after.astimezone(UTC)
    if schedule.every is not None:
        return after + timedelta(seconds=schedule.every)
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.date()
    for offset in range(_SEARCH_DAYS):
        current = day + timedelta(days=offset)
        if not _day_matches(schedule, current):
            continue
        floor = (start.hour, start.minute) if offset == 0 else (0, 0)
        for hour in sorted(schedule.hours):
            for minute in sorted(schedule.minutes):
                if (hour, minute) >= floor:
                    return datetime(current.year, current.month, current.day, hour, minute, tzinfo=UTC)
    raise ScheduleError("the cron expression never fires")


def _day_matches(schedule: Schedule, day: date) -> bool:
    if day.month not in schedule.months:
        return False
    in_month = day.day in schedule.days
    in_week = (day.isoweekday() % 7) in schedule.weekdays
    if schedule.days_any or schedule.weekdays_any:
        return in_month and in_week  # an unrestricted field matches every day, so this is the restricted one
    return in_month or in_week
