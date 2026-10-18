# Feature Specification: Core Ledger and Merge Engine

**Feature Branch**: `001-core-ledger-merge-engine`

**Created**: 2026-10-18

**Status**: Draft

**Depends on**: docs/SPEC.md sections 3 to 8, 10, 16.1 and 16.2 (phases P0 to P3 and P6, minimal P7).

**Input**: User description: "Build the core of aduku (Document Oriented Design): a ledger of contributions that is deterministically merged into a document. Many uncoordinated writers (a backfill job, end users, admins) change the same document; we need a current document whose value for every field is predictable, explainable and independent of write races. Source: docs/SPEC.md sections 3, 4, 5, 6, 7 (P0-P3), 8 and 16.1."

## Clarifications

### Session 2026-10-18

- Q: When a field has a schema default and someone deletes it, should the merged document leave the field absent or fall back to the default? → A: It depends on a per-field schema setting, with absent as the default.
- Q: When a contribution is rejected, should the request fail or succeed with a rejected entry recorded? → A: The request fails with an error and nothing is recorded. Rejection reasons are schema errors (unknown field, wrong type) and validation-rule failures evaluated on the merged result. A request flag ("non-rejection") lets a writer record data that breaks validation rules; the violations are kept as model errors and surfaced with the document. Later contributions to a document with model errors are accepted only if they reduce the errors and add none. Deferred values never feed validation. A schema or rule version change re-derives the document and recomputes model errors on the next read.
- Q: When a document already has model errors, should a contribution that doesn't touch any of them be accepted or rejected? → A: Accepted if it adds no new model errors; a contribution that reduces errors is also accepted.
- Q: Who is allowed to send the non-rejection flag? → A: Only tiers or contributors the schema explicitly permits to use it (for example, backfill); a flagged request from anyone else is rejected.
- Q: Can rejected contributions optionally be kept for audit? → A: Yes. Rejection auditing is a setting per document type (schema) with an optional per-document override; default off. When on, rejected requests go to a separate rejection audit log that is never part of the ledger, merges or documents. The log is capped by a retention setting: a maximum number of rejected contributions, a maximum age, or both.
- Q: Should a multi-entry request be all-or-nothing? → A: Yes, always. A request is accepted or rejected as a whole; there is no partial acceptance, because partial acceptance makes audit and attribution of model errors hard (it would require ablation).
- Q: Does the "add no new errors" rule still apply to a permitted writer's flagged request on a document that already has model errors? → A: No. A flagged request skips that rule; any new violations are recorded as model errors.

### Session 2026-10-18 (follow-up)

- Q: How is a retried request recognized? → A: By a caller-supplied request key plus an identical request hash (payload and identity). A duplicate is not admitted and returns the original result with the latest materialized view, in the same shape as the original success. The same key with a different payload is rejected. Without a key every request is new, so a deliberate repeat is applied.
- Q: What does a rejection list when a request has both schema errors and validation-rule failures? → A: Only the schema errors; rule failures are reported once the schema errors are fixed.
- Q: Does the rejection audit log have a mode that avoids storing values? → A: Yes. A metadata-only mode keeps contributor, tier, time, paths, operations and reasons but never the attempted values.
- Q: Are tier names fixed? → A: No. The schema author defines tier names, count and order (presets may be offered for scaffolding); one tier order applies to every field in v1.
- Q: Can a document override who may use the non-rejection flag, and must a reason be given? → A: No override; the schema alone decides. A reason or ticket is optional in `meta`.
- Q: Is a model error that gets worse (a third spouse where two already break the limit) a new error? → A: Not in v1. Identity stays (rule, path scope); per-rule definitions of "worse" are future work.
- Q: What if writing a rejection audit record fails? → A: The rejection is still returned and the failure is logged; auditing is best effort and never changes a request's outcome.
- Q: What happens when a schema version drops a tier that existing entries use? → A: Activation of that version is refused.
- Q: Who may use the non-rejection flag when tier and contributor permissions differ? → A: It is permitted when the contributor is listed, or when every tier used in the request is listed.
- Q: What if a validator itself faults? → A: The caller gets an error, never "no violations": admission rejects the request and a read fails.
- Q: What happens on a stale expected sequence number, or a storage failure mid-commit? → A: `SEQ_CONFLICT` with nothing recorded; `STORAGE_ERROR` (not a rejection) with no entry visible.
- Q: Do dynamic (macro) defaults belong in this feature? → A: No. They are allowed by the SPEC but arrive with computed fields; this feature has static defaults only.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Record contributions durably (Priority: P1)

