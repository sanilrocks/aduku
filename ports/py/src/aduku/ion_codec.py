"""Ion <-> immutable internal values, and the canonical Ion text writer.

The normative canonical form is defined in specs/001-core-ledger-merge-engine/contracts/conformance-vectors.md
("Canonical Ion text"). Documents use these internal values:

    null -> None            bool, int, float, Decimal, str, Timestamp as-is
    symbol -> Symbol        list -> tuple          struct -> FrozenStruct
    annotated -> Annotated  deleted::null -> TOMBSTONE
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import amazon.ion.simpleion as ion
from amazon.ion.core import IonType, Timestamp, TimestampPrecision

__all__ = [
    "ABSENT",
    "MAX_DEPTH",
    "TOMBSTONE",
    "Timestamp",
    "Annotated",
    "FrozenStruct",
    "IonError",
    "Symbol",
    "Value",
    "normalize",
    "or_placeholder",
    "timestamp_representable",
    "timestamp_sort_key",
    "parse",
    "to_canonical",
]


MAX_DEPTH = 64  # deeper values are refused: they have no canonical form we can promise to write


class IonError(ValueError):
    """Text is not valid Ion, or a value has no canonical form."""


class Symbol(str):
    """An Ion symbol (kept distinct from a string)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"Symbol({str.__repr__(self)})"


class _Tombstone:
    """`deleted::null`: distinct from absent and from null."""

    _instance: _Tombstone | None = None

    def __new__(cls) -> _Tombstone:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "TOMBSTONE"

    def __copy__(self) -> _Tombstone:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _Tombstone:
        return self


TOMBSTONE = _Tombstone()


class _Absent:
    """No value at a path (distinct from null and from a tombstone). The left identity of every merge rule."""

    _instance: _Absent | None = None

    def __new__(cls) -> _Absent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"

    def __copy__(self) -> _Absent:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _Absent:
        return self


ABSENT = _Absent()


@dataclass(frozen=True)
class Annotated:
    annotations: tuple[str, ...]
    value: Any


class FrozenStruct(Mapping[str, Any]):
    """An immutable Ion struct (unordered; equality ignores insertion order)."""

    __slots__ = ("_data", "_hash")

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(data or {})
        self._hash: int | None = None

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FrozenStruct) and self._data == other._data

    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash(frozenset((key, _hashable(value)) for key, value in self._data.items()))
        return self._hash

    def __repr__(self) -> str:
        return f"FrozenStruct({self._data!r})"

    def with_field(self, key: str, value: Any) -> FrozenStruct:
        return FrozenStruct({**self._data, key: value})


def _hashable(value: Any) -> Any:
    return value if not isinstance(value, list) else tuple(value)


Value = Any  # None | bool | int | float | Decimal | str | Symbol | Timestamp | tuple | FrozenStruct | Annotated | _Tombstone


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def parse(text: str) -> Value:
    """Parse one Ion value from text into an internal value."""
    try:
        raw = ion.loads(text)
        return _convert(raw)
    except RecursionError as error:
        raise IonError("invalid Ion text: nested too deeply") from error
    except IonError:
        raise
    except Exception as error:  # the library raises several error types
        raise IonError(f"invalid Ion text: {error}") from error


def _convert(raw: Any) -> Value:
    annotations = tuple(token.text for token in getattr(raw, "ion_annotations", ()) if token.text is not None)
    value = _convert_plain(raw)
    if annotations == ("deleted",) and value is None:
        return TOMBSTONE
    if annotations:
        return Annotated(annotations, value)
    return value


def _convert_plain(raw: Any) -> Value:
    ion_type = getattr(raw, "ion_type", None)
    if ion_type is IonType.NULL or raw is None:
        return None
    if getattr(raw, "ion_type", None) is not None and type(raw).__name__ == "IonPyNull":
        return None
    if ion_type is IonType.BOOL:
        return bool(raw)
    if ion_type is IonType.INT:
        return int(raw)
    if ion_type is IonType.FLOAT:
        return float(raw)
    if ion_type is IonType.DECIMAL:
        return Decimal(raw)
    if ion_type is IonType.TIMESTAMP:
        return raw if type(raw) is Timestamp else _to_timestamp(raw)
    if ion_type is IonType.SYMBOL:
        return Symbol(raw.text if hasattr(raw, "text") else str(raw))
    if ion_type is IonType.STRING:
        return str(raw)
    if ion_type in (IonType.BLOB, IonType.CLOB):
        return bytes(raw)
    if ion_type in (IonType.LIST, IonType.SEXP):
        return tuple(_convert(item) for item in raw)
    if ion_type is IonType.STRUCT:
        fields: dict[str, Any] = {}
        for key, item in raw.iteritems():
            if key in fields:
                raise IonError(f"duplicate struct field {key!r}")
            fields[str(key)] = _convert(item)
        return FrozenStruct(fields)
    raise IonError(f"unsupported Ion value {raw!r}")


def _to_timestamp(raw: Any) -> Timestamp:
    return Timestamp(
        raw.year,
        raw.month,
        raw.day,
        raw.hour,
        raw.minute,
        raw.second,
        raw.microsecond,
        raw.tzinfo,
        precision=getattr(raw, "precision", TimestampPrecision.SECOND),
        fractional_precision=getattr(raw, "fractional_precision", None),
        fractional_seconds=getattr(raw, "fractional_seconds", None),
    )


# ---------------------------------------------------------------------------
# canonical text
# ---------------------------------------------------------------------------

