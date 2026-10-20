"""Runs conformance cases (conformance/*.ion, contracts/conformance-vectors.md) against an `Aduku` service.

The service API used here is the planned facade (specs/001-core-ledger-merge-engine/contracts/library-api.md);
`run_case` is exercised from the user-story phases onward.
"""

from __future__ import annotations

import inspect
import pathlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aduku.errors import RejectedError
from aduku.ion_codec import Annotated, FrozenStruct, parse, to_canonical
from aduku.model import EntryInput, Identity

CONFORMANCE_DIR = pathlib.Path(__file__).resolve().parents[3] / "conformance"


@dataclass(frozen=True)
class Step:
    kind: str
    arg: Any
    expect: FrozenStruct


@dataclass(frozen=True)
class Case:
    name: str
    schemas: tuple[str, ...]  # schema texts, first is activated
    fixtures: tuple[str, ...]
    steps: tuple[Step, ...]
    clock: FrozenStruct | None = None  # `{start: <timestamp>, step_seconds: n}`: what the harness clock reads
    options: FrozenStruct | None = (
        None  # `{fetch_deadline: "100ms", scheduler_budget: "1500ms"}`: settings of the service (feature 003)
    )


STEP_KINDS = (
    "append",
    "get",
    "explain",
    "activate_schema",
    "rebuild",
    "load_schema",
    "hook_calls",
    "recompute",
    # feature 003
    "clock",
    "source_calls",
    "invalidate",
    "notify",
    "scheduler_run",
)
_DURATION = re.compile(r"(\d+)(ms|s|m|h|d)\Z")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def seconds_of(text: object) -> float:
    match = _DURATION.match(str(text))
    if not match:
        raise ValueError(f"not a duration: {text!r}")
    return int(match.group(1)) * _UNITS[match.group(2)]


def load_case(text: str) -> Case:
    document = parse(text)
    if not (
        isinstance(document, Annotated)
        and document.annotations == ("case",)
        and isinstance(document.value, FrozenStruct)
    ):
        raise ValueError("a conformance file must be a struct annotated `case::`")
    body = document.value
    raw_schemas = body.get("schemas") or ((body["schema"],) if "schema" in body else ())
    schemas = tuple(to_canonical(item) for item in raw_schemas)
    steps: list[Step] = []
    for raw in body.get("steps", ()):
        kind = next((key for key in STEP_KINDS if key in raw), None)
        if kind is None:
            raise ValueError(f"unknown step {sorted(raw)}")
        steps.append(Step(kind, raw[kind], raw.get("expect", FrozenStruct())))
    return Case(
        str(body["name"]),
        schemas,
        tuple(str(item) for item in body.get("fixtures", ())),
        tuple(steps),
        body.get("clock"),
        body.get("options"),
    )


def load_case_file(name: str) -> Case:
    return load_case((CONFORMANCE_DIR / name).read_text())


def run_case(case: Case, service: Any) -> None:
    """Execute every step; raises AssertionError describing the first mismatch."""
    for index, step in enumerate(case.steps, start=1):
        label = f"{case.name} step {index} ({step.kind})"
        if step.kind == "append":
            _append(step, service, label)
        elif step.kind == "get":
            result = _get(step, service)
            _check_document(step.expect, result, label)
            _check_deferred(step.expect, result, label)
        elif step.kind in ("explain", "rebuild") and not hasattr(service, step.kind):
            raise AssertionError(f"{label}: this implementation has no {step.kind!r} yet")
        elif step.kind == "explain":
            explanation = service.explain(step.arg["doc"], step.arg["path"])
            for key, wanted in step.expect.items():
                assert getattr(explanation, key) == wanted, (
                    f"{label}: {key} {getattr(explanation, key)!r} != {wanted!r}"
                )
        elif step.kind == "activate_schema":
            service.schemas.activate(int(step.arg))
        elif step.kind == "load_schema":
            _load_schema(step, service, label)
        elif step.kind == "hook_calls":
            name, _, version = str(step.arg).partition("@")
            behavior = service.behaviors.get(name, int(version or 1))
            assert behavior is not None, f"{label}: no hook {step.arg}"
            wanted = int(step.expect["count"])
            assert behavior.impl.calls == wanted, (
                f"{label}: {step.arg} was called {behavior.impl.calls} times, not {wanted}"
            )
        elif step.kind == "recompute":
            _recompute(step, service, label)
        elif step.kind == "clock":
            _clock(step, service, label)
        elif step.kind == "source_calls":
            _source_calls(step, service, label)
        elif step.kind == "invalidate":
            _invalidate(step, service, label)
        elif step.kind == "notify":
            marked = service.notify(str(step.arg["topic"]), [str(d) for d in step.arg.get("docs", ())] or None)
            assert marked == int(step.expect["marked"]), f"{label}: marked {marked}, not {step.expect['marked']}"
        elif step.kind == "scheduler_run":
            _scheduler_run(step, service, label)
        elif step.kind == "rebuild":
            report = service.rebuild(step.arg)
            assert report.identical == step.expect.get("identical", True), f"{label}: rebuild differs"


