"""Load a schema from Ion text (contracts/schema-format.md). Every problem is reported together."""

from __future__ import annotations

import re
from typing import Any

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import SchemaError
from aduku.ion_codec import Annotated, FrozenStruct, IonError, Symbol, parse
from aduku.paths import PathError, parse_path, parse_pattern
from aduku.schema.model import (
    MAX_HOOK_DEADLINE,
    MAX_HOOK_RETRIES,
    MAX_HOOK_TIMEOUT,
    MISSING,
    AuditSettings,
    BehaviorRef,
    ComputedSpec,
    FieldSpec,
    ImpureSpec,
    MacroArg,
    Schema,
    conforms,
    parse_type,
)

__all__ = ["load_schema"]

TOP_KEYS = {
    "id",
    "version",
    "tiers",
    "roles",
    "lenient",
    "rejection_audit",
    "impure_failure_mode",
    "fields",
    "document_validate",
    "doc",
}
FIELD_KEYS = {
    "type",
    "merge",
    "default",
    "on_delete",
    "validate",
    "authority",
    "computed",
    "deferred",
    "redact",
    "ops",
    "doc",
    "failure_mode",
    "on_missing",
}
COMPUTED_KEYS = {"macro", "args", "impure", "doc"}
IMPURE_REQUIRED = ("hook", "depends", "reason", "timeout", "retry", "deadline", "recompute_by")
IMPURE_KEYS = {*IMPURE_REQUIRED, "failure_mode"}
ON_MISSING = ("null", "absent", "error")
FAILURE_MODES = {"reject_on_failure", "model_error_on_failure"}
CORE_OPS = {"set", "delete"}
EXTENSION_OPS = {"increment", "append", "reset"}  # SPEC 4.3: arrive in v1.1
_DURATION = re.compile(r"(\d+)([smhdw])\Z")
_SHORT_DURATION = re.compile(r"(\d+)(ms|s|m|h)\Z")  # hook timings: 200ms, 2s, 1m
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def load_schema(text: str, behaviors: BehaviorRegistry | None = None) -> Schema:
    """Parse and validate a schema, failing closed. Raises SchemaError listing every problem, each with a stable code.

    Merge behaviors are looked up in `behaviors` (default: the built-in rules).
    """
    try:
        document = parse(text)
    except IonError as error:
        raise SchemaError(f"schema is not valid Ion: {error}") from error
    if not (
        isinstance(document, Annotated)
        and document.annotations == ("schema",)
        and isinstance(document.value, FrozenStruct)
    ):
        raise SchemaError("a schema document must be a struct annotated `schema::`")
    problems: list[str] = []
    schema = _build(document.value, problems, behaviors or BehaviorRegistry.with_builtins())
    if problems or schema is None:
        raise SchemaError(problems or ["schema could not be built"])
    return schema


def _names(value: Any, where: str, problems: list[str]) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        problems.append(f"{where} must be a list of names")
        return ()
    return tuple(str(item) for item in value)


