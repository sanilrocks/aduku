# /speckit.specify prompts, one per feature, in this order

Each prompt describes WHAT and WHY only. Read the cited sections of `docs/SPEC.md` for exact rules.

---

## Feature 1: Core ledger and merge engine (build first)

Build the core of aduku (Document Oriented Design): a ledger of contributions that is deterministically merged into a
document. Read SPEC.md sections 3, 4, 5, 6, 7 (P0-P3), 8 and 16.1.

**Why:** Many uncoordinated writers (a backfill job, end users, admins) change the same document. We need a
current document whose value for every field is predictable, explainable and independent of write races.

**User stories (priority order):**
- P1: As a writer, I submit a request of one or more entries (create/update/delete at an x-path, with my tier);
  it is accepted or rejected as a whole, and accepted entries are durably recorded with arrival sequence numbers.
  A rejected request fails with an error listing every reason and records nothing.
- P1: As a reader, I fetch a document that is the deterministic merge of all accepted entries, where higher tiers
  win conflicts unless the field's merge rule says otherwise.
- P1: As a schema author, I declare per-field merge rules (last-write-wins, max, union, struct field-wise) in a
  central schema.
- P2: As a schema author, I declare default values that any contribution can replace.
- P2: As an operator, I delete a field with a tombstone annotation and a later write re-creates it.
- P2: As a schema author, I choose per document type whether rejected requests are kept in a separate audit-only
  log, with count and/or time retention caps (default off).
- P2: As a backfill operator permitted by the schema, I send a request with a non-rejection flag so data that breaks
  validation rules is still recorded; violations become model errors returned with the document. Later requests
  to a document with model errors are accepted only if they add no new errors. A schema or rule version change
  re-derives the document and recomputes model errors on the next read.
- P3: As an auditor, I ask why a field has its value and get the winning entry and rule.

**Acceptance (use the worked example in SPEC.md 16.1):** the 14 entries produce exactly the layers and merged
document listed there before computed/deferred fields. Squashing each tier then merging equals one flat fold
ordered by (tier, seq). Changing only wall-clock time does not change results.

**Requirements highlights:** typed Ion/JSON values with annotations; x-path addressing; immutable entries with
per-document seq; idempotency keys; atomic requests; rejected requests record nothing in the ledger (optional
capped audit log); validation and the no-new-errors rule evaluated once per request on the resulting document;
deferred values never feed validation; tombstone rules in SPEC 6.7 with per-field on_delete; merge behaviors
declare algebraic laws. See SPEC sections 4.5 and 10.

**Out of scope for this feature:** computed and deferred fields, a library of validation rule types, per-field
authority, snapshots, compaction, detailed schema evolution.

---

## Feature 2: Computed fields and non-writability

Add computed fields and enforce that contributions can never override them. Read SPEC 5.3, 9.1-9.4, 11 (derived
authority).

**Why:** Derived values must stay consistent with their inputs and be trustworthy regardless of who writes.

**Stories:** P1 declare a computed field with preset macros (upper, concat, coalesce, ...) over declared
dependencies; P1 entries targeting a computed path are rejected with FIELD_NOT_WRITABLE (not a ledger entry; kept only in the optional rejection audit log);
P2 register custom macros that run sandboxed and see only declared dependencies; P2 dependency cycles are
rejected when the schema loads; P3 define behavior when a dependency is missing; P2 impure computes (SPEC 9.3.1): a computed field whose hook does IO, with
timeout/retry/deadline, recorded outcomes in an effect log, `failure_mode: reject_on_failure` (default) | `model_error_on_failure`, set per field or per document type, `recompute`, and
rebuild that makes no calls.

**Acceptance:** `displayName = upper(name)` and `label = macro(name, address.city)` appear after merge with the
`computed::` annotation; the rejected "HACKED" entry in SPEC 16.1 never changes the document.

---

## Feature 3: Deferred computed fields

*Done: specified, planned and implemented in `specs/003-deferred-fields/` (2026-10-19).*

Add fields populated through IO hooks with refresh policies. Read SPEC 9.5, 13 (hooks).

**Why:** Some values need external calls (scores, rates). They must not make the core merge impure or slow.

**Stories:** P1 `once` fields are computed before first fetch and persisted; P1 `per_fetch` fields are computed on
read with ttl and fallback; P2 `scheduled` fields refresh in batches across documents; P2 `notification` fields
recompute when a topic event arrives; P1 a deferred field may depend on plain, computed and other deferred fields and its
hook runs only once its deferred inputs have values (promise); P2 hook failures follow the per-field `on_failure`
(error/serve_stale/absent) after retries and a circuit breaker, are reported as deferred status, and never reject a write
or create a model error; P2 computed fields, validators and defaults cannot depend on deferred fields (refused at load);
P3 pending state holds dependent deferred fields back.

[NEEDS CLARIFICATION] Should `once` fields recompute when dependencies change? (default: no, opt-in)

---

## Feature 4: Validation and authority

*Done: specified, planned and implemented in `specs/004-validation-authority/` (2026-10-20).*

Add contribution-level and document-level validation, and per-field authority. Read SPEC 10, 11.

**Why:** Reject bad or unauthorized contributions early, and detect when valid contributions yield an invalid
document, without silently fixing it.

**Stories:** P1 type and constraint validators run at admission; P1 per-field authority limits who may write; P2
cross-field document validators run after merge; violations become rejections or model errors as defined in Feature 1
(non-rejection flag, no-new-errors rule); P3 validators on computed values (never deferred values).

---

## Feature 5: Storage, snapshots, compaction and time travel

*Done: specified, planned and implemented in `specs/005-snapshots-compaction-time-travel/` (2026-10-20).*

Make reads fast and history manageable. Read SPEC 12.2, 15.

**Why:** Replaying the entire ledger on every read does not scale, but results must stay identical.

**Stories:** P1 per-tier snapshots with watermarks and incremental invalidation; P1 `rebuild` reproduces
snapshots exactly; P2 `as_of` reads by seq, tx_time or valid_time; P2 compaction preserves merged result and
provenance; P3 auditable purge for legal erasure.

---

## Feature 6: Schema versioning and evolution

*Done: specified, planned and implemented in `specs/006-schema-evolution/` (2026-10-20). Per-field precedence and multi-region seq were decided out of v1 (SPEC 17 items 2 and 6).*

Read SPEC 14. **Why:** Schemas change; merged results must remain reproducible and breaking changes safe.

**Stories:** P1 immutable versions and activation; P1 breaking-change classification with dry-run diff and
rebuild; P2 per-document schema pinning; P2 shadowing of entries made obsolete by a schema change.

[NEEDS CLARIFICATION] Per-field precedence (SPEC 17 item 2) and multi-region seq assignment (item 6) before
Feature 1 is finalized if either is needed in v1.
