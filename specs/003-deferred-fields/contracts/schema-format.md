# Contract: Schema Format, delta to Features 001 and 002

Ion text, as before. This adds the declaration of a deferred field and the schema-level default for its failure policy.

## Declaration

```
"/creditScore": { type: int,
                  deferred: { hook: "credit.fetch_score@1", depends: ["/name"],
                              refresh: once, timeout: "2s", invalidate_by: [admin] } },
"/fxRate":      { type: decimal,
                  deferred: { hook: "fx.rate@2", depends: ["/currency"],
                              refresh: per_fetch, ttl: "5m", on_failure: serve_stale, invalidate_by: [admin] } },
"/riskBand":    { type: string,
                  deferred: { hook: "risk.band@1", depends: ["/creditScore", "/country"],
                              refresh: [once, scheduled::{every: "1h"}], on_failure: serve_stale,
                              retry: {max: 2, backoff: "200ms"}, deadline: "6s", invalidate_by: [admin] } }
```

| Option | Required | Values |
|---|---|---|
| `hook` | yes | `"name@version"` of a registered hook (`@1` if omitted) |
| `depends` | yes | non-empty list of declared paths (plain, computed, impure or deferred) |
| `refresh` | yes | one policy or a list: `once`, `per_fetch`, `scheduled::{every: "<duration>"}`, `scheduled::{cron: "<5 fields>"}`, `notification::{topic: "<name>"}` |
| `ttl` | with `per_fetch` only | duration, at least `"1s"` |
| `on_dependency_change` | no, with `once` only | `keep` (default) or `recompute` |
| `on_failure` | no | `error` (default, or the schema's `deferred_on_failure`), `serve_stale`, `absent` |
| `on_missing` | no | on the field, as for computed fields: `null` (default) or `absent`; what a pending field shows |
| `timeout` | no | per attempt; default `"2s"`, at most `"30s"` |
| `retry` | no | `{max: 0..5, backoff: duration up to "10s"}`; default `{max: 2, backoff: "200ms"}` |
| `deadline` | no | the whole retry envelope of one call; default `"6s"`, at most `"60s"` |
| `invalidate_by` | yes | non-empty list of tiers or roles allowed to call `invalidate` and `scheduler_run` for the field |

Durations are strings with a unit (`ms`, `s`, `m`, `h`, `d`). Cron expressions have five fields (minute, hour, day of
month, month, day of week) with `*`, lists, ranges and steps, in UTC.

A deferred field carries a `type` (required: the answer is checked against it) and may carry `doc`; it MUST NOT carry `merge`, `default`, `validate`, `computed` or `ops`.

## Schema-level setting

```
schema::{ ..., deferred_on_failure: serve_stale, ... }      // the default `on_failure`; absent means error
```

## Load-time checks (each names the field; every problem is reported)

| Code | Cause |
|---|---|
| `DEFERRED_DECLARATION_INCOMPLETE` | `hook`, `depends`, `refresh` or `invalidate_by` missing or empty |
| `BAD_REFRESH_POLICY` | unknown policy, malformed `scheduled`/`notification` options, `every` and `cron` both or neither, bad cron expression, empty topic |
| `BAD_DEFERRED_OPTION` | an option that does not belong to the policy (`ttl` without `per_fetch`, `ttl` missing for it, `on_dependency_change` without `once`), a duration that does not parse, a value outside the platform bounds, an unknown `on_failure` or `on_missing` value, an `on_missing: error` |
| `UNKNOWN_TIER` | `invalidate_by` names something that is neither a tier nor a declared role |
| `UNKNOWN_BEHAVIOR` | the hook is not registered (as a hook) |
| `UNKNOWN_DEPENDENCY` | a `depends` path is not declared |
| `DEPENDENCY_CYCLE` | a loop among derived fields, now including deferred fields |
| `DEPENDS_ON_DEFERRED` | a computed field, validator, document rule or default depends on a deferred field (unchanged) |
| `COMPUTED_AND_DEFERRED` | both declarations on one field (unchanged) |
| `DERIVED_HAS_MERGE_OR_DEFAULT` | `merge` or `default` on a deferred field (unchanged); `validate` on it keeps `DEPENDS_ON_DEFERRED` |

## Compatibility

Feature 001 parsed `deferred:` loosely; a schema that declared one without `invalidate_by` (and `refresh`) is now refused
with `DEFERRED_DECLARATION_INCOMPLETE`. The two such fixtures in this repository are updated in the same change.
`rules_fingerprint` covers the deferred declaration, so editing it discards cached views (harmless: they are re-derived).