def _build(top: FrozenStruct, problems: list[str], behaviors: BehaviorRegistry) -> Schema | None:
    for key in top:
        if key not in TOP_KEYS:
            problems.append(f'unknown top-level key "{key}"')

    schema_id = top.get("id")
    if not isinstance(schema_id, str) or not schema_id:
        problems.append('missing or invalid required key "id" (a non-empty string)')
    version = top.get("version")
    if "version" not in top:
        problems.append('missing required key "version"')
    elif not isinstance(version, int) or isinstance(version, bool):
        problems.append('"version" must be an integer')

    tiers: tuple[str, ...] = ()
    if "tiers" not in top:
        problems.append('missing required key "tiers"')
    else:
        tiers = _names(top["tiers"], '"tiers"', problems)
        if not tiers:
            problems.append('"tiers" must list at least one tier')
        seen: set[str] = set()
        for tier in tiers:
            if tier in seen:
                problems.append(f"DUPLICATE_TIER: duplicate tier {tier!r}")
            seen.add(tier)

    roles = _names(top["roles"], '"roles"', problems) if "roles" in top else ()

    lenient_tiers: tuple[str, ...] = ()
    lenient_contributors: tuple[str, ...] = ()
    lenient = top.get("lenient")
    if lenient is not None:
        if not isinstance(lenient, FrozenStruct):
            problems.append('"lenient" must be a struct')
        else:
            for key in lenient:
                if key not in ("allowed", "contributors"):
                    problems.append(f'unknown key "lenient.{key}"')
            lenient_tiers = _names(lenient.get("allowed", ()), "lenient.allowed", problems)
            lenient_contributors = _names(lenient.get("contributors", ()), "lenient.contributors", problems)

    audit = _audit(top.get("rejection_audit"), problems)

    mode = top.get("impure_failure_mode", "reject_on_failure")
    if mode not in FAILURE_MODES:
        problems.append(f'"impure_failure_mode" must be one of {sorted(FAILURE_MODES)}')
        mode = "reject_on_failure"

    fields: list[FieldSpec] = []
    raw_fields = top.get("fields", FrozenStruct())
    if not isinstance(raw_fields, FrozenStruct):
        problems.append('"fields" must be a struct')
    else:
        for pattern in sorted(raw_fields):
            spec = _field(pattern, raw_fields[pattern], problems, behaviors, (*tiers, *roles))
            if spec is not None:
                fields.append(spec)

    document_validate = _refs(top.get("document_validate", ()), "document_validate", problems)
    for ref in document_validate:
        _check_validator("document_validate", ref, behaviors, problems)

    schema = Schema(
        id=schema_id if isinstance(schema_id, str) else "",
        version=version if isinstance(version, int) and not isinstance(version, bool) else 0,
        tiers=tiers,
        fields=tuple(fields),
        lenient_tiers=lenient_tiers,
        lenient_contributors=lenient_contributors,
        roles=roles,
        rejection_audit=audit,
        document_validate=document_validate,
        impure_failure_mode=str(mode),
    )
    _check_computed(schema, behaviors, problems)
    _semantic(schema, problems)  # runs even after structural problems, so that everything is reported together
    return None if problems else schema


def _audit(raw: Any, problems: list[str]) -> AuditSettings:
    if raw is None:
        return AuditSettings()
    if not isinstance(raw, FrozenStruct):
        problems.append('"rejection_audit" must be a struct')
        return AuditSettings()
    for key in raw:
        if key not in ("enabled", "mode", "retention"):
            problems.append(f'unknown key "rejection_audit.{key}"')
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        problems.append("rejection_audit.enabled must be true or false")
        enabled = False
    mode = str(raw.get("mode", "full"))
    if mode not in ("full", "metadata_only"):
        problems.append(f"rejection_audit.mode must be full or metadata_only, not {mode!r}")
        mode = "full"
    max_entries: int | None = None
    max_age: int | None = None
    retention = raw.get("retention")
    if retention is not None:
        if not isinstance(retention, FrozenStruct):
            problems.append("rejection_audit.retention must be a struct")
        else:
            for key in retention:
                if key not in ("max_entries", "max_age"):
                    problems.append(f'unknown key "rejection_audit.retention.{key}"')
            if "max_entries" in retention:
                value = retention["max_entries"]
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    max_entries = value
                else:
                    problems.append("rejection_audit.retention.max_entries must be a positive integer")
            if "max_age" in retention:
                match = _DURATION.match(str(retention["max_age"]))
                if match and int(match.group(1)) * _UNITS[match.group(2)] > 0:
                    max_age = int(match.group(1)) * _UNITS[match.group(2)]
                elif match:
                    problems.append(
                        f"RETENTION_NOT_POSITIVE: rejection_audit.retention.max_age {retention['max_age']!r} must be above zero"
                    )
                else:
                    problems.append(
                        f"rejection_audit.retention.max_age {retention['max_age']!r} is not a duration such as 90d, 12h, 30m, 45s"
                    )
    if enabled and max_entries is None and max_age is None:
        problems.append(
            "AUDIT_RETENTION_REQUIRED: rejection_audit is enabled without a retention limit;"
            " set retention.max_entries or retention.max_age"
        )
    return AuditSettings(enabled=enabled, mode=mode, max_entries=max_entries, max_age_seconds=max_age)  # type: ignore[arg-type]


