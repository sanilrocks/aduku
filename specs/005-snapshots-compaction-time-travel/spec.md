# Feature Specification: Snapshots, Time Travel, Compaction and Purge

**Feature Branch**: `005-snapshots-compaction-time-travel`

**Created**: 2026-10-20

**Status**: Draft

**Depends on**: Features 001 to 004, all implemented; docs/SPEC.md sections 4 (the ledger), 9.3.1 (effect log), 9.5 (derived
store), 12.2 (read, `as_of`), 12.4 (admin), 15 (storage, snapshots, compaction, time) and 16 item 6 (replay).

**Input**: User description: "Storage, snapshots, compaction and time travel. Make reads fast and history manageable. Why: Replaying the entire ledger on every read does not scale, but results must stay identical. Stories: per-tier snapshots with watermarks and incremental invalidation; `rebuild` reproduces snapshots exactly; `as_of` reads by seq, tx_time or valid_time; compaction preserves the merged result and provenance, with non-idempotent behaviors compacted only with their idempotency keys and never touching the effect log or the derived store; auditable purge for legal erasure, a recorded operation distinct from a tombstone."

## Clarifications

### Session 2026-10-20

- Q: In a valid-time read, does an entry with no `valid_time` take part? → A: Yes, always, as if true from the beginning. `tx_time` never influences a valid-time read; only entries that state a `valid_time` after the asked moment are left out.
- Q: Does purge include crypto-shredding? → A: No. Physical removal only; crypto-shredding is future work and can be added without changing the purge requirements, which are stated by outcome.
- Q: A purge targets a path whose originals were expired from the archive; erase or refuse? → A: Erase the whole compacted contribution at that path; the purge log says so. An expired archive never blocks erasure.
- Q: Can one `as_of` read combine two points (for example tx_time and valid_time)? → A: No. Exactly one point; two are refused as ambiguous. Combining them is future work.
- Q: Do archived originals expire only on operator action? → A: Yes. Expiry is an explicit, recorded operator action; automatic expiry by age or count is future work.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Reads stay fast as the ledger grows (Priority: P1)

A document that has collected a hundred thousand entries over years is read as quickly as a young one. The system keeps, for
each document and each tier, a saved folded layer together with the position in the ledger it covers (its **watermark**).
When a read comes in, or after a restart, the system takes the saved layers, folds in only the entries newer than each
watermark, and merges. When a contribution is recorded at one tier, only that tier's layer and what is derived from the
touched paths need work; the other tiers' layers are reused as they are. Nobody has to know the layers exist: the document,
its model errors, its status and every `explain` answer are exactly what a replay of the whole ledger would give.

**Why this priority**: Replaying the whole ledger on each read does not scale, and without this nothing else in this feature
matters. It is also the riskiest change, because a wrong saved layer would silently give wrong documents; that is why it is
paired with story 2.

**Independent Test**: Build a document with 100,000 entries across three tiers. Read it, record one entry at one tier, read
again, restart the service and read again. Each read equals a fresh derivation from the ledger alone, byte for byte, and the
reads after the first take about as long as a read of a document with 100 entries.

**Acceptance Scenarios**:

1. **Given** a document whose layers are saved, **When** one entry is recorded at tier `user`, **Then** the next read folds
   in only that entry, the saved layers of the other tiers are reused unchanged, and the result equals a replay of the whole
   ledger.
2. **Given** a service that was just restarted, **When** a document is first read, **Then** it uses the saved layers and does
   not replay the older ledger, and the result equals a full replay.
3. **Given** saved layers made under one schema version, **When** a read happens under a version whose merge rules differ for
   some field, **Then** the saved layers that no longer fit are not used, the document is derived from the ledger, the layers
   are saved again, and the result equals a fresh derivation under the new version.
4. **Given** the saved layers are missing, damaged or cannot be written, **When** a document is read or written, **Then** the
   read still returns the exact correct document (more slowly) and the write is still accepted; losing every layer changes no
   result, only speed.
