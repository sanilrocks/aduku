# Feature Specification: Deferred Computed Fields

**Feature Branch**: `003-deferred-fields`

**Created**: 2026-10-19

**Status**: Draft

**Depends on**: Feature 001 (core ledger and merge engine, implemented), Feature 002 (computed fields, impure computes, effect
log and dependency graph, implemented); docs/SPEC.md sections 5.3, 9.1, 9.4, 9.5 (deferred fields), 10.5, 12.2 to 12.4,
13 (sandboxing and hooks), 15.1 (derived store), 16 items 6 and 11, and 17 item 5 (operational semantics of `once`).

**Input**: User description: "Deferred computed fields. Add fields populated through IO hooks with refresh policies. Why: Some values need external calls (scores, rates). They must not make the core merge impure or slow. Stories: `once` fields are computed before first fetch and persisted; `per_fetch` fields are computed on read with ttl and fallback; `scheduled` fields refresh in batches across documents; `notification` fields recompute when a topic event arrives; a deferred field may depend on plain, computed and other deferred fields and its hook runs only once its deferred inputs have values (promise); hook failures follow the per-field `on_failure` (error/serve_stale/absent) after retries and a circuit breaker, are reported as deferred status, and never reject a write or create a model error; computed fields, validators and defaults cannot depend on deferred fields (refused at load); pending state holds dependent deferred fields back."

## Clarifications

### Session 2026-10-19

- Q: When a `per_fetch` value is younger than `ttl` but one of its inputs has changed, is it reused or fetched again? → A: Fetched again. A stored value is reused only while its inputs are unchanged and it is younger than `ttl`; a changed input always causes a new call, whatever the `ttl`.
- Q: Who may call `hook.invalidate` and `scheduler.run`, and where is that set? → A: The schema lists, per deferred field, the tiers or roles allowed (`invalidate_by`, required on every deferred field, same vocabulary as `authority` and `recompute_by`). `scheduler.run` is permitted to the union of those for the fields the source serves. Anyone else gets `NOT_AUTHORIZED`, with no call made.
- Q: Does the circuit breaker count failures per source or per deferred field? → A: Per source (hook name and version): one breaker shared by every field and document that uses the source, so an outage stops all of its calls together.
- Q: Is the fetch deadline for the whole read or for each deferred field? → A: For the whole read: one deadline shared by every field fetched during it. A field not finished by then follows its failure policy, and a field waiting on an unfinished upstream is pending.
- Q: When a notification invalidates a stored value, when is the new value fetched? → A: Handling a notification only marks the values out of date. They are recomputed by the next `scheduler.run` for the source (in batches, within its rate limit); a read serves the stored value meanwhile and makes no call for it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A value fetched once and kept (Priority: P1)

A schema author declares a field whose value comes from another system, for example a credit score looked up from the
customer's name, and says it is fetched once. The first time a document is read and the field has no stored value, the
system asks the other system, stores the answer and shows it with the document marked as deferred. Every later read serves
the stored value without asking again, even if the name changes, unless the author has opted that field in to being fetched
again when its inputs change.

**Why this priority**: This is the simplest useful deferred field and the base of the others: a value the core merge
cannot produce, stored apart from the ledger so that the ledger stays pure and replayable.

**Independent Test**: Declare `creditScore` as a `once` field depending on `/name`. Write a name, read the document twice,
change the name, read again. The other system is called once; the same stored value (712) appears each time. Repeat with the
opt-in set: the name change causes exactly one new call.

**Acceptance Scenarios**:

1. **Given** a `once` field with no stored value, **When** the document is read, **Then** the other system is called once,
   the answer is stored, and the document shows the value marked `deferred::` with status `ok`.
2. **Given** a stored value for a `once` field, **When** the document is read again, **Then** no call is made and the same
   value is shown.
3. **Given** a stored `once` value and a changed input, **When** the document is read and the field has no opt-in,
   **Then** the stored value is kept and no call is made.
4. **Given** the same case with `on_dependency_change: recompute` on the field, **When** the document is read after an input
   changed, **Then** one new call is made and the new value replaces the stored one.
5. **Given** a document whose inputs for the field have no value yet, **When** the document is read, **Then** no call is
   made and the field is shown as pending until they have one.
6. **Given** a ledger, **When** the stored deferred values are deleted and the document is rebuilt, **Then** the merged
   document, its computed fields and its model errors are identical to before; only the deferred values and their status
   differ until they are fetched again.

