"""Deterministic limits for custom macros (research R-2).

"Time" is a budget of evaluation steps and "memory" a budget of value sizes, so the same ledger faults on every machine
or on none. The platform fixes defaults and a maximum; a registration may set its own limits up to the maximum.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace

from aduku.errors import SchemaError

__all__ = ["DEFAULTS", "Limits", "MAXIMUM"]


@dataclass(frozen=True)
class Limits:
    steps: int
    expression_chars: int
    depth: int
    value_bytes: int  # canonical bytes of the values an evaluation builds, summed
    input_bytes: int  # canonical bytes of the inputs, summed

    @classmethod
    def for_macro(cls, **wanted: int) -> Limits:
        """The limits of one macro: the defaults, with any given value set (above zero and not over the maximum)."""
        names = {f.name for f in fields(cls)}
        for name, value in wanted.items():
            if name not in names:
                raise SchemaError(f"unknown macro limit {name!r}; the limits are {sorted(names)}")
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SchemaError(f"macro limit {name} must be a positive whole number, not {value!r}")
            if value > getattr(MAXIMUM, name):
                raise SchemaError(f"macro limit {name}={value} is above the platform maximum {getattr(MAXIMUM, name)}")
        return replace(DEFAULTS, **wanted)


DEFAULTS = Limits(steps=10_000, expression_chars=2_000, depth=32, value_bytes=64 * 1024, input_bytes=256 * 1024)
MAXIMUM = Limits(steps=100_000, expression_chars=10_000, depth=64, value_bytes=1024 * 1024, input_bytes=256 * 1024)