A writer (a backfill job, an end user, or an admin) records a contribution: a create, update or delete of one
value at a named location (path) inside a document, labelled with the writer's tier. The contribution is kept
permanently, never altered, and given an arrival sequence number unique within that document.

**Why this priority**: Nothing else works without a trustworthy, immutable history of who contributed what.

**Independent Test**: Append a series of contributions to one document and read them back. Every contribution is
present, unchanged, and ordered by arrival sequence. Rejected requests leave no entries.

**Acceptance Scenarios**:

1. **Given** an empty document, **When** a user-tier writer records "set /name = Alice", **Then** the entry is
   stored with the next sequence number and the writer's identity and tier.
2. **Given** a request with a request key that is already in the ledger with an identical payload and identity,
   **When** it is submitted again, **Then** it is not admitted, no entry is created, and the response has the same shape
   as the original success with the original entries and the latest materialized view.
3. **Given** an entry that does not conform to the schema (unknown path or wrong value type), **When** it is
   submitted, **Then** the request fails with an error listing every reason, and nothing is recorded.
4. **Given** a request with several entries where one fails, **When** it is submitted, **Then** none of its
   entries are recorded and the error lists the reasons for every failing entry.
5. **Given** a contribution that would make the merged document break a validation rule, **When** it is
   submitted without the non-rejection flag, **Then** the request fails with the rule violations and nothing
   is recorded.
6. **Given** a request that has both a schema error (for example an unknown field) and would break a validation rule,
   **When** it is submitted, **Then** the error lists every schema error and no rule violations; the rule violations are
   reported once the schema errors are fixed.
7. **Given** rejection auditing is on in metadata-only mode, **When** a request is rejected, **Then** the audit log
   holds the contributor, tier, time, attempted paths and operations, and every reason, and none of the attempted
   values.
8. **Given** a schema that declares `/displayName` as a derived field, **When** a writer submits an entry for
   `/displayName`, with or without the non-rejection flag, **Then** the request is rejected with `FIELD_NOT_WRITABLE`
   and nothing is recorded.

---

### User Story 2 - Read one deterministic merged document (Priority: P1)

A reader fetches a document. It is the merge of every accepted contribution: higher tiers win conflicts unless a
field's merge rule says otherwise, and the result does not depend on write races or wall-clock timestamps.

**Why this priority**: This is the reason the architecture exists: predictable results from many writers.

**Independent Test**: Load the worked example of 13 entries (docs/SPEC.md 16.1) and compare the merged result, before
derived fields, with the expected layers and document.

**Acceptance Scenarios**:

1. **Given** the worked example's entries, **When** the document is read, **Then** it equals the expected merged
   document (name "Alice S.", age 30, tags x/y/z, address zip and city, currency EUR, plan "free", phone absent).
2. **Given** the same accepted entries and tiers, **When** only the recorded wall-clock times are changed,
   **Then** the merged document is identical.
3. **Given** a late entry from a lower tier, **When** it arrives after higher-tier entries for the same
   last-write-wins field, **Then** the higher-tier value still wins.
4. **Given** each tier's entries are combined first and then the tiers are combined, **When** compared with
   combining all entries in one pass ordered by tier then sequence, **Then** the results are identical.

---

### User Story 3 - Declare merge behavior per field in a central schema (Priority: P1)

A schema author declares, in one central versioned schema, the type of each field and how conflicting values
are combined: last-write-wins, maximum, set union, or field-by-field for nested structures.

**Why this priority**: Merge outcomes must be defined once and apply to every writer, not be coded into clients.

**Independent Test**: Define a schema with one field per rule, contribute conflicting values from different
tiers, and verify each field resolves per its rule.

**Acceptance Scenarios**:

1. **Given** an age field with a "maximum" rule, **When** backfill sets 30 and a user sets 28, **Then** the
   merged age is 30.
2. **Given** a tags field with a "union" rule, **When** three tiers each contribute one tag, **Then** the merged
   tags contain all three in tier order without duplicates.
3. **Given** a nested address with field-by-field merging, **When** backfill sets zip and admin sets city,
   **Then** the merged address contains both.