---

### User Story 2 - A value fetched on every read, within a time limit (Priority: P1)

A schema author declares a field, for example an exchange rate, that should be fresh when a document is read, but not so
fresh that every read waits for another system. The field is fetched on read; an answer younger than the field's `ttl` is
reused. A read never waits longer than the fetch deadline: if the other system is too slow or down, the field follows its
failure policy, for example showing the last good value marked stale.

**Why this priority**: This is the freshness side of deferred fields, and the one that touches the read path, so its time
limit and fallback are what keep reads fast.

**Independent Test**: Declare `fxRate` as `per_fetch` with a `ttl` and `on_failure: serve_stale`. Read twice inside the ttl
(one call), read after it (a second call), then make the other system slow and read again: the read ends at the deadline
with the last good rate marked stale.

**Acceptance Scenarios**:

1. **Given** a `per_fetch` field with no stored value, **When** the document is read, **Then** the other system is called,
   the value is shown (`status: ok`), and it MAY be stored as the last good value.
2. **Given** a stored value younger than the field's `ttl` and unchanged inputs, **When** the document is read, **Then** no
   call is made.
3. **Given** a stored value older than the `ttl`, **When** the document is read, **Then** the other system is called again
   and the new value replaces it.
3a. **Given** a stored value younger than the `ttl` but a changed input, **When** the document is read, **Then** the other
   system is called again, because the stored value answers for the old inputs.
4. **Given** a read that asks for fresh data (`consistency: fresh`), **When** the document is read, **Then** the `ttl` is
   ignored and the other system is called.
5. **Given** a slow other system, **When** the document is read, **Then** the read returns by the fetch deadline and the
   field follows its `on_failure` policy.
5a. **Given** a read that needs several deferred values (including a chain), **When** the document is read, **Then** the
   one fetch deadline covers them all: the read returns by the deadline, a field not finished by then follows its
   `on_failure` policy, and a field waiting on an unfinished upstream is pending.
6. **Given** a `per_fetch` field whose last good value is kept, **When** the other system fails and the policy is
   `serve_stale`, **Then** the read shows the last good value with `status: stale`.

---

### User Story 3 - A deferred field that needs another deferred field (Priority: P1)

A deferred field may depend on ordinary fields, on computed fields and on other deferred fields. For example a credit limit
may be looked up from the credit score, which is itself looked up. The limit's source is not asked until the score has a
value. Until then the limit is *pending*, not failed.

**Why this priority**: Without it a deferred field would have to be asked with missing inputs or would force authors to
chain fetches by hand.

**Independent Test**: Declare `creditLimit` depending on `/creditScore` (deferred) and `/country` (plain). Read the document
with the score's source down: the limit is pending and its source is never called. Bring the source up, read again: the
score and then the limit are fetched in that order.

**Acceptance Scenarios**:

1. **Given** deferred field B depending on deferred field A, **When** A has no value, **Then** B's source is not called and
   B is shown as pending.
2. **Given** the same fields, **When** A gets a value, **Then** B's source is called with A's value and the plain and
   computed values it also depends on, and B gets its value.
3. **Given** a chain of deferred fields, **When** all are due, **Then** they are fetched in dependency order, with ties
   broken by path, so the order is the same on every run.
4. **Given** B depending on A, **When** A is later refreshed to a different value, **Then** B follows the refresh policy it
   has (a `once` field B keeps its value unless it opted in to `on_dependency_change: recompute`).
5. **Given** a source for B, **When** it is called, **Then** it receives only B's declared dependencies and metadata, never
   other fields.

---

### User Story 4 - Values refreshed in batches across documents (Priority: P2)

A schema author declares a field that is refreshed on a schedule, for example every hour, for many documents at once. A
reads serves the stored value and never waits for the other system. When the scheduler runs, it gathers the documents that
are due, groups them by source, sends them in batches if the source accepts batches, and respects the source's rate limit.
Every run is recorded.

**Why this priority**: Bulk refresh keeps values fresh without a call per read and without slowing any read.

**Independent Test**: Declare `riskBand` as `scheduled` with an interval and a batch source with `max: 50`. Create 120
documents; run the scheduler. The source receives three calls (50, 50, 20); a second immediate run does nothing; reads never
call the source.

**Acceptance Scenarios**:

