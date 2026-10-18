"""Model errors and who introduced them (SPEC 10, research R-7). Pure.

A model error is a rule violation of a document, identified by (rule, path scope). It is attributed to the request after which
it first appeared, or, when it appeared only because the schema version changed, to that version. An error keeps its
attribution until it disappears; if it comes back it is a new error.

Errors are carried forward in the derivation state, so an append evaluates one request. A document derived from scratch is
replayed request by request, each under the schema version recorded on it, so the same ledger and schema versions always
give the same attribution.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import (
    DerivationState,
    Prepared,
    build_document,
    fold_entries,
    merge_tiers,
)
from aduku.engine.validate import validate_document
from aduku.ion_codec import FrozenStruct
from aduku.model import Entry, ModelError, Violation
from aduku.paths import Path, format_path
from aduku.schema.model import Schema

__all__ = [
    "Extension",
    "Origin",
    "Replay",
    "advance",
    "catch_up",
    "extend",
    "has_validators",
    "render",
    "replay",
]


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

    @property
    def errors(self) -> tuple[ModelError, ...]:
        return self.state.errors

    @property
    def conflicts(self) -> tuple[str, ...]:
        return self.state.conflicts


def has_validators(schema: Schema) -> bool:
    return bool(schema.document_validate) or any(field.validate for field in schema.fields)


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
    schema: Schema, behaviors: BehaviorRegistry, prepared: Prepared, state: DerivationState
) -> tuple[FrozenStruct, list[Violation], tuple[str, ...]]:
    clashes: list[Path] = []
    merged = merge_tiers(schema, behaviors, state, prepared, clashes)
    document = build_document(schema, behaviors, merged, prepared, clashes)
    violations = validate_document(schema, behaviors, document) if has_validators(schema) else []
    return document, violations, tuple(sorted({format_path(path) for path in clashes}))


def extend(
    schema: Schema,
    behaviors: BehaviorRegistry,
    prepared: Prepared,
    state: DerivationState | None,
    entries: Sequence[Entry],
    origin: Origin,
    previous: Sequence[ModelError] = (),
) -> Extension:
    """Fold `entries` (normally one request) into `state`, build the document, validate it and update the errors."""
    folded = fold_entries(schema, behaviors, state, entries, prepared)
    document, violations, conflicts = _evaluate(schema, behaviors, prepared, folded)
    errors = update_errors(previous, violations, origin)
    before = {error.identity for error in previous}
    after = {error.identity for error in errors}
    return Extension(
        replace(folded, errors=errors, conflicts=conflicts),
        document,
        tuple(error for error in errors if error.identity not in before),
        tuple(error for error in previous if error.identity not in after),
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


def render(schema: Schema, behaviors: BehaviorRegistry, prepared: Prepared, state: DerivationState) -> FrozenStruct:
    """The document a state stands for."""
    return _evaluate(schema, behaviors, prepared, state)[0]


def advance(
    schema: Schema, behaviors: BehaviorRegistry, prepared: Prepared, state: DerivationState, entries: Sequence[Entry]
) -> Extension:
    """Catch a state up with entries accepted since (possibly by another process), one request at a time."""
    if not entries:
        return Extension(state, render(schema, behaviors, prepared, state), (), ())
    if not has_validators(schema):
        return extend(schema, behaviors, prepared, state, entries, Origin())
    ext: Extension | None = None
    for group in group_requests(entries):
        ext = extend(schema, behaviors, prepared, state, group, Origin(request_id=group[0].request_id), state.errors)
        state = ext.state
    assert ext is not None
    return ext


def catch_up(
    schema: Schema, behaviors: BehaviorRegistry, prepared: Prepared, state: DerivationState, entries: Sequence[Entry]
) -> DerivationState:
    """Like `advance`, but builds nothing when there is nothing to catch up with."""
    return state if not entries else advance(schema, behaviors, prepared, state, entries).state


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
) -> Replay:
    """Derive from scratch: the document under `read_schema`, and each error attributed as the ledger dictates.

    Requests are walked in order, each under the schema version recorded on it. When the version changes, errors that the
    new version finds in the document as it stood are attributed to that version; errors that appear only because the version
    read under differs from the last request's are attributed to the version read under. `capture` names a request whose
    errors before and after are returned too.
    """
    ordered = sorted(entries, key=lambda e: e.seq)
    read = prepare_for(read_schema)
    if not has_validators(read_schema):  # nothing can be an error under this version, whatever the history was
        return Replay(extend(read_schema, behaviors, read, None, ordered, Origin(schema_version=read_schema.version)))

    errors: tuple[ModelError, ...] = ()
    state: DerivationState | None = None
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
                _, violations, _ = _evaluate(schema, behaviors, prepared, refolded)
                errors = update_errors(errors, violations, Origin(schema_version=version))
                state = replace(refolded, errors=errors)
            current = version
        if capture == group[0].request_id:
            before = errors
        last = extend(schema, behaviors, prepared, state, group, Origin(request_id=group[0].request_id), errors)
        state, errors = last.state, last.errors
        if capture == group[0].request_id:
            after = errors
        done.extend(group)

    if last is not None and current == read_schema.version:
        return Replay(last, before, after)
    final = extend(read_schema, behaviors, read, None, ordered, Origin(schema_version=read_schema.version), errors)
    return Replay(final, before, after)
