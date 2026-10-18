# Contract: Schema File Format

Ion text, aduku's own vocabulary (SPEC 5.2), restricted to what this feature evaluates. Fields for computed and
deferred behavior are parsed and recorded (they make a path unwritable) but not evaluated here.

```ion
schema::{
  id: "customer", version: 7,
  tiers: [backfill, user, admin],
  lenient: { allowed: [backfill] },                 // tier names; contributors via contributors: ["job-17"]
  rejection_audit: { enabled: true, retention: { max_entries: 1000, max_age: "90d" } },
  fields: {
    "/name":     { type: string, merge: lww, validate: [non_empty, max_len::{n:120}] },
    "/age":      { type: int, merge: max, validate: [range::{min:0, max:150}] },
    "/tags":     { type: "list<string>", merge: union },
    "/address":  { type: struct, merge: struct },
    "/currency": { type: string, merge: lww, default: "USD", on_delete: absent },
    "/plan":     { type: string, merge: lww, default: "free", authority: [admin] },
    "/displayName": { type: string, computed: { macro: upper, args: ["/name"] } },   // parsed, unwritable
    "/*":        { merge: lww }
  },
  document_validate: [ "acme.max_spouses_by_country@1" ]
}
```

## Rules

| Key | Rule |
|---|---|
| `id`, `version` | required; `(id, version)` is immutable once registered |
| `tiers` | required, non-empty, unique names, lowest first |
| `fields` | exact path > `*` (one segment) > `**` (any depth); first most specific match wins |
| `type` | `string`, `symbol`, `int`, `decimal`, `float`, `bool`, `timestamp`, `struct`, `list`, `list<T>`, `any`; entries must conform (`TYPE_MISMATCH`). `list<T>` is not valid Ion syntax, so write it as a quoted string: `type: "list<string>"`. A null value conforms only to `any`. |
| `merge` | one of `lww`, `max`, `min`, `union`, `struct`, or `custom::"name@v"` registered with `associative` |
| `default` | static Ion value conforming to `type`, on a field with an exact path (`DEFAULT_ON_PATTERN` for a wildcard pattern); per-child for structs unless the struct declares one |
| `on_delete` | `absent` (default) or `default` |
| `authority` | tiers or roles allowed to write; absent means any permitted tier |
| `validate`, `document_validate` | registered pure validators; may not depend on deferred fields |
| `computed.impure` | declared impure compute (parsed, not evaluated in Feature 001): needs `hook`, `depends`, `reason`, `timeout`, `retry`, `deadline`; optional `failure_mode: reject_on_failure \| model_error_on_failure` |
| `impure_failure_mode` | schema-level default for impure `failure_mode`; default `reject_on_failure` |
| `ops` | optional list of the operations a field accepts; only `set` and `delete` exist in this version (`increment`, `append`, `reset` are `OP_NOT_SUPPORTED`, anything else `UNKNOWN_OPERATION`) |
| `rejection_audit.retention.max_age` | a duration above zero such as `90d`, `12h`, `30m`, `45s`; zero is `RETENTION_NOT_POSITIVE` |
| `roles` | optional list of role names that `authority` may name besides tiers; any other name in `authority` is `UNKNOWN_TIER` |
| `lenient.allowed` | tiers (and optionally contributors) that may send the non-rejection flag |
| `rejection_audit` | default `enabled: false`; `mode: full \| metadata_only` (default `full`); enabled requires `max_entries` or `max_age` |

## Load failures (all reported together as `SchemaError`)

Unknown behavior or version; merge behavior missing `associative`; type error in `default`; `default` or `merge` on a
derived field; field with both `computed` and `deferred`; dependency cycle among derived fields; validator, computed field or default
dependent on a deferred field; `rejection_audit` enabled without retention; unknown tier in `lenient.allowed` or `authority`;
impure compute missing `reason`, `timeout`, `retry` or `deadline`, or depending on a deferred field; duplicate tier; `on_delete: default` on a field without a default; re-registering `(id, version)` with different content.
