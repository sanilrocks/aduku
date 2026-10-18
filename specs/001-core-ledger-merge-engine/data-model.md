# Data Model: Core Ledger and Merge Engine

All types are immutable. Names follow SPEC 4.1, 5 and 10. Python types are indicative; the shapes are the contract.

## Entities

### Entry (ledger row; only accepted contributions)

| Field | Type | Notes |
|---|---|---|
| `id` | string | ULID; unique; immutable |
| `doc` | string | document id |
| `seq` | int | per-document, strictly increasing, consecutive within a request; assigned at commit |
| `request_id` | string | shared by all entries of one request |
| `tier` | string | must be in the schema tier list and permitted for the contributor |
| `path` | string | normalized (SPEC 3.2) |
| `op` | `set` \| `delete` | core ops only |
| `value` | Ion value \| absent | absent for `delete` (engine uses `TOMBSTONE`) |
| `contributor` | string | authenticated identity |
| `tx_time` | timestamp | informational; never read by P1-P3, P6, P7 |
| `idem_key` | string? | per-entry key; unique per document when present; required by non-idempotent merge behaviors |
| `request_key` | string? | caller-supplied retry key, same on all entries of the request |
| `request_hash` | string? | SHA-256 of the canonical request (SPEC 4.2 item 4); set when `request_key` is present |
| `schema_version` | int | version in force at admission |
| `lenient` | bool | true if accepted under the non-rejection flag |
| `meta` | struct? | free-form |

Constraints: unique `(doc, seq)`, unique `(doc, idem_key)` where not null, unique `(doc, contributor, request_key)` per request where not null; `UPDATE`/`DELETE` forbidden.

### Request (transient, plus its audit form)

`{ doc, entries[], contributor, expected_seq?, lenient? }`. Accepted or rejected as a whole. Entries carry `tier`,
`path`, `op`, `value`, `idem_key?`, `meta?`; request-level `request_key?`. A request has no row of its own; `request_id` on its entries is its identity.

### Schema (immutable per version)

```
Schema {
  id, version,
  tiers: [name]                      # lowest to highest; rank = index
  lenient_allowed: {tiers[], contributors[]}
  rejection_audit: {enabled, max_entries?, max_age?}
  fields: [FieldSpec]                # matched exact path first, then `*`, then `**`
  document_validate: [BehaviorRef]
}
FieldSpec {
  pattern, type, merge: BehaviorRef, default?, on_delete: absent|default,
  validate: [BehaviorRef], ops: set[], authority?: [tier|role],
  derived?: computed|deferred       # parsed only; makes the path unwritable (FIELD_NOT_WRITABLE)
}
BehaviorRef { name, version, args }
```

Activation rule: a schema version that drops or renames a tier used by any entry is refused. Flag permission: the non-rejection flag
is allowed when `lenient_allowed.contributors` lists the contributor or `lenient_allowed.tiers` lists every tier used in the request.
Rules enforced at load: merge behavior declares `associative`; `default` conforms to `type`; no `default`/`merge`
on derived fields; validators may not depend on deferred fields; `rejection_audit.enabled` needs a retention limit;
`lenient_allowed` names known tiers; tier names unique; cycles among derived fields refused.

### Behavior (registry entry)

`{ name, version, kind: merge|validate, purity: pure, laws: {associative, commutative?, idempotent?}, impl }`.
Merge `impl`: `combine(path, old, new) -> value` over `absent | value | TOMBSTONE`. Validate `impl`:
`check(view) -> [Violation]` where `Violation = {rule_id, path_scope, message}`.

### Layer

In the engine a layer maps a path to a fold state (a plain value, a tombstone, or a re-created value), see the merge table
below. A struct written to a field-wise field is spread over its child paths.

`Defaults` or `Squash(tier)`: a mapping from leaf path to value or `TOMBSTONE`. Not persisted in this feature (snapshots are out of scope).

### DocumentView (cache row, derived)

| Field | Notes |
|---|---|
| `doc` | |
| `watermark_seq` | highest `seq` included |
| `schema_version`, `rules_fingerprint` | version it was derived under (FR-026) |
| `document` | canonical Ion text (R-3) |
| `model_errors` | list of ModelError |
| `provenance` | `{skipped, conflicts}`: entries and paths left out of the document (`explain` is worked out from the ledger when asked, not stored) |