5. **Given** a layer that lags behind the ledger (its watermark is below the last entry of its tier), **When** the document is
   read, **Then** the missing entries are folded in and the document is correct.
6. **Given** a write that is rejected, **When** it finishes, **Then** no saved layer, watermark or view has changed.

---

### User Story 2 - `rebuild` proves the saved layers are exact (Priority: P1)

An operator runs `rebuild` on one document or on all of them. The system derives every document from the ledger alone,
without using any saved layer, and compares what it derived with what is saved: each tier's layer, its watermark, and the
cached document view. A saved item that differs from the derivation is reported (document, tier, what differs) and repaired;
an item that is missing, or was made under another schema version, is derived and saved without counting as a difference.
`rebuild` never writes to the ledger and never calls a deferred source or an impure hook. This is the replay guarantee of
docs/SPEC.md 16 item 6, extended from cached views to saved layers.

**Why this priority**: Saved layers are only acceptable if anyone can prove, at any time, that they equal what the ledger says.
This is the check that makes story 1 safe to ship and to run in production.

**Independent Test**: Save the layers for a set of documents, damage one layer, one watermark and one cached view by hand, then
run `rebuild all`. The three differences are reported and repaired, the untouched documents are reported clean, the ledger is
byte-identical before and after, and a second `rebuild` reports nothing.

**Acceptance Scenarios**:

1. **Given** correct saved layers, **When** `rebuild` runs, **Then** it reports no difference and the stored layers are
   byte-identical afterwards.
2. **Given** a layer whose content was altered, **When** `rebuild` runs, **Then** the document, tier and the fact that the
   layer differs are reported, and the layer is replaced by the derived one.
3. **Given** a watermark that claims more of the ledger than the layer holds, **When** `rebuild` runs, **Then** it is
   reported and repaired, and until then reads do not trust it: a saved layer that cannot be shown to fit the ledger is not used, and the document
   is derived from the ledger.
4. **Given** a document that cannot be derived (for example, its pinned schema version is unknown), **When** `rebuild all`
   runs, **Then** that document is reported and the others are still checked.
5. **Given** a rebuild with a populated effect log, **When** it runs, **Then** it makes no hook call and no source call; where
   an impure outcome is missing it makes exactly one call for it and records the outcome, and still no source call.

---

### User Story 3 - Read a document as it was: `as_of` by seq, tx_time or valid_time (Priority: P2)

An auditor, a support engineer or a data scientist asks what a document looked like at an earlier point. A read takes
`as_of` with one of three kinds of point, and returns the document, model errors and status as they would have been derived
at that point, with the same rules as a normal read:

- **seq**: the document after the entry with that sequence number was recorded (and nothing after it).
- **tx_time**: the document as the system knew it at that moment of recording; it is the same as the seq of the last entry
  recorded at or before that moment.
- **valid_time**: the document restricted to what was true in the world at that moment. A contribution may carry an optional
  `valid_time`, the moment its fact became true. A valid-time read leaves out the entries whose `valid_time` is after the
  asked moment, then folds the rest in the usual order.

`valid_time` is new on contributions in this feature. It is recorded with the entry, never affects ordering, and never
affects a normal read. A time-travel read changes nothing: it saves no view, no layer and no derived value, and it never calls
a deferred source or an impure hook. `explain` accepts the same `as_of` and answers as of that point.

**Why this priority**: History that cannot be inspected is not auditable. It builds on the saved layers (story 1) for speed but
is useful on its own.

**Independent Test**: Record a history with known tx times, some entries with `valid_time` and some without. For every seq,
read `as_of` that seq and compare it with the live document as it was returned when that entry was committed. Read `as_of` a
tx_time between two commits and a valid_time between two facts, and compare with the expected documents.

**Acceptance Scenarios**:

1. **Given** a history of 14 entries, **When** the document is read `as_of` seq 6, **Then** it equals the document that was the
   live answer right after entry 6 was recorded, with the same model errors.