_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\Z")
_SYMBOL_ID = re.compile(r"\$[0-9]+\Z")  # `$10` is not a name in Ion text: it means symbol id 10
_RESERVED = frozenset({"null", "true", "false", "nan"})
_SHORT_ESCAPES = {
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
    "\0": "\\0",
    '"': '\\"',
    "\\": "\\\\",
}


def to_canonical(value: Value) -> str:
    """Render a value as canonical Ion text (compact, struct fields sorted by code point)."""
    return _write(value)


def _to_utc(stamp: Timestamp) -> datetime:
    """The naive UTC time of a timestamp; an instant before year 1 or after year 9999 in UTC is an IonError."""
    try:
        utc: datetime = stamp.replace(tzinfo=None) - (stamp.utcoffset() or timedelta(0))
        return utc
    except OverflowError as error:
        raise IonError("the timestamp is outside the years 1 to 9999 in UTC") from error


def timestamp_representable(stamp: Timestamp) -> bool:
    """True if the timestamp can be written (and ordered) in UTC."""
    try:
        _to_utc(stamp)
    except IonError:
        return False
    return True


def timestamp_sort_key(stamp: Timestamp) -> tuple[int, int, int, int, int, int, Decimal]:
    """Orders timestamps by the instant they denote (offsets honoured; a missing offset counts as UTC).

    The fraction of a second keeps every digit, so instants closer than a microsecond still order correctly.
    """
    utc = _to_utc(stamp)
    fraction = getattr(stamp, "fractional_seconds", None)
    if fraction is None:  # a plain datetime: microseconds only
        fraction = Decimal(utc.microsecond) / Decimal(1_000_000)
    return (utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second, Decimal(fraction))


def normalize(value: Value) -> Value:
    """The value as it reads back from canonical text (offsets in UTC, typed nulls plain, and so on).

    Everything stored or returned goes through this, so that a value is the same whichever backend or cache holds it.
    """
    return parse(to_canonical(value))


def or_placeholder(value: Value) -> Value:
    """The value itself, or a short text naming its type when it has no canonical Ion form (blob, foreign object).

    For records that must not fail on bad input, such as the request hash and the rejection audit log.
    """
    try:
        to_canonical(value)
    except (TypeError, IonError, RecursionError):
        return f"<{type(value).__name__} with no Ion form>"
    return value


def _write(value: Value) -> str:
    if value is TOMBSTONE:
        return "deleted::null"
    if isinstance(value, Annotated):
        return "".join(_symbol(name) + "::" for name in value.annotations) + _write(value.value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _float(value)
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, Symbol):
        return _symbol(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, Timestamp):
        return _timestamp(value)
    if isinstance(value, tuple | list):
        return "[" + ",".join(_write(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ",".join(f"{_symbol(key)}:{_write(value[key])}" for key in sorted(value)) + "}"
    raise TypeError(f"no canonical Ion form for {type(value).__name__} (blobs and clobs are outside the vector set)")


def _symbol(text: str) -> str:
    if _IDENT.match(text) and text not in _RESERVED and not _SYMBOL_ID.match(text):
        return text
    return "'" + _escape(text).replace('\\"', '"').replace("'", "\\'") + "'"


def _escape(text: str) -> str:
    out: list[str] = []
    for char in text:
        if char in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(char)
    return "".join(out)


def _string(text: str) -> str:
    return '"' + _escape(text) + '"'


def _float(number: float) -> str:
    if number != number:
        return "nan"
    if number in (float("inf"), float("-inf")):
        return "+inf" if number > 0 else "-inf"
    mantissa, _, exponent = repr(number).lower().partition("e")
    if mantissa.endswith(".0"):
        mantissa = mantissa[:-2]
    return f"{mantissa}e{int(exponent) if exponent else 0}"


def _decimal(number: Decimal) -> str:
    if not number.is_finite():
        raise IonError("non-finite decimals have no canonical form")
    sign, digits, exponent = number.as_tuple()
    assert isinstance(exponent, int)
    text = "".join(str(d) for d in digits)
    prefix = "-" if sign else ""
    if exponent == 0:
        return f"{prefix}{text}."
    if exponent > 0:
        return f"{prefix}{text}d{exponent}"
    places = -exponent
    if len(text) <= places:
        return f"{prefix}0.{'0' * (places - len(text))}{text}"
    return f"{prefix}{text[:-places]}.{text[-places:]}"


def _timestamp(stamp: Timestamp) -> str:
    precision = getattr(stamp, "precision", TimestampPrecision.SECOND)
    if precision is TimestampPrecision.YEAR:
        return f"{stamp.year:04d}T"
    if precision is TimestampPrecision.MONTH:
        return f"{stamp.year:04d}-{stamp.month:02d}T"
    if precision is TimestampPrecision.DAY:
        return f"{stamp.year:04d}-{stamp.month:02d}-{stamp.day:02d}T"
    unknown = stamp.utcoffset() is None  # `-00:00`: the offset is not known, which is not the same as UTC
    utc = _to_utc(stamp) if stamp.utcoffset() else stamp
    zone = "-00:00" if unknown else "Z"
    base = f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T{utc.hour:02d}:{utc.minute:02d}"
    if precision is TimestampPrecision.MINUTE:
        return base + zone
    text = f"{base}:{utc.second:02d}"
    seconds = getattr(stamp, "fractional_seconds", None)
    if seconds is None:  # a plain datetime: microseconds only
        seconds = Decimal(stamp.microsecond) / Decimal(1_000_000)
    exponent = Decimal(seconds).as_tuple().exponent
    digits = max(getattr(stamp, "fractional_precision", 0) or 0, -exponent if isinstance(exponent, int) else 0)
    if digits:
        text += "." + f"{Decimal(seconds):.{digits}f}".split(".", 1)[1]  # every digit, however fine
    return text + zone