4. **Given** a custom merge rule that does not declare associativity, **When** the schema is loaded, **Then**
   loading is refused with an explanation.

---

### User Story 4 - Default values that contributions may replace (Priority: P2)

A schema author declares default values for fields. A default applies when no one has contributed a value, and
any contribution replaces it.

**Why this priority**: Defaults make documents complete without forcing every writer to supply common values.

**Independent Test**: Read a document with no contribution to a defaulted field, then add one and read again.

**Acceptance Scenarios**:

1. **Given** a plan field with default "free" and no contributions, **When** the document is read, **Then**
   plan is "free".
2. **Given** a currency field with default "USD", **When** a user contributes "EUR", **Then** the merged
   currency is "EUR".
3. **Given** a defaulted field with the default delete setting, **When** a writer deletes it, **Then** the
   field is absent from the merged document.
4. **Given** a defaulted field whose schema says deletes fall back to the default, **When** a writer deletes
   it, **Then** the merged document shows the default value.

---

### User Story 5 - Delete fields with tombstones (Priority: P2)

An operator deletes a field by recording a delete (a tombstone). The field disappears from the merged document,
lower-tier values cannot bring it back, and a later contribution from the same or a higher tier re-creates it.

**Why this priority**: Deletion must be recordable, auditable and reversible without erasing history.

**Independent Test**: Contribute a value at a low tier, delete it at a high tier, read; then contribute again at
the high tier, read.

**Acceptance Scenarios**:

1. **Given** backfill set /phone and admin later deleted /phone, **When** the document is read, **Then** phone
   is absent.
2. **Given** that delete, **When** a new backfill write to /phone arrives later, **Then** phone remains absent.
3. **Given** that delete, **When** admin later sets /phone again, **Then** the new value appears.
4. **Given** a delete on a nested structure, **When** read, **Then** the whole subtree is absent unless
   re-created by later contributions.

---

### User Story 6 - Explain why a field has its value (Priority: P3)

An auditor asks why a field has its current value and receives the winning entry, the rule applied, and any
entries that were superseded.

**Why this priority**: Auditability is a core motivation, but it builds on the previous stories.

**Independent Test**: Ask for the explanation of each field in the worked example and verify the winning entry
and rule.

**Acceptance Scenarios**:

1. **Given** the worked example, **When** the auditor asks about /age, **Then** the answer names the
   maximum rule and the entry that supplied 30.
2. **Given** a defaulted field nobody contributed to, **When** asked, **Then** the answer says the value comes
   from the default.

---

### User Story 7 - Record data that breaks validation rules, and surface the problems (Priority: P2)

A writer loading legacy or backfilled data sends a contribution with a non-rejection flag. If the data breaks
validation rules, it is still recorded, and each violation becomes a model error stored with the document and
returned whenever the document is read. For example, a country allows one spouse; backfilled data lists two. The
data is kept and flagged so users can be nudged to fix it later.

**Why this priority**: Bulk and legacy data is often imperfect; refusing it would lose it, while silently
accepting it would hide the problem.

**Independent Test**: Submit a backfill that violates a rule with the flag, then without it, and read the
document each time.

**Acceptance Scenarios**:

1. **Given** a rule that allows at most one spouse, **When** a backfill with two spouses is submitted with the
   non-rejection flag, **Then** it is recorded and the document is returned with a model error describing the
   violation.
2. **Given** the same contribution without the flag, **When** it is submitted, **Then** the request fails and
   nothing is recorded.
3. **Given** a document with a model error, **When** a permitted writer sends a flagged backfill that adds
   new violations, **Then** it is recorded and the new violations are returned as model errors alongside the
   existing ones.
4. **Given** a user-tier writer whom the schema does not permit to use the flag, **When** a request carries the
   flag, **Then** it fails and nothing is recorded, even if the data would have passed validation.
5. **Given** a document with a model error, **When** a later contribution reduces the errors and adds none,
   **Then** it is accepted and the document shows fewer model errors.
6. **Given** a document with a model error, **When** a later contribution leaves the existing errors unchanged
   and adds none (for example, updating an unrelated field), **Then** it is accepted.
7. **Given** a document with a model error, **When** a later contribution without the flag would introduce any new model error,
   **Then** the request fails and nothing is recorded.