def _field(
    pattern: str, raw: Any, problems: list[str], behaviors: BehaviorRegistry, names: tuple[str, ...]
) -> FieldSpec | None:
    where = f"field {pattern}"
    try:
        parse_pattern(pattern)
    except PathError as error:
        problems.append(f"{where}: invalid pattern ({error})")
        return None
    if not isinstance(raw, FrozenStruct):
        problems.append(f"{where}: declaration must be a struct")
        return None
    for key in raw:
        if key not in FIELD_KEYS:
            problems.append(f'{where}: unknown key "{key}"')
    type_text: str | None = None
    if "type" in raw:
        type_text = str(raw["type"])
        try:
            parse_type(type_text)
        except ValueError:
            problems.append(f"{where}: unknown type {type_text!r}")
    kinds = [kind for kind in ("computed", "deferred") if kind in raw]
    if len(kinds) == 2:
        problems.append(f"COMPUTED_AND_DEFERRED: {where} cannot be both computed and deferred")
    if kinds and ("merge" in raw or "default" in raw):
        problems.append(f"DERIVED_HAS_MERGE_OR_DEFAULT: {where} is derived and must not declare merge or default")
    merge = None
    if "merge" in raw:
        merge = _ref(raw["merge"], f"{where}: merge", problems)
        if merge is not None and not kinds:
            _check_merge(where, merge, type_text, behaviors, problems)
    if "default" in raw and not kinds and type_text is not None and _valid_type(type_text):
        if not conforms(type_text, raw["default"]):
            problems.append(f"DEFAULT_TYPE_MISMATCH: {where}: the default does not conform to type {type_text}")
    if "default" in raw and "*" in pattern:
        problems.append(f"DEFAULT_ON_PATTERN: {where}: a default needs an exact path, not a wildcard pattern")
    if raw.get("on_delete") == "default" and "default" not in raw:
        problems.append(f"ON_DELETE_NEEDS_DEFAULT: {where}: on_delete: default needs a default value")
    on_delete = str(raw["on_delete"]) if "on_delete" in raw else None
    if on_delete is not None and on_delete not in ("absent", "default"):
        problems.append(f"{where}: on_delete must be absent or default, not {on_delete!r}")
        on_delete = None
    validators = _refs(raw.get("validate", ()), f"{where}: validate", problems)
    for ref in validators:
        _check_validator(where, ref, behaviors, problems)
    ops = _ops(raw["ops"], where, problems) if "ops" in raw else None
    failure_mode = str(raw["failure_mode"]) if "failure_mode" in raw else None
    if failure_mode is not None and failure_mode not in FAILURE_MODES:
        problems.append(f"{where}: failure_mode must be one of {sorted(FAILURE_MODES)}, not {failure_mode!r}")
    authority = _names(raw["authority"], f"{where}: authority", problems) if "authority" in raw else None
    for name in authority or ():
        if name not in names:
            problems.append(
                f"UNKNOWN_TIER: {where}: authority names {name!r}, which is neither a tier nor a declared role"
            )
    derived = kinds[0] if kinds else None
    derived_spec = raw.get(kinds[0]) if kinds else None
    computed_spec: ComputedSpec | None = None
    impure_spec: ImpureSpec | None = None
    if "computed" in raw:
        computed_spec, impure_spec = _computed(raw["computed"], where, names, problems)
    # the setting named `null` reads as the Ion null value when written bare
    on_missing = ("null" if raw["on_missing"] is None else str(raw["on_missing"])) if "on_missing" in raw else "null"
    if on_missing not in ON_MISSING:
        problems.append(f"{where}: on_missing must be one of {list(ON_MISSING)}, not {on_missing!r}")
        on_missing = "null"
    elif "on_missing" in raw and "computed" not in raw:
        problems.append(f"{where}: on_missing applies only to computed fields")
    return FieldSpec(
        pattern=pattern,
        type=type_text,
        merge=merge,
        default=raw["default"] if "default" in raw else MISSING,
        on_delete=on_delete,  # type: ignore[arg-type]
        validate=validators,
        authority=authority,
        derived=derived,  # type: ignore[arg-type]
        derived_spec=derived_spec,
        failure_mode=failure_mode,
        ops=ops,
        redact=raw.get("redact"),
        computed=computed_spec,
        impure=impure_spec,
        on_missing=on_missing,  # type: ignore[arg-type]
    )


