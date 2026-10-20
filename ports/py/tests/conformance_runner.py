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
    # feature 005
    "restart",
    "damage_layer",
    "as_of",
    "compact",
    "expire",
    "purge",
    # feature 006
    "diff",
    "plan",
    "pin",
    "unpin",
    "migrate",
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


def restart_service(service: Any) -> Any:
    """A new service on the same storage, schemas and behaviors: nothing kept in memory (feature 005)."""
    from aduku.service import Aduku

    return Aduku(
        service.storage,
        service.schemas,
        clock=service.clock,
        ids=service.ids,
        behaviors=service.behaviors,
        layers=service.layers,
    )


def _activate(step: Step, service: Any, label: str) -> None:
    """`activate_schema: N` (the registry's own activation) or `activate_schema: {version, operator, acknowledge_breaking?,
    rebuild?}` (the operation of feature 006), with `expect: {refused: CODE}` when the version must be refused and, for the
    second form, `class` and `rollback`."""
    from aduku.errors import RejectedError, SchemaError

    operation = isinstance(step.arg, FrozenStruct)
    try:
        if operation:
            report = service.activate_schema(
                int(step.arg["version"]),
                operator=str(step.arg.get("operator", "conformance")),
                acknowledge_breaking=bool(step.arg.get("acknowledge_breaking", False)),
                rebuild=bool(step.arg.get("rebuild", False)),
            )
        else:
            service.schemas.activate(int(step.arg))
    except (SchemaError, RejectedError) as error:
        wanted = step.expect.get("refused")
        assert wanted is not None, f"{label}: unexpected refusal {error}"
        codes = error.codes if isinstance(error, SchemaError) else (error.code.value,)
        assert str(wanted) in codes, f"{label}: codes {codes} lack {wanted}"
        return
    assert step.expect.get("refused") is None, (
        f"{label}: expected {step.expect['refused']} but the version was activated"
    )
    if operation:
        if "class" in step.expect:
            assert report.change_class == str(step.expect["class"]), f"{label}: class {report.change_class}"
        if "rollback" in step.expect:
            assert report.rollback == bool(step.expect["rollback"]), f"{label}: rollback {report.rollback}"


def _pins(step: Step, service: Any, label: str) -> None:
    """`pin: {doc, version, operator}`, `unpin: {doc, operator}` or `migrate: {docs?, to?, operator}` (feature 006), with
    `expect: {refused: CODE}` or, for a migration, `{moved, already_moved, not_pinned, failed}` (document names)."""
    from aduku.errors import RejectedError, SchemaError

    operator = str(step.arg.get("operator", "conformance"))
    try:
        if step.kind == "pin":
            service.pin_document(str(step.arg["doc"]), int(step.arg["version"]), operator=operator)
        elif step.kind == "unpin":
            service.unpin_document(str(step.arg["doc"]), operator=operator)
        else:
            report = service.migrate(
                [str(d) for d in step.arg["docs"]] if "docs" in step.arg else None,
                to=int(step.arg["to"]) if "to" in step.arg else None,
                operator=operator,
            )
    except (SchemaError, RejectedError) as error:
        wanted = step.expect.get("refused")
        assert wanted is not None, f"{label}: unexpected refusal {error}"
        codes = error.codes if isinstance(error, SchemaError) else (error.code.value,)
        assert str(wanted) in codes, f"{label}: codes {codes} lack {wanted}"
        return
    assert step.expect.get("refused") is None, f"{label}: expected {step.expect['refused']} but it was accepted"
    if step.kind == "migrate":
        got = {
            "moved": list(report.moved),
            "already_moved": list(report.already_moved),
            "not_pinned": list(report.not_pinned),
            "failed": [doc for doc, _ in report.failed],
        }
        for key, names in got.items():
            if key in step.expect:
                assert names == [str(d) for d in step.expect[key]], f"{label}: {key} {names}"


