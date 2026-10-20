# Contract: Conformance vectors, delta

New files in `conformance/` and new runner directives. The runner is in `ports/py/tests/conformance_runner.py`; a vector is a list
of cases of `steps` and `expect`, as in the earlier files.

## New steps

| Step | Meaning |
|---|---|
| `append: {..., valid_time: <timestamp>}` | an entry with `valid_time` |
| `as_of: {seq: N}` / `{tx_time: T}` / `{valid_time: T}` | read the document at that point; result compared with `expect` |
| `compact: {through_seq: N}` | compact; the runner reads the document, errors and `explain` of every path before and after and requires equality |
| `expire: {through_seq: N}` | expire the archive |
| `purge: {path: P, reason: R, as: <identity>}` | purge |
| `rebuild: {}` | rebuild; `expect: {identical: true}` |
| `clock: <timestamp>` | set the fake clock (the existing step), used for `tx_time` |

## New expectations

`document`, `model_errors`, `explain: {path: ..., winner|winner_synthetic_of: ..., inputs: [...], shadowed: [...]}`,
`resolved_seq`, `included`, `refused: <code>`, `absent_everywhere: "<marker text>"` (no stored text of the service contains the marker; the
runner scans every table of the backend), `purge_log_count`.

## New vectors

- `time-travel.ion`: a history read at every seq; `tx_time` between commits and before the first; `valid_time` with entries that do
  and do not carry it; before the first entry; beyond the last; `AS_OF_AMBIGUOUS`; `BAD_VALID_TIME` records nothing; `valid_time`
  is part of the request hash only when given (retry duplicate, `REQUEST_KEY_REUSED`). The impure compute and deferred field at a point are
  covered by the Python integration tests (`test_as_of_isolation.py`), because they need hook fixtures a vector cannot state.
- `compaction.ion`: compact at several watermarks (a watermark inside a request moves down); tombstones; a counter-style field
  with keys (a repeated key still rejected; missing keys refused `IDEM_KEY_MISSING`); a retried request; `explain` exact while
  the archive is kept and `originals_kept: false` after expiry; `as_of` inside a range before and after expiry. The runner compares the
  document, model errors and `explain` of every path before and after each `compact`.
- `compaction-versions.ion`: a range written under two schema versions refused (`MIXED_SCHEMA_RANGE`); a version that folds
  differently applied from the archive while it is kept and refused (`HISTORY_COMPACTED`) once it is expired.
- `purge.ion`: purge a path with entries at several tiers; unauthorized, reason-less and unknown-path purges refused; a retried
  request after purge; `explain` names the purge; a struct written above the path keeps the rest; compacted, archived and expired
  history; `rebuild` clean; `absent_everywhere`; `purge_log_count`.
- `snapshots.ion`: a long history read, appended to, restarted (`restart` step: a new service on the same storage) and read, each
  equal to the same steps run with layers off; a damaged layer repaired by `rebuild`.
- Not portable as vectors, and tested in the Python suite only: a deliberately wrong compaction refused (it needs an injected
  plan), crash injection at each step of an operation, and impure computes and deferred fields at a point in time.
- Existing vectors run with layers on and off and give the same results.
