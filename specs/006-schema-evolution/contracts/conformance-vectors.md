# Contract: Conformance vectors, delta

New steps (the runner already has `restart`, `activate_schema`, `get`, `explain`, `rebuild`):

| Step | Meaning |
|---|---|
| `diff: {old: V, new: V}` | the change list between two schemas of the case, compared with `expect: {overall, changes: [{kind, path, class, classified}]}` |
| `plan: {target: V, limit?: N, docs?: [..]}` | the dry run; `expect: {examined, left_out, changed, unchanged, failed, would_call_hook, documents: {doc: {document_changed, errors_added, ...}}}` |
| `activate_schema: V` | extended: `operator`, `acknowledge_breaking`, `rebuild`; `expect: {refused: CODE}` |
| `pin: {doc, version, operator}` / `unpin: {doc, operator}` | |
| `migrate: {docs?, to?, operator}` | `expect: {moved, already_moved, not_pinned, failed}` |
| `get` expectation `shadowed: [{seq, reason}]` | |

New vectors:

- `schema-changes.ion`: one pair of versions per row of the classification table in `changes.md` (the class and the kind), several
  changes at once with the overall class, an unclassified difference, and the same list twice.
- `schema-dry-run.ion`: a dry run followed by the activation and the reads it predicted; a limit and a list; a breaking activation
  refused without the acknowledgement; a rollback that reads byte-identically; `TIER_IN_USE`; `tier_not_in_schema` and
  `no_longer_conforms` counted per reason. Pinned documents left out of a dry run are in `schema-pins.ion`.
- `schema-shadow.ion`: the reasons a live activation can produce (`now_derived`, `no_longer_conforms`) shown by `get` and `explain`, and none under the version that wrote them or after a rollback. `tier_not_in_schema` (a used tier cannot be dropped by an activation) is in `schema-dry-run.ion`; `cannot_be_merged` (a defect of a merge) is in the unit tests.
- `schema-pins.ion`: pin, write under the pin, a single migration, a batch with a document that cannot be derived, and a re-run.

The vectors of Features 1 to 5 run unchanged.