1. **Given** documents whose scheduled field is due, **When** the scheduler runs for that source, **Then** one call carries
   up to the source's batch maximum of documents and as many calls are made as needed.
2. **Given** a source with a rate limit, **When** the scheduler runs, **Then** it never exceeds the limit and leaves the
   remaining documents due for the next run.
3. **Given** a run that is repeated, interrupted and started again, **When** it completes, **Then** every due document has
   been refreshed exactly once for that due time (the run is idempotent).
4. **Given** a scheduled field, **When** a document is read, **Then** the stored value is shown and no call is made, even
   if the value is overdue.
5. **Given** any run, **When** it ends, **Then** a record states the source, the documents attempted, how many succeeded,
   failed or were skipped, and when.
6. **Given** a field with combined policies (for example `once` plus `scheduled`), **When** the document is first read,
   **Then** the value is fetched once on that read and refreshed by the scheduler afterwards.

---

### User Story 5 - Values recomputed when an event arrives (Priority: P2)

A schema author links a field to a topic, for example "sanctions list updated". When a notification for the topic arrives,
the stored values of the affected documents are marked out of date. The next scheduler run for the source recomputes
them, in batches and within the source's rate limit. Until then, a read serves the stored value.

**Why this priority**: Some values change when something outside changes, not on a clock or a read; this lets the system
react without polling.

**Independent Test**: Declare `sanctionsFlag` as `notification` on topic `sanctions.updated`. Fetch it for two documents,
send a notification naming one of them (no call is made), then run the scheduler for the source: only that document's
source call is made and only its value changes.

**Acceptance Scenarios**:

1. **Given** a field linked to a topic, **When** a notification for that topic names documents, **Then** only those
   documents' stored values are marked out of date, no source is called while handling it, and the next scheduler run for
   the source recomputes them.
2. **Given** a notification that names no documents, **When** it arrives, **Then** all documents holding a stored value
   for fields linked to the topic are invalidated.
3. **Given** a notification for another topic, **When** it arrives, **Then** nothing changes.
4. **Given** an invalidated value that has not been recomputed yet, **When** the document is read, **Then** the stored
   value is shown and no call is made for it by the notification; other policies of the field still apply as they say.
5. **Given** the same notification delivered twice, **When** the second arrives, **Then** it causes no extra change beyond
   the first (delivery is at least once).

---

### User Story 6 - A failing source never harms the document (Priority: P2)

Another system can be slow, down or wrong. A schema author says per field what should then happen: show an error status
(`error`, the default), show the last good value marked stale (`serve_stale`), or show the field as absent with an error
status (`absent`). Calls have a timeout and are retried a bounded number of times; a source that keeps failing is cut off for
a while (a circuit breaker) so that it is not hammered. A failure never rejects a write and is never a model error: it is a
status shown with the field.

**Why this priority**: An outside outage must never decide what the system accepts or what the core document says.

**Independent Test**: Make a source fail on every call. Write to a document that has the field: the write is accepted with
no model error. Read it: the field shows its failure status according to the policy. Repeat for each policy, then restore
the source and recompute: the status clears.

**Acceptance Scenarios**:

1. **Given** a source that fails after all retries, **When** the policy is `error`, **Then** the field's status is `error`
   with the error and the number of attempts, and the document is otherwise unchanged.
2. **Given** the same failure and `serve_stale` with a last good value, **When** the document is read, **Then** the last
   good value is shown with `status: stale`; with no last good value the status is `error`.
3. **Given** the same failure and `absent`, **When** the document is read, **Then** the field is shown absent with
   `status: error`.
4. **Given** any failure, **When** a contribution is submitted to the document, **Then** it is accepted or rejected exactly
   as if the field did not exist, and no model error is added or removed because of the failure.
5. **Given** a source that has failed repeatedly, **When** the breaker opens, **Then** further calls to that source, for
   any field or document, are not made for the cool-down period and the fields it serves follow their policy; after the
   period one trial call decides whether the breaker closes.
6. **Given** a failed field, **When** a call later succeeds (a manual `hook.invalidate`, a schedule, a notification or a
   changed input where the policy allows it), **Then** the status returns to `ok` and the failure is cleared.
7. **Given** a source whose answer does not conform to the field's type, **When** it is returned, **Then** it is treated as
   a failure, and the bad value is not stored or shown.

---

### User Story 7 - Deferred values can never leak into the core (Priority: P2)

