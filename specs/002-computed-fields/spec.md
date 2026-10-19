# Feature Specification: Computed Fields and Non-Writability

**Feature Branch**: `002-computed-fields`

**Created**: 2026-10-19

**Status**: Draft

**Depends on**: Feature 001 (core ledger and merge engine, implemented); docs/SPEC.md sections 5.3, 9.1 to 9.4 (including
9.3.1), 10.4 (computed values and validation), 11 (derived authority), 13 (sandboxing) and 16.1 (phase P4).

**Input**: User description: "Computed fields and non-writability. Add computed fields and enforce that contributions can never override them. Derived values must stay consistent with their inputs and be trustworthy regardless of who writes. Stories: declare a computed field with preset macros over declared dependencies; entries targeting a computed path are rejected with FIELD_NOT_WRITABLE (not a ledger entry; kept only in the optional rejection audit log); register custom macros that run sandboxed and see only declared dependencies; dependency cycles are rejected when the schema loads; define behavior when a dependency is missing; impure computes (SPEC 9.3.1): a computed field whose hook does IO, with timeout, retry and deadline, recorded outcomes in an effect log, a failure mode of reject or model error set per field or per document type, recompute, and a rebuild that makes no calls. Acceptance: displayName = upper(name) and label = macro(name, address.city) appear after merge with the computed:: annotation; the rejected \"HACKED\" entry in SPEC 16.1 never changes the document."

## Clarifications

### Session 2026-10-19

- Q: When one of a computed field's inputs has no value, is the macro still run with that input as null, or skipped so that `on_missing` applies straight away? → A: Each macro declares whether it accepts missing inputs. `coalesce`, `if`, `eq` and `join` accept them and receive the missing input as null; every other macro, including custom macros by default, is skipped and `on_missing` applies.
- Q: Does the model error from `on_missing: error` count against a request under the no-new-errors rule? → A: Yes, like any other new model error: an unflagged request that causes it is rejected (`MODEL_ERROR_INTRODUCED`, or `VALIDATION_FAILED` when the document had no errors), and a permitted flagged request is accepted with the error recorded. Only the impure-failure error of `model_error_on_failure` is exempt.
- Q: Which error does a request get when a pure macro crashes, exceeds a limit, or returns the wrong type? → A: A new dedicated code, `COMPUTE_FAULT`, with a reason naming the macro. `VALIDATION_FAILED` stays for data that breaks a rule and `IMPURE_COMPUTE_FAILED` for another system failing.
- Q: Who is allowed to trigger `recompute`, and where is that permission defined? → A: The schema declares it, per impure field, with the same vocabulary as `authority` (tiers or roles). It is required on every impure field. A caller outside the list is refused with `NOT_AUTHORIZED`.
- Q: How are the time and memory limits of a custom macro set? → A: The platform sets defaults and a maximum. A macro's registration may set a lower limit for that macro, never a higher one.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - See computed values in the document (Priority: P1)

A schema author declares that some fields are computed from other fields, for example `displayName` as the upper-case form
of `name`, using one of the ready-made macros (upper, lower, trim, concat, coalesce, len, sum, min, max, round, format_ts,
hash, join, if, eq) and a list of declared dependencies. After the ledger is merged, every computed field appears in the
document, marked as computed, and always agrees with its inputs. Nobody writes these values; they follow the data.

**Why this priority**: This is the reason the feature exists: derived values that can never drift from their inputs.

**Independent Test**: Load the worked example of docs/SPEC.md 16.1 with `displayName = upper(name)` and
`label = macro(name, address.city)`; read the document and compare it with the expected output of the example.

**Acceptance Scenarios**:

1. **Given** the worked example whose merged `name` is "Alice S." and `address.city` is "Paris", **When** the document is
   read, **Then** it contains `displayName` as the computed value "ALICE S." and `label` as the computed value
   "Alice S. (Paris)", each carrying the `computed::` annotation, and every other field is as in feature 001.
2. **Given** that document, **When** a new contribution changes `name` to "Bob", **Then** the next read shows
   `displayName` "BOB" and a `label` built from "Bob", with no separate write to either computed field.
