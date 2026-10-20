# Research: Schema Versioning and Evolution

Spec clarifications (2026-10-20) are taken as given: the shared store holds the active version and the pins; a tightened type shadows
(never refuses); any operator may act with a recorded name; an unlimited dry run examines every document; an activation stands when
its eager rebuild finds a document it cannot derive.

What the code does today, which these decisions build on: `SchemaRegistry` (in `schema/registry.py`) holds immutable versions (`put`
refuses changed content under a number, within one process), an in-memory active version and pins; `activate` and `pin` check that
the version keeps every tier the ledger uses and (Feature 5) that it can serve compacted history; the service asks
`version_for(doc)` once per operation; `fold_entries` skips entries it cannot use (unknown tier, a field that became derived, an
unmergeable value, an invalid path) and lists their seqs; a read under another version re-derives (views and layers carry the
version and `rules_fingerprint`). Applications build the registry before the service (`registry.put(...)`, `registry.activate(...)`,
then `Aduku(storage, registry)`), and many tests rely on that order.

## R-1. Shared schema state behind the registry

- **Decision**: four tables in the ledger's storage: `schema_versions(schema_id, version, fingerprint, parts, first_served)`,
  `schema_state(schema_id, active_version, generation)`, `doc_pins(schema_id, doc, version, since)` and an insert-only
  `schema_history`. The registry gets an optional `SchemaState` interface (defined in `schema/registry.py`, implemented by
  `schema_state.py` over a `Storage`). `Aduku.__init__` binds its registry: from then on `active()`, `version_for(doc)` and `get`
  see the stored state, and `activate`, `pin` and `unpin` write through it, each as one transaction with its history record and a
  bump of `generation`. A registry that is never bound behaves as today.
- **Reading cost**: `version_for` first reads the single `generation` row; only if it changed since this process last looked does it
  reload the active version and all pins (one query each). A request therefore adds one indexed single-row read.
- **Order of construction**: tests and applications activate on the registry before building the service. At bind, if the store holds
  no state the registry's own active version and pins are adopted into it (a bootstrap record in the history, operator
  `"bootstrap"`); if the store holds state it wins, and a registry whose local active version differs is brought into line (logged).
  If the stored active version is not among the registry's versions, serving stops with `SCHEMA_INACTIVE` naming it (clarification 1).
- **Rationale**: one place decides which version serves, atomically with its record, for every process; no new concept for callers.
- **Gate result (T020, analysis A1)**: with the shared state on by default, the existing suite (Features 1 to 5) ran unchanged except
  for six tests whose *setup* built several services on one storage with different schema content under the same version
  number (the stored fingerprint rightly refuses that) or counted the storage reads of one request (the generation read is
  the designed extra cost, covered by `tests/integration/test_schema_state.py`). Each got `schema_state=False` on the service it
  builds second (or on the counted one), which is the pre-feature behavior; no assertion about a document changed and none was
  weakened: `test_computed_not_writable::test_6_auditing_on_keeps_exactly_one_record_off_keeps_none`,
  `test_deferred_failures::test_a_source_that_is_down_never_changes_what_a_write_does`,
  `test_deferred_promises::test_a_once_dependent_keeps_its_value_when_the_upstream_refreshes_unless_it_opted_in`,
  `test_impure_failures::test_7_the_field_setting_overrides_the_schema_setting`,
  `test_rejection_audit::test_per_document_override`, `test_resilience::test_an_append_is_one_write_transaction_and_carries_its_view`.
  The test helpers `make_service` and `deferred_helpers.build` pass the option through.
- **Alternatives considered**: a separate admin object that owns the state (every existing call site would change); keeping state in
  the registry and syncing it (two sources of truth).

## R-2. Fingerprints of served versions

