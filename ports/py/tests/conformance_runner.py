"""Runs conformance cases (conformance/*.ion, contracts/conformance-vectors.md) against an `Aduku` service.

The service API used here is the planned facade (specs/001-core-ledger-merge-engine/contracts/library-api.md);
`run_case` is exercised from the user-story phases onward.
"""

from __future__ import annotations

import pathlib
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


STEP_KINDS = ("append", "get", "explain", "activate_schema", "rebuild", "load_schema")


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
            result = service.get(step.arg)
            _check_document(step.expect, result, label)
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
            assert any(r.path == reason.get("path") for r in error.reasons), f"{label}: missing reason for {reason}"
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
    from aduku.schema.loader import load_schema
    from aduku.schema.registry import SchemaRegistry
    from aduku.service import Aduku
    from aduku.storage.base import FakeClock, SequentialIds
    from aduku.storage.memory import MemoryStorage
    from tests.fixtures import register_fixtures

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
        FakeClock(clock["start"], float(clock.get("step_seconds", 0)))
        if clock is not None
        else FakeClock(step_seconds=1)
    )
    return Aduku(storage or MemoryStorage(), registry, clock=fake, ids=SequentialIds(), behaviors=behaviors)