2. **Given** commits at 10:00, 10:05 and 10:10, **When** the document is read `as_of` tx_time 10:07, **Then** it equals the
   read `as_of` the seq of the 10:05 commit, and the answer states which seq it resolved to.
3. **Given** an entry with `valid_time` March 1 recorded in June and an entry with `valid_time` April 1 recorded in May,
   **When** the document is read `as_of` valid_time March 15, **Then** only the March 1 entry (and entries with no
   `valid_time`) take part, and the document is what that subset folds to; a normal read is unchanged.
4. **Given** a time before the first entry, **When** read, **Then** the answer is the empty document (defaults and computed
   fields over it) and states that it resolved to seq 0; a seq beyond the last entry reads the current state and states the
   seq it resolved to.
5. **Given** a field with an impure compute or a deferred source, **When** the document is read `as_of` any point, **Then**
   no hook or source is called: an impure compute uses the outcome recorded for that state of its inputs (a state with no
   recorded outcome yields a model error saying so, not a call), and each deferred field is listed with a status saying that
   history is not kept for it and has no value in the document.
6. **Given** a request with two points (for example seq and valid_time), **When** read, **Then** it is refused as ambiguous.
7. **Given** a contribution whose `valid_time` is not a timestamp, **When** it is submitted, **Then** the request is rejected
   with a reason naming the entry and nothing is recorded; a request that carries no `valid_time` keeps exactly the hash it had before this feature, so its retries still match (FR-012).
8. **Given** `explain` asked `as_of` seq 6, **When** answered, **Then** the winner, inputs and shadowed entries are those of
   the document at seq 6.

---

### User Story 4 - Compact old history without changing any answer (Priority: P2)

An operator keeps ledgers small. `compact` takes a document and a policy (everything at or below a given seq, or older than a
given age) and replaces the accepted entries in that range with **one synthetic entry per tier**. A
synthetic entry reproduces exactly what its sources folded to, remembers the ids of its sources, and carries every
idempotency key they had, so that a repeated contribution is still refused as a duplicate. The original entries are kept in
a separate archive (the cold store) until an operator expires them. Before it changes anything, compaction derives the
document, its model errors and the `explain` answer of every path both ways; if any differs it refuses and changes nothing.

After a compaction the document, its model errors, its status and every `explain` answer are the same as before, with one
allowed difference: an entry inside the compacted range is named by its synthetic entry, which lists the original entries it
stands for. While the archive keeps the originals, `explain` is answered from them and is exactly the pre-compaction answer;
once they are expired, `explain` names the synthetic entry, treats it as one entry (an input if any of its originals was an
input, shadowed if none was), and says the originals are no longer kept. Compaction never touches the effect log, the derived store, the rejection audit log
or the request records that make retries return their original answer.

**Why this priority**: History must not grow without bound, but compaction rewrites the ledger, so it ships after the layers
and the proof tools (stories 1 and 2) are in place.

**Independent Test**: Build random histories with several tiers, tombstones, re-creations and a counter-style (non-idempotent)
field. Record the document, model errors, status and `explain` for every path; compact at several watermarks; compare after
each. Then break the compaction on purpose (a deliberately wrong synthetic entry) and see it refused with nothing changed.

**Acceptance Scenarios**:

1. **Given** a document with 10,000 entries on 20 fields in 3 tiers, **When** it is compacted at the last seq, **Then** at
   most 60 entries remain, and the document, model errors and every `explain` answer (expanded) are unchanged.
2. **Given** tombstones and re-creations in the range, **When** compacted, **Then** the synthetic entry keeps the tombstone
   and the re-creation marks, so a later write or delete still behaves as it would have against the original entries.
3. **Given** a counter-style field with an idempotency key on every contribution, **When** compacted, **Then** the sum is
   unchanged and the synthetic entry carries all the keys; a contribution that repeats a compacted key is still rejected as a
   duplicate. A non-idempotent field is never compacted without its keys: if keys were not recorded for some of its entries,
   compaction of that field refuses.