- **Decision**: `Schema.content_fingerprint` is the SHA-256 of the full canonical description of a version (the same traversal as
  `rules_fingerprint`, which is only 16 hex digits and covers the same content, but the full digest is kept for identity). `parts`
  is a canonical struct `{top: digest, fields: {pattern: digest}}` so that a mismatch can name which fields (and whether the
  top-level settings) differ. A version is **served** from its first activation or pin (and by the bootstrap): that moment records
  its fingerprint and parts. `put` and bind compare any version they hold against the recorded ones and raise
  `SchemaError("SCHEMA_VERSION_CHANGED: ...")` naming up to five differing fields. A version only registered, never activated or
  pinned, stays editable.
- **Rationale**: Principle IX across restarts without keeping the schema text (spec assumption); field-level names are what an
  author needs to find the edit.
- **Alternatives considered**: storing the schema text (contradicts the assumption, grows the store); a single digest (cannot name
  the difference).

## R-3. The classification table

- **Decision**: `schema/diff.py` compares attribute by attribute and maps each to a class, with one table that is also written in
  `contracts/changes.md` and added to docs/SPEC.md 14:

  | Difference | Class |
  |---|---|
  | field added (plain or derived) | compatible |
  | field removed | visible (its recorded values still show, merged by last-write-wins; new writes are refused) |
  | `default` added, changed or removed; `on_delete` changed | visible |
  | `merge` behavior or its version changed | breaking |
  | `type` changed | breaking |
  | `validate` rule added, removed, or its arguments or version changed | visible |
  | `authority` list added, changed or removed; `ops` changed; `redact` changed | visible |
  | field becomes derived (computed or deferred) | breaking; derived becomes plain: breaking |
  | computed macro, arguments or `on_missing` changed | visible |
  | impure hook, version, `depends`, timeout or retry changed; `failure_mode` | visible |
  | deferred hook, `depends`, `refresh`, `ttl`, `on_failure` or `invalidate_by` changed | visible |
  | tier added at the top | compatible |
  | tier inserted, reordered, renamed or dropped | breaking |
  | `roles` added | compatible; removed: visible |
  | `lenient`, `purge_by`, `impure_failure_mode`, `deferred_on_failure` changed | visible |
  | `rejection_audit` changed | compatible |
  | `document_validate` rule added, removed or changed | visible |
  | any other difference | visible, `not classified` |

  The overall class is the worst. The list is ordered by (path, kind) so it is the same each time. "Constraint" in the spec's
  "tighten a type or constraint" means the type expression (for example `list<string>`); a validation rule is a model error, not a
  shadow, and is classed visible (10.4).
- **Rationale**: the SPEC 14 table decides the existing rows. A removed field is not in it; the code keeps folding entries at an
  undeclared path (the document still shows them, merged as last-write-wins), so removing a field changes governance, not the
  document, and is visible. Making such entries disappear would change results of Features 1 to 5 (FR-021), so it is not done.
- **Alternatives considered**: classing unknown differences as breaking (safe but noisy; the spec chose visible plus a mark).

## R-4. Why an entry is skipped

- **Decision**: `fold_entries` keeps producing the skipped seqs; `engine/shadow.py` gives each a reason, by re-running the same checks
  in the same order for those seqs only: `tier_not_in_schema`, `now_derived` (with the field's kind), `no_longer_conforms` (with the
  type), `cannot_be_merged` (the rule). The one new fold rule: an entry whose `schema_version` differs from the version in force and
  whose value does not conform to the field's type under it is skipped. Entries admitted under the version in force always conform
  (admission checked), so the check is skipped for them, and no ledger that never changed a type is affected. A struct written to
  a field-wise field is checked child by child, like admission does.
- **Reasons are not stored**: `DocumentResult.shadowed` is computed with the document (`Shadowed(seq, id, tier, path, reason,
  detail)`); `skipped` (the seqs) is unchanged. `explain` lists the shadowed entries that touch the path.
- **Rationale**: derived on demand, they cannot disagree with ledger and version (FR-019).

## R-5. The dry run