def _check_computed(schema: Schema, behaviors: BehaviorRegistry, problems: list[str]) -> None:
    """Resolve each computed field's macro: it must exist, take these arguments, and fit the declared type."""
    from aduku.behaviors.compute import (
        PRESETS,
    )  # imported late: the presets are the engine's, the loader only reads them

    for spec in schema.fields:
        if spec.impure is not None:
            hook = behaviors.get(spec.impure.hook, spec.impure.version)
            if hook is None or hook.kind != "hook":
                problems.append(
                    f"UNKNOWN_BEHAVIOR: field {spec.pattern}: no hook {spec.impure.hook}@{spec.impure.version} is registered"
                )
        computed = spec.computed
        if computed is None:
            continue
        where = f"field {spec.pattern}"
        name = f"{computed.macro}@{computed.version}"
        preset = PRESETS.get(computed.macro) if computed.version == 1 else None
        if preset is not None:
            count = len(computed.args)
            if count < preset.fewest_args or (preset.most_args is not None and count > preset.most_args):
                problems.append(f"BAD_MACRO_ARGS: {where}: {name} was given {count} argument(s)")
            elif preset.result_type and spec.type and spec.type not in (preset.result_type, "any"):
                problems.append(
                    f"BAD_MACRO_ARGS: {where}: {name} gives a {preset.result_type}, but the field is declared {spec.type}"
                )
            continue
        behavior = behaviors.get(computed.macro, computed.version)
        if behavior is None or behavior.kind != "macro":
            problems.append(f"UNKNOWN_BEHAVIOR: {where}: no macro {name} is registered")
            continue
        params = getattr(behavior.impl, "params", ())
        if len(computed.args) != len(params):
            problems.append(
                f"BAD_MACRO_ARGS: {where}: {name} takes {len(params)} argument(s) ({', '.join(params)}),"
                f" got {len(computed.args)}"
            )
        for arg in computed.args:
            if arg.path is None:
                continue
            target = schema.field_for(parse_path(arg.path))
            if target is not None and target.type is not None and _unsupported_input(target.type):
                problems.append(
                    f"MACRO_INPUT_UNSUPPORTED: {where}: custom macro {name} cannot take {arg.path}, a {target.type}"
                    " (floats and timestamps are not supported in macro expressions)"
                )


def _unsupported_input(type_text: str) -> bool:
    try:
        kind, element = parse_type(type_text)
    except ValueError:
        return False
    return kind in ("float", "timestamp") or element in ("float", "timestamp")


def _seconds(raw: Any, where: str, problems: list[str]) -> float | None:
    match = _SHORT_DURATION.match(str(raw))
    if not match:
        problems.append(f"{where} {raw!r} is not a duration such as 200ms, 2s, 1m")
        return None
    unit = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[match.group(2)]
    return int(match.group(1)) * unit


def _computed(
    raw: Any, where: str, names: tuple[str, ...], problems: list[str]
) -> tuple[ComputedSpec | None, ImpureSpec | None]:
    """`computed: { macro, args }` (pure) or `computed: { impure: {...} }` (SPEC 9.3, 9.3.1)."""
    if not isinstance(raw, FrozenStruct):
        problems.append(f"{where}: computed must be a struct")
        return None, None
    for key in raw:
        if key not in COMPUTED_KEYS:
            problems.append(f'{where}: unknown key "computed.{key}"')
    has_macro, has_impure = "macro" in raw, "impure" in raw
    if has_macro and has_impure:
        problems.append(f"{where}: computed declares both a macro and an impure hook; choose one")
        return None, None
    if not has_macro and not has_impure:
        problems.append(f"{where}: computed needs a macro or an impure hook")
        return None, None
    if has_impure:
        return None, _impure(raw["impure"], where, names, problems)
    ref = _ref(raw["macro"], f"{where}: computed.macro", problems)
    args = _macro_args(raw.get("args", ()), where, problems)
    if ref is None:
        return None, None
    return ComputedSpec(macro=ref.name, version=ref.version, args=args), None