4. **Given** a document that is written to while it is being compacted, **When** both finish, **Then** every entry recorded
   meanwhile is present, sorts after the compacted range, and is not lost or reordered.
5. **Given** a request recorded before compaction with a retry key, **When** the same request is retried after compaction,
   **Then** it is still a duplicate and returns its original answer (original entry ids and seqs, latest document).
6. **Given** a compacted document, **When** an operator asks to verify it against the archive, **Then** the document derived
   from the originals equals the document derived from the compacted ledger, or the difference is reported.
7. **Given** the compaction check finds any difference in the document, model errors, status or an `explain` answer, **When**
   compaction would commit, **Then** it refuses with the first differences named and the ledger, archive and saved layers are
   exactly as before.
8. **Given** an `as_of` point inside a compacted range, **When** read while the archive still holds the originals, **Then**
   the answer is correct (served from the originals); **When** the originals have been expired, **Then** the read is refused
   with the earliest seq it can still answer (a `tx_time` or `valid_time` inside the range is refused the same way, FR-029),
   never answered with a wrong document.
9. **Given** an operator who expires archived originals up to a seq, **When** it is done, **Then** the expiry is recorded,
   synthetic names still list their source ids, and `explain` says the originals are no longer kept.

---

### User Story 5 - Erase data for legal reasons with an auditable purge (Priority: P3)

A data-protection officer must remove a person's data. A **purge** names a document and what to erase (every contribution at
a path, or specific entries), and a required reason or ticket. The identities allowed to purge are named in the schema; by
default nobody may. The purge physically removes the erased values and everything the system keeps that holds them or was
derived from them: the entries' values, their `meta` and `valid_time`, copies in the archive, saved layers, cached views, the
inputs and outputs of effect-log records and the derived values that depend on the erased paths, and the attempted values at
the erased paths in the document's rejection audit records. The entries' places in the ledger remain as **purged entries** (id, seq, tier,
path, kind of operation, contributor, recording time, request, schema version, lenient flag and idempotency key; the
idempotency key is a caller-chosen token, kept so that a repeated contribution is still refused) so that sequence numbers stay continuous and `explain` can say
"this entry was purged on this date under this ticket". A purged entry has no effect on the document, as if its contribution
had never been made, and the document is recomputed accordingly.

A purge is recorded in a **purge log**: who, when, why, which document and scope, how many entries and stores were affected,
and never the erased values. It is different from a tombstone: a tombstone is a contribution that keeps the history and says
the field is deleted; a purge removes the history itself. The purge log is never read by any derivation and is itself not
subject to purge.

**Why this priority**: Legal erasure is essential for some deployments, but it is the rarest operation and the most
destructive, so it comes last and only after compaction exists to interact with.

**Independent Test**: Record entries with a known marker value at several tiers, let them be compacted, saved into layers,
cached and used by a computed field and an impure compute. Purge the path. Search every store for the marker: it appears
nowhere; the document equals a derivation of the ledger with those contributions absent; the purge log has one record with no
marker; `explain` names the purge; the other fields and the other documents are unchanged.

**Acceptance Scenarios**:

1. **Given** a path with contributions at three tiers, **When** it is purged, **Then** the document shows what the remaining
   contributions fold to (defaults if none), and the ledger holds purged entries in place of the erased ones.
2. **Given** an erased value that a computed field used, **When** purged, **Then** the computed value is recomputed without it
   and no earlier computed value or hook outcome that contains the erased value remains anywhere.
3. **Given** a value also present in compacted history and in the archive, **When** purged, **Then** it is gone from the
   synthetic entry and from the archive. If the originals have already been expired, the whole compacted contribution at that
   path is erased, and the purge log says so.
4. **Given** an identity not listed in the schema for purge, **When** it asks to purge, **Then** it is refused as not
   authorized and nothing changes; **Given** a purge request with no reason, **Then** it is refused.