def _load_schema(step: Step, service: Any, label: str) -> None:
    """expect `{ok: true}` or `{problems: [CODE, ...]}`: every listed code must be among the reported problem codes."""
    from aduku.errors import SchemaError
    from aduku.schema.loader import load_schema

    try:
        load_schema(str(step.arg), service.behaviors)
    except SchemaError as error:
        wanted = {str(code) for code in step.expect.get("problems", ())}
        assert wanted, f"{label}: unexpected schema error {error}"
        assert wanted <= set(error.codes), f"{label}: expected {sorted(wanted)}, got {sorted(error.codes)}"
        return
    assert step.expect.get("ok") is True, (
        f"{label}: the schema loaded but problems {step.expect.get('problems')} were expected"
    )


def _identity(who: FrozenStruct) -> Identity:
    return Identity(
        str(who["id"]), tuple(str(t) for t in who.get("tiers", ())), tuple(str(r) for r in who.get("roles", ()))
    )


def _get(step: Step, service: Any) -> Any:
    """`get: d1` or `get: {doc: d1, consistency: fresh, include: [pending]}`."""
    if isinstance(step.arg, FrozenStruct):
        return service.get(
            str(step.arg["doc"]),
            consistency=str(step.arg.get("consistency", "cached")),
            include=tuple(str(item) for item in step.arg.get("include", ())),
        )
    return service.get(step.arg)


def _check_deferred(expect: FrozenStruct, result: Any, label: str) -> None:
    """`deferred: { "/creditScore": { status: ok, value: 712, waiting_for: [...], reason: ... } }`."""
    from aduku.engine.document import value_at
    from aduku.ion_codec import ABSENT
    from aduku.paths import parse_path

    if "deferred" not in expect:
        return
    shown = {status.path: status for status in result.deferred}
    for path, wanted in expect["deferred"].items():
        assert path in shown, f"{label}: no deferred status for {path} (have {sorted(shown)})"
        status = shown[path]
        if "status" in wanted:
            assert status.status == str(wanted["status"]), f"{label}: {path} is {status.status}, not {wanted['status']}"
        if "value" in wanted:
            value = value_at(result.document, parse_path(path))
            plain = value.value if isinstance(value, Annotated) else value
            assert plain is not ABSENT and to_canonical(plain) == to_canonical(wanted["value"]), (
                f"{label}: {path} shows {plain!r}, not {wanted['value']!r}"
            )
        if "absent" in wanted:
            assert value_at(result.document, parse_path(path)) is ABSENT, f"{label}: {path} should be left out"
        if "waiting_for" in wanted:
            assert list(status.waiting_for) == [str(p) for p in wanted["waiting_for"]], (
                f"{label}: {path} waits for {status.waiting_for}"
            )
        if "reason" in wanted:
            assert status.reason == str(wanted["reason"]), f"{label}: {path} reason {status.reason}"


def _clock(step: Step, service: Any, label: str) -> None:
    from datetime import UTC, datetime

    spec: FrozenStruct = step.arg
    assert hasattr(service.clock, "advance"), f"{label}: the case needs the harness's manual clock"
    if "advance" in spec:
        service.clock.advance(seconds_of(spec["advance"]))
    else:
        moment = spec["set"]
        service.clock.set(
            moment if isinstance(moment, datetime) else datetime.fromisoformat(str(moment)).astimezone(UTC)
        )