The core document (merge, defaults, computed fields and validation) must stay a pure function of the ledger and the schema.
A schema in which a computed field, a validator or a default reads a deferred field is refused when it loads. Nothing in the
core ever sees a deferred value, and contributions can never write one.

**Why this priority**: This is the guarantee that makes everything else safe; it is mostly established by Features 1 and 2,
and this feature completes and proves it for working deferred fields.

**Independent Test**: Load schemas in which a computed field, a field validator, a document validator and a default read a
deferred field, and schemas with a faulty deferred declaration; each is refused with a stable code. Then run a document
through writes and rebuilds with the deferred store empty, full and changing: the core output never differs.

**Acceptance Scenarios**:

1. **Given** a computed field, a field validator, a document-level rule or a default that depends on a deferred field,
   **When** the schema is loaded, **Then** it is refused with `DEPENDS_ON_DEFERRED`.
2. **Given** a deferred field, **When** a contribution targets its path, **Then** it is rejected with
   `FIELD_NOT_WRITABLE`, as for computed fields.
3. **Given** a deferred declaration with an unknown refresh policy, a missing source, a dependency that is not a declared
   path, a dependency cycle among deferred fields, a `ttl` on a policy that has none, or a field that is also computed,
   **When** the schema is loaded, **Then** it is refused with a stable code naming the field.
4. **Given** any document, **When** its deferred values change or are removed, **Then** the output of merge, computed
   fields, validation and model errors (and of a rebuild of them) does not change.
5. **Given** a deferred field, **When** `explain` is asked about its path, **Then** it says the value is deferred, names the
   source and its version, the inputs it was fetched with, and when (`as_of`).

---

### User Story 8 - Seeing what is pending, and why (Priority: P3)

An operator who reads a document wants to know which deferred fields have no value yet and why: waiting for an input,
waiting for an upstream deferred field, or failed. A read can ask for this, and an operator can force a field to be fetched
again.

**Why this priority**: It makes the deferred stage observable and recoverable, but the earlier stories work without it.

**Independent Test**: Make an upstream deferred field fail and read a document that includes its dependents asking for
pending state. The upstream shows its error, the dependents show pending with the reason "waiting for" the upstream, and
`hook.invalidate` on the upstream followed by a successful call resolves both.

**Acceptance Scenarios**:

1. **Given** a read that asks for pending state, **When** a deferred field has no value yet, **Then** the answer lists it
   with its status and, if pending, what it is waiting for.
2. **Given** an upstream deferred field in `error` with no stale value to serve, **When** a dependent is due, **Then** the
   dependent stays pending and its source is not called.
3. **Given** an upstream field serving a stale value (accepted stale), **When** a dependent is due, **Then** the dependent's
   source is called with the stale value.
4. **Given** `hook.invalidate(doc, field)`, **When** it is called by a caller whose tiers or roles the field lists in
   `invalidate_by`, **Then** the stored value is marked out of date and the next fetch under the field's policy
   recomputes it.
5. **Given** a caller outside that list, **When** it calls `hook.invalidate` or `scheduler.run`, **Then** the request is
   refused with `NOT_AUTHORIZED`, no source is called and nothing is changed; `scheduler.run` refreshes only the fields the
   caller may invalidate.

---

### Edge Cases

- A `once` field whose inputs have no value yet is pending, not an error, and is fetched on the first read after they do.
- A source that answers with no value or with null is a failure (kind `bad_value`), not an empty value: a stored value is
  never null, as for impure computes in Feature 2. A source that has no value for a document fails for it.
- Two readers ask for the same `once` or `per_fetch` value at the same moment: the source is called once and both receive
  the result (as for impure computes in Feature 2).
- A source that answers after the read's deadline: the answer is stored for later reads if it is valid, and the read that
  timed out has already followed its failure policy.
- A schema change that alters a deferred field's source version or dependencies: values stored for the old version are not
  reused, and the field is fetched again under its policy.
- A document with no entries yet or an empty document has no deferred fields fetched; fields whose inputs are absent are
  pending.
- A time-travel read of an earlier state (when it exists) never calls a source; it shows no deferred values.
- A deferred field that depends on a computed field that is itself missing or null follows the field's `on_missing`
  setting: it is pending, shown as null, or shown as absent, and its source is not called.
