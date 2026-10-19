"""The preset macros (SPEC 9.3, feature 002 research R-4). Pure functions of their arguments.

Each preset has an arity, an optional fixed result type, and a flag `accepts_missing`: only `coalesce`, `if`, `eq` and
`join` run when an input has no value (it is passed as None); the engine skips every other macro in that case. A wrong
argument type is a `PresetError`, never a coerced value.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from typing import Any

from aduku.ion_codec import FrozenStruct, IonError, Timestamp, to_canonical, to_utc

__all__ = ["DECIMAL_PRECISION", "PRESETS", "Preset", "PresetError"]

DECIMAL_PRECISION = 38  # significant digits of every decimal operation, here and in the expression evaluator


class PresetError(ValueError):
    """A preset was given the wrong number or type of arguments. The message never repeats a value."""


@dataclass(frozen=True)
class Preset:
    name: str
    fewest_args: int
    most_args: int | None
    accepts_missing: bool
    result_type: str | None
    run: Callable[[Sequence[Any]], Any]

    def call(self, args: Sequence[Any]) -> Any:
        count = len(args)
        if count < self.fewest_args or (self.most_args is not None and count > self.most_args):
            wanted = f"{self.fewest_args}" if self.fewest_args == self.most_args else f"{self.fewest_args}+"
            raise PresetError(f"{self.name} takes {wanted} argument(s), got {count}")
        return self.run(args)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def _string(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise PresetError(f"{name} takes a string")
    return str(value)


def _text(name: str, value: Any) -> str:
    """The text of a string or scalar for concat and join."""
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | Decimal):
        return to_canonical(value)
    raise PresetError(f"{name} takes strings, whole numbers, decimals or booleans")


def _upper(args: Sequence[Any]) -> Any:
    return _string("upper", args[0]).upper()


def _lower(args: Sequence[Any]) -> Any:
    return _string("lower", args[0]).lower()


def _trim(args: Sequence[Any]) -> Any:
    return _string("trim", args[0]).strip()


def _concat(args: Sequence[Any]) -> Any:
    return "".join(_text("concat", arg) for arg in args)


def _coalesce(args: Sequence[Any]) -> Any:
    return next((arg for arg in args if arg is not None), None)


def _len(args: Sequence[Any]) -> Any:
    value = args[0]
    if isinstance(value, str | tuple | FrozenStruct):
        return len(value)
    raise PresetError("len takes a string, a list or a struct")


def _numbers(name: str, args: Sequence[Any], *, allow_empty: bool) -> list[Any]:
    items: list[Any] = []
    for arg in args:
        items.extend(arg if isinstance(arg, tuple) else [arg])
    if not items and not allow_empty:
        raise PresetError(f"{name} needs at least one value")
    return items


def _sum(args: Sequence[Any]) -> Any:
    items = _numbers("sum", args, allow_empty=True)
    if not all(_is_number(item) for item in items):
        raise PresetError("sum takes numbers")
    total: Any = 0
    try:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            for item in items:
                total = total + item
    except TypeError:
        raise PresetError("sum cannot mix decimals and floats") from None
    except DecimalException:
        raise PresetError("sum is out of the range of a decimal") from None
    return total


def _extreme(name: str, pick: Callable[..., Any], args: Sequence[Any]) -> Any:
    items = _numbers(name, args, allow_empty=False)
    if all(_is_number(item) for item in items) or all(isinstance(item, str) for item in items):
        return pick(items)
    raise PresetError(f"{name} takes numbers or strings, not a mixture")


def _min(args: Sequence[Any]) -> Any:
    return _extreme("min", min, args)


def _max(args: Sequence[Any]) -> Any:
    return _extreme("max", max, args)


def _round(args: Sequence[Any]) -> Any:
    value, digits = args
    if not _is_number(value):
        raise PresetError("round takes a number")
    if not isinstance(digits, int) or isinstance(digits, bool):
        raise PresetError("round takes a whole number of digits")
    if isinstance(value, Decimal):
        try:
            with localcontext() as context:
                context.prec = DECIMAL_PRECISION
                return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN)
        except DecimalException:  # more digits than the precision allows, or an exponent out of range
            raise PresetError("round cannot give that many digits") from None
    return round(value, digits)  # int stays int; a float is rounded half to even on its exact binary value


_FORMAT_TOKEN = re.compile(r"%(.)")


def _format_ts(args: Sequence[Any]) -> Any:
    stamp, pattern = args
    if not isinstance(stamp, Timestamp):
        raise PresetError("format_ts takes a timestamp")
    pattern = _string("format_ts", pattern)
    try:
        utc = to_utc(stamp)
    except IonError:
        raise PresetError("format_ts cannot format an instant outside the years 1 to 9999 in UTC") from None
    parts = {
        "Y": f"{utc.year:04d}",
        "m": f"{utc.month:02d}",
        "d": f"{utc.day:02d}",
        "H": f"{utc.hour:02d}",
        "M": f"{utc.minute:02d}",
        "S": f"{utc.second:02d}",
        "%": "%",
    }

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token not in parts:
            raise PresetError(f"format_ts does not know the token %{token}; it knows %Y %m %d %H %M %S %%")
        return parts[token]

    return _FORMAT_TOKEN.sub(replace, pattern)


def _hash(args: Sequence[Any]) -> Any:
    try:
        text = to_canonical(args[0])
    except (TypeError, IonError):
        raise PresetError("hash takes a value with a canonical Ion form") from None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _join(args: Sequence[Any]) -> Any:
    items, separator = args
    separator = _string("join", separator)
    if items is None:
        return ""
    if not isinstance(items, tuple):
        raise PresetError("join takes a list")
    return separator.join(_text("join", item) for item in items)


def _if(args: Sequence[Any]) -> Any:
    condition, then, otherwise = args
    if condition is not None and not isinstance(condition, bool):
        raise PresetError("if takes a boolean condition")
    return then if condition else otherwise


def _eq(args: Sequence[Any]) -> Any:
    left, right = args
    if _is_number(left) and _is_number(right):
        return bool(left == right)
    if left is None or right is None:
        return left is None and right is None
    if _is_number(left) or _is_number(right):
        return False
    try:
        return to_canonical(left) == to_canonical(right)
    except (TypeError, IonError):
        raise PresetError("eq takes values with a canonical Ion form") from None


def _preset(
    name: str, fewest: int, most: int | None, run: Callable[[Sequence[Any]], Any], *, missing: bool = False,
    result: str | None = None,
) -> Preset:  # fmt: skip
    return Preset(name, fewest, most, missing, result, run)


PRESETS: dict[str, Preset] = {
    p.name: p
    for p in (
        _preset("upper", 1, 1, _upper, result="string"),
        _preset("lower", 1, 1, _lower, result="string"),
        _preset("trim", 1, 1, _trim, result="string"),
        _preset("concat", 1, None, _concat, result="string"),
        _preset("coalesce", 1, None, _coalesce, missing=True),
        _preset("len", 1, 1, _len, result="int"),
        _preset("sum", 1, None, _sum),
        _preset("min", 1, None, _min),
        _preset("max", 1, None, _max),
        _preset("round", 2, 2, _round),
        _preset("format_ts", 2, 2, _format_ts, result="string"),
        _preset("hash", 1, 1, _hash, result="string"),
        _preset("join", 2, 2, _join, missing=True, result="string"),
        _preset("if", 3, 3, _if, missing=True),
        _preset("eq", 2, 2, _eq, missing=True, result="bool"),
    )
}