5. **Given** a purge that fails partway (for example a store cannot be written), **When** it ends, **Then** nothing was
   changed (a purge is all or nothing) and the failure is reported, not logged as a success.
6. **Given** a purged document, **When** `rebuild` runs, **Then** it reports no difference, and the document still equals a
   derivation without the purged contributions.
7. **Given** a tombstone and a purge on the same path, **When** compared, **Then** the tombstone remains an entry that
   `explain` can show and the purge leaves no value anywhere; the purge log shows only the purge.
8. **Given** a request that was retried with its retry key after its entries were purged, **When** retried, **Then** it is
   still a duplicate and returns its original answer with the purged entries marked purged, never with their values.

---

### Edge Cases

- **A layer is saved from a state the ledger no longer has** (damaged, restored from a backup, or older than a compaction or
  purge): a layer is used only if it fits the ledger it is read with (its watermark, the schema version and the ledger's
  compaction and purge positions agree); otherwise it is not used and the document is derived from the ledger.
- **Two readers and a writer at once**: a reader never sees a layer saved halfway, and never uses a layer that does not fit the
  cached view it is read with (layers and view are saved in one transaction; a layer that could not be saved, or an older process's,
  lags the view, and the reader then derives from the ledger).
- **A schema version change** makes layers for the affected fields unusable until re-derived; the re-derivation happens on
  the first read, as for views today (SPEC 16 item 10).
- **`as_of` seq inside a request**: entries of one request commit together, but a seq can fall between two entries of the
  same request; the read answers as of that seq anyway and says that the point is inside a request.
- **Clock skew**: `tx_time` is not guaranteed to grow with `seq`; a `tx_time` point resolves to the highest seq whose entry was
  recorded at or before it, and the answer states that seq, so the result is a point in the ledger and not a filter.
- **A valid-time read and rules**: model errors are computed over the filtered document; a document that was valid at the
  time may show errors in the filtered form. That is correct and states what the data says for that moment.
- **`valid_time` and retries**: a request with no `valid_time` keeps the same canonical form and hash as before this feature,
  so retries of old requests still match; a request that carries `valid_time` includes it in the hash.
- **Compaction of a document with no entries in range, or already compacted at that watermark**: reported as nothing to do and
  changes nothing.
- **Compaction and schema versions**: a range holding entries written under more than one schema version is refused (the
  operator picks a watermark inside one run of versions). Once a document has compacted history, a schema version that folds
  differently (merge rules, types, tiers or which fields are derived) is derived from the archived originals; if they have
  been expired, activating or pinning that version is refused for that document, saying the history is compacted. A version that
  only changes validation or computed rules reuses the synthetic entries.
- **Compaction watermark**: a request is atomic, so a watermark inside a request is moved down to the end of the previous one.
- **Purge of a struct written above the path**: an entry that wrote a struct containing the erased path keeps its other
  values; only the part at the erased path is removed from it.
- **Purge and retry keys**: the hash of a request that held erased values is erased too (a hash of a value is still a copy of
  it); a retry of such a request with its key is answered as a duplicate with its original answer, without comparing payloads.
- **Purge of an entry that other entries depend on** (for example an increment): the dependent entries stay; the document is
  re-folded without the purged one, which changes the sum; that is the intent of erasure.
- **Purge versus a deferred value**: a deferred value derived from erased data is removed from the derived store and
  fetched again on the next read that needs it, subject to its normal policy.
- **Archive and purge races**: a purge never leaves a copy behind in an archive being written; compaction and purge on one
  document run one at a time, and both exclude concurrent writes to that document only for the moment the change commits.
- **Very large documents**: operations that scan the ledger (rebuild, compaction check, purge) report progress and can be
  repeated safely if interrupted; an interrupted operation changes nothing or completes in full.

## Requirements *(mandatory)*

### Functional Requirements

**Saved layers (story 1)**

- **FR-001**: The system MUST be able to keep, per document and per tier, a saved layer of what that tier's entries fold to,
  with its watermark (the last seq it covers) and the schema version it was made under.