def _macro_args(raw: Any, where: str, problems: list[str]) -> tuple[MacroArg, ...]:
    if not isinstance(raw, tuple):
        problems.append(f"BAD_MACRO_ARGS: {where}: computed.args must be a list")
        return ()
    found: list[MacroArg] = []
    for item in raw:
        if isinstance(item, Annotated) and item.annotations == ("lit",):
            found.append(MacroArg(literal=item.value))
        elif isinstance(item, str) and item.startswith("/"):
            try:
                parse_path(str(item))
            except PathError as error:
                problems.append(f"BAD_MACRO_ARGS: {where}: argument {item!r} is not a valid path ({error})")
                continue
            found.append(MacroArg(path=str(item)))
        else:
            problems.append(
                f"BAD_MACRO_ARGS: {where}: argument {item!r} must be a path (a string starting with /) or lit::<value>"
            )
    return tuple(found)


def _impure(raw: Any, where: str, names: tuple[str, ...], problems: list[str]) -> ImpureSpec | None:
    if not isinstance(raw, FrozenStruct):
        problems.append(f"{where}: computed.impure must be a struct")
        return None
    before = len(problems)
    for key in raw:
        if key not in IMPURE_KEYS:
            problems.append(f'{where}: unknown key "computed.impure.{key}"')
    for key in IMPURE_REQUIRED:
        if key not in raw:
            problems.append(f"IMPURE_DECLARATION_INCOMPLETE: {where}: an impure compute needs {key}")
    if len(problems) > before:
        return None
    ref = _ref(raw["hook"], f"{where}: impure.hook", problems)
    depends = _names(raw["depends"], f"{where}: impure.depends", problems)
    for path_text in depends:
        try:
            parse_path(path_text)
        except PathError as error:
            problems.append(f"BAD_MACRO_ARGS: {where}: dependency {path_text!r} is not a valid path ({error})")
    if not depends:
        problems.append(f"IMPURE_DECLARATION_INCOMPLETE: {where}: an impure compute needs at least one dependency")
    reason = raw["reason"]
    if not isinstance(reason, str) or not reason.strip():
        problems.append(f"IMPURE_DECLARATION_INCOMPLETE: {where}: reason must be non-empty text")
    timeout = _seconds(raw["timeout"], f"{where}: impure.timeout", problems)
    deadline = _seconds(raw["deadline"], f"{where}: impure.deadline", problems)
    retry = raw["retry"]
    retry_max, backoff = 0, 0.0
    if not isinstance(retry, FrozenStruct) or "max" not in retry:
        problems.append(f"IMPURE_DECLARATION_INCOMPLETE: {where}: retry needs max (and optionally backoff)")
    else:
        value = retry["max"]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_HOOK_RETRIES:
            problems.append(f"{where}: impure.retry.max must be a whole number from 0 to {MAX_HOOK_RETRIES}")
        else:
            retry_max = value
        if "backoff" in retry:
            backoff = _seconds(retry["backoff"], f"{where}: impure.retry.backoff", problems) or 0.0
    if timeout is not None and not 0 < timeout <= MAX_HOOK_TIMEOUT:
        problems.append(f"{where}: impure.timeout must be above zero and at most {MAX_HOOK_TIMEOUT:g}s")
    if deadline is not None and (deadline > MAX_HOOK_DEADLINE or (timeout is not None and deadline < timeout)):
        problems.append(f"{where}: impure.deadline must be at least the timeout and at most {MAX_HOOK_DEADLINE:g}s")
    recompute_by = _names(raw["recompute_by"], f"{where}: impure.recompute_by", problems)
    if not recompute_by:
        problems.append(f"IMPURE_DECLARATION_INCOMPLETE: {where}: recompute_by must list at least one tier or role")
    for name in recompute_by:
        if name not in names:
            problems.append(
                f"UNKNOWN_TIER: {where}: recompute_by names {name!r}, which is neither a tier nor a declared role"
            )
    failure_mode = str(raw["failure_mode"]) if "failure_mode" in raw else None
    if failure_mode is not None and failure_mode not in FAILURE_MODES:
        problems.append(f"{where}: impure.failure_mode must be one of {sorted(FAILURE_MODES)}, not {failure_mode!r}")
        failure_mode = None
    if ref is None or timeout is None or deadline is None or len(problems) > before:
        return None
    return ImpureSpec(
        hook=ref.name,
        version=ref.version,
        depends=depends,
        reason=str(reason),
        timeout=timeout,
        retry_max=retry_max,
        backoff=backoff,
        deadline=deadline,
        recompute_by=recompute_by,
        failure_mode=failure_mode,
    )


