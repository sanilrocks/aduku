# Contract: Schema format, delta to Features 001 to 004

One new top-level key. Everything else is unchanged.

```ion
purge_by: [admin, compliance]      // tiers or declared roles that may purge a document; absent or empty: nobody
```

- Each name must be a declared tier or a declared role; otherwise the schema is refused with `UNKNOWN_TIER` (the existing code,
  as for `invalidate_by`), naming `purge_by` and the name.
- A duplicate name is ignored. An empty list is allowed and means nobody (the default).
- `purge_by` is part of the schema version's fingerprint, so changing it is a new version (SPEC 14: a non-breaking change class,
  no rebuild).
- `purge_by` is checked as `invalidate_by` is: the caller's tier or one of its roles must be listed.

## Load codes

No new code. `UNKNOWN_TIER` is reused. Existing vectors are unchanged.

## Schema check (CLI)

`aduku schema check FILE` prints the JSON list `purge_by`; an empty list means nobody may purge.