8. **Given** a document with a model error, **When** a later contribution fixes it, **Then** the document is
   returned with no model errors.

---

### User Story 8 - Re-evaluate documents when the schema or rules change (Priority: P2)

When a document is read and the schema or validation rules have changed since the document was last derived, the
system discards the previously derived document and re-derives it from the ledger using the current schema and
rules. Rule violations found are recorded as model errors and returned with the document.

**Why this priority**: Rules evolve; existing documents must show how they stand against today's rules.

**Independent Test**: Read a document, tighten a validation rule so it now fails, read again.

**Acceptance Scenarios**:

1. **Given** a document derived under schema version 1, **When** version 2 adds a rule the data violates and the
   document is read, **Then** the document is re-derived and returned with the new model error.
2. **Given** a document read under the current schema version, **When** it is read again, **Then** no
   re-derivation is needed and the same result is returned.

---

### Edge Cases

- A write targets a path not in the schema, or a value of the wrong type: the request fails; nothing recorded.
- A request reuses a request key with a different payload: it is rejected and the original is kept.
- A request has no request key and repeats an earlier payload (for example, setting a value back after another writer
  changed it): it is a new request and is applied.
- An entry repeats an idempotency key already seen for the document: its request is rejected and nothing is recorded.
- A delete arrives before any value exists for the field: it still takes effect and suppresses lower-tier values.
- A write beneath a path whose parent was deleted earlier: re-creates only the written leaves.
- An empty ledger: the document contains only defaults.
- A writer submits an entry for a tier it is not permitted to use: the request fails; nothing recorded.
- Concurrent submissions to the same document: they receive distinct, strictly increasing sequence numbers.
- A merge rule applied to values of mismatched types: the request fails rather than merging.
- A struct written through a parent field carries a key the schema does not declare: the request fails (`UNKNOWN_PATH`).
- A user deletes a parent path whose declared child needs an authority the user lacks: the request fails.
- A service starts with an active schema version that drops a tier the ledger uses: it refuses to start (`TIER_IN_USE`).
- A validation rule depends on a value that comes from an external call (deferred value): not allowed; such a
  rule is refused when the schema is loaded.
- The non-rejection flag is sent by a writer the schema does not permit to use it: rejected.
- The non-rejection flag is sent with an entry that has a schema error (unknown field, wrong type): still
  rejected; the flag only affects validation-rule failures.
- A request whose entries depend on each other (for example, one fixes a value another needs): valid, because
  only the final result of the whole request is checked.
- A contribution to a document that already has model errors fixes one and introduces another: rejected,
  because it introduces a new model error.
- An entry targets a path the schema declares as derived (for example `/displayName`): the request is rejected with
  `FIELD_NOT_WRITABLE`, even when the non-rejection flag is set.
- A validator itself fails (a fault, not a violation): the caller receives an error and it is never treated as "no
  violations"; at admission the request is rejected, and on a read the read fails.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST record each contribution as an immutable, append-only entry containing document id,
  path, operation, value, contributor, tier, arrival sequence number and recorded time.
- **FR-002**: System MUST assign each entry a sequence number that is strictly increasing within its document.
- **FR-003**: System MUST ignore recorded wall-clock time when deciding merge results; only tier and sequence
  decide order.
- **FR-004**: System MUST support documents made of typed values (primitives, nested structures, and lists)
  with annotations, addressed by paths.
- **FR-005**: System MUST support create/update and delete operations; a delete MUST be expressed as an
  annotation (tombstone) on the value.
- **FR-006**: System MUST check each contribution request before recording anything: path known to the schema,
  value type, operation allowed, contributor allowed to use the tier (schema errors), and validation rules
  evaluated on the document that would result from the merge. A struct written through a parent field is checked child
  by child: every child MUST be declared by the schema, and its operations, authority, type and mergeability apply as
  if it had been written directly. A delete of a path also needs the authority of every declared field beneath it,
  because it wipes them. A value with no canonical Ion form (for example a timestamp whose UTC instant falls outside the
  years 1 to 9999, or a name that Ion reads as a symbol id such as `$10`) is a type error.
- **FR-007**: System MUST reject a request that fails these checks by returning an error that lists every
  reason of the first failing stage (all schema errors; validation-rule failures only when there are no schema errors), and MUST record nothing in the ledger for it; rejected contributions never enter the ledger or any
  merge. When rejection auditing is enabled for the document (FR-027), the rejection is recorded only in the
  rejection audit log.
