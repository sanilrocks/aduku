"""Behavior registry: named, versioned, pure behaviors with declared algebraic laws (SPEC 5.4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from aduku.errors import SchemaError

__all__ = ["Behavior", "BehaviorRegistry", "Laws"]


@dataclass(frozen=True)
class Laws:
    associative: bool = False
    commutative: bool = False
    idempotent: bool = False


@dataclass(frozen=True)
class Behavior:
    name: str
    version: int
    kind: Literal["merge", "validate"]
    impl: Callable[..., Any]
    laws: Laws = Laws()
    purity: Literal["pure"] = "pure"
    # Kinds of schema type the behavior can merge (see schema.model.parse_type); None means any type.
    applies_to: tuple[str, ...] | None = None
    # True for a rule that merges a struct child by child: writes of struct values are spread over the child paths.
    field_wise: bool = False
    # For a validator: the problems in the arguments a schema gives it (checked when the schema loads).
    check_args: Callable[[Any], list[str]] | None = None


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

    def _add(self, behavior: Behavior) -> Behavior:
        key = (behavior.name, behavior.version)
        if key in self._behaviors:
            raise SchemaError(f"behavior {behavior.name}@{behavior.version} is already registered")
        self._behaviors[key] = behavior
        return behavior

    def get(self, name: str, version: int) -> Behavior | None:
        return self._behaviors.get((name, version))

    def all(self) -> tuple[Behavior, ...]:
        return tuple(self._behaviors.values())