def _plan(step: Step, service: Any, label: str) -> None:
    """`plan: {target, limit?, docs?}` with `expect: {examined, left_out, changed, unchanged, failed, would_call_hook,
    documents: {doc: {document_changed?, skipped_after?, shadowed?: {reason: n}, errors_added?, errors_resolved?}}}`; the
    documents named are exactly the ones the report lists."""
    report = service.plan_schema(
        int(step.arg["target"]),
        docs=[str(d) for d in step.arg["docs"]] if "docs" in step.arg else None,
        limit=int(step.arg["limit"]) if "limit" in step.arg else None,
    )
    for key in ("examined", "left_out", "changed", "unchanged", "failed", "would_call_hook"):
        if key in step.expect:
            assert getattr(report, key) == int(step.expect[key]), f"{label}: {key} {getattr(report, key)}"
    if "documents" in step.expect:
        listed = {c.doc: c for c in report.documents}
        assert sorted(listed) == sorted(str(d) for d in step.expect["documents"]), (
            f"{label}: documents {sorted(listed)}"
        )
        for doc, wanted in step.expect["documents"].items():
            change = listed[str(doc)]
            if "document_changed" in wanted:
                assert change.document_changed == bool(wanted["document_changed"]), f"{label}: {doc} document_changed"
            if "skipped_after" in wanted:
                assert list(change.skipped_after) == [int(n) for n in wanted["skipped_after"]], (
                    f"{label}: {doc} skipped"
                )
            if "shadowed" in wanted:
                got = dict(change.shadowed_by_reason)
                assert got == {str(k): int(v) for k, v in wanted["shadowed"].items()}, f"{label}: {doc} shadowed {got}"
            for key in ("errors_added", "errors_resolved"):
                if key in wanted:
                    assert list(getattr(change, key)) == [str(e) for e in wanted[key]], f"{label}: {doc} {key}"


def _diff(step: Step, service: Any, label: str) -> None:
    """`diff: {old: V, new: V}` with `expect: {overall, changes: [{kind, path, class, classified?}]}` (feature 006)."""
    from aduku.schema.diff import schema_changes

    found = schema_changes(service.schemas.get(int(step.arg["old"])), service.schemas.get(int(step.arg["new"])))
    got = [(c.kind, c.path, c.change_class, c.classified) for c in found.changes]
    want = [
        (str(c["kind"]), str(c["path"]), str(c["class"]), bool(c.get("classified", True)))
        for c in step.expect.get("changes", ())
    ]
    assert got == want, f"{label}: changes {got} != {want}"
    assert found.overall == str(step.expect["overall"]), f"{label}: overall {found.overall}"


def _as_of(step: Step, service: Any, label: str) -> None:
    """`as_of: {doc, seq | tx_time | valid_time}` with `expect: {document, resolved_seq, included, refused}`."""
    from aduku.model import AsOf

    spec: FrozenStruct = step.arg
    point = AsOf(seq=spec.get("seq"), tx_time=spec.get("tx_time"), valid_time=spec.get("valid_time"))
    try:
        result = service.get(str(spec["doc"]), as_of=point)
    except RejectedError as error:
        wanted = step.expect.get("refused")
        assert wanted is not None, f"{label}: unexpected refusal {error}"
        assert error.code.value == str(wanted), f"{label}: code {error.code.value} != {wanted}"
        return
    assert step.expect.get("refused") is None, f"{label}: expected {step.expect['refused']} but the read was answered"
    _check_document(step.expect, result, label)
    if "resolved_seq" in step.expect:
        assert result.as_of.resolved_seq == int(step.expect["resolved_seq"]), f"{label}: resolved {result.as_of}"
    if "included" in step.expect:
        assert result.as_of.included == int(step.expect["included"]), f"{label}: included {result.as_of}"


