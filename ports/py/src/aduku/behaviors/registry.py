"""Behavior registry: named, versioned, pure behaviors with declared algebraic laws (SPEC 5.4)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from aduku.errors import SchemaError
from aduku.expr.errors import ExprError
from aduku.expr.evaluator import Compiled, compile_expression
from aduku.expr.limits import Limits

__all__ = ["Behavior", "BehaviorRegistry", "Laws", "MacroImpl", "PRESET_NAMES"]


@dataclass(frozen=True)
class Laws:
    associative: bool = False
    commutative: bool = False
    idempotent: bool = False


# Names of the ready-made macros (SPEC 9.3). They are reserved: no custom macro or hook may take one.
PRESET_NAMES = frozenset(
    {
        "upper",
        "lower",
        "trim",
        "concat",
        "coalesce",
        "len",
        "sum",
        "min",
        "max",
        "round",
        "format_ts",
        "hash",
        "join",
        "if",
        "eq",
    }
)


@dataclass(frozen=True)
class MacroImpl:
    """A registered custom macro: a compiled expression, and whether it runs when an input is missing."""

    name: str
    version: int
    program: Compiled
    accepts_missing: bool

    @property
    def params(self) -> tuple[str, ...]:
        return self.program.params

    def call(self, args: Sequence[Any]) -> Any:
        """The value for these arguments. Raises ExprFault (a limit, or a type error in the expression)."""
        return self.program.run(args)

    __call__ = call  # a macro is a behavior like any other: its `impl` is callable


@dataclass(frozen=True)
class Behavior:
    name: str
    version: int
    kind: Literal["merge", "validate", "macro", "hook"]
    impl: Callable[..., Any]
    laws: Laws = Laws()
    purity: Literal["pure", "io"] = "pure"
    # Kinds of schema type the behavior can merge (see schema.model.parse_type); None means any type.
    applies_to: tuple[str, ...] | None = None
    # True for a rule that merges a struct child by child: writes of struct values are spread over the child paths.
    field_wise: bool = False
    # For a validator: the problems in the arguments a schema gives it (checked when the schema loads).
    check_args: Callable[[Any], list[str]] | None = None
    # For a hook used by deferred fields (feature 003): items per call, calls per second, and whether the call also
    # receives the call context (document, field, attempt, deadline).
    batch_max: int | None = None
    rate_limit: float | None = None
    with_context: bool = False


class BehaviorRegistry:
    def __init__(self) -> None:
        self._behaviors: dict[tuple[str, int], Behavior] = {}

    @classmethod
    def with_builtins(cls) -> BehaviorRegistry:
        """A registry holding the built-in merge rules (lww, max, min, union, struct) and validators (non_empty, range, max_len)."""
        from aduku.behaviors.merge import register_builtins
        from aduku.behaviors.validate import register_validators

        registry = cls()
        register_builtins(registry)
        register_validators(registry)
        return registry

    def register_merge(
        self,
        name: str,
        version: int,
        combine: Callable[..., Any],
        laws: Laws,
        applies_to: tuple[str, ...] | None = None,
        field_wise: bool = False,
    ) -> Behavior:
        """`combine(path, old, new, ctx)`; registering without `associative` succeeds, but schemas using it are refused."""
        return self._add(Behavior(name, version, "merge", combine, laws, applies_to=applies_to, field_wise=field_wise))

    def register_validate(
        self, name: str, version: int, check: Callable[..., Any], check_args: Callable[[Any], list[str]] | None = None
    ) -> Behavior:
        return self._add(Behavior(name, version, "validate", check, check_args=check_args))

    def register_macro(
        self,
        name: str,
        version: int,
        params: Sequence[str],
        expression: str,
        *,
        accepts_missing: bool = False,
        limits: Mapping[str, int] | None = None,
    ) -> Behavior:
        """A custom macro: `expression` over the named `params`, run in the sandbox of `expr/` (feature 002).

        `limits` may name any of steps, expression_chars, depth, value_bytes, input_bytes, up to the platform maximum.
        Raises SchemaError for a preset name, an expression that does not compile or uses a name that is not a
        parameter, a bad parameter list, or a limit over the maximum.
        """
        program_limits = Limits.for_macro(**(limits or {}))
        try:
            program = compile_expression(expression, params, program_limits)
        except ExprError as error:
            raise SchemaError(f"BAD_MACRO_EXPRESSION: macro {name}@{version}: {error}") from error
        return self._add(Behavior(name, version, "macro", MacroImpl(name, version, program, accepts_missing)))

    def register_hook(
        self,
        name: str,
        version: int,
        call: Callable[..., Any],
        *,
        batch_max: int | None = None,
        rate_limit: float | None = None,
        with_context: bool = False,
    ) -> Behavior:
        """`call(inputs, timeout)` for an impure compute or a deferred field: idempotent, given only its declared
        dependencies (features 002 and 003).

        `batch_max=N` makes it a batch source, `call(items, timeout) -> list` with one answer per item (a value, or an
        `Exception` for that item); `rate_limit` is calls per second; `with_context` adds a third argument `ctx`.
        """
        if batch_max is not None and batch_max < 1:
            raise SchemaError(f"hook {name}@{version}: batch_max must be at least 1")
        if rate_limit is not None and not rate_limit > 0:
            raise SchemaError(f"hook {name}@{version}: rate_limit must be above zero (calls per second)")
        return self._add(
            Behavior(
                name,
                version,
                "hook",
                call,
                purity="io",
                batch_max=batch_max,
                rate_limit=rate_limit,
                with_context=with_context,
            )
        )

    def _add(self, behavior: Behavior) -> Behavior:
        if behavior.kind in ("macro", "hook") and behavior.name in PRESET_NAMES:
            raise SchemaError(f"RESERVED_MACRO_NAME: {behavior.name!r} is the name of a preset macro and is reserved")
        key = (behavior.name, behavior.version)
        if key in self._behaviors:
            raise SchemaError(f"behavior {behavior.name}@{behavior.version} is already registered")
        self._behaviors[key] = behavior
        return behavior

    def get(self, name: str, version: int) -> Behavior | None:
        return self._behaviors.get((name, version))

    def all(self) -> tuple[Behavior, ...]:
        return tuple(self._behaviors.values())