- **FR-008**: A request MAY carry a caller-supplied request key. The *request hash* is a SHA-256 over the canonical
  form of the document id, the contributor identity, the non-rejection flag and the entries (tier, path, operation,
  value, idempotency key, meta); the expected sequence number is excluded. System MUST treat a request whose
  `(document, contributor, request key)` is already in the ledger with the same request hash as a *duplicate*: it MUST
  NOT be admitted, validated, audited or written. The response MUST have the same shape as the original success: the
  original entries (id, sequence), the request id, the model-error changes recorded for that request, the latest
  materialized view, and a duplicate indicator. The same request key with a different hash MUST be rejected. A request
  without a request key is never a duplicate. A request key whose original request was rejected is treated as new,
  because nothing was stored. If two requests with the same key arrive together, exactly one is committed and the other
  receives the duplicate response. An entry idempotency key is separate: it is required only by merge rules that are
  not idempotent (none in this feature), and an entry reusing an already-seen idempotency key MUST cause its request to
  be rejected.
- **FR-009**: System MUST load a central, versioned, immutable schema declaring tiers (lowest to highest),
  field types, merge rules, defaults, which tiers or contributors may use the non-rejection flag, and, for each
  defaulted field, what a delete means (field absent, or fall back to the default). The setting defaults to
  "field absent". Tier names, count and order are chosen by the schema author, and one order applies to every field.
  A schema version that drops or renames a tier used by any entry in the ledger MUST be refused at activation. The same
  check applies when a document is pinned to a version (for that document's entries) and when a service starts with a
  version already active or pinned; the problem code is `TIER_IN_USE`.
- **FR-010**: System MUST support merge rules for last-write-wins, maximum, minimum, set union, and
  field-by-field merging of nested structures. A struct value written to a field with a whole-value rule (such as
  last-write-wins) replaces the whole value; written to a field-by-field struct it is applied to each child. Union
  treats list items as equal when their values are equal and keeps the order of first appearance in tier order.
  Maximum and minimum are defined only for ordered types (numbers, strings, timestamps); a schema that uses them on
  another type MUST be refused at load.
- **FR-011**: System MUST refuse to load a schema that uses a merge rule not declaring associativity.
- **FR-012**: System MUST resolve every field by ordering accepted entries by tier, then sequence, and applying
  the field's merge rule in that order.
- **FR-013**: System MUST produce identical results whether each tier is combined first and tiers combined
  after, or all entries are combined in one ordered pass.
- **FR-014**: System MUST apply schema defaults as the lowest layer, replaced by any contribution.
- **FR-015**: System MUST apply tombstone rules: a delete beats earlier same-tier and all lower-tier values;
  a later same-tier or higher-tier contribution re-creates the field; a lower-tier write never revives a
  higher-tier delete; deleting a nested structure removes its subtree.
- **FR-016**: System MUST remove tombstoned fields from the final document after merging, unless the field's
  schema setting says deletes fall back to its default, in which case the field takes its default value. For a
  struct with child defaults, a delete applies per child using the child's own delete setting.
- **FR-017**: System MUST allow a reader to fetch the merged document of a given document id.
- **FR-018**: System MUST allow a reader to request, for any path, the winning entry, the rule applied, and
  superseded entries (or the default as source).
- **FR-019**: System MUST produce byte-for-byte identical documents for identical ledgers and schema versions,
  including stable ordering of fields. The canonical form of documents and model errors is defined in the conformance
  vector contract: compact text, struct fields sorted by Unicode code point, annotations first. Decimal and timestamp
  values keep their written precision, and conformance vectors avoid binary floating-point values.
- **FR-020**: System MUST be validated by tests for each merge rule's declared laws (associativity, and
  commutativity or idempotence where declared) and by the normative worked example.
- **FR-021**: System MUST evaluate validation rules on the merged document that would result from a contribution
  and MUST NOT let any deferred (external-call) value be an input to a validation rule; a schema declaring such
  a rule MUST be refused at load. The arguments of the built-in validators are checked at load as well (for example a
  non-numeric `range` bound, or `max_len` without a whole-number `n`): a schema with bad arguments is refused with
  `BAD_BEHAVIOR_ARGS`, so a document cannot become unwritable at its first write.