- **FR-002**: A read MUST use a saved layer only if it fits the ledger it is read with, and MUST fold in only the entries newer
  than its watermark. A layer that does not fit MUST NOT be used and MUST be replaced from the ledger.
- **FR-003**: Recording an entry at a tier MUST NOT require recomputing the saved layers of the other tiers.
- **FR-004**: The document, model errors, status and `explain` obtained through saved layers MUST be byte-identical to those
  derived from the ledger alone. Saved layers MUST NOT change any result, only the cost of getting it.
- **FR-005**: Saved layers MUST be disposable: if they are missing, stale, damaged or cannot be saved, reads MUST still be
  correct and writes MUST still be accepted. Failing to save a layer MUST NOT fail a request and MUST NOT be a rejection.
- **FR-006**: A rejected request MUST NOT change any saved layer, watermark or view.
- **FR-007**: Reading after a restart MUST NOT replay ledger entries that saved layers already cover.

**Rebuild (story 2)**

- **FR-008**: `rebuild` MUST derive each document from the ledger alone and compare every saved layer, watermark and cached view
  with the derivation, byte for byte, reporting each difference (document, tier, item) and repairing it.
- **FR-009**: `rebuild` MUST treat a missing item, or one made under another schema version, as derived afresh and saved, not
  as a difference; MUST report a document that cannot be derived without stopping the others; MUST NOT write to the ledger;
  and MUST NOT call a deferred source. It MUST NOT call an impure hook while the recorded outcomes cover the inputs; where an
  outcome is missing (for example after a purge of an input, or a hook version change) it follows Feature 2 and fills it by
  calling the hook once and recording the result.
- **FR-010**: A second `rebuild` immediately after a first MUST report no difference.

**Time travel (story 3)**

- **FR-011**: A contribution MAY carry `valid_time`, a timestamp stating when its fact became true in the world. A value that
  is not a timestamp MUST be rejected at admission with a reason naming the entry. `valid_time` MUST NOT affect ordering,
  merging, validation or any normal read.
- **FR-012**: The canonical form and hash of a request that carries no `valid_time` MUST be exactly what they were before this
  feature; a request that carries one MUST include it in its hash.
- **FR-013**: A read MUST accept `as_of` with exactly one point: a seq, a `tx_time` or a `valid_time`. More than one MUST be
  refused as ambiguous.
- **FR-014**: `as_of` seq N MUST give the document derived from the entries with seq up to N, and MUST equal the live document
  returned when entry N was committed.
- **FR-015**: `as_of` `tx_time` T MUST resolve to the highest seq whose entry was recorded at or before T and then behave as
  `as_of` that seq; the answer MUST state the seq it resolved to.
- **FR-016**: `as_of` `valid_time` T MUST leave out every entry whose `valid_time` is after T, keep entries with no
  `valid_time`, fold the rest in the usual order, and state how many entries took part.
- **FR-017**: A time-travel read MUST NOT save any view, layer or derived value, MUST NOT call any deferred source or impure
  hook, and MUST list each deferred field with a status saying no history is kept for it, with no value.
- **FR-018**: An impure compute in a time-travel read MUST use only the outcome recorded for the inputs at that point; where
  none is recorded the read MUST report a model error saying so and MUST NOT call the hook.
- **FR-019**: `explain` MUST accept the same `as_of` and answer for the document at that point.
- **FR-020**: Time-travel reads MUST use the schema version the document currently uses; choosing another version for a read
  is Feature 6.

**Compaction (story 4)**