def _observe(service: Any, doc: str, paths: list[str] | None = None) -> tuple[Any, ...]:
    """Everything a reader sees of a document, to compare before and after a compaction (the archive is kept, so even
    `explain` is exactly the old answer; whether originals stood in for synthetic entries is not part of the answer)."""
    from dataclasses import replace

    from aduku.errors import RejectedError as Rejected
    from aduku.paths import format_path, parse_path

    result = service.get(doc)
    if paths is None:
        paths = sorted({format_path(parse_path(e.path)) for e in service.list_entries(doc) if e.op != "layer"})
    explained = {}
    for path in paths:
        try:
            explained[path] = replace(service.explain(doc, path), originals_kept=None, sources=())
        except Rejected:
            continue
    return (to_canonical(result.document), result.model_errors, result.skipped, result.conflicts, explained)


def _compact(step: Step, service: Any, label: str) -> None:
    """`compact: {doc, through_seq}` with `expect: {noop, watermark, replaced, remaining, refused}`."""
    doc, through = str(step.arg["doc"]), int(step.arg["through_seq"])
    before = _observe(service, doc)
    try:
        report = service.compact(doc, through_seq=through)
    except RejectedError as error:
        wanted = step.expect.get("refused")
        assert wanted is not None, f"{label}: unexpected refusal {error}"
        assert error.code.value == str(wanted), f"{label}: code {error.code.value} != {wanted}"
        assert _observe(service, doc, list(before[4])) == before, f"{label}: a refused compaction changed an answer"
        return
    assert step.expect.get("refused") is None, (
        f"{label}: expected {step.expect['refused']} but the compaction went through"
    )
    assert report.noop == bool(step.expect.get("noop", False)), f"{label}: noop is {report.noop}"
    assert _observe(service, doc, list(before[4])) == before, f"{label}: a compaction changed an answer"
    if "watermark" in step.expect:
        assert report.watermark_seq == int(step.expect["watermark"]), f"{label}: watermark {report.watermark_seq}"
    if "replaced" in step.expect:
        assert report.replaced == int(step.expect["replaced"]), f"{label}: replaced {report.replaced}"
    if "remaining" in step.expect:
        remaining = len(service.list_entries(doc))
        assert remaining == int(step.expect["remaining"]), f"{label}: {remaining} entries remain"


def stored_text(storage: Any) -> str:
    """Every stored value of every table (or every field of the in-memory state), lower-cased, to look for a marker."""
    if hasattr(storage, "connection"):
        parts = []
        for (table,) in storage.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            parts.extend(str(tuple(row)) for row in storage.connection.execute(f"SELECT * FROM {table}"))
        return "\n".join(parts).lower()
    return (repr(storage._state) + repr(storage.audit_backend()._rows)).lower()  # noqa: SLF001


def _purge(step: Step, service: Any, label: str) -> None:
    """`purge: {doc, path | entries, tier, reason, as}` with `expect: {refused, entries_erased, purge_log_count,
    absent_everywhere}`."""
    from aduku.model import PurgeScope

    spec: FrozenStruct = step.arg
    scope = PurgeScope(
        path=spec.get("path"), tier=spec.get("tier"), entries=tuple(str(i) for i in spec.get("entries", ()))
    )
    doc = str(spec["doc"])
    try:
        report = service.purge(doc, scope, reason=str(spec["reason"]), caller=_identity(spec["as"]))
    except RejectedError as error:
        wanted = step.expect.get("refused")
        assert wanted is not None, f"{label}: unexpected refusal {error}"
        assert error.code.value == str(wanted), f"{label}: code {error.code.value} != {wanted}"
        return
    assert step.expect.get("refused") is None, f"{label}: expected {step.expect['refused']} but the purge went through"
    if "entries_erased" in step.expect:
        assert report.entries_erased == int(step.expect["entries_erased"]), f"{label}: erased {report.entries_erased}"
    if "purge_log_count" in step.expect:
        count = len(service.purge_log(doc))
        assert count == int(step.expect["purge_log_count"]), f"{label}: {count} purge log records"
    if "absent_everywhere" in step.expect:
        marker = str(step.expect["absent_everywhere"]).lower()
        assert marker not in stored_text(service.storage), f"{label}: {marker!r} is still stored somewhere"


