"""The deferred stage as a pure planner (feature 003, research R-1, R-4, R-5). Pure: no IO, and the time is an argument.

Given the document of P1 to P4, the records the derived store holds and `now`, the planner says for every deferred field
what it is (`ok`, `stale`, `pending`, `error`), what to show, why it waits, and which call, if any, is due and why. It
never calls a source: the executor (`aduku.fetch`) does that and then asks again, so a field whose inputs depend on
another deferred field gets its turn once the upstream value is in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from aduku.engine.document import strip_computed, value_at, with_value
from aduku.engine.effects import fingerprint
from aduku.engine.graph import deferred_levels
from aduku.engine.schedule import next_fire, parse_schedule
from aduku.ion_codec import ABSENT, Annotated, FrozenStruct, Value, parse
from aduku.model import DeferredStatus, DerivedRecord
from aduku.paths import Path, PathError, parse_path, parse_pattern
from aduku.schema.model import FieldSpec, Schema

__all__ = ["FieldPlan", "Plan", "overlay", "plan", "statuses"]

State = Literal["ok", "stale", "pending", "error"]
Action = Literal["first", "invalidated", "inputs", "ttl", "fresh", "schedule"]
Mode = Literal["read", "scheduler"]


@dataclass(frozen=True)
class FieldPlan:
    """What the planner decided for one deferred field."""

    spec: FieldSpec
    status: State
    shown: Value  # the value to put in the document (not annotated); meaningful only when `include`
    include: bool  # False: the field is left out of the document
    action: Action | None  # the call that is due, and why; None when none is
    reason: str | None  # for `pending`: inputs_missing, upstream or not_fetched
    waiting_for: tuple[str, ...]
    level: int
    inputs: tuple[tuple[str, Value], ...]  # the dependency values, when they are all there
    fingerprint: str | None
    record: DerivedRecord | None  # the stored record, when it is reusable for this field's declaration

    @property
    def path(self) -> str:
        return self.spec.pattern


@dataclass(frozen=True)
class Plan:
    fields: tuple[FieldPlan, ...]  # by level, then by path
    levels: tuple[tuple[str, ...], ...]

    def field(self, path: str) -> FieldPlan | None:
        return next((f for f in self.fields if f.path == path), None)

    def due(self) -> tuple[FieldPlan, ...]:
        return tuple(f for f in self.fields if f.action is not None)


def plan(
    schema: Schema,
    document: Value,
    records: Mapping[str, DerivedRecord],
    now: datetime,
    *,
    consistency: Literal["cached", "fresh"] = "cached",
    mode: Mode = "read",
    skip: frozenset[str] = frozenset(),
) -> Plan:
    """The plan for one document. `records` holds its stored results by field pattern; `mode` says who asks: a read
    fetches `once` and `per_fetch` values, the scheduler the rest. Fields in `skip` have already been tried by this
    request and are not due again (so a `fresh` read, or a failed call, cannot loop)."""
    view = strip_computed(document)
    assert isinstance(view, FrozenStruct)
    where = {spec.pattern: parse_pattern(spec.pattern) for spec in schema.fields if spec.derived == "deferred"}
    resolved: set[str] = set()  # the deferred fields that have a value to give to the ones that depend on them
    planned: list[FieldPlan] = []
    for number, level in enumerate(deferred_levels(schema)):
        for spec in level:
            field = _plan_field(
                spec,
                number,
                view,
                records.get(spec.pattern),
                now,
                consistency,
                mode,
                skip,
                where,
                resolved,
                schema.on_failure_of(spec),
            )
            planned.append(field)
            if field.status in ("ok", "stale") and field.include:
                resolved.add(spec.pattern)
                with_it = with_value(view, parse_pattern(spec.pattern), field.shown)
                view = with_it if with_it is not None else view
    return Plan(tuple(planned), tuple(tuple(s.pattern for s in level) for level in deferred_levels(schema)))


def _plan_field(
    spec: FieldSpec,
    level: int,
    view: Value,
    stored: DerivedRecord | None,
    now: datetime,
    consistency: str,
    mode: Mode,
    skip: frozenset[str],
    where: Mapping[str, Path],
    resolved: set[str],
    on_failure: str,
) -> FieldPlan:
    deferred = spec.deferred
    assert deferred is not None
    record = stored if stored is not None and _reusable(spec, stored) else None
    inputs: list[tuple[str, Value]] = []
    waiting: list[str] = []
    upstream = False
    for path_text in deferred.depends:
        if _unresolved_upstream(path_text, spec.pattern, where, resolved):
            waiting.append(path_text)  # another deferred field it reads has no value yet (a promise)
            upstream = True
            continue
        found = _read(view, path_text)
        if found is ABSENT or found is None:
            waiting.append(path_text)
        else:
            inputs.append((path_text, found))
    if waiting:
        reason = "upstream" if upstream else "inputs_missing"
        return FieldPlan(
            spec,
            "pending",
            None,
            spec.on_missing != "absent",
            None,
            reason,
            tuple(waiting),
            level,
            (),
            None,
            record,
        )
    digest = fingerprint(deferred.source, inputs)
    action = None if spec.pattern in skip else _due(spec, record, digest, now, consistency, mode)
    if record is None:
        return FieldPlan(
            spec,
            "pending",
            None,
            spec.on_missing != "absent",
            action,
            "not_fetched",
            (),
            level,
            tuple(inputs),
            digest,
            None,
        )
    if record.status in ("stale", "error"):  # the last call failed: what is shown is the field's failure policy
        kept = record.has_value and on_failure == "serve_stale"
        status: State = "stale" if kept else "error"
        shown = parse(record.value or "null") if kept else None
        return FieldPlan(
            spec, status, shown, on_failure != "absent", action, None, (), level, tuple(inputs), digest, record
        )
    return FieldPlan(
        spec, "ok", parse(record.value or "null"), True, action, None, (), level, tuple(inputs), digest, record
    )


def _unresolved_upstream(path_text: str, own: str, where: Mapping[str, Path], resolved: set[str]) -> bool:
    """True if `path_text` is, holds, or lies beneath a deferred field (other than this one) that has no value yet."""
    try:
        wanted = parse_pattern(path_text)
    except PathError:
        return False
    for pattern, there in where.items():
        shorter = min(len(wanted), len(there))
        if pattern != own and wanted[:shorter] == there[:shorter] and pattern not in resolved:
            return True
    return False


def _reusable(spec: FieldSpec, record: DerivedRecord) -> bool:
    """A stored record answers this declaration only if the source, the dependency list and the type are the same (FR-030)."""
    deferred = spec.deferred
    assert deferred is not None
    return record.hook == deferred.source and record.shape == deferred.shape(spec.type)


def _read(view: Value, path_text: str) -> Value:
    try:
        return value_at(view, parse_path(path_text))
    except PathError:
        return ABSENT


def _due(
    spec: FieldSpec,
    record: DerivedRecord | None,
    digest: str,
    now: datetime,
    consistency: str,
    mode: Mode,
) -> Action | None:
    """The call that is due now, for a read or for the scheduler (research R-4)."""
    deferred = spec.deferred
    assert deferred is not None
    kinds = {p.kind for p in deferred.refresh}
    if mode == "read" and kinds & {"once", "per_fetch"}:
        if record is None or record.last_attempt is None:  # never actually tried (a refusal is not an attempt)
            return "first"
        if record.invalidated:
            return "invalidated"
        if "per_fetch" in kinds:
            if record.input_fingerprint != digest:
                return "inputs"  # a value for other inputs answers another question, whatever the ttl (FR-007)
            if consistency == "fresh":
                return "fresh"
            asked = record.last_attempt or record.as_of  # a failed attempt counts too: a down source is not hammered
            if asked is not None and deferred.ttl is not None and (now - asked).total_seconds() >= deferred.ttl:
                return "ttl"
        if "once" in kinds and record.input_fingerprint != digest:
            if not record.has_value or deferred.on_dependency_change == "recompute":
                return "inputs"
    if mode == "scheduler" and kinds & {"scheduled", "notification"}:
        if record is None or record.last_attempt is None:
            return "first"
        if record.invalidated:
            return "invalidated"
        asked = record.last_attempt or record.as_of
        if asked is not None:
            for policy in deferred.refresh:
                if (
                    policy.kind == "scheduled"
                    and next_fire(parse_schedule(every=policy.every, cron=policy.cron), asked) <= now
                ):
                    return "schedule"
    return None


def overlay(document: FrozenStruct, planned: Plan) -> FrozenStruct:
    """The document with the deferred fields that are shown, each as `deferred::<value>`."""
    for field in planned.fields:
        if not field.include:
            continue
        changed = with_value(document, parse_path(field.path), Annotated(("deferred",), field.shown))
        if changed is not None:
            document = changed
    return document


def statuses(planned: Plan, detail: bool = False) -> tuple[DeferredStatus, ...]:
    """What a read says about each deferred field, in path order; `detail` adds what a pending field waits for."""
    found = []
    for field in sorted(planned.fields, key=lambda f: f.path):
        record = field.record
        found.append(
            DeferredStatus(
                path=field.path,
                status=field.status,
                as_of=record.as_of if record is not None and field.status in ("ok", "stale") else None,
                error=record.error if record is not None and field.status in ("stale", "error") else None,
                attempts=record.attempts if record is not None else None,
                waiting_for=field.waiting_for if detail else (),
                reason=field.reason if detail and field.status == "pending" else None,
            )
        )
    return tuple(found)