- **FR-022**: System MUST accept a per-request non-rejection flag that lets validation-rule failures be recorded
  instead of rejecting the request. The flag MUST NOT bypass schema errors (unknown path, wrong type, operation
  or tier not permitted). Only tiers or contributors the schema permits MAY use the flag: the flag is permitted when the
  contributor is listed in the schema's flag permissions, or when every tier used in the request is listed; a flagged
  request from anyone else MUST be rejected. A flagged request MAY carry a reason or ticket in `meta`; it is not required.
- **FR-023**: System MUST record each validation-rule violation found as a model error stored with the document
  and MUST return all current model errors whenever the document is read. A model error is identified by the validation
  rule and the path scope it applies to; two violations of the same rule at the same scope are the same error. A
  worsening of an existing error is not detected as a new error.
- **FR-024**: When a document has model errors, System MUST accept a contribution only if the set of model
  errors after it adds none that did not exist before; a contribution that reduces model errors is accepted.
  This rule does not apply to a request carrying the non-rejection flag from a permitted writer, whose new
  violations are recorded as model errors.
- **FR-025**: System MUST recompute model errors whenever the document is derived (including after a
  contribution), so fixed errors disappear and stale errors are not shown. Errors are recomputed from the current
  document; an error whose path no longer exists disappears unless the rule still fails on the document.
- **FR-026**: When a document is read and the schema or validation-rule version differs from the version under
  which it was last derived, System MUST discard the previously derived document and re-derive it from the
  ledger under the current version, recording the violations found as model errors. A version change is a change in the
  schema version under which the document is read: the active version, or the version the document is pinned to.
  Validation-rule versions are part of the schema version. Reading under an inactive version MUST be rejected with
  `SCHEMA_INACTIVE`.
- **FR-027**: System MUST support a rejection-auditing setting declared per document type in the schema, with an
  optional per-document override, defaulting to off. When on, System MUST write each rejected request (contributor,
  tier, attempted contribution, every reason, time) to a rejection audit log separate from the ledger. The
  rejection audit log MUST NOT be read by any merge, validation, model-error calculation, or document read, and
  MUST NOT affect sequence numbers. When off, a rejected request leaves no record. The setting MAY select a
  metadata-only mode that keeps the contributor, tier, time, flag, attempted paths and operations, every reason and the
  schema version, and omits the attempted values. Auditing is best effort: if writing an audit record fails, the
  request's rejection is still returned and the failure is logged; it never changes a request's outcome.
- **FR-028**: System MUST cap the rejection audit log with a retention setting that is a maximum number of
  rejected contributions, a maximum age, or both (whichever limit is exceeded first prunes the oldest records).
  A schema that enables rejection auditing without a retention limit MUST be refused at load. Pruning removes the
  oldest records first (by recorded time) and runs after each audit write and on demand. Pruning MUST NOT
  affect the ledger or any document.
- **FR-029**: System MUST treat a contribution request as atomic: every entry in it is accepted or none is,
  with no partial acceptance. Accepted entries of one request MUST be committed together with consecutive
  sequence numbers and a shared request identifier. A request's entries are committed as a single unit: if storage
  fails during the commit, no entry of the request becomes visible and the caller receives a `STORAGE_ERROR`, which is
  not a rejection (retrying with a request key is safe).
- **FR-030**: System MUST evaluate validation rules and the "no new model errors" rule once per request, on the
  document that results from applying all of the request's entries, and MUST attribute each model error to the
  request that introduced it. A model error found only because the schema version changed is attributed to that
  schema version. An error keeps its attribution until it disappears; if it reappears it is a new error.
- **FR-031**: System MUST reject at admission, with reason `FIELD_NOT_WRITABLE`, any entry whose path is, or lies beneath,
  a field the schema declares as derived (computed or deferred). The flag MUST NOT bypass this rejection. This feature
  only reads such declarations from the schema; evaluating derived fields belongs to later features.
- **FR-032**: System MUST accept an authenticated contributor identity together with the set of tiers it may use.
  Authentication itself is out of scope.
- **FR-033**: System MUST provide `rebuild` (one document or all): derive each document from the ledger alone and
  compare it with its cached view. A cached view that is current but differs MUST be reported and repaired; a view that
  is missing or from another schema version MUST be derived afresh and stored without being counted as different; a
  document that cannot be derived (a validator faults, a schema version is not registered) MUST be reported and MUST NOT
  stop the other documents from being checked. `rebuild` never writes to the ledger.

