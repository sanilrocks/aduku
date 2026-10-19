"""P4: computed fields (SPEC 9.3, feature 002). Pure.

Evaluated after the full merge, in the dependency order of `engine/graph.py`. A macro is given only the values of its
declared arguments. Results are inserted into the document with the `computed::` annotation and are never written to the
ledger. A macro that errors or returns the wrong type is a `ComputeFault`, never a null.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from aduku.behaviors.compute import PRESETS, PresetError
from aduku.behaviors.registry import BehaviorRegistry, MacroImpl
from aduku.engine.document import strip_computed, value_at, with_value
from aduku.engine.effects import EffectView, PendingCall, fingerprint
from aduku.engine.graph import order
from aduku.errors import ComputeFault
from aduku.expr.errors import ExprFault
from aduku.ion_codec import ABSENT, Annotated, FrozenStruct
from aduku.model import Violation
from aduku.paths import parse_path
from aduku.schema.model import ComputedSpec, FieldSpec, ImpureSpec, Schema, conforms

__all__ = ["ComputeResult", "ComputedResult", "compute_fields", "failure_mode_of"]

Status = Literal["value", "null", "absent", "error"]


@dataclass(frozen=True)
class ComputedResult:
    """What one computed field came to: its status and value, the values it was computed from, and its source."""

    path: str
    status: Status
    value: Any
    dependencies: tuple[tuple[str, Any], ...]
    source: str  # "upper@1", or "tax.lookup@2" for a hook
    effect_seq: int | None = None


@dataclass(frozen=True)
class ComputeResult:
    document: FrozenStruct
    results: dict[str, ComputedResult] = field(default_factory=dict)
    violations: tuple[Violation, ...] = ()
    pending: tuple[PendingCall, ...] = ()  # hook calls the derivation still needs (impure fields with no outcome yet)


def compute_fields(
    schema: Schema,
    behaviors: BehaviorRegistry,
    document: FrozenStruct,
    fields: Sequence[FieldSpec] | None = None,
    effects: EffectView | None = None,
) -> ComputeResult:
    """The document with its computed fields added, plus how each was computed. `fields` is `order(schema)`, if known."""
    fields = order(schema) if fields is None else fields
    if not fields:
        return ComputeResult(document)
    results: dict[str, ComputedResult] = {}
    violations: list[Violation] = []
    pending: list[PendingCall] = []
    view = effects if effects is not None else EffectView()
    for spec in fields:
        if spec.computed is not None:
            result = _evaluate(spec, spec.computed, behaviors, document)
        elif spec.impure is not None:
            result, failure = _evaluate_impure(spec, spec.impure, document, view, pending)
            if failure is not None:
                violations.append(failure)
        else:
            continue
        results[spec.pattern] = result
        if result.status == "error":
            lacking = ", ".join(path for path, value in result.dependencies if value is ABSENT or value is None)
            violations.append(
                Violation(f"computed:{spec.pattern}", spec.pattern, f"{result.source} has no value for: {lacking}")
            )
        if result.status in ("value", "null"):
            inserted = with_value(document, parse_path(spec.pattern), Annotated(("computed",), result.value))
            if inserted is None:
                raise ComputeFault(result.source, f"the parent of {spec.pattern} holds a value that is not a struct")
            document = inserted
    return ComputeResult(document, results, tuple(violations), tuple(pending))


def failure_mode_of(schema: Schema, spec: FieldSpec) -> str:
    """What a failing hook does for this field: its own setting, else the field-level one, else the schema's."""
    impure = spec.impure
    return (impure.failure_mode if impure else None) or spec.failure_mode or schema.impure_failure_mode


def _evaluate_impure(
    spec: FieldSpec, impure: ImpureSpec, document: FrozenStruct, view: EffectView, pending: list[PendingCall]
) -> tuple[ComputedResult, Violation | None]:
    """An impure field takes its value from the recorded outcome for its current inputs; it never calls a hook.

    A recorded failure is a model error `impure:<path>`; the field follows `on_missing` (an `error` setting adds no second
    error: the failure is the one error)."""
    source = f"{impure.hook}@{impure.version}"
    dependencies = tuple((path, _plain(value_at(document, parse_path(path)))) for path in impure.depends)
    if any(value is ABSENT or value is None for _, value in dependencies):
        status: Status = spec.on_missing  # the hook is not called without all of its inputs
        return ComputedResult(spec.pattern, status, None, dependencies, source), None
    digest = fingerprint(source, dependencies)
    outcome = view.lookup(spec.pattern, digest, source)
    if outcome is None:
        pending.append(PendingCall(spec.pattern, source, dependencies, digest))
        return ComputedResult(spec.pattern, "absent", None, dependencies, source), None  # until the call is made
    if outcome.kind == "value":
        return ComputedResult(spec.pattern, "value", outcome.value, dependencies, source, outcome.effect_seq), None
    status = "absent" if spec.on_missing == "error" else spec.on_missing
    failure = Violation(f"impure:{spec.pattern}", spec.pattern, f"{source} failed ({outcome.error})")
    return ComputedResult(spec.pattern, status, None, dependencies, source, outcome.effect_seq), failure


def _plain(value: Any) -> Any:
    """A dependency as a macro or a hook sees it: without `computed::`, also on the fields of a struct it holds."""
    return strip_computed(value.value if isinstance(value, Annotated) else value)


def _evaluate(
    spec: FieldSpec, computed: ComputedSpec, behaviors: BehaviorRegistry, document: FrozenStruct
) -> ComputedResult:
    source = f"{computed.macro}@{computed.version}"
    values: list[Any] = []
    dependencies: list[tuple[str, Any]] = []
    for arg in computed.args:
        if arg.path is None:
            values.append(arg.literal)
            continue
        found = _plain(value_at(document, parse_path(arg.path)))
        dependencies.append((arg.path, found))
        values.append(found)
    run, accepts_missing = _macro(computed, behaviors, source)
    missing = [v for (_, v) in dependencies if v is ABSENT or v is None]
    if missing and not accepts_missing:
        status: Status = spec.on_missing  # "null", "absent" or "error"
        return ComputedResult(spec.pattern, status, None, tuple(dependencies), source)
    arguments = [None if v is ABSENT else v for v in values]
    try:
        value = run(arguments)
    except (PresetError, ExprFault) as error:
        raise ComputeFault(source, str(error)) from error
    if value is not None and spec.type is not None and not conforms(spec.type, value):
        raise ComputeFault(source, f"the result does not conform to type {spec.type}")
    return ComputedResult(spec.pattern, "value", value, tuple(dependencies), source)


def _macro(computed: ComputedSpec, behaviors: BehaviorRegistry, source: str):  # type: ignore[no-untyped-def]
    """(function of the argument list, accepts_missing) for a preset or a registered macro."""
    preset = PRESETS.get(computed.macro) if computed.version == 1 else None
    if preset is not None:
        return preset.call, preset.accepts_missing
    behavior = behaviors.get(computed.macro, computed.version)
    if behavior is None or behavior.kind != "macro":
        raise ComputeFault(source, "the macro is not registered")
    impl = behavior.impl
    assert isinstance(impl, MacroImpl)
    return impl.call, impl.accepts_missing