3. **Given** a computed field that depends on another computed field (for example `label` depends on `displayName`),
   **When** the document is derived, **Then** the fields are evaluated in dependency order and the result is the same on
   every derivation.
4. **Given** a ledger and a schema version, **When** the document is derived twice, on two machines or two storage
   backends, **Then** the computed values are byte-for-byte identical (including the annotation).
5. **Given** a computed field, **When** an auditor asks for the explanation of its path, **Then** the answer says the
   value is computed, names the macro and its version, and lists the dependency values it was computed from.
6. **Given** any document with computed fields, **When** its ledger is inspected, **Then** no entry contains a computed
   value: results are never recorded in the ledger.

---

### User Story 2 - A computed value can never be overridden (Priority: P1)

No contributor, whatever their tier or authority, can set or delete a computed field, or any path beneath one. A request
that tries is rejected whole with the reason `FIELD_NOT_WRITABLE`. It is not a ledger entry, it consumes no sequence
number, and it is recorded only in the optional rejection audit log. The non-rejection flag cannot bypass it.

**Why this priority**: Without this, computed values cannot be trusted; it is the other half of the feature's promise.

**Independent Test**: Submit the "HACKED" request of docs/SPEC.md 16.1 (admin sets `/displayName`), with auditing on and
off; the document, the ledger and the sequence numbers must be unchanged.

**Acceptance Scenarios**:

1. **Given** the worked example, **When** an admin submits `/displayName = "HACKED"`, **Then** the request is rejected with
   `FIELD_NOT_WRITABLE`, nothing is appended, no sequence number is consumed, and the document is unchanged.
2. **Given** rejection auditing is on, **When** that request is rejected, **Then** exactly one audit record exists for it;
   when auditing is off, none.
3. **Given** the same request sent with the non-rejection flag by a contributor permitted to use it, **When** it is
   submitted, **Then** it is still rejected with `FIELD_NOT_WRITABLE`.
4. **Given** a request with one valid entry and one entry on a computed path, **When** it is submitted, **Then** the whole
   request is rejected and the valid entry is not recorded either.
5. **Given** a write to a path beneath a computed field, or a delete of a computed field, **When** it is submitted,
   **Then** it is rejected with `FIELD_NOT_WRITABLE` in the same way.
6. **Given** an impure computed field (User Story 6), **When** a contributor writes to its path, **Then** it is rejected
   with `FIELD_NOT_WRITABLE` as well.
7. **Given** a field that becomes computed in a later schema version, **When** the document is read under that version,
   **Then** earlier accepted entries for it are retained in the ledger but ignored (shadowed), and the computed value is
   what the document shows.

---

### User Story 3 - Register custom macros that run safely (Priority: P2)

Beyond the presets, a schema author or platform team can register their own macros, for example `acme.label`, referenced by
name and version. A custom macro runs in a sandbox: it receives only the values of its declared dependencies, has no
access to the clock, randomness, network, files or environment, and is stopped when it exceeds its resource limits.

**Why this priority**: Presets cover common cases; real schemas need their own logic, but only if it cannot undermine
determinism or leak data.

**Independent Test**: Register a macro that tries to read a field outside its dependencies and one that runs forever;
the first sees nothing outside its inputs and the second is stopped, and neither changes any other result.

**Acceptance Scenarios**:

1. **Given** a registered custom macro and a computed field using it, **When** the document is derived, **Then** the
   field has the macro's result, exactly as with a preset.
2. **Given** a macro that tries to read a path it did not declare, **When** it runs, **Then** it cannot see that path or any
   other part of the document beyond its declared dependencies.
3. **Given** a macro that tries to use the clock, randomness, the network or the filesystem, **When** it runs, **Then**
   the attempt has no effect (it is unavailable) and the macro's result is still a function of its inputs only.
4. **Given** a macro that exceeds its time or memory limit, crashes, or returns a value of the wrong type, **When** a
   document is derived, **Then** the engine reports a fault naming the macro: a request that would trigger it is rejected
   with `COMPUTE_FAULT`, a read reports the fault, and it is never treated as an empty or null value.