- **Decision**: `plan_schema(target, *, docs=None, limit=None)` returns a `DryRunReport`: the change list from the active version to
  the target, and for each examined document a `DocumentChange` (document text differs, model errors added and removed by identity,
  skipped before and after, would call a hook, could not be derived and why), counts, the documents left out (`limit`, or pinned
  documents which the activation would not touch) and the generation it compared against. For each document it derives under the
  current version and under the target with the engine's `replay` over the same entries and the recorded effect outcomes, without
  storing a view or layer. A document whose derivation under the target has pending impure calls is marked `would_call_hook` and its
  document is compared as derived (the field absent). A document that raises (a validator faults, compacted history that cannot be
  folded: `HISTORY_COMPACTED`) is marked failed with the code. A limit picks documents in order of `sha256(doc)`, so the same limit
  gives the same documents. The deferred values are not part of the comparison (they live in the derived store, outside P1 to P4, P6).
- **Cost**: one replay per examined document and version; linear in the ledgers examined.
- **Rationale**: the same code path as a read, so the prediction equals the read (SC-002).

## R-6. Activation

- **Decision**: `activate_schema(version, *, operator, acknowledge_breaking=False, rebuild=False, expect_generation=None)`:
  computes the change list from the current active version, refuses a breaking one without the acknowledgement
  (`BREAKING_CHANGE_NOT_ACKNOWLEDGED`, naming the first breaking changes), runs the registry's checks (tiers, compacted history),
  and writes the new active version and the history record in one transaction (a given `expect_generation` that no longer matches is
  `SCHEMA_STATE_CHANGED`). With `rebuild`, it then runs `rebuild()` over every document not pinned and returns the report; the
  activation stands whatever it finds (clarification 5). A rollback is `activate_schema(older)`; it is recorded with action
  `rollback` when the version is lower than the one it replaces. The low-level `registry.activate(version)` stays (tests and
  applications use it): it records the same history and class but takes no acknowledgement (documented: the safety rail is the
  service operation and the CLI).
- **Operator name**: required by the service operations and the CLI; the low-level calls record `"unspecified"`.

## R-7. Pinning and migration

- **Decision**: `pin_document(doc, version, *, operator)` and `unpin_document` write through the state store (one transaction each,
  with history). `plan_migration(doc, to=None)` is the dry run for one document. `migrate(docs=None, *, to=None, operator,
  batch=100)`: for each document (all pinned ones if none are named): skip and report `not_pinned` for an unpinned one (FR-017),
  derive under the target and, if it cannot, report `failed` and leave it; else write the new pin (or unpin if the target is the
  active version) and the history record in one transaction. A second run finds the moved ones no longer pinned to the old
  version and does not touch them (`already_moved`). Progress is returned per batch through an optional callback.
- **Rationale**: per-document transactions make a stopped batch safe by construction.

## R-8. Atomicity of a request and an activation

- **Decision**: `append` reads `version_for(doc)` once at its start and uses that schema object for admission and for the version it
  records on every entry; an activation in between affects the next request. (Today's code already works this way; this feature adds a
  test and the guarantee to the contract.)

## R-9. Interfaces

- **Decision**: library: `db.schema_changes(old, new)` (no store), `db.plan_schema(target, ...)`, `db.activate_schema(...)`,
  `db.pin_document`, `db.unpin_document`, `db.plan_migration`, `db.migrate`, `db.schema_history(limit)`. CLI: `schema diff OLD NEW`
  (two files, no store), `schema plan --target V [--limit N] [--doc D ...]`, `schema activate V --operator NAME
  [--acknowledge-breaking] [--rebuild]`, `schema history [--limit N]`, `pin DOC V --operator NAME`, `unpin DOC --operator NAME`,
  `migrate [DOC ...] [--to V] --operator NAME [--dry-run] [--batch N]`; `--schema` may be given several times or name a directory so a
  process holds every version it may need.

## R-10. Conformance

- **Decision**: new steps `diff` (two schemas of the case, expecting the change list), `plan` (the dry run), `activate_schema` with
  `operator`, `acknowledge_breaking`, `rebuild` and `refused`, `pin`, `unpin`, `migrate`, and the expectation `shadowed` on `get`;
  vectors in `contracts/conformance-vectors.md`. The runner already restarts a service on the same storage, which covers the shared
  state.