- **FR-021**: `compact(doc, policy)` MUST replace the accepted entries in the chosen range with one synthetic entry per tier
  (covering every path that tier's entries touched) that reproduces their fold exactly, lists the ids of its sources, and keeps tombstones and re-creation
  marks.
- **FR-022**: Compaction MUST NOT change the document, model errors (with who introduced each), status or the skipped and
  conflicting paths. While the archive keeps the originals, every `explain` answer MUST be exactly the pre-compaction answer.
  After expiry, `explain` MUST name the synthetic entry, list its source ids, treat it as an input if any source was one and
  as shadowed if none was, and say the originals are no longer kept.
- **FR-023**: Compaction MUST verify, before committing, that the document, model errors, status and every `explain` answer
  derived from the compacted ledger alone (synthetic entries as single entries, as after expiry) agree with those derived from
  the originals, under the rule of FR-022 for the roles; it MUST refuse, naming the first differences and changing nothing, if
  any differs. It MAY refuse a case it cannot prove equal.
- **FR-024**: A synthetic entry MUST carry every idempotency key of its sources; a field with a non-idempotent merge behavior
  MUST be compacted only together with its keys and compaction of it MUST refuse if keys are missing. A repeated key MUST still
  be rejected after compaction.
- **FR-025**: Compaction MUST keep the original entries in an archive until an operator expires them; expiry MUST be a recorded
  operation; after expiry, synthetic entries MUST still list their source ids.
- **FR-026**: Compaction MUST NOT remove or alter effect-log records, derived-store records, rejection audit records, or the
  request records that make a retry return its original answer. A retried request MUST still return its original answer.
- **FR-027**: Compaction and expiry MUST be atomic: an interrupted run changes nothing or completes in full. Entries recorded
  during a compaction MUST be preserved, after the compacted range, in order.
- **FR-028**: An operator MUST be able to verify a compacted document against its archive: the document derived from the
  originals MUST equal the one derived from the compacted ledger, or the differences are reported.
- **FR-029**: A time-travel read at a point inside a compacted range MUST be answered from the archived originals while they
  are kept. When they are not kept, it MUST be refused, and MUST NOT return a document derived from the synthetic entries as if
  it were exact. After expiry the points that can be answered exactly are: a seq at or above the compaction's watermark; a
  `tx_time` that is earlier than every compacted entry's recording time or not earlier than the last of them; and a
  `valid_time` when none of the compacted entries carried one. Every other point inside the range is refused, and the refusal
  names the earliest seq that can still be answered.

**Purge (story 5)**

- **FR-030**: `purge` MUST take a document, a scope (a path with all its contributions, or listed entries) and a reason or
  ticket (required), and MUST be allowed only to identities the schema lists for purge; by default nobody is.
- **FR-031**: A purge MUST remove the erased values, `meta` and `valid_time` from the ledger, the archive and compacted
  entries, the saved layers and cached views, the effect-log inputs and outputs and the derived values that depend on the
  erased paths, and the attempted values at the erased paths in the rejection audit records of that document (an attempt that
  names other paths keeps its values: it holds nothing that was erased).
- **FR-032**: A purged entry MUST stay in the ledger as a record without value (id, seq, tier, path, operation, contributor,
  recording time, request, schema version, lenient flag and idempotency key; the value, `meta`, `valid_time` and request hash are
  erased) and MUST have no effect on any derivation. The document MUST equal the derivation of the ledger
  without the erased contributions, and `explain` MUST report the entry as purged with the date and reason.
- **FR-033**: Every purge MUST be recorded in a purge log (who, when, reason, document, scope, counts), never holding an
  erased value; the log MUST NOT be read by any derivation and MUST NOT be purgeable.
- **FR-034**: A purge MUST be atomic across everything it touches: a failure leaves everything as it was and is reported.
- **FR-035**: A purge MUST be a different operation from a tombstone and MUST NOT be possible through a contribution.
- **FR-036**: When the originals of a compacted range are no longer kept, a purge of a path MUST erase the whole compacted
  contribution at that path and the purge log MUST say so.
- **FR-037**: After a purge, `rebuild` MUST report no difference and a retried request MUST return its original answer with the
  purged entries marked purged.

**Cross-cutting**

- **FR-038**: The ledger MUST stay append-only except through compaction (FR-021) and purge (FR-030), the only operations that
  may change or remove existing entries, both of which are recorded operations. Documents, rejections and every earlier feature
  keep their behavior; the conformance vectors of Features 001 to 004 pass unchanged.
- **FR-039**: Structured logs MUST record snapshot repairs, compactions, expiries and purges (without values).
- **FR-040**: The operations in this feature MUST be available through the library and the command line, with the same results.

### Key Entities

- **Saved layer (snapshot)**: for one document and one tier, the folded result of that tier's entries up to a watermark, with
  the schema version it was made under. Disposable; always re-derivable from the ledger.
- **Watermark**: the last seq a saved layer covers.
- **Time point**: the `as_of` choice; a seq, a `tx_time` (resolved to a seq) or a `valid_time` (a filter).
- **Valid time**: an optional timestamp on a contribution, when its fact became true in the world.
- **Synthetic entry**: the entry that stands for the compacted entries of one tier, covering all the paths they touched; lists its
  source ids and their idempotency keys.
- **Archive (cold store)**: the original entries replaced by compaction, kept until expired.
- **Purged entry**: a ledger record whose value, `meta` and `valid_time` were erased; still has a seq and a place.
- **Purge log**: the append-only record of purges, without values.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On a document with 100,000 entries, the first write after a restart, the read after it, and the first read after a
  restart each take no more than 5 times as long as the same step on a document with 100 entries.
- **SC-002**: Over at least 1,000 random histories (several tiers, tombstones, re-creations, computed and counter fields),
  the document, model errors, status and every `explain` answer obtained through saved layers are byte-identical to those
  derived from the ledger alone.
- **SC-003**: Every deliberately damaged layer, watermark or view in a test set is found and repaired by `rebuild`, the ledger
  is byte-identical before and after, and a second `rebuild` finds nothing.
- **SC-004**: For every seq of random histories, `as_of` that seq equals the live document returned when that entry was
  committed; `as_of` reads make zero hook and source calls.
- **SC-005**: Over at least 1,000 random histories and several watermarks each, compaction leaves the document, model errors,
  status and every expanded `explain` answer unchanged; a deliberately wrong compaction is refused with nothing changed.
- **SC-006**: Compacting a document of 10,000 entries on 20 fields in 3 tiers leaves at most 60 entries, plus those after the
  watermark.
- **SC-007**: After a purge of a marker value, a search of every store finds the marker nowhere, the purge log holds one record
  without it, and the document equals a derivation without the erased contributions.
- **SC-008**: With layers disabled entirely, every result of this feature and of Features 001 to 004 is identical to the
  result with them enabled; the shared vectors of docs/SPEC.md 16.1 and 16.2 pass unchanged.

## Assumptions

- Saved layers are an optimization and a proof target, not a source of truth: the ledger (with archives and purges) is.
- `valid_time` is supplied by the contributor; the system does not check it against `tx_time` and does not use it for anything
  but `as_of` reads. An entry with no `valid_time` counts as valid at every moment in a valid-time read, so it always takes
  part.
- A `tx_time` read is a point in the ledger (a seq), not a filter, because `tx_time` is informational and is not guaranteed
  to grow with `seq`.
- Combining two points in one read (for example "as known at T, about the world at V") is future work.
- Time-travel reads use the document's current schema version; reading under an older version is Feature 6.
- Deferred values have no history in the derived store, so a historical read never shows them.
- Archive expiry is an explicit operator action; automatic expiry by age is future work.
- Crypto-shredding (making values unreadable by destroying a key) is not part of this feature; physical removal is. A purge
  is specified by its result (the value is unrecoverable from anything the system stores), so a later key-based mode can meet
  the same requirements. It needs key management that this feature does not define.
- The rule that decides who may purge is declared in the schema, like `invalidate_by`; the constitution's Principle I and
  docs/SPEC.md 4.2 item 1 (entries are immutable and append-only) must be amended in this feature to name compaction and
  purge as the only exceptions, and docs/SPEC.md 12.2, 12.4, 15 and 16 updated to match.
- Cross-document operations, multi-region replication and redaction of `explain` stay out of scope (SPEC section 17).