def _valid_type(text: str) -> bool:
    try:
        parse_type(text)
    except ValueError:
        return False
    return True


def _check_validator(where: str, ref: BehaviorRef, behaviors: BehaviorRegistry, problems: list[str]) -> None:
    behavior = behaviors.get(ref.name, ref.version)
    if behavior is None or behavior.kind != "validate":
        problems.append(f"UNKNOWN_BEHAVIOR: {where}: no validator {ref.name}@{ref.version} is registered")
    elif behavior.check_args is not None:
        for problem in behavior.check_args(ref.args):
            problems.append(f"BAD_BEHAVIOR_ARGS: {where}: {ref.name}@{ref.version}: {problem}")


def _check_merge(
    where: str, ref: BehaviorRef, type_text: str | None, behaviors: BehaviorRegistry, problems: list[str]
) -> None:
    behavior = behaviors.get(ref.name, ref.version)
    if behavior is None or behavior.kind != "merge":
        problems.append(f"UNKNOWN_BEHAVIOR: {where}: no merge behavior {ref.name}@{ref.version} is registered")
        return
    if not behavior.laws.associative:
        problems.append(
            f"MERGE_NOT_ASSOCIATIVE: {where}: merge behavior {ref.name}@{ref.version} does not declare associativity"
        )
    if behavior.applies_to is not None:
        kind = parse_type(type_text)[0] if type_text is not None and _valid_type(type_text) else None
        if type_text is None or (_valid_type(type_text) and kind not in behavior.applies_to):
            problems.append(
                f"MERGE_TYPE_UNSUITABLE: {where}: merge {ref.name} needs a field of type {', '.join(behavior.applies_to)};"
                f" this field is {type_text or 'untyped'}"
            )


def _ops(raw: Any, where: str, problems: list[str]) -> tuple[str, ...] | None:
    names = _names(raw, f"{where}: ops", problems)
    for name in names:
        if name in EXTENSION_OPS:
            problems.append(
                f"OP_NOT_SUPPORTED: {where}: operation {name!r} is not supported until v1.1 (only set and delete)"
            )
        elif name not in CORE_OPS:
            problems.append(f"UNKNOWN_OPERATION: {where}: ops contains unknown operation {name!r}")
    return names


def _refs(raw: Any, where: str, problems: list[str]) -> tuple[BehaviorRef, ...]:
    if not isinstance(raw, tuple):
        problems.append(f"{where} must be a list of behavior references")
        return ()
    refs = [_ref(item, where, problems) for item in raw]
    return tuple(ref for ref in refs if ref is not None)


