# Data Model: Deferred Computed Fields

Entities from the spec (Key Entities) with their fields, rules and state transitions. Types are language-neutral;
`contracts/` gives the exact shapes. Feature 001 and 002 entities (entry, request, view, effect record) are unchanged.

## DeferredSpec (schema, on a field)

| Field | Type | Rule |
|---|---|---|
| `hook` | name, version | A registered hook; `UNKNOWN_BEHAVIOR` if not. Version defaults to 1 in text (`name@version`). |
| `depends` | list of paths | Non-empty; each a declared path (`UNKNOWN_DEPENDENCY`); may be plain, computed, impure or deferred; no cycles among derived fields (`DEPENDENCY_CYCLE`). |
| `refresh` | list of RefreshPolicy | At least one; written as one policy or a list; `once` and `per_fetch` at most once each; `scheduled` and `notification` (with different topics) may repeat. |
| `ttl` | duration | Required for `per_fetch`, forbidden otherwise; at least 1 s. |
| `on_dependency_change` | `keep` \| `recompute` | Allowed only with `once`; default `keep`. |
| `on_failure` | `error` \| `serve_stale` \| `absent` | Default: the schema's `deferred_on_failure`, else `error`. |
| `on_missing` | `null` \| `absent` | What a pending field shows. Default `null`. (`error` is refused: no model errors.) |
| `timeout` | duration | Default 2 s, at most 30 s. Per attempt. |
| `retry` | `{max, backoff}` | `max` 0 to 5 (default 2); `backoff` 0 to 10 s (default 200 ms), fixed between attempts. |
| `deadline` | duration | Default 6 s, at most 60 s; the whole retry envelope of one call. |
| `invalidate_by` | list of tier or role names | Required, non-empty; names must be tiers or roles the schema knows. |

A field also has its `type` (required; the answer must conform) and no `merge`, `default` or `validate`.

**Shape digest** (stored on records): sha256 of the canonical text of `hook@version`, the `depends` paths in
declaration order and the field `type`. A change in any of them makes stored records of the field not reusable.

## RefreshPolicy

| Kind | Options | Notes |
|---|---|---|
| `once` | none | Fetched on read when there is no current record. |
| `per_fetch` | uses the field's `ttl` | Fetched on read when missing, invalidated, inputs changed, past `ttl` or `consistency: fresh`. |
| `scheduled` | exactly one of `every` (duration, at least 1 s) or `cron` (5 fields, UTC) | Fetched by the scheduler when its next fire after `as_of` is due. |
| `notification` | `topic` (non-empty string) | Recomputed by the scheduler after the topic marks the record invalidated. |

## DerivedRecord (derived store; one per document and deferred field)

| Field | Type | Rule |
|---|---|---|
| `doc`, `field` | text | Key. `field` is the schema pattern of the field. |
| `value` | canonical Ion text or null | The last good value (never null once `has_value`); kept while a later fetch fails. Never a failed, null or non-conforming answer. |
| `has_value` | bool | False until the first success. |
| `as_of` | timestamp | When the last good value was fetched. |
| `input_fingerprint` | text | Feature 002 fingerprint of the source and input values of the last attempt (a success gave the value). |
| `hook` | text | `name@version` that produced it. |
| `shape` | text | Shape digest at the time (above). |
| `schema_version` | int | Version in force when fetched; informative, not a reuse key. |
| `status` | `ok` \| `stale` \| `error` | `pending` is never stored: it is derived by the planner from the current inputs. |
| `error` | text or null | Kind of the last failure (`error`, `timeout`, `deadline`, `bad_value`, `breaker_open`, `rate_limited`); never a message or value. |
| `attempts` | int | Attempts of the last call. |
| `last_attempt` | timestamp | When the last call (success or failure) ended. |
| `invalidated` | bool | Set by `invalidate` and `notify`; cleared by a successful fetch. |

State transitions of a record:

```
(none) --fetch ok--> ok
(none) --fetch fails--> error            (has_value false; shown as deferred::null under `error`, left out under `absent`)
ok     --fetch fails--> stale | error    (as on_failure: serve_stale keeps the value as stale; error/absent keep the value stored but do not show it)
stale/error --fetch ok--> ok             (invalidated cleared)
any    --invalidate/notify--> same status, invalidated = true
any    --shape differs--> treated as none (the old row is replaced by the next write)
```

The shown status is computed on every read by the planner: `ok`, `stale`, `error`, or `pending` (derived).

## DeferredStatus (in a read result)

| Field | Type | Meaning |
|---|---|---|
| `path` | text | The field. |
| `status` | `ok` \| `stale` \| `pending` \| `error` | As above. |
| `as_of` | timestamp or null | Of the value shown. |
| `error` | text or null | Kind of failure when `error` or `stale`. |
| `attempts` | int or null | Of the last call. |
| `waiting_for` | list of paths | For `pending`: the inputs or upstream fields it waits on; empty when it was simply not fetched yet (then `reason: not_fetched`). |
| `reason` | text or null | `inputs_missing`, `upstream`, `not_fetched`. |

`DocumentResult.deferred` is a tuple of these, in path order, for every deferred field of the schema, so a reader never has
to guess. `waiting_for` and `reason` are filled only when the read asks `include: pending` (FR-026); otherwise they are
empty.

## Planner types (pure)

| Type | Fields |
|---|---|
| `FieldPlan` | `field`, `status`, `show` (value or absent), `action` (`none` or `fetch`), `reason`, `level` (depth in the deferred graph) |
| `Plan` | `fields` (ordered by level then path), `levels` (list of lists of field patterns) |

## SchedulerRun and SchedulerReport

| Field | Type | Meaning |
|---|---|---|
| `run_id` | text | Unique id. |
| `hook` | text | `name@version`. |
| `started`, `finished` | timestamp | |
| `attempted`, `succeeded`, `failed`, `skipped` | int | Counted in (document, field) pairs. |
| `calls` | int | Calls made (batches count once). |
| `reason` | text or null | Why the run stopped early (`lease held`, `breaker open`, `time limit`). |

`SchedulerReport` is the same plus the list of (document, field, outcome) for the run, outcome in `ok`, `failed`,
`skipped`.

## SchedulerLease

| Field | Type | Rule |
|---|---|---|
| `hook` | text | Key. |
| `owner` | text | A random id of the run. |
| `expires` | timestamp | Live while later than the clock; renewed per batch; released at the end of the run. |

## Notification (not stored)

`topic`, optional list of documents. Effect only: sets `invalidated` on matching records. Delivered at least once; safe to
repeat.

## Breaker state (memory only)

Per `name@version`: `state` (`closed`, `open`, `half_open`), `failures` (consecutive), `opened_at` (monotonic seconds).
Platform settings: threshold 5, cool-down 30 s.

## Relationships

- A deferred field belongs to a schema; its records belong to documents. A record is current only while the field's
  shape digest matches.
- The deferred graph is the subgraph of derived fields (computed, impure, deferred) that the Feature 002 graph already
  builds; deferred nodes may point at any derived node, nothing but deferred nodes points at them.
- Records are never joined with ledger entries, the effect log or model errors.
