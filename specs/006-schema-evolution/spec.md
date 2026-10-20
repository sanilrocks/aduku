# Feature Specification: Schema Versioning and Evolution

**Feature Branch**: `006-schema-evolution`

**Created**: 2026-10-20

**Status**: Draft

**Depends on**: Features 001 to 005, all implemented; docs/SPEC.md sections 14 (versioning and evolution), 10.4 (re-evaluation
on a schema change), 9.2 (a field that becomes derived), 4.4 (tiers) and 15.3 (compaction and schema versions).

**Input**: User description: "Schema versioning and evolution. Schemas change; merged results must remain reproducible and breaking changes safe. Stories: immutable versions and activation; breaking-change classification with a dry-run diff and rebuild; per-document schema pinning and migration; shadowing of entries made obsolete by a schema change, with a recorded reason. Not yet there: no classification of what changed between two versions, no dry-run that shows how documents would change before a version is activated, no way to move a pinned document to a new version, no recorded reason for a skipped entry, and no schema diff in the CLI."

## Clarifications

### Session 2026-10-20

- Q: Do the active version and each document's pin live in the shared store, or does each process choose its own? → A: The shared store holds them. An activation, pin, unpin or migration takes effect for every process on its next request or read; the application's schema files only have to contain the versions. A process whose files lack the active version stops serving with a clear refusal naming the missing version.
- Q: When a version tightens a type or constraint, are non-conforming entries shadowed or is the change refused? → A: Shadowed, with the reason "no longer conforms"; the change is classed breaking, needs the acknowledgement, and the change list and dry run count the entries. It is never refused for that.
- Q: Who may activate, roll back, pin or migrate? → A: Any operator with access to the library or command line, who must give an operator name that is recorded in the history. The schema lists nobody; access is the deployment's concern, as for `rebuild` and `compact`.
- Q: Does an unlimited dry run examine every document or a default sample? → A: Every document, unless a limit or a list of documents is given; the report always states how many documents it examined and how many it left out.
- Q: If one document cannot be derived during an eager rebuild after an activation, does the activation stand? → A: Yes. The activation is recorded and stands; the rebuild reports the documents it could not derive and continues with the rest. A rollback is a separate, explicit activation.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Versions stay immutable, and moving between them is a recorded act (Priority: P1)

A schema author publishes version 7 of a customer schema, an operator activates it, and months later version 8. Every entry
records the version it was admitted under, and a document is always a pure function of its ledger and one version. This story
makes that promise survive restarts and mistakes: the system remembers a fingerprint of each version it has served, so that
version 7 can never silently mean something else the next time a process starts. Activating a version, activating an older one
again to roll back, pinning a document and migrating it are all **recorded acts**: who did them, when, from which version to
which, and how risky the change was. The record can be listed. A rollback is simply activating the older version again;
nothing in the ledger changes, and the documents read as they did before.

**Why this priority**: Reproducibility is the reason for the ledger. If a version could change under a running system, no
document could be trusted and no rollback could be exact.

**Independent Test**: Register version 7, activate it, write entries, restart with the same file: everything works. Restart with a
changed version 7: it is refused naming the difference. Activate version 8, then activate 7 again: the documents read exactly
as they did under 7, and the history lists three acts.

**Acceptance Scenarios**:

1. **Given** version 7 was served before, **When** a process starts with different content under the number 7, **Then**
   registering it is refused with a stable code that names the first differences, and nothing is served from it.
2. **Given** an active version 7, **When** an operator activates version 8, **Then** new requests are admitted under 8, entries
   already recorded keep their version 7, and the act appears in the history with the operator's name and the class of change.
3. **Given** version 8 is active and a document read under it, **When** version 7 is activated again, **Then** the document
   reads exactly as it did when 7 was active, and the ledger is byte-identical before and after.
4. **Given** version 8 added a tier at the top and entries now use it, **When** version 7 (without the tier) is activated,
   **Then** it is refused (`TIER_IN_USE`), as before this feature.