- A deferred field that depends on an impure computed field uses the recorded impure value, like any computed field.
- A notification for a document that does not exist is ignored and recorded.
- A scheduler run that overlaps another run for the same source does not double-call: one wins, the other skips.
- Rebuild of all documents, with deferred values present or absent, makes no source call.
- A source with a rate limit that is lower than the number of due reads on a `per_fetch` field: the excess reads follow
  the field's failure policy for that read (the call is not made and counts as a failed attempt of kind `rate_limited`),
  without delaying beyond the deadline.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST evaluate deferred fields (phase P5) outside the pure chain: they read the output of P1 to P4 and
  write only to the derived store; nothing in admission, merge, computed fields, validation or projection reads a deferred
  value.
- **FR-002**: System MUST call a deferred field's source with only the values of its declared dependencies and call
  metadata (document, field, attempt number, deadline). The source MUST be idempotent; the system MUST NOT pass any other
  field.
- **FR-003**: System MUST check that a source's answer conforms to the field's declared type; an answer that does not, or
  that is null or absent, is a failure and is neither stored nor shown.
- **FR-004**: System MUST keep a derived store record per document and deferred field with the value, the time it is as of,
  the fingerprint of the inputs it was computed from, the source and its version, the schema version, a status (`ok`,
  `stale`, `pending` or `error`), the error if any, and the number of attempts.
- **FR-005**: System MUST support the refresh policy `once`: when no stored value exists and the field's inputs have
  values, fetch it before the document is first returned, store it, and thereafter serve the stored value without a call.
- **FR-006**: System MUST keep a stored `once` value when its inputs change, unless the field declares
  `on_dependency_change: recompute`, in which case a changed input fingerprint causes one new call (docs/SPEC.md 17 item 5).
- **FR-007**: System MUST support the policy `per_fetch`: on a read, reuse a stored value only if its inputs are unchanged
  and it is younger than the field's `ttl`, otherwise call the source (a changed input always causes a call); a read that asks for fresh data ignores the `ttl`. The value MAY be kept as the last good value
  (declared per field).
- **FR-008**: System MUST NOT let deferred calls make a read exceed the fetch deadline, which covers the whole read and
  every field fetched during it (not each field separately); on timeout the unfinished fields follow their failure policy
  (a field waiting on an unfinished upstream is pending) and the read returns.
- **FR-009**: System MUST support the policy `scheduled` (an interval or a cron expression): a scheduler run refreshes the
  stored values of all due documents; a read serves the stored value and makes no call.
- **FR-010**: System MUST, in a scheduler run, group due documents by source, honor the source's rate limit and, for a
  source that declares `batch: { max: N }`, send up to N documents per call; a run MUST be idempotent (repeating or
  resuming it never refreshes a document twice for the same due time) and MUST record every run.
- **FR-011**: System MUST support the policy `notification`: a field is linked to a topic; a notification marks out of
  date the stored value of the documents it names, or of all documents holding a value if it names none, and makes no
  call itself; the next scheduler run for the source recomputes them in batches within its rate limit. Handling a
  notification MUST be safe to repeat.
- **FR-012**: System MUST allow policies to be combined for one field (for example `once` and `scheduled`), each applying as
  its own rules say.
- **FR-013**: System MUST allow a deferred field to depend on plain, computed (including impure) and other deferred fields,
  and MUST call its source only when every deferred field it depends on has a value (`ok`, or `stale` that is accepted);
  until then its status is `pending`. Dependencies on plain and computed fields are read from the P1 to P4 output.
- **FR-014**: System MUST process deferred fields in dependency order, with ties broken by path in code-point order, so
  that the order is the same on every run; a source MUST never run with an unresolved deferred input.
- **FR-015**: System MUST keep a dependent deferred field `pending` while an upstream deferred field is in `error` with no
  stale value to serve; a dependent MUST run with an upstream value served stale.
- **FR-016**: System MUST apply the field's `on_missing` rule to a deferred field whose
  computed input is missing: its source is not called, and the field is shown pending, null or absent as `on_missing` says.
- **FR-017**: System MUST give each source a timeout, a bounded number of retries with backoff and a circuit breaker, one per
  source and shared by all the fields and documents that use it, that stops calls to a failing source for a cool-down
  period and then allows one trial call; the retry envelope belongs to the
  deferred stage, not to the source.