def _ref(raw: Any, where: str, problems: list[str]) -> BehaviorRef | None:
    """name | "name@3" | custom::"name@3" | name::{args}"""
    custom = False
    args = FrozenStruct()
    if isinstance(raw, Annotated):
        if raw.annotations == ("custom",) and isinstance(raw.value, str):
            custom, raw = True, raw.value
        elif len(raw.annotations) == 1 and isinstance(raw.value, FrozenStruct):
            args, raw = raw.value, Symbol(raw.annotations[0])
        else:
            problems.append(f"{where}: unsupported behavior reference {raw!r}")
            return None
    if not isinstance(raw, str):
        problems.append(f"{where}: unsupported behavior reference {raw!r}")
        return None
    name, _, version_text = str(raw).partition("@")
    if not name:
        problems.append(f"{where}: empty behavior name")
        return None
    version = 1
    if version_text:
        if not version_text.isdigit():
            problems.append(f"{where}: bad behavior version in {str(raw)!r}")
            return None
        version = int(version_text)
    return BehaviorRef(name=name, version=version, args=args, custom=custom)


# -- cross-field rules ---------------------------------------------------------------------------------------------


def _paths_in(value: Any) -> list[str]:
    """Every path-like string under the keys `depends` and `args`, at any depth."""
    found: list[str] = []
    if isinstance(value, Annotated):
        value = value.value
    if isinstance(value, FrozenStruct):
        for key, item in value.items():
            if key in ("depends", "args") and isinstance(item, tuple):
                found.extend(str(p) for p in item if isinstance(p, str) and p.startswith("/"))
            else:
                found.extend(_paths_in(item))
    return found


def _semantic(schema: Schema, problems: list[str]) -> None:
    for tier in schema.lenient_tiers:
        if tier not in schema.tiers:
            problems.append(f"UNKNOWN_TIER: lenient.allowed names {tier!r}, which is not a tier")

    def deferred_target(path_text: str) -> FieldSpec | None:
        try:
            target = schema.derived_field_at_or_above(parse_path(path_text))
        except PathError:
            return None
        return target if target is not None and target.derived == "deferred" else None

    def refuse(source: str, path_text: str, target: FieldSpec) -> None:
        problems.append(
            f"DEPENDS_ON_DEFERRED: {source} depends on {path_text}, which is deferred field {target.pattern};"
            " deferred values never feed validation, computed fields or defaults"
        )

    for spec in schema.fields:
        for path_text in (
            *(spec.computed.paths if spec.computed else ()),
            *(spec.impure.depends if spec.impure else ()),
        ):
            try:
                path = parse_path(path_text)
                # declared directly, or beneath a derived field (its computed value may be a struct)
                declared = schema.field_for(path) is not None or schema.derived_field_at_or_above(path) is not None
            except PathError:
                continue  # reported where the declaration was parsed
            if not declared:
                problems.append(
                    f"UNKNOWN_DEPENDENCY: computed field {spec.pattern} depends on {path_text},"
                    " which no field of the schema declares"
                )
        if spec.derived == "deferred" and spec.validate:
            problems.append(f"DEPENDS_ON_DEFERRED: deferred field {spec.pattern} cannot be validated")
        if spec.derived == "computed":
            for path_text in _paths_in(spec.derived_spec):
                target = deferred_target(path_text)
                if target is not None:
                    refuse(f"computed field {spec.pattern}", path_text, target)
        for ref in spec.validate:
            for path_text in _paths_in(ref.args):
                target = deferred_target(path_text)
                if target is not None:
                    refuse(f"validator {ref.name} on {spec.pattern}", path_text, target)
        if spec.has_default and isinstance(spec.default, FrozenStruct) and "macro" in spec.default:
            for path_text in _paths_in(spec.default):
                target = deferred_target(path_text)
                if target is not None:
                    refuse(f"the default of {spec.pattern}", path_text, target)
    for ref in schema.document_validate:
        for path_text in _paths_in(ref.args):
            target = deferred_target(path_text)
            if target is not None:
                refuse(f"document validator {ref.name}", path_text, target)
    _cycles(schema, problems)


def _cycles(schema: Schema, problems: list[str]) -> None:
    """Derived fields may not depend on each other in a loop."""
    from aduku.engine.graph import find_cycles  # the graph lives with the engine; imported late to keep layers acyclic

    for loop in find_cycles(schema):
        problems.append("DEPENDENCY_CYCLE: derived fields depend on each other in a loop: " + " -> ".join(loop))