5. **Given** a schema that names a macro (or macro version) that is not registered, **When** it is loaded, **Then** it is
   refused with the unknown behavior reported.
6. **Given** a preset name, **When** someone tries to register a custom macro under it, **Then** it is refused: preset
   names are reserved.
7. **Given** a macro registered with no limits of its own, **When** it runs, **Then** the platform's default time and
   memory limits apply.
8. **Given** a macro registered with a lower limit than the platform default, **When** it exceeds that lower limit,
   **Then** it is stopped and reported as a fault, even though it stayed within the platform default.
9. **Given** a macro registered with a limit above the platform maximum, **When** it is registered, **Then** the
   registration is refused.

---

### User Story 4 - Refuse impossible dependencies when the schema loads (Priority: P2)

A schema author is told at load time, not at the first read, when computed fields cannot be evaluated: two fields depend on
each other (directly or through others), a dependency names a path the schema does not declare, or a computed field
depends on a deferred (external-call) field.

**Why this priority**: A cycle or a dangling dependency makes every document unreadable; failing closed at load keeps that
out of production.

**Independent Test**: Load schemas with a two-field cycle, a longer cycle, an undeclared dependency and a deferred
dependency; each is refused with a stable code and the offending fields named.

**Acceptance Scenarios**:

1. **Given** two computed fields that depend on each other, **When** the schema is loaded, **Then** it is refused with
   `DEPENDENCY_CYCLE` and the fields in the cycle are listed.
2. **Given** a cycle of three fields, or a field that depends on itself, **When** the schema is loaded, **Then** it is
   refused in the same way.
3. **Given** a computed field that depends on a path no field of the schema declares, **When** the schema is loaded,
   **Then** it is refused and the missing dependency is named.
4. **Given** a computed field that depends on a deferred field, **When** the schema is loaded, **Then** it is refused with
   `DEPENDS_ON_DEFERRED`.
5. **Given** a computed field that also declares a merge rule or a default, or that is declared both computed and
   deferred, **When** the schema is loaded, **Then** it is refused.
6. **Given** a valid schema, **When** it is loaded, **Then** the dependency order of its computed fields is fixed and does
   not depend on the order the fields are written in the schema file.

---

### User Story 5 - Decide what happens when an input is missing (Priority: P3)

A schema author states, per computed field, what the field does when one of its dependencies has no value: become an
explicit null (the default), be left out of the document, or be reported as an error. Document readers then see consistent
behavior instead of surprises.

**Why this priority**: Missing inputs are common (a new document has no `name` yet); the behavior must be a decision, not
an accident, but the default covers most schemas.

**Independent Test**: With a document that has no `name`, read `displayName` under each of the three settings.

**Acceptance Scenarios**:

1. **Given** no setting (the default `null`) and a macro that does not accept missing inputs (such as `upper`), **When** a
   dependency is missing, **Then** the macro is not run and the computed field is present in the document with a null
   value.
2. **Given** `absent`, **When** a dependency is missing, **Then** the computed field is left out of the document.
3. **Given** `error`, **When** a dependency is missing, **Then** the computed field is left out and a model error is
   recorded for it, returned with the document.
4. **Given** a dependency that is itself computed and missing, **When** the document is derived, **Then** the dependent
   field follows its own setting with the missing computed value as input.
5. **Given** a dependency that has a default (feature 001), **When** nobody contributed to it, **Then** the default counts
   as its value and the computed field is computed from it.
6. **Given** a dependency that was deleted (a tombstone), **When** the document is derived, **Then** it is missing for the
   purpose of this setting, unless its schema falls back to a default on delete.
7. **Given** `coalesce(nickname, name)` with `nickname` missing and `name` set, **When** the document is derived, **Then**
   the field has the value of `name`: the macro ran with `nickname` as null, whatever `on_missing` says.
