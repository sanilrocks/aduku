"""Model errors and who introduced them (SPEC 10, research R-7). Pure.

A model error is a rule violation of a document, identified by (rule, path scope). It is attributed to the request after which
it first appeared, or, when it appeared only because the schema version changed, to that version. An error keeps its
attribution until it disappears; if it comes back it is a new error.

Errors are carried forward in the derivation state, so an append evaluates one request. A document derived from scratch is
replayed request by request, each under the schema version recorded on it, so the same ledger and schema versions always
give the same attribution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.compute import compute_fields
from aduku.engine.derive import (
    DerivationState,
    Prepared,
    build_document,
    fold_entries,
    merge_tiers,
)
from aduku.engine.effects import EffectView, PendingCall
from aduku.engine.validate import validate_document
from aduku.ion_codec import FrozenStruct
from aduku.model import Entry, ModelError, Violation
from aduku.paths import Path, format_path
from aduku.schema.model import Schema

__all__ = [
    "Base",
    "Extension",
    "Origin",
    "Replay",
    "advance",
    "can_have_errors",
    "catch_up",
    "extend",
    "has_validators",
    "render",
    "replay",
]


@dataclass(frozen=True)
class Base:
    """What a replay of requests had computed when a compaction replaced them (feature 005, research R-8): the model errors
    with who introduced each, and the seqs the fold could not use. A replay seeds them after the compaction's synthetic entries."""

    errors: tuple[ModelError, ...]
    skipped: tuple[int, ...] = ()


def _with_base(extension: Extension, base: Base) -> Extension:
    """`extension` (a state that has just folded a compaction's synthetic entries) with the base's attribution and skips."""
    known = {error.identity: error for error in base.errors}
    errors = tuple(
        known.get(error.identity, error) for error in extension.errors
    )  # an error the base knew keeps its origin
    skipped = tuple(sorted({*base.skipped, *extension.state.skipped}))
    return replace(extension, state=replace(extension.state, errors=errors, skipped=skipped))


@dataclass(frozen=True)
class Origin:
    """What a newly appearing error is attributed to: a request, or a schema version."""

    request_id: str | None = None
    schema_version: int | None = None


@dataclass(frozen=True)
class Extension:
    """A state with more entries folded in, the document it stands for, and what changed in its errors."""

    state: DerivationState
    document: FrozenStruct
    added: tuple[ModelError, ...]
    resolved: tuple[ModelError, ...]
    pending: tuple[PendingCall, ...] = ()  # hook calls the derivation still needs

    @property
    def errors(self) -> tuple[ModelError, ...]:
        return self.state.errors

    @property
    def conflicts(self) -> tuple[str, ...]:
        return self.state.conflicts


def has_validators(schema: Schema) -> bool:
    return bool(schema.document_validate) or any(field.validate for field in schema.fields)


def can_have_errors(schema: Schema) -> bool:
    """True if a document under this schema can have model errors: validators, or computed fields that report them."""
    return (
        has_validators(schema)
        or any(field.impure is not None for field in schema.fields)
        or any(field.derived == "computed" and field.on_missing == "error" for field in schema.fields)
    )


def update_errors(
    previous: Sequence[ModelError], violations: Sequence[Violation], origin: Origin
) -> tuple[ModelError, ...]:
    """The errors for `violations`: known ones keep their attribution, new ones get `origin`, fixed ones disappear."""
    known = {error.identity: error for error in previous}
    updated: list[ModelError] = []
    for violation in violations:
        old = known.get((violation.rule_id, violation.path_scope))
        updated.append(
            ModelError(
                violation.rule_id,
                violation.path_scope,
                violation.message,
                old.introduced_by_request if old else origin.request_id,
                old.introduced_by_schema_version if old else origin.schema_version,
            )
        )
    return tuple(sorted(updated, key=lambda error: error.identity))


def _evaluate(
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    state: DerivationState,
    effects: EffectView | None = None,
) -> tuple[FrozenStruct, list[Violation], tuple[str, ...], tuple[PendingCall, ...]]:
    clashes: list[Path] = []
    merged = merge_tiers(schema, behaviors, state, prepared, clashes)
    document = build_document(schema, behaviors, merged, prepared, clashes)
    violations: list[Violation] = []
    pending: tuple[PendingCall, ...] = ()
    if prepared.computed:  # P4, then validation of the whole result
        computed = compute_fields(schema, behaviors, document, prepared.computed, effects)
        document, violations, pending = computed.document, list(computed.violations), computed.pending
    if has_validators(schema):
        violations = [*violations, *validate_document(schema, behaviors, document)]
    return document, violations, tuple(sorted({format_path(path) for path in clashes})), pending


def extend(
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    state: DerivationState | None,
    entries: Sequence[Entry],
    origin: Origin,
    previous: Sequence[ModelError] = (),
    effects: EffectView | None = None,
) -> Extension:
    """Fold `entries` (normally one request) into `state`, build the document, validate it and update the errors."""
    folded = fold_entries(schema, behaviors, state, entries, prepared)
    document, violations, conflicts, pending = _evaluate(schema, behaviors, prepared, folded, effects)
    errors = update_errors(previous, violations, origin)
    before = {error.identity for error in previous}
    after = {error.identity for error in errors}
    return Extension(
        replace(folded, errors=errors, conflicts=conflicts, effects_seq=effects.seq if effects else 0),
        document,
        tuple(error for error in errors if error.identity not in before),
        tuple(error for error in previous if error.identity not in after),
        pending,
    )