- **FR-018**: System MUST apply the field's `on_failure` after retries are exhausted: `error` (the default) sets status
  `error`; `serve_stale` shows the last good value with `status: stale` (status `error` if there is none); `absent` shows
  the field absent with `status: error`. A document-type default MUST apply to fields that declare none.
- **FR-019**: System MUST NOT reject a contribution because of a deferred field or its failure, and MUST NOT create,
  remove or change a model error because of a deferred value or its status; a deferred failure is only a status held in the
  derived store and shown with the field.
- **FR-020**: System MUST clear a failure status when a later call succeeds (through `hook.invalidate`, a schedule, a
  notification, or a changed input where the policy recomputes).
- **FR-021**: System MUST refuse at schema load, with stable codes naming the field: a computed field, field validator,
  document-level rule or default that depends on a deferred field (`DEPENDS_ON_DEFERRED`, already enforced); a deferred
  field that is also computed; a deferred field with a `merge`, a `default` or a validator; an unknown refresh policy; a
  source that is not registered; a dependency that is not a declared path; a dependency cycle among deferred fields; a
  policy option that does not belong to the policy (for example a `ttl` on `once`); and a deferred field with no
  `invalidate_by`.
- **FR-022**: System MUST keep deferred paths unwritable by contributions (`FIELD_NOT_WRITABLE`), as Feature 1 does.
- **FR-023**: System MUST show stored deferred values in the response marked `deferred::`, with their status, overlaid at
  projection; they MUST NOT be stored in the ledger, the cached merged document or the model errors.
- **FR-024**: System MUST keep the output of merge, computed fields, validation and model errors identical whether the
  derived store is empty, full or changing, and a rebuild of them MUST NOT depend on the derived store or call a source.
  Deferred status is not part of rebuild equality.
- **FR-025**: System MUST provide `hook.invalidate(doc?, field)` to mark stored values out of date, and
  `scheduler.run(source)` to run due refreshes for a source. Both are permitted only to callers whose tiers or roles are
  among those the schema lists for the field (`invalidate_by`, required on every deferred field, the same vocabulary as
  `authority`); otherwise `NOT_AUTHORIZED`, with no call and no change. `scheduler.run` covers only the fields the caller
  may invalidate.
- **FR-026**: System MUST let a read ask for pending state (`include: pending`) and answer, for each deferred field without
  a value, its status and what it is waiting for.
- **FR-027**: System MUST give `explain` a meaningful answer for a deferred path: that it is deferred, the source and its
  version, the inputs it was computed from, and the time it is as of.
- **FR-028**: System MUST emit a structured log event for each source call (document, field, source, attempts, result), each
  circuit breaker change, each scheduler run and each notification handled; the event MUST NOT contain the value.
- **FR-029**: System MUST call a source once for concurrent requests that need the same value at the same moment.
- **FR-030**: System MUST treat a changed source version, changed dependencies or a changed type of the field (for example through
  a new schema version) as making the stored value not reusable: it is fetched again under the field's policy. A new
  schema version that leaves the field's declaration as it is keeps the stored value (a `once` value never changes just
  because the schema was versioned).
- **FR-031**: System MUST allow sources (hooks) to be supplied by the deployment as registered behaviors, with their
  credentials and network allow-lists outside the schema file, run outside the pure sandbox with their own limits and audit
  logging (docs/SPEC.md 13 item 3).
- **FR-032**: System MUST add conformance vectors and tests for the cases above, including the worked example in
  docs/SPEC.md 16.1 (`creditScore` once → 712, `fxRate` per_fetch → 0.92), each written and seen to fail before the
  behavior is built.

### Key Entities

- **Deferred field**: A schema field with no contributed value, whose value comes from a source; has a source and version,
  dependencies, one or more refresh policies, a type, an `on_failure` policy, an `on_missing` setting, a timeout and the
  tiers or roles allowed to invalidate it.
- **Source (hook)**: A registered, versioned, idempotent behavior that may do IO; receives only declared dependencies and
  metadata and returns a value or an error.
- **Refresh policy**: `once`, `per_fetch` (with `ttl`), `scheduled` (interval or cron) or `notification` (a topic); may be
  combined.
- **Derived store record**: The stored result for one document and field: value, as-of time, input fingerprint, source
  version, schema version, status, error and attempts.