def _damage_layer(step: Step, service: Any) -> None:
    from aduku.storage.base import LayerRow

    with service.storage.transaction() as tx:
        rows = [r for r in tx.get_layers(str(step.arg["doc"])) if r.tier == str(step.arg["tier"])]
        if rows:  # with layers off there is nothing to damage
            r = rows[0]
            tx.put_layers([LayerRow(r.doc, r.tier, r.tier_seq, r.schema_version, r.rules_fingerprint, r.epoch, "[")])


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
                if key == "purged_entries":  # feature 005: how many purged entries the answer names
                    assert len(explanation.purged) == int(wanted), f"{label}: purged marks {explanation.purged}"
                    continue
                if key == "shadowed_by_schema":  # feature 006: [{seq, reason}]
                    got = [(item.seq, item.reason) for item in explanation.shadowed_by_schema]
                    want = [(int(item["seq"]), str(item["reason"])) for item in wanted]
                    assert got == want, f"{label}: shadowed_by_schema {got} != {want}"
                    continue
                assert getattr(explanation, key) == wanted, (
                    f"{label}: {key} {getattr(explanation, key)!r} != {wanted!r}"
                )
        elif step.kind == "activate_schema":
            _activate(step, service, label)
        elif step.kind == "diff":
            _diff(step, service, label)
        elif step.kind == "plan":
            _plan(step, service, label)
        elif step.kind in ("pin", "unpin", "migrate"):
            _pins(step, service, label)
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
        elif step.kind == "as_of":
            _as_of(step, service, label)
        elif step.kind == "compact":
            _compact(step, service, label)
        elif step.kind == "purge":
            _purge(step, service, label)
        elif step.kind == "expire":
            gone = service.expire_archive(str(step.arg["doc"]), through_seq=int(step.arg["through_seq"]))
            assert gone.expired == int(step.expect["expired"]), f"{label}: expired {gone.expired}"
        elif step.kind == "restart":
            service = restart_service(service)
        elif step.kind == "damage_layer":
            _damage_layer(step, service)
        elif step.kind == "rebuild":
            report = service.rebuild(step.arg)
            wanted_identical = step.expect.get("identical", True)
            if "layers_differing" in step.expect and not service.layers:
                wanted_identical = True  # with layers off there are no layers to differ
            assert report.identical == wanted_identical, f"{label}: rebuild differs"
            if "layers_differing" in step.expect and service.layers:
                assert len(report.layers_differing) == int(step.expect["layers_differing"]), (
                    f"{label}: layers differing {report.layers_differing}"
                )


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
            valid_time=item.get("valid_time"),
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
    if "skipped" in expect:
        assert list(result.skipped) == [int(seq) for seq in expect["skipped"]], f"{label}: skipped {result.skipped}"
    if "shadowed" in expect:  # feature 006: [{seq, reason}] in seq order
        got = [(item.seq, item.reason) for item in result.shadowed]
        want = [(int(item["seq"]), str(item["reason"])) for item in expect["shadowed"]]
        assert got == want, f"{label}: shadowed {got} != {want}"
    if "model_errors" in expect:
        got = sorted((e.rule_id, e.path_scope) for e in result.model_errors)
        want = sorted((str(e["rule"]), str(e["path"])) for e in expect["model_errors"])
        assert got == want, f"{label}: model errors {got} != {want}"


ServiceFactory = Callable[[Case], Any]


def build_service(case: Case, storage: Any = None, layers: bool = True) -> Any:
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
    return Aduku(
        storage or MemoryStorage(),
        registry,
        clock=fake,
        ids=SequentialIds(),
        behaviors=behaviors,
        layers=layers,
        **extra,
    )