def group_requests(entries: Sequence[Entry]) -> list[list[Entry]]:
    """Entries in seq order, split into the requests they were accepted in (their seqs are consecutive)."""
    groups: list[list[Entry]] = []
    for entry in sorted(entries, key=lambda e: e.seq):
        if groups and groups[-1][0].request_id == entry.request_id:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    return groups


def render(
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    state: DerivationState,
    effects: EffectView | None = None,
) -> tuple[FrozenStruct, tuple[PendingCall, ...]]:
    """The document a state stands for, and the hook calls it still needs."""
    document, _, _, pending = _evaluate(schema, behaviors, prepared, state, effects)
    return document, pending


def advance(
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    state: DerivationState,
    entries: Sequence[Entry],
    effects: EffectView | None = None,
) -> Extension:
    """Catch a state up with entries accepted since (possibly by another process), one request at a time."""
    if not entries:
        document, pending = render(schema, behaviors, prepared, state, effects)
        return Extension(state, document, (), (), pending)
    if not can_have_errors(schema):
        return extend(schema, behaviors, prepared, state, entries, Origin(), effects=effects)
    ext: Extension | None = None
    for group in group_requests(entries):
        ext = extend(
            schema, behaviors, prepared, state, group, Origin(request_id=group[0].request_id), state.errors, effects
        )
        state = ext.state
    assert ext is not None
    return ext


def catch_up(
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    state: DerivationState,
    entries: Sequence[Entry],
    effects: EffectView | None = None,
) -> DerivationState:
    """Like `advance`, but builds nothing when there is nothing to catch up with."""
    return state if not entries else advance(schema, behaviors, prepared, state, entries, effects).state


@dataclass(frozen=True)
class Replay:
    """The result of replaying a ledger: the final extension, and, if asked for, the errors around one request."""

    final: Extension
    before: tuple[ModelError, ...] = ()
    after: tuple[ModelError, ...] = ()


def replay(
    read_schema: Schema,
    schema_for: Callable[[int], Schema],
    behaviors: BehaviorRegistry,
    prepare_for: Callable[[Schema], Prepared],
    entries: Sequence[Entry],
    capture: str | None = None,
    effects: EffectView | None = None,
    bases: Mapping[str, Base] | None = None,
    trail: dict[str, tuple[tuple[ModelError, ...], tuple[ModelError, ...]]] | None = None,
) -> Replay:
    """Derive from scratch: the document under `read_schema`, and each error attributed as the ledger dictates.

    Requests are walked in order, each under the schema version recorded on it. When the version changes, errors that the
    new version finds in the document as it stood are attributed to that version; errors that appear only because the version
    read under differs from the last request's are attributed to the version read under. `capture` names a request whose
    errors before and after are returned too.
    """
    ordered = sorted(entries, key=lambda e: e.seq)
    read = prepare_for(read_schema)
    bases = bases or {}
    if not can_have_errors(read_schema):  # nothing can be an error under this version, whatever the history was
        only = extend(
            read_schema, behaviors, read, None, ordered, Origin(schema_version=read_schema.version), effects=effects
        )
        skipped = {
            seq for key, base in bases.items() if any(e.request_id == key for e in ordered) for seq in base.skipped
        }
        if skipped - set(only.state.skipped):
            only = replace(only, state=replace(only.state, skipped=tuple(sorted({*skipped, *only.state.skipped}))))
        return Replay(only)

    errors: tuple[ModelError, ...] = ()
    state: DerivationState | None = None
    carried: set[int] = set()
    done: list[Entry] = []
    current: int | None = None
    before: tuple[ModelError, ...] = ()
    after: tuple[ModelError, ...] = ()
    last: Extension | None = None
    for group in group_requests(ordered):
        version = group[0].schema_version
        schema = schema_for(version)
        prepared = prepare_for(schema)
        if version != current:
            if done:  # the rules changed under the document as it stood
                refolded = fold_entries(schema, behaviors, None, done, prepared)
                _, violations, _, _ = _evaluate(schema, behaviors, prepared, refolded, effects)
                errors = update_errors(errors, violations, Origin(schema_version=version))
                # the entries a compaction replaced are not here to be skipped again: their skips are carried over
                state = replace(refolded, errors=errors, skipped=tuple(sorted({*refolded.skipped, *carried})))
            current = version
        if capture == group[0].request_id:
            before = errors
        errors_before = errors
        last = extend(
            schema, behaviors, prepared, state, group, Origin(request_id=group[0].request_id), errors, effects
        )
        base = bases.get(group[0].request_id)
        if base is not None:
            # the synthetic entries of a compaction: take over what the requests it replaced had computed
            last = _with_base(last, base)
            carried.update(base.skipped)
        state, errors = last.state, last.errors
        if trail is not None:  # the errors each request found and left, for every request (feature 005)
            trail[group[0].request_id] = (errors_before, errors)
        if capture == group[0].request_id:
            after = errors
        done.extend(group)

    if last is not None and current == read_schema.version:
        return Replay(last, before, after)
    final = extend(
        read_schema, behaviors, read, None, ordered, Origin(schema_version=read_schema.version), errors, effects
    )
    if bases:  # the read schema differs from the last request's: keep the attribution and skips the bases carried
        known = {error.identity: error for base in bases.values() for error in base.errors}
        final = _with_base(
            final,
            Base(tuple(known.get(e.identity, e) for e in errors), tuple(s for b in bases.values() for s in b.skipped)),
        )
    return Replay(final, before, after)