Disposable: deleting it never changes meaning; `rebuild` reproduces it bit-for-bit.

### ModelError

`{ rule_id, path_scope, message, introduced_by: {request: id} | {schema_version: n} }`. Identity is
`(rule_id, path_scope)`. The set is a pure function of ledger plus schema versions (R-7).

### Reason (rejection)

`{ entry_index?, code, rule?, path?, message }` with codes from SPEC 12.5. A rejected request carries every reason.

### RejectionAuditRecord (separate store)

`{ id, doc, contributor, tier(s), lenient, attempted_entries, reasons[], schema_version, time }` (in `metadata_only` mode
`attempted_entries` keep path and op only, no values) plus per-document log
metadata `{pruned_count}` and the per-document override `{doc, enabled|inherit}`. No foreign keys to entries; nothing
in `engine/*` reads it.

## Relationships

- Document 1..n Entry (ordered by `seq`); Request 1..n Entry via `request_id`.
- Schema versions are referenced by `Entry.schema_version`, `DocumentView.schema_version` and `ModelError.introduced_by`.
- DocumentView 1..1 Document (cache); RejectionAuditRecord n..1 Document (no integrity link to the ledger).

## State and validation rules

### Per-path merge state (P1/P2/P3)

States: `absent`, `value(v)`, `tombstone`, `recreated(v)` (a value written after a tombstone; it keeps the reset).

| old \ new | `set v` | `delete` |
|---|---|---|
| absent | `rule(absent, v)` | tombstone |
| value(u) | `rule(u, v)` | tombstone |
| tombstone | `recreated(rule(absent, v))` | tombstone |
| recreated(u) | `recreated(rule(u, v))` | tombstone |

A `recreated(w)` on the right discards everything on the left. P3 turns `recreated(w)` into `w`.

A tombstone on a struct path also clears accumulated descendants. P3 removes tombstones; the path becomes absent, or
the default when `on_delete: default`.

### Request lifecycle

```
received ─► V0 schema checks (all entries; collect every reason) ─► [any reason] ─► REJECTED (optionally audited)
        └► lenient permission check ─► [not permitted] ─► REJECTED
        └► dry-run derive on whole request ─► V1/V2 validators
                 ├─ no violations ─► ACCEPTED
                 ├─ violations, flag permitted ─► ACCEPTED, errors recorded (no-new-errors rule skipped)
                 └─ violations, unflagged ─► errors_before empty ? REJECTED(VALIDATION_FAILED)
                                                              : new errors ⊄ before ? REJECTED(MODEL_ERROR_INTRODUCED) : ACCEPTED
ACCEPTED ─► one transaction: assign consecutive seq, insert entries, invalidate view
```

### Admission order (collect-all-reasons)

1. Duplicate check (R-10): same `(doc, contributor, request_key)` and same hash returns the original result and stops; same key,
   different hash is `REQUEST_KEY_REUSED`; a reused entry `idem_key` is `DUPLICATE_IDEM_KEY`.
2. V0 for every entry: `UNKNOWN_PATH`, `TYPE_MISMATCH`, `OP_NOT_ALLOWED`, `TIER_NOT_PERMITTED`, `NOT_AUTHORIZED`,
   `FIELD_NOT_WRITABLE`, merge type conflict; plus `LENIENT_NOT_PERMITTED` for the request.
3. If V0 clean: dry-run derivation of the whole request, V1/V2, then the no-new-errors rule.

V0 reasons are always reported together; V1/V2 reasons are reported only when V0 is clean (they need a resolvable
document). Decided 2026-10-18 and reflected in FR-007, user story 1 scenario 6 and SPEC 10.1.

## Derived output shapes

- `get` returns `{ document, model_errors[], status }` (`status` here is `ok`; `pending`/`stale` arrive with Feature 3).
- `explain(doc, path)` returns `{ value, winner: entry id | "default", rule: name@version, inputs[], shadowed[],
  model_errors[] }`.