- **Status**: `ok`, `stale`, `pending` (waiting for inputs or upstream deferred fields) or `error`.
- **Failure policy**: `error`, `serve_stale` or `absent`, per field with a document-type default.
- **Circuit breaker**: Per-source state (one per source name and version, shared by all its fields) (closed, open, trial) that stops calls to a failing source for a cool-down.
- **Scheduler run**: One execution of due refreshes for a source, with a record of what it attempted and what happened.
- **Notification**: An event naming a topic and optionally documents, which invalidates linked fields.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In 100% of cases, a document's merged output, computed fields, validation results and model errors are
  identical with the derived store empty, full or changing, and a rebuild of a corpus makes zero source calls.
- **SC-002**: With a source that is down or slow for every call, 100% of contributions are accepted or rejected exactly as
  with the field removed, and no model error appears or disappears.
- **SC-003**: A read that includes a `per_fetch` field returns within the fetch deadline plus a small fixed margin in 100%
  of cases, including when the source never answers.
- **SC-004**: A stored `once` value is served with zero calls on every read after the first, and an input change causes
  zero new calls without the opt-in and exactly one with it.
- **SC-005**: A source is never called while a deferred input it depends on has no value, in 100% of runs, and a chain of
  deferred fields is fetched in the same order on every run.
- **SC-006**: A scheduled refresh of N due documents for a batch source with maximum M makes exactly ceil(N/M) calls, a
  repeated run makes zero, and no read makes any.
- **SC-007**: A notification for a topic marks out of date only the documents it names (or all linked documents if it names
  none) and makes zero source calls itself; the next scheduler run recomputes exactly those; the same notification
  delivered twice produces the same final state as once.
- **SC-008**: After a source has failed beyond the breaker's threshold, calls to it stop within one call of the threshold
  and resume with one trial call after the cool-down, in 100% of runs.
- **SC-009**: Every schema in which a computed field, validator or default reads a deferred field, and every faulty deferred
  declaration, is refused at load with a stable code in 100% of cases, and no such schema is ever used for a read.
- **SC-010**: The `creditScore` (once → 712) and `fxRate` (per_fetch → 0.92) values of docs/SPEC.md 16.1 appear marked
  `deferred::` with status `ok`, and the rest of that document is unchanged.
- **SC-011**: For concurrent requests needing the same value, the source is called once in 100% of cases.

## Assumptions

- Out of scope: computed and impure-compute fields (Feature 2, done), the library of validation rule types and per-field
  authority (Feature 4), snapshots, compaction and time travel (Feature 5), schema evolution (Feature 6), cross-document
  derivation (docs/SPEC.md 17 item 1) and redaction. Where a requirement mentions redaction, it applies once it exists.
- `once` keeps its stored value when inputs change unless `on_dependency_change: recompute` is set; this is the decision in
  docs/SPEC.md 17 item 5, so no open question remains.
- The system does not run a timer by itself: a deployment's job runner calls `scheduler.run(source)` (for example from a
  cron job) and delivers notifications; the system decides what is due, in what batches and in what order, and records the
  run. Time enters only through the clock the deployment supplies, and never reaches phases P0 to P4 and P6.
- A notification is delivered at least once by the deployment; the system makes handling it safe to repeat. A notification
  that names no documents applies to all documents that hold a value for a linked field.
- Hooks are registered behaviors supplied by the deployment, as in Feature 2; their credentials, egress allow-lists and
  rate limits are set outside the schema file. A source's own rate limit and batch maximum are declared at registration.
- Default values for the platform bounds (timeout, retries, backoff, circuit breaker threshold and cool-down, fetch
  deadline) are chosen at planning; the per-field values live in the schema, within the platform bounds.
- The effect log of Feature 2 is for impure computes only; deferred results live in the derived store, which is a separate
  store with its own retention (the latest record per document and field is kept for as long as the document exists).
- `explain` for a deferred path reads the derived store; it never calls a source.
- A notification marks the stored values of the fields linked to its topic in the active schema; documents pinned to another
  schema version are not marked until they are moved to one that declares the topic.
- The mark that says "out of date" is one flag per stored value. A mark made while a source call is running on a clean value is
  kept when the answer is stored; a mark made while the call that answers an earlier mark is running is folded into that
  answer (it cannot be told apart from the earlier mark).
- The derived store keeps the rows of a field that a later schema version removes; they are never read and are not cleaned up
  in this feature.
- The normative worked example in docs/SPEC.md 16.1 is the reference test case for the deferred values.