def _source_calls(step: Step, service: Any, label: str) -> None:
    name, _, version = str(step.arg).partition("@")
    behavior = service.behaviors.get(name, int(version or 1))
    assert behavior is not None, f"{label}: no source {step.arg}"
    impl = behavior.impl
    if "count" in step.expect:
        assert impl.calls == int(step.expect["count"]), f"{label}: {step.arg} was called {impl.calls} times"
    if "batches" in step.expect:
        wanted = [int(n) for n in step.expect["batches"]]
        assert impl.batches == wanted, f"{label}: {step.arg} batches {impl.batches}, not {wanted}"


def _invalidate(step: Step, service: Any, label: str) -> None:
    spec: FrozenStruct = step.arg
    try:
        marked = service.invalidate(spec.get("doc"), str(spec["field"]), caller=_identity(spec["contributor"]))
    except RejectedError as error:
        wanted = step.expect.get("rejected")
        assert wanted is not None, f"{label}: unexpected rejection {error}"
        assert error.code.value == str(wanted["code"]), f"{label}: code {error.code.value} != {wanted['code']}"
        return
    assert step.expect.get("rejected") is None, f"{label}: expected a rejection but the invalidate went through"
    if "marked" in step.expect:
        assert marked == int(step.expect["marked"]), f"{label}: marked {marked}, not {step.expect['marked']}"


def _scheduler_run(step: Step, service: Any, label: str) -> None:
    spec: FrozenStruct = step.arg
    try:
        report = service.scheduler_run(str(spec["hook"]), caller=_identity(spec["contributor"]))
    except RejectedError as error:
        wanted = step.expect.get("rejected")
        assert wanted is not None, f"{label}: unexpected rejection {error}"
        assert error.code.value == str(wanted["code"]), f"{label}: code {error.code.value} != {wanted['code']}"
        return
    assert step.expect.get("rejected") is None, f"{label}: expected a rejection but the run went through"
    for key in ("attempted", "succeeded", "failed", "skipped", "calls"):
        if key in step.expect:
            assert getattr(report, key) == int(step.expect[key]), f"{label}: {key} {getattr(report, key)}"
    if "reason" in step.expect:
        assert report.reason == str(step.expect["reason"]), f"{label}: reason {report.reason!r}"


def _recompute(step: Step, service: Any, label: str) -> None:
    spec: FrozenStruct = step.arg
    who = spec["contributor"]
    caller = Identity(
        str(who["id"]), tuple(str(t) for t in who.get("tiers", ())), tuple(str(r) for r in who.get("roles", ()))
    )
    try:
        report = service.recompute(str(spec["doc"]), spec.get("field"), caller=caller)
    except RejectedError as error:
        wanted = step.expect.get("rejected")
        assert wanted is not None, f"{label}: unexpected rejection {error}"
        assert error.code.value == str(wanted["code"]), f"{label}: code {error.code.value} != {wanted['code']}"
        return
    assert step.expect.get("rejected") is None, f"{label}: expected a rejection but the recompute went through"
    if "recomputed" in step.expect:
        assert list(report.recomputed) == [str(f) for f in step.expect["recomputed"]], (
            f"{label}: recomputed {report.recomputed}"
        )
    if "failed" in step.expect:
        assert [f for f, _ in report.failed] == [str(f) for f in step.expect["failed"]], (
            f"{label}: failed {report.failed}"
        )


