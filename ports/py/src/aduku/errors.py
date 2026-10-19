"""Error codes (SPEC 12.5) and exception types."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    FIELD_NOT_WRITABLE = "FIELD_NOT_WRITABLE"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    TIER_NOT_PERMITTED = "TIER_NOT_PERMITTED"
    UNKNOWN_PATH = "UNKNOWN_PATH"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    OP_NOT_ALLOWED = "OP_NOT_ALLOWED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    MODEL_ERROR_INTRODUCED = "MODEL_ERROR_INTRODUCED"
    COMPUTE_FAULT = "COMPUTE_FAULT"
    IMPURE_COMPUTE_FAILED = "IMPURE_COMPUTE_FAILED"
    LENIENT_NOT_PERMITTED = "LENIENT_NOT_PERMITTED"
    DUPLICATE_IDEM_KEY = "DUPLICATE_IDEM_KEY"
    REQUEST_KEY_REUSED = "REQUEST_KEY_REUSED"
    IDEM_KEY_REQUIRED = "IDEM_KEY_REQUIRED"
    SEQ_CONFLICT = "SEQ_CONFLICT"
    SCHEMA_INACTIVE = "SCHEMA_INACTIVE"
    STORAGE_ERROR = "STORAGE_ERROR"


@dataclass(frozen=True)
class Reason:
    """One reason a request was rejected."""

    code: ErrorCode
    message: str
    entry_index: int | None = None
    rule: str | None = None
    path: str | None = None


class AdukuError(Exception):
    """Base class for aduku errors."""


class SchemaError(AdukuError):
    """A schema (or the registry) is invalid; `problems` lists every problem found."""

    def __init__(self, problems: list[str] | tuple[str, ...] | str) -> None:
        self.problems: tuple[str, ...] = (problems,) if isinstance(problems, str) else tuple(problems)
        super().__init__("; ".join(self.problems))

    @property
    def codes(self) -> tuple[str, ...]:
        """Stable problem codes (the `CODE:` prefix of a problem), which other ports and shared vectors rely on."""
        found = (re.match(r"([A-Z][A-Z_]+): ", problem) for problem in self.problems)
        return tuple(match.group(1) for match in found if match)


class RejectedError(AdukuError):
    """A request was rejected. Nothing was recorded in the ledger."""

    def __init__(
        self, code: ErrorCode, reasons: list[Reason] | tuple[Reason, ...] | None = None, message: str = ""
    ) -> None:
        self.code = code
        self.reasons: tuple[Reason, ...] = tuple(reasons) if reasons else (Reason(code, message or code.value),)
        super().__init__(message or "; ".join(f"{r.code.value}: {r.message}" for r in self.reasons))


class StorageError(AdukuError):
    """Storage failed. Not a rejection: no entry of the request is visible."""

    code = ErrorCode.STORAGE_ERROR


class ComputeFault(AdukuError):
    """A pure macro errored, exceeded a limit or returned the wrong type: an engine fault, never a data problem.

    The message names the macro and the reason (the limit's name, never a value). At admission it becomes a rejection
    with `ErrorCode.COMPUTE_FAULT`; on a read it propagates.
    """

    code = ErrorCode.COMPUTE_FAULT

    def __init__(self, macro: str, reason: str) -> None:
        self.macro = macro
        self.reason = reason
        super().__init__(f"macro {macro} faulted: {reason}")