8. **Given** `coalesce(nickname, name)` with both inputs missing, **When** the document is derived, **Then** the field's
   value is null (the macro's own result), not a model error.
9. **Given** `error` and a request that deletes the dependency, **When** the request is not flagged, **Then** it is
   rejected because it would add a model error (`MODEL_ERROR_INTRODUCED` if the document already had errors, otherwise
   `VALIDATION_FAILED`) and nothing is recorded.
10. **Given** the same request sent with the non-rejection flag by a permitted writer, **When** it is submitted, **Then** it
   is accepted and the model error for the computed field is recorded and attributed to that request.

---

### User Story 6 - Impure computes with recorded outcomes (Priority: P2)

For the rare value that only another system can provide and that the caller cannot proceed without (for example a tax id
issued by a registry), a schema author declares an impure computed field: a hook that calls out, with a stated reason, a
per-attempt timeout, a bounded retry policy and an overall deadline. The caller gets the value, or the failure, in the same
response. Every outcome is written to an effect log that is separate from the ledger and holds no contributions, so
derivation stays repeatable: the same ledger, effect log and schema version give the same document, and rebuilding or
re-reading makes no outbound call when an outcome is already recorded.

**Why this priority**: It makes the tradeoff between strong consistency and determinism explicit and safe, but most schemas
will not need it, so it follows the pure path.

**Independent Test**: Use a hook that counts its calls. Submit a request that changes the hook's inputs, read the document
several times, rebuild the document, and retry the request; the hook is called once.

**Acceptance Scenarios**:

1. **Given** an impure field with a reason, timeout, retry and deadline, **When** a request changes the field's inputs,
   **Then** the hook is called while the request is being handled, the response contains the resulting value, and the
   outcome is recorded.
2. **Given** a recorded outcome for the current inputs, **When** the document is read, rebuilt, or the request is retried,
   **Then** the recorded value is used and the hook is not called.
3. **Given** a request that changes the inputs of an impure field, **When** the same inputs are submitted again later,
   **Then** the hook is not called again: outcomes are reused when the inputs and the hook version are unchanged.
4. **Given** an attempt that fails or times out, **When** retries remain and the deadline has not passed, **Then** the hook
   is retried with the declared backoff; after the last attempt, or at the deadline, the outcome is a failure.
5. **Given** a request that is rejected for another reason after the hook succeeded, **When** the rejection is returned,
   **Then** the outcome produced for that request is discarded and not reused.
6. **Given** another computed field or a validator that depends on the impure field, **When** the document is derived,
   **Then** it uses the impure value like any other value; an impure field cannot depend on a deferred field.
7. **Given** an impure field declared without a reason, timeout, retry or deadline, **When** the schema is loaded, **Then**
   it is refused.
8. **Given** a request being handled, **When** a hook is called, **Then** it is never called while the storage write lock
   is held; if the document changed while the hook ran, the document is derived again and recorded outcomes for unchanged
   inputs are reused without a new call.
9. **Given** the schema, **When** `schema check` is run, **Then** it reports the inventory of impure computes with their
   reasons.

---

### User Story 7 - Choose what an impure failure does, and recover (Priority: P2)

A schema author decides, for each impure field or once for the whole document type, whether a hook that cannot deliver a
value stops the request (`reject_on_failure`, the default) or lets the request through with a model error
(`model_error_on_failure`). An operator can fix the cause and recompute, and the failure goes away.

**Why this priority**: A failing dependency on another system must have a defined, visible outcome; the choice is the
author's because the right answer depends on the business.

**Independent Test**: With a hook that always fails, send the same request under each setting; check the response, the
ledger, and the model errors on a later read; then fix the hook, recompute, and read again.

**Acceptance Scenarios**:

1. **Given** `reject_on_failure` and a hook that exhausts its retries, **When** a request changes its inputs, **Then** the
   request is rejected with `IMPURE_COMPUTE_FAILED` and nothing is recorded.
2. **Given** `reject_on_failure` and a hook that fails during a read or a rebuild, **When** the document is read, **Then**
   the read is not rejected: the failure is shown as a model error.
3. **Given** `model_error_on_failure`, **When** a request changes the inputs and the hook fails, **Then** the request is
   accepted, a model error for that field is recorded and attributed to that request, and the field follows its missing
   dependency setting.
4. **Given** that model error, **When** the no-new-errors rule of feature 001 is applied, **Then** this error does not
   count against the request, but every other new model error still does.
5. **Given** a computed field or validator that depends on the failed value, **When** the document is derived, **Then** the
   dependent computed field follows its own missing dependency setting, and a validator that depends on the missing value is
   not evaluated and adds no further error, so the failure is the single error.
6. **Given** a setting declared on a field and another for the document type, **When** a failure occurs, **Then** the
   field's setting wins; with neither, the default is `reject_on_failure`.
7. **Given** a model error from an impure failure, **When** an operator fixes the cause and recomputes the document (or one
   field), **Then** the hook is called again with the current inputs, the new outcome supersedes the earlier one, and the
   model error disappears on the next derivation.
8. **Given** a changed input, **When** the next request or read needs the value, **Then** the hook is called again on its
   own, without an explicit recompute.
9. **Given** an impure field that lists the tiers or roles allowed to recompute it, **When** a caller outside that list asks
   for a recompute of the field, **Then** the request is refused with `NOT_AUTHORIZED`, the hook is not called and nothing
   is recorded.
10. **Given** a recompute of a document without naming a field, **When** the caller may recompute some of its impure fields
   and not others, **Then** only the permitted fields are recomputed and the answer lists which fields were recomputed
   and which were skipped for lack of permission.

---

### Edge Cases

- A computed field's dependency is not set, is deleted, or comes from a default: see User Story 5.
- A computed field depends on another computed field that is missing, or on an impure field that failed: it follows its own
  missing-dependency setting.
- A macro returns a value that does not match the declared type of the computed field: this is a fault in the macro, handled
  like any macro fault (User Story 3), never a silently coerced or null value.
- A macro fault happens during the dry run of a request: the request is rejected with `COMPUTE_FAULT` only if the fault is
  caused by the document that the request would produce.
- The same request is retried with its request key after an impure computation: the original outcome is reused and the
  hook is not called again.
- Two requests arrive together and change the same impure inputs: each outcome is recorded against its own inputs; a
  request whose document changed while its hook ran is derived again.
- A hook version changes in a new schema version: earlier outcomes for the old version are not reused, and the next
  derivation that needs the value calls the new hook.
- A computed field is added by a new schema version to a document that has entries at that path: those entries are kept in
  the ledger and ignored under the new version.
- The effect log is lost or empty while the ledger remains: a rebuild with a pure schema is unaffected; with an impure
  field, the next derivation that needs the value calls the hook again and records a new outcome.
- A rejected request never leaves a recorded outcome that later derivations could use. A hook may already have been called
  by then (for example when the request is refused on its expected sequence number); its outcome is discarded.
- Several callers need the same outcome at the same moment (concurrent first reads, or concurrent requests with the same
  inputs): they share one hook call and one record.
- The rejection audit log never influences a computed value (feature 001 rule, unchanged).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST support computed fields: fields whose value is derived from the merged document by a named macro
  over an explicit list of declared dependencies, evaluated after the full merge and before validation of the result.
- **FR-002**: System MUST provide these ready-made macros, all pure: `upper`, `lower`, `trim`, `concat`, `coalesce`, `len`,
  `sum`, `min`, `max`, `round`, `format_ts`, `hash`, `join`, `if`, `eq`. Their names are reserved and their results are
  the same on every machine for the same inputs.
- **FR-003**: System MUST pass a macro only the values of its declared dependencies, never the rest of the document, the
  ledger, or any other ambient information.
- **FR-004**: System MUST evaluate computed fields in an order that respects their dependencies, a computed field MAY
  depend on other computed fields (pure or impure) and on plain fields, and the order MUST NOT depend on the order of
  declaration in the schema.
- **FR-005**: System MUST include each computed value in the document with the `computed::` annotation, in the same
  canonical text form as every other value, so that identical ledgers, effect logs and schema versions give byte-for-byte
  identical documents.
- **FR-006**: System MUST NOT record computed values in the ledger. A computed value MAY be kept in a cache, and a cache
  hit MUST give the same result as a fresh evaluation.
- **FR-007**: System MUST reject, at admission, every entry whose path is a computed field (pure or impure) or lies beneath
  one, with the reason `FIELD_NOT_WRITABLE`, for set and for delete alike. The whole request fails, nothing is appended, no
  sequence number is consumed, and the document is unchanged.
- **FR-008**: The `FIELD_NOT_WRITABLE` rejection MUST NOT be bypassable by the non-rejection flag, by any tier or
  authority, or by any other means, and MUST be recorded only in the rejection audit log when auditing is enabled.
- **FR-009**: When a field becomes computed in a later schema version, System MUST retain its earlier accepted entries in
  the ledger and ignore them when deriving under the new version, and MUST report them as shadowed in provenance.
- **FR-010**: System MUST allow custom macros to be registered by name and version, MUST refuse a schema that names a macro
  or version that is not registered, and MUST refuse to register a custom macro under a preset name.
- **FR-011**: System MUST run a custom macro with no access to the clock, randomness, network, filesystem or environment,
  with limits on time and memory (time is measured as a deterministic budget of evaluation steps, and memory as the total
  size in bytes of the values the macro builds, so the same ledger gives the same result on every machine). The platform sets default limits and a maximum; a macro's registration MAY set lower
  limits for that macro and MUST be refused if it sets higher ones than the maximum. System MUST report a macro that exceeds a limit, errors, or returns a value of the wrong
  type as a fault that names the macro. A fault MUST NOT be treated as a null or empty value: a request whose resulting
  document would trigger it is rejected with the code `COMPUTE_FAULT` and a reason naming the macro, and a read reports
  the fault. `COMPUTE_FAULT` is distinct from `VALIDATION_FAILED` (the data breaks a rule) and `IMPURE_COMPUTE_FAILED`
  (another system failed).
- **FR-012**: System MUST refuse at schema load: a dependency cycle among computed fields, including a field that depends
  on itself (`DEPENDENCY_CYCLE`, listing the fields); a dependency on a path the schema does not declare; a computed field
  that depends on a deferred field (`DEPENDS_ON_DEFERRED`); a derived field that declares a merge rule or a default; and a
  field declared both computed and deferred.
- **FR-013**: System MUST support a per-field `on_missing` setting, with values `null` (the default), `absent` and `error`,
  that decides what a computed field does when any of its dependencies has no value and its macro does not accept missing
  inputs: be present with a null value, be left out of the document, or be left out with a model error recorded for it.
  The macro is not run in that case.
- **FR-013a**: Each macro MUST declare whether it accepts missing inputs. The presets `coalesce`, `if`, `eq` and `join`
  accept them: a missing input is passed to the macro as null and the macro's result is the field's value, whatever
  `on_missing` says. Every other preset, and every custom macro unless it declares otherwise, does not accept them.
- **FR-013b**: A model error from `on_missing: error` MUST count against a request under the no-new-errors rule of
  feature 001 like any other new model error, and MUST be recorded, attributed to the request, when a permitted flagged
  request is accepted.
- **FR-014**: A dependency counts as having no value when it was never set and has no default, was deleted and does not fall
  back to a default, or is itself a computed field that is missing or failed. A null value counts as no value, whether it is a
  document value or the null result of another computed field. A defaulted dependency counts as having its default.
- **FR-015**: System MUST allow validation rules (field-level and document-level) to depend on computed values, including
  impure ones that have a value, and MUST evaluate them after the computed values are known.
- **FR-016**: System MUST support impure computed fields: a field whose behavior calls another system. The declaration MUST
  state a hook and its version, its dependencies, a reason, a per-attempt timeout, a retry policy with a maximum and a
  backoff, an overall deadline, and the tiers or roles allowed to recompute it (the same vocabulary as `authority`); a
  schema missing any of these MUST be refused at load, and an impure field MUST NOT
  depend on a deferred field.
- **FR-017**: System MUST pass an impure hook only its declared dependencies, MUST treat the hook as idempotent (it may be
  retried), and MUST run it outside the sandbox used for pure macros. Any credentials a hook needs belong to the hook
  itself; the engine passes it nothing but its declared dependencies and its timeout.
- **FR-018**: System MUST call an impure hook while handling a request whose changes alter the field's inputs, and while
  reading a document that has no recorded outcome for the current inputs, and MUST return the resulting value or failure in
  the same response.
- **FR-019**: System MUST record every outcome of an impure computation (value or failure, the inputs it was computed
  from, the hook version, the number of attempts) in an append-only effect log that is separate from the ledger and holds no
  contributions. Derivation MUST be a pure function of the ledger, the effect log and the schema version: it uses the
  latest recorded outcome for the same field, inputs and hook version and makes no call.
- **FR-020**: A rebuild, a repeated read, and a retry of an accepted request MUST NOT call any hook while a recorded outcome
  exists for the inputs. A changed input, or a changed hook version, MUST cause a new call on its own.
- **FR-021**: System MUST discard the outcomes produced for a request that is rejected, and MUST NOT call a hook while the
  storage write lock is held; if the document changed while a hook ran, System MUST derive again and reuse outcomes whose
  inputs are unchanged without calling the hook again.
- **FR-022**: System MUST bound a hook by its timeout per attempt, its retry maximum and its deadline overall; running out
  of retries or time produces a failure outcome.
- **FR-023**: System MUST support a `failure_mode` for impure fields, `reject_on_failure` (the default) or
  `model_error_on_failure`, set on the field or once for the whole document type, with the field's setting overriding the
  document type's. Under `reject_on_failure`, a failure while handling a request rejects it with `IMPURE_COMPUTE_FAILED`
  and records nothing, while a failure during a read or rebuild is returned as a model error and never rejects the read.
  Under `model_error_on_failure`, the request is accepted and a model error for the field is recorded and attributed to
  the request.
- **FR-024**: A model error recorded for an impure failure MUST be exempt from the no-new-errors rule of feature 001, and
  every other new model error MUST still count. Fields and validators that depend on the failed value MUST follow the
  missing-dependency rules: a dependent computed field follows its own `on_missing`, a validator that depends on the missing
  value is not evaluated and adds no further error.
- **FR-025**: System MUST provide `recompute` (one document, optionally one field), permitted only to callers whose tiers or
  roles are among those the schema lists for the field (otherwise `NOT_AUTHORIZED`, with no hook call and nothing recorded;
  a recompute without a field covers only the fields the caller may recompute). It calls the hook again with the
  current inputs and records a new outcome that supersedes the earlier one; a successful outcome MUST remove the failure's
  model error on the next derivation.
- **FR-026**: System MUST give `explain` a meaningful answer for a computed path: that the value is computed, the macro or
  hook and its version, the dependency values used, and, for an impure field, the effect log record the value came from.
- **FR-027**: System MUST keep the latest outcome per field, inputs and hook version for as long as the ledger entries it
  derives from exist, and MAY prune superseded outcomes by count and age without changing any derivation. Listing the
  effect log MUST return metadata only (when, which field, which hook, value or failure, attempts) and never a value or an
  error text; a recorded value is visible only through the document, so the effect log is never a way around the access and
  redaction rules of the document.
- **FR-028**: System MUST report, in `schema check`, the inventory of impure computes with their reasons, and MUST include
  the new load failures in the conformance vectors with stable codes.
- **FR-029**: System MUST emit a structured log event for each hook call and each recompute, containing the document, the
  field, the hook version, the number of attempts and the result, and no field values.

### Key Entities

- **Computed field**: A schema field with no stored value; has a macro (or hook) with a version, a list of declared
  dependencies, an `on_missing` setting, and a declared type.
- **Macro**: A named, versioned, pure behavior. Presets are provided and reserved; custom macros are registered and run
  sandboxed.
- **Dependency graph**: The directed graph over computed fields and their inputs, with a fixed evaluation order and no
  cycles.
- **Impure compute**: A computed field whose hook calls another system; carries a reason, timeout, retry policy, deadline,
  failure mode and the list of tiers or roles allowed to recompute it.
- **Effect log record**: One outcome of an impure computation: the document, the field, the inputs it was computed from,
  the hook version, the value or failure, the number of attempts, and its position in the log. Append-only and separate
  from the ledger.
- **Failure mode**: The choice between rejecting a request and recording a model error when a hook cannot deliver.
- **Computed model error**: A model error for a missing dependency under `on_missing: error`, or for an impure failure,
  attributed like the model errors of feature 001.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For the worked example of docs/SPEC.md 16.1 extended with `displayName` and `label`, 100% of the document
  after the merge, including both computed values and their annotation, matches the expected output exactly.
- **SC-002**: The "HACKED" request and every other write to a computed path, whatever the tier, authority or flag, is
  rejected in 100% of cases, leaves 0 new entries, consumes 0 sequence numbers and changes 0 documents.
- **SC-003**: Across at least 1,000 randomly generated ledgers, deriving a document twice, from a cache and from scratch,
  and on both storage backends gives byte-for-byte identical computed values in 100% of cases.
- **SC-004**: A change to any input is reflected in every computed field that depends on it (directly or through other
  computed fields) on the next read in 100% of cases, and fields that do not depend on it are unchanged.
- **SC-005**: Each schema error of User Story 4 (a cycle of any length, an undeclared dependency, a dependency on a
  deferred field) is reported at load in 100% of cases with a stable code, and no such schema is ever used for a read.
- **SC-006**: A custom macro that tries to use anything outside its declared dependencies, the clock, randomness, the
  network or the filesystem obtains nothing in 100% of attempts, and a macro that exceeds a limit is stopped and reported in
  100% of attempts.
- **SC-007**: For a document with 1,000 accepted entries and at least 5 computed fields, an uncached read completes in
  under 1 second.
- **SC-008**: After an impure computation, 0 hook calls are made by repeated reads, by a rebuild, or by a retry of the
  same request, across at least 100 repetitions; a rebuild reproduces the cached view bit for bit.
- **SC-009**: Under each failure mode, a failing hook produces the specified outcome in 100% of cases: rejection with
  nothing recorded, or acceptance with exactly one model error; and recompute after the cause is fixed clears the model error
  in 100% of cases.
- **SC-010**: A request never holds the storage write lock while a hook runs: a hook that takes 2 seconds does not delay a
  write to another document by more than 100 milliseconds.
- **SC-011**: Every computed path can be explained with a single request, and the answer names the macro or hook, its
  version and the dependency values in 100% of cases.

## Assumptions

- Out of scope for this feature: deferred (external-call, stand-alone) fields and their stored results (Feature 3),
  authoring a library of validation rule types and per-field authority beyond what feature 001 parses (Feature 4),
  snapshots, compaction and time travel (Feature 5), detailed schema evolution (Feature 6), and redaction. Where a
  requirement here mentions redaction, it applies once redaction exists.
- Computed fields are never merged, defaulted, or written by contributors; the rules of feature 001 for derived paths
  (unwritable, left out of merging, shadowing of earlier entries) are unchanged and are what this feature builds on.
- The language or mechanism used to sandbox custom macros is a planning decision; this feature states what a macro may see
  and do and the limits that apply, not how they are enforced.
- The values of the platform default and maximum limits for custom macros are chosen at planning; the timeout, retry and
  deadline of an impure compute are set in the schema, with reasonable platform bounds chosen at planning.
- `label` in the worked example is a custom macro (for example `acme.label`) that joins the name and the city as "Alice S.
  (Paris)"; the exact text the macro produces is the reference output in docs/SPEC.md 16.1.
- The effect log is stored by the same storage as the ledger but is separate from it and from the rejection audit log;
  its default retention (5 superseded outcomes per key, 90 days) follows docs/SPEC.md 9.3.1.
- A hook is a registered behavior, supplied by the deployment like a custom macro; hooks and their credentials are not
  defined by the schema author inside the schema file.
- The list of who may recompute is required on every impure field, so that recovery is an explicit decision; this extends
  the list of required declarations in docs/SPEC.md 9.3.1, which is updated with this clarification.
- A macro fault (an error, a limit exceeded, a wrong result type) is treated as an engine fault, like a validator fault in
  feature 001, and not as a data problem; it is rejected with the new code `COMPUTE_FAULT`.
- The normative worked example in docs/SPEC.md 16.1 (including its computed values) and the model-error example in 16.2 are
  the reference test cases.
