# Data Model: Schema Versioning and Evolution

Stored text is canonical (SPEC 3.1) in both backends.

## New tables

- `schema_versions(schema_id, version, fingerprint, parts, first_served)`: primary key `(schema_id, version)`. `fingerprint` is the
  SHA-256 of the full canonical description of the version; `parts` is canonical text of `{top, fields: {pattern: digest}}`. A row
  is written once, when the version is first activated or pinned (or bootstrapped); never updated.
- `schema_state(schema_id, active_version, generation)`: one row. `generation` grows by one on every activation, rollback, pin,
  unpin and migration; readers compare it with the one they last saw.
- `doc_pins(schema_id, doc, version, since)`: primary key `(schema_id, doc)`.
- `schema_history(id, at, operator, action, doc, from_version, to_version, change_class, note)`: insert only (trigger); `action` is
  `bootstrap`, `activate`, `rollback`, `pin`, `unpin` or `migrate`; `doc` is null for a global act; `change_class` is the overall
  class of the change between the versions, or null for a pin that changes none; `note` is a short canonical struct of names and counts (for example the target version, the number of documents rebuilt, the
  codes of the first breaking changes), never a
  value).

## Changed results

- `DocumentResult.shadowed: tuple[Shadowed, ...]` with `Shadowed(seq, entry_id, tier, path, reason, detail)`; `reason` is one of
  `tier_not_in_schema`, `now_derived`, `no_longer_conforms`, `cannot_be_merged`. `skipped` (the seqs) stays.
- `Explanation.shadowed_by_schema: tuple[Shadowed, ...]`: the shadowed entries that touch the path. (`Explanation.shadowed`, the entry ids whose contribution made no difference to the value, keeps its meaning from Feature 001.)

## New values

- `Change(kind, path, description, change_class, classified, detail)`: `kind` such as `field_added`, `merge_changed`, `tier_reordered`;
  `change_class` in `compatible | visible | breaking`; `classified` false for "not classified".
- `ChangeList(old_version, new_version, changes, overall)`.
- `DryRunReport(change_list, target, generation, examined, left_out, changed, unchanged, failed, would_call_hook, documents)` where
  `documents` is a tuple of `DocumentChange(doc, document_changed, errors_added, errors_resolved, skipped_before, skipped_after,
  shadowed_by_reason, would_call_hook, failure_code, conflicts_changed)` (`failed` is true when `failure_code` is set).
- `ActivationReport(version, previous, change_class, rollback, rebuild, changed)` with the `RebuildReport` of Feature 1/5 when asked, and in
  `changed` the documents whose answer changes (the dry run's list).
- `MigrationReport(moved, already_moved, not_pinned, failed, batches)`.
- `HistoryRecord(id, at, operator, action, doc, from_version, to_version, change_class, note)`.

## Relationships and invariants

- A document's schema is its pin if it has one, else the active version; both come from the store (generation-checked).
- For every document: derive(ledger, version) is unchanged by anything in this feature except the new skip of non-conforming
  entries admitted under another version.
- A dry run's `DocumentChange` equals the difference between the document read now and the document read after the activation.
- `schema_history` is never read by a derivation and has no operation that removes a row.