5. **Given** an activation that fails halfway, **When** it ends, **Then** the active version and the history are as they were.
6. **Given** a request being admitted while an activation happens, **When** both finish, **Then** the request was admitted
   wholly under one version, and its entries say which.

---

### User Story 2 - See how risky a change is, and what it would do, before activating it (Priority: P1)

Before moving from version 7 to 8 an author asks what changed. The system answers with a list of changes, each with its **class**
as in docs/SPEC.md section 14: *compatible* (nothing changes for existing documents), *visible* (documents change on read, for
instance a default changed or a rule was tightened, and model errors can appear) or *breaking* (folds change, entries may become
unusable, tiers are reordered, a field becomes derived), and an overall class that is the worst of them. Then a **dry run**
answers the question that matters: for the documents that exist, how would the merged document, the model errors and the
skipped entries change under the new version? The dry run reads only; it writes nothing, calls no hook or source, and can be
limited to a sample or to some documents so that it stays quick on a large store. Activating a *breaking* version requires the
operator to say they have seen it is breaking; the system refuses otherwise. After the activation the documents re-derive
on their next read, or the operator can ask for an eager rebuild that refreshes every saved layer and view and reports what
differed.

**Why this priority**: A schema change is the one operation that can change the meaning of data already stored. Showing its
consequences first is what makes it safe to do.

**Independent Test**: Take two versions that differ by one change of each class. The change list names each with the right class
and the overall class is breaking. Run the dry run over 100 documents; then activate and read the same documents: each document,
its model errors and its skipped entries are exactly what the dry run said, and the dry run changed no byte of storage.

**Acceptance Scenarios**:

1. **Given** two versions that differ by an added field, a changed default, a changed merge rule and a field that became derived,
   **When** their changes are listed, **Then** each is named with its path or field, its class (compatible, visible, visible,
   breaking), and the overall class is breaking.
2. **Given** two versions that differ in a way the SPEC 14 table does not name, **When** they are compared, **Then** the change
   is listed as *visible* with the words "not classified" (a conservative default), never dropped.