def _append(step: Step, service: Any, label: str) -> None:
    spec: FrozenStruct = step.arg
    who = spec["contributor"]
    identity = Identity(
        str(who["id"]), tuple(str(t) for t in who.get("tiers", ())), tuple(str(r) for r in who.get("roles", ()))
    )
    entries = [
        EntryInput(
            tier=str(item["tier"]),
            path=str(item["path"]),
            op=str(item.get("op", "set")),  # type: ignore[arg-type]
            value=item.get("value"),
            idem_key=item.get("idem_key"),
            meta=item.get("meta"),
        )
        for item in spec["entries"]
    ]
    try:
        result = service.append(
            str(spec["doc"]),
            entries,
            contributor=identity,
            lenient=bool(spec.get("lenient", False)),
            request_key=spec.get("request_key"),
        )
    except RejectedError as error:
        wanted = step.expect.get("rejected")
        assert wanted is not None, f"{label}: unexpected rejection {error}"
        assert error.code.value == str(wanted["code"]), f"{label}: code {error.code.value} != {wanted['code']}"
        for reason in wanted.get("reasons", ()):
            assert any(
                r.path == reason.get("path") and (reason.get("rule") is None or r.rule == str(reason["rule"]))
                for r in error.reasons
            ), f"{label}: missing reason for {reason}"
        # a reason states the constraint and never repeats a value the request submitted (feature 004, FR-008)
        text = " ".join(f"{r.message} {r.rule}" for r in error.reasons)
        for entry in entries:
            if isinstance(entry.value, str) and len(entry.value) >= 3:
                assert entry.value not in text, f"{label}: a reason repeats the submitted value {entry.value!r}"
        return
    wanted_rejection = step.expect.get("rejected")
    assert wanted_rejection is None, (
        f"{label}: expected rejection {wanted_rejection['code']} but the request was accepted"
    )
    if "accepted" in step.expect:
        wanted_seqs = [int(item["seq"]) for item in step.expect["accepted"]]
        got_seqs = [seq for _, seq in result.entries]
        assert got_seqs == wanted_seqs, f"{label}: accepted seqs {got_seqs} != {wanted_seqs}"
    if "duplicate" in step.expect:
        assert result.duplicate == bool(step.expect["duplicate"]), f"{label}: duplicate flag"


def _check_document(expect: FrozenStruct, result: Any, label: str) -> None:
    if "document" in expect:
        assert to_canonical(result.document) == to_canonical(parse(str(expect["document"]))), (
            f"{label}: document differs"
        )
    if "model_errors" in expect:
        got = sorted((e.rule_id, e.path_scope) for e in result.model_errors)
        want = sorted((str(e["rule"]), str(e["path"])) for e in expect["model_errors"])
        assert got == want, f"{label}: model errors {got} != {want}"


ServiceFactory = Callable[[Case], Any]


def build_service(case: Case, storage: Any = None) -> Any:
    """A service for the case: its schemas registered (the first activated), fixtures registered, a fake clock."""
    from aduku.behaviors.registry import BehaviorRegistry
    from aduku.hooks import HookRunner
    from aduku.schema.loader import load_schema
    from aduku.schema.registry import SchemaRegistry
    from aduku.service import Aduku
    from aduku.storage.base import SequentialIds
    from aduku.storage.memory import MemoryStorage
    from tests.fixtures import ManualClock, register_fixtures

    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors, list(case.fixtures))
    registry = SchemaRegistry()
    schemas = [load_schema(text, behaviors) for text in case.schemas]
    for schema in schemas:
        registry.put(schema)
    if schemas:
        registry.activate(schemas[0].version)
    clock = case.clock
    fake = (
        ManualClock(clock["start"], float(clock.get("step_seconds", 0)))
        if clock is not None
        else ManualClock(step_seconds=1)
    )
    extra: dict[str, Any] = {}
    parameters = inspect.signature(Aduku.__init__).parameters
    if "monotonic" in parameters:  # feature 003: the deferred stage reads time from the same manual clock
        extra["monotonic"] = fake.monotonic
        extra["hook_runner"] = HookRunner(sleep=fake.sleep, monotonic=fake.monotonic)
        extra["sleep"] = fake.sleep
        if case.options is not None and "fetch_deadline" in case.options:
            extra["fetch_deadline"] = seconds_of(case.options["fetch_deadline"])
        if case.options is not None and "scheduler_budget" in case.options:
            extra["scheduler_budget"] = seconds_of(case.options["scheduler_budget"])
    return Aduku(storage or MemoryStorage(), registry, clock=fake, ids=SequentialIds(), behaviors=behaviors, **extra)
