"""Recorded outcomes of impure computes, as the pure engine sees them (research R-9). Pure: no IO, no clock.

Derivation is a function of the ledger, the effect log and the schema version. The log is seen through an `EffectView`:
the outcome in force for each (field, fingerprint, hook). A fingerprint is a digest of the hook and the values it is
given, so a changed input, or a new hook version, is a new key and needs a new call.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from aduku.ion_codec import parse, to_canonical
from aduku.model import EffectRecord

__all__ = ["EffectView", "Outcome", "PendingCall", "fingerprint", "view_from_records"]


def fingerprint(hook: str, inputs: Sequence[tuple[str, Any]]) -> str:
    """SHA-256 of the canonical text of the hook (name@version) and its dependency values in declared order."""
    text = to_canonical((hook, tuple((path, value) for path, value in inputs)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Outcome:
    """One recorded outcome: the value, or the kind of failure."""

    kind: Literal["value", "failure"]
    value: Any = None
    error: str | None = None
    attempts: int = 1
    effect_seq: int | None = None


@dataclass(frozen=True)
class PendingCall:
    """An impure field whose current inputs have no recorded outcome: a hook call the derivation still needs."""

    field: str  # the field's path pattern
    hook: str  # "name@version"
    inputs: tuple[tuple[str, Any], ...]  # the dependency values, plain, in declared order
    fingerprint: str


@dataclass(frozen=True)
class EffectView:
    outcomes: Mapping[tuple[str, str, str], Outcome] = field(default_factory=dict)
    seq: int = 0  # the greatest effect_seq in the view: a state or view derived with it is stale once the log has more

    def lookup(self, field_path: str, fingerprint_: str, hook: str) -> Outcome | None:
        return self.outcomes.get((field_path, fingerprint_, hook))

    def with_outcomes(self, more: EffectView) -> EffectView:
        """A view holding these outcomes and those of `more` (which win on the same key). Neither is changed."""
        return EffectView({**self.outcomes, **more.outcomes}, max(self.seq, more.seq))


def view_from_records(records: Iterable[EffectRecord]) -> EffectView:
    """The view of effect-log records: for each key the one with the greatest `effect_seq`."""
    latest: dict[tuple[str, str, str], EffectRecord] = {}
    for record in records:
        key = (record.field, record.fingerprint, record.hook)
        kept = latest.get(key)
        if kept is None or (record.effect_seq or 0) >= (kept.effect_seq or 0):
            latest[key] = record
    seq = max((record.effect_seq or 0 for record in latest.values()), default=0)
    return EffectView({key: _outcome(record) for key, record in latest.items()}, seq)


def _outcome(record: EffectRecord) -> Outcome:
    if record.outcome == "value":
        return Outcome("value", parse(record.value or "null"), None, record.attempts, record.effect_seq)
    return Outcome("failure", None, record.error, record.attempts, record.effect_seq)