3. **Given** a stored set of documents, **When** a dry run is asked for a target version, **Then** it reports per document
   whether the merged document, the model errors or the skipped entries would change, with counts and the first differences, and
   it writes nothing and calls no hook or source (a document that would need an impure hook call is reported as "would call
   a hook once").
4. **Given** a dry run limited to a sample or to named documents, **When** it runs, **Then** it examines only those and says
   how many documents it left out.
5. **Given** a breaking target version, **When** an operator activates it without acknowledging the class, **Then** the
   activation is refused; with the acknowledgement it proceeds.
6. **Given** a compatible target version, **When** it is activated, **Then** no per-document work happens at activation.
7. **Given** an activation with an eager rebuild, **When** it finishes, **Then** every document's view and saved layers are
   current under the new version and the report lists the documents whose answer changed.
8. **Given** a dry run followed by an activation, **When** documents are read, **Then** each equals what the dry run predicted.

---

### User Story 3 - Hold a document on its version and move it when ready (Priority: P2)

Some documents cannot move yet: a customer's contract data is validated under last year's schema until a review is done. An
operator **pins** a document to a version. It reads and is written under that version while everything else follows the active
one. When the review is done the operator **migrates** it: a dry run for that one document shows what changes, and then the
document moves to a chosen version (usually the active one) and is unpinned. Migration can be done in batches over many
documents, reports progress, can be stopped and resumed, and a failure on one document does not stop the others. Pins and
migrations are recorded acts like activations.

**Why this priority**: Pinning exists in SPEC 14 but is only a switch; without a planned way off it, pinned documents are
stranded.

**Independent Test**: Pin 50 documents to version 7, activate 8, and write to a pinned and an unpinned document: the first is
admitted under 7, the second under 8. Migrate the 50 in batches of 10: after each batch the migrated ones read under 8, the rest
under 7, and the history lists each act.

**Acceptance Scenarios**:

1. **Given** a document pinned to version 7 with 8 active, **When** it is read and written, **Then** both use version 7.
2. **Given** a pin to a version that would drop a used tier or fold differently from expired compacted history, **When** it is
   attempted, **Then** it is refused for the same reasons as an activation.
3. **Given** a pinned document, **When** a single-document dry run to version 8 is asked, **Then** it shows the document's
   changes and nothing is written.
4. **Given** a batch migration over documents, **When** one of them cannot be derived under the target, **Then** it is reported
   as failed and left on its old version, and the others move.
5. **Given** a batch migration stopped partway, **When** it is run again, **Then** the documents already moved are not touched
   again and the rest move.
6. **Given** an unpinned document, **When** it is migrated, **Then** the request is refused (there is nothing to migrate: it
   follows the active version).

---

### User Story 4 - Know why an entry is not part of the document (Priority: P2)

When a version cannot use an entry, the entry stays in the ledger and is left out of the document. Today a reader only sees its
sequence number in a "skipped" list. This story gives every such entry a **reason**, shown with the document, in `explain`
(as shadowed) and in the dry run: the entry's tier is not in the schema; its field became derived (computed or deferred); its
value no longer conforms to a changed type or constraint; or its value cannot be merged with the others by the rule now in force.
The reason is worked out from the ledger and the version each time; it is not stored, so it can never disagree with them. A
change that would shadow entries says so in the change list and the dry run counts them.

**Why this priority**: A silent omission looks like data loss. A named reason turns it into an explainable consequence of a
chosen change.

**Independent Test**: In a ledger with one entry of each kind, read under a version that shadows all four: the skipped list names
each entry with its reason; `explain` of the field names it as shadowed with the same reason; reading under the older version
shows none skipped.

**Acceptance Scenarios**:

1. **Given** an entry written to a field that a later version makes computed, **When** the document is read, **Then** the entry
   is listed as skipped with the reason "now derived" and the field's kind.
2. **Given** an entry whose tier a later version no longer has (only possible for a pinned document, since activation refuses
   it), **When** read, **Then** the reason is "tier not in the schema".
3. **Given** a version that tightens a field's type or constraint, **When** documents are read, **Then** entries that no longer
   conform are skipped with the reason "no longer conforms" and the first thing that failed, and entries that conform are not.
4. **Given** two tiers whose values the rule now in force cannot merge, **When** read, **Then** the entry that could not be
   merged is skipped with the reason "cannot be merged".
5. **Given** the same ledger read under the version that wrote it, **When** read, **Then** nothing is skipped.
6. **Given** a change that shadows entries, **When** the change list is read, **Then** it says which reasons the change can give;
   **When** a dry run is read, **Then** it shows the number of entries per reason.

---

### Edge Cases

- **A version registered twice with the same content** (a restart): a no-op. With different content: refused (scenario 1.1).
- **Two processes with different schema files**: the one whose version content differs from what was served is refused at start;
  the history shows which fingerprint was served.
- **A version that names a behavior this process has not registered**: refused at registration with the missing name, as today;
  the change list cannot be made for it.
- **Version numbers need not grow**: activating a lower number is a rollback and is allowed (it is recorded as such).
- **Compacted history** (Feature 5): a change that folds differently is classed breaking and, as a target, is subject to the
  `HISTORY_COMPACTED` refusal of Feature 5; the dry run reports that refusal rather than a document it cannot derive.
- **Impure and deferred fields**: a change to a hook, its inputs or a deferred policy is classed visible; the dry run says which
  documents would call a hook again; it never calls one.
- **Authority and `purge_by` changes** apply to requests and purges from then on; they change no existing document and are
  classed visible (SPEC 14).
- **Very large stores**: a dry run without a limit examines every document and says so; with a limit it samples
  deterministically (the same limit gives the same documents), so two runs are comparable.
- **A document with no entries** has nothing to differ and is counted as unchanged.
- **Activation during a dry run**: the dry run reports the version it compared against; its result is a statement about that
  version, not about whatever is active when it is read.
- **A change list for the same two versions** is the same each time, in a fixed order.

## Requirements *(mandatory)*

### Functional Requirements

**Immutable versions and the history (story 1)**

- **FR-001**: The system MUST remember, durably, a fingerprint of the content of every schema version it has served, and MUST
  refuse to serve a version whose content differs from its remembered fingerprint, naming the first differing fields (up to
  five) or the top-level settings. A version counts as served from its first activation or pin; one only registered stays editable.
- **FR-002**: The active version and each document's pin MUST be kept in the shared store, so every process reads and writes under
  the same ones. Activating a version, pinning a document, unpinning it and migrating it MUST each be recorded in a history that
  can be listed: what was done, to which version or document, from which version, by whom (an operator name that is required),
  when, and the class of change.
- **FR-003**: Activating a version MUST be atomic with its history entry and with the check that it does not drop a used tier or
  fold differently from expired compacted history; a failure leaves the active version and the history unchanged.
- **FR-004**: Activating an older version again (rollback) MUST be allowed whenever those checks pass, MUST change no entry, and
  MUST make documents read as they did when that version was last active, byte for byte.
- **FR-005**: Every request MUST be admitted wholly under one version and record it on each of its entries; an activation that
  happens meanwhile MUST NOT split a request.

**Classification and dry run (story 2)**

- **FR-006**: The system MUST list the changes between two versions, each with the field or path it concerns, a short
  description, and its class (compatible, visible, breaking) according to docs/SPEC.md section 14, and MUST give the overall
  class as the worst of them. The list MUST be the same, in the same order, each time for the same two versions.
- **FR-007**: A change the classification does not name MUST be listed as visible and marked "not classified"; none may be
  dropped.
- **FR-008**: A dry run MUST report, for each examined document, whether the merged document, the model errors and the skipped
  entries would differ under a target version, with counts and the first differences, and MUST report how many documents it
  left out.
- **FR-009**: A dry run MUST write nothing (no view, layer, history entry or derived value), MUST NOT call a hook or a deferred
  source, and MUST report a document that would need an impure hook call as such instead of calling it.
- **FR-010**: A dry run MUST accept a limit or a list of documents; a limit MUST pick the same documents each time.
- **FR-011**: The documents a dry run reports as unchanged MUST read unchanged after the activation, and the ones it reports as
  changed MUST read exactly as reported.
- **FR-012**: Activating a breaking version MUST be refused unless the operator acknowledges that it is breaking; activating a
  compatible or visible one MUST NOT need it. A refusal MUST name the class and the first breaking changes. A rollback (FR-004)
  needs no acknowledgement: it is allowed whenever the tier and compaction checks pass, and its class is still recorded.
- **FR-013**: Activation MUST NOT touch documents (they re-derive on their next read). An operator MAY ask for an eager
  rebuild with the activation; it MUST refresh every saved layer and view under the new version, report the documents whose
  answer changed and those that could not be derived, and MUST NOT write to the ledger. The activation stands whatever the
  rebuild finds; a rollback is a separate activation.

**Pinning and migration (story 3)**

- **FR-014**: A pinned document MUST be read and written under its pinned version, and a pin MUST be refused for the reasons an
  activation is.
- **FR-015**: The system MUST offer a dry run for one document and a migration of a document to a chosen version (the active one
  by default) that unpins it, records the act, and leaves its entries as they were.
- **FR-016**: A batch migration MUST work over a list of documents or all pinned ones, report progress, continue past a document
  that cannot be derived under the target (reporting it and leaving it on its version), and be safe to run again after a stop
  without touching what already moved.
- **FR-017**: Migrating a document that is not pinned MUST be refused.

**Reasons for shadowed entries (story 4)**

- **FR-018**: Every entry left out of a document because the version in force cannot use it MUST have a reason, one of: tier not
  in the schema; field now derived (computed or deferred); no longer conforms to the type (including a parameterized one such as a list of strings; a validation rule is a model error,
  not a shadow); cannot be merged with
  the other values. The reason MUST be shown with the document's skipped entries, in `explain` as shadowed, and in the dry run.
- **FR-019**: Reasons MUST be worked out from the ledger and the version each time and MUST NOT be stored, so that they cannot
  disagree with them; reading under the version that wrote the entries MUST skip none.
- **FR-020**: A dry run MUST count the entries a change would shadow, per reason; the change list names which of its changes can
  shadow entries (and for which reasons), since it is worked out from the two schemas alone.

**Cross-cutting**

- **FR-021**: No merged result, model error, provenance answer or rejection of Features 001 to 005 MAY change: documents under a
  version are exactly what they are today; the conformance vectors of Features 001 to 005 pass unchanged.
- **FR-022**: Structured logs MUST record activations, pins, migrations, refused activations and dry runs (without values).
- **FR-023**: Every operation MUST be available through the library and the command line with the same results, including
  listing changes between two schema files without any store.

### Key Entities

- **Schema version**: an immutable schema content with a number; remembered by fingerprint once served.
- **Change**: one difference between two versions, with its path or field, description and class.
- **Change class**: compatible, visible or breaking (docs/SPEC.md 14); the overall class of a change list is the worst.
- **Dry-run report**: per examined document, what would change under a target version; plus counts and the documents left out.
- **History record**: an activation, rollback, pin, unpin or migration with who, when, from and to, and the class.
- **Pin**: a document's fixed version until migrated.
- **Shadow reason**: why an entry is not part of a document under a version (the four reasons of FR-018).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For every pair of versions in the shared vectors, the change list names every difference of the SPEC 14 table with
  the right class, the overall class is the worst, and a difference outside the table is listed as not classified (100%).
- **SC-002**: On at least 1,000 random pairs of versions and ledgers, the dry run's prediction of each document, its model errors and
  its skipped entries equals what is read after activating the target (100%), and a dry run changes no stored byte and makes no
  hook or source call.
- **SC-003**: Activating and then re-activating an earlier version returns every document to a byte-identical read, over at least
  1,000 random ledgers, with the ledger byte-identical throughout.
- **SC-004**: A restart with different content under an already-served version number is refused in every case tried, and a restart
  with identical content is never refused.
- **SC-005**: Activating a compatible version takes the same time for a store of 100,000 documents as for one of 100 (it touches no
  document), and a dry run limited to 1,000 documents takes no more than a tenth of an unlimited one over 10,000.
- **SC-006**: Every skipped entry in the vectors and in random ledgers has exactly one of the four reasons, and none is skipped when
  read under the version that wrote it.
- **SC-007**: A batch migration of 1,000 pinned documents stopped and restarted moves each once and leaves none half moved.

## Assumptions

- Versions are supplied by the application (a schema file or text); the system remembers each served version's fingerprint, the
  active version, the pins and the history in the shared store, not the schema text itself. Keeping the text is the deployer's concern (a repository).
- Activation, pinning, unpinning, dry runs and migration are **operator** operations like `rebuild` and `compact`: they take an
  operator name for the record and are not reachable by contributors. The schema does not list who may use them.
- Entries are never rewritten by a schema change; classes and reasons are always about what the current version does with them.
- A tightened type or constraint shadows entries that no longer conform, with a reason, rather than refusing the change (the
  SPEC 14 alternative); the dry run shows how many before anyone activates.
- Per-field precedence and multi-region sequence numbers (SPEC 17 items 2 and 6) are out of v1 and out of this feature.
- Reading a document under a version other than its own for comparison (a dry run) uses the same derivation as a read, without
  storing anything.
- The conformance vectors gain version-change cases; the Python port's CLI gains `schema diff`, `schema plan`, `schema activate`,
  `schema history`, `pin` and `migrate`.