### Key Entities

- **Document**: A typed structured value identified by a document id; derived entirely from its ledger.
- **Contribution request**: One submission of one or more entries, accepted or rejected as a whole, optionally
  carrying a caller-supplied request key so a retry is recognized.
- **Entry**: One immutable contribution (path, operation, value, contributor, tier, sequence). Only accepted
  contributions become entries.
- **Rejection audit log**: An optional, separate, audit-only record of rejected requests; never used to build
  documents.
- **Model error**: A recorded violation of a validation rule by a document, stored with the document and
  returned whenever it is read.
- **Non-rejection flag** (named `lenient` in the interfaces): A per-request marker that lets validation-rule failures be recorded as model errors
  instead of rejecting the request.
- **Tier**: An ordered class of contributor (for example backfill, user, admin); higher tiers win by default.
- **Schema**: A versioned, immutable definition of tiers, field types, merge rules and defaults.
- **Merge rule**: A named way to combine an older and a newer value of a field, with declared algebraic laws.
- **Layer**: The combined state of one tier's entries, or the defaults layer.
- **Tombstone**: A delete marker carried as an annotation on a value.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For the worked example of 13 entries (docs/SPEC.md 16.1), 100% of the layers and the merged document, before computed and
  deferred values, match the expected results exactly.
- **SC-002**: Across at least 1,000 randomly generated ledgers, combining per tier then across tiers matches the
  single-pass result in 100% of cases.
- **SC-003**: Changing only recorded wall-clock times in those ledgers changes the merged document in 0 cases.
- **SC-004**: For every field in a merged document, a reader obtains its explanation (winning entry or default,
  and rule) in a single request.
- **SC-005**: 100% of rejected requests return an error listing every reason and leave 0 entries in the ledger
  and 0 changes in any merged result.
- **SC-006**: An uncached read of a document with up to 1,000 accepted entries, each submitted as its own request,
  completes in under 1 second on the CI runner.
- **SC-007**: A schema author can add a new merge-governed field and see it merge correctly without any change
  to writers.
- **SC-008**: Every rule violation accepted under the non-rejection flag is returned with the document on 100%
  of reads until a contribution fixes it.
- **SC-009**: A document read under a newer schema or rule version reflects the new rules (including model
  errors) on its first read after the change.
- **SC-010**: With rejection auditing on, 100% of rejected requests appear in the rejection audit log with their
  reasons (until pruned by retention); with it off, 0 appear; in both cases the log has 0 influence on any
  document.
- **SC-011**: After pruning, the rejection audit log never exceeds its configured maximum number of rejected
  contributions and holds no record older than its configured maximum age.
- **SC-012**: Re-submitting any accepted request with the same request key and payload leaves 0 new entries and returns
  a response of identical shape to the original success, in 100% of generated cases.
- **SC-013**: With rejection auditing in metadata-only mode, 0 attempted values appear in the rejection audit log for
  100% of rejected requests.

## Assumptions

- Out of scope for this feature: evaluating computed fields, deferred (external-call) fields, authoring a library of
  validation rule types, per-field authority, snapshots, compaction, time-travel reads, and detailed schema
  evolution. These are separate features; this feature defines how any validation rule's outcome affects
  acceptance and model errors.
- Validation rules are evaluated on the merged document (and on computed values once they exist); deferred
  values are never available to validation rules.
- Sampling, global caps and access control for the rejection audit log are not defined here; this feature defines the
  on/off setting, the full or metadata-only mode, and the count and time retention caps.
- A single global tier order applies to every field in v1 (per-field precedence is deferred).
- If a request states an expected sequence number that no longer matches, it is rejected with `SEQ_CONFLICT` and
  nothing is recorded.
- Entries for one document are sequenced by a single logical writer per document; multi-region sequencing is
  deferred.
- Only the core merge rules listed in FR-010 are included; counters and ordered logs are deferred.
- Contributor authentication is provided elsewhere; this feature receives an authenticated identity and the
  tiers it may use.
- Lists use whole-value replacement or set union; concurrent ordered-list editing is out of scope.
- The normative worked example in docs/SPEC.md 16.1 is the reference test case.
