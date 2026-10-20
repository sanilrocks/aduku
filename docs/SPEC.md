# aduku — Specification

Status: Draft 0.13 · Audience: implementers and reviewers

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY** are used as in RFC 2119.

> Naming: **aduku** (from the Tamil *adukku*, "to put in order") is the project name. **Document Oriented
> Design (DOD)** names the design approach. "Document-oriented" is commonly used for document databases, so check
> for collisions before using DOD publicly.

---

## 1. Overview

aduku implements Document Oriented Design (DOD), a persistence architecture in which:

1. **Writers contribute facts, not state.** Every change is an immutable *entry* in an append-only
   *ledger*: a create, update or delete against an x-path inside a typed document.
2. **The document is derived.** A *schema* — a single, central, annotated definition — declares, per field,
   how contributions are validated, merged, defaulted, computed and enriched. The current document is the
   deterministic result of running the ledger through the schema.
3. **Sources have rank.** Each entry carries a *tier* (e.g. `backfill < user < admin`). Higher tiers win
   conflicts unless the field's merge rule says otherwise.
4. **Behavior is bound by annotation.** Schema annotations reference named *behaviors* (presets or custom
   code) in a *behavior registry*. The engine owns sequencing; behaviors own field semantics.

### 1.1 Goals

- Full provenance: every value in a document can be traced to entries and rules.
- Deterministic, order-tolerant merging across many uncoordinated writers.
- One place (the schema) that defines the meaning of a document type.
- Derived data (computed and deferred fields) that contributions can never corrupt.
- Safe side effects: IO is confined to explicitly declared hooks.
- Reversible: removing or correcting a contribution recomputes to the correct result.

### 1.2 Non-goals (v1)

- Real-time collaborative text editing or ordered-list CRDTs (see 6.6).
- Cross-document transactions or joins (see 17, open questions).
- Prescribing a storage engine, wire protocol or transport.
- Authentication; the spec assumes the ledger receives an authenticated *contributor identity*.

### 1.3 Terminology

| Term | Meaning |
|---|---|
| **Document** | A typed Ion/JSON value rooted at `/`, identified by a document id. |
| **Path** | An x-path addressing a node in a document (section 3.2). |
| **Entry** | One immutable ledger record: a contribution to one path (section 4). |
| **Tier** | An ordered priority class of contributors, e.g. `backfill`, `user`, `admin`. |
| **Seq** | Per-document, strictly increasing arrival number assigned by the ledger at commit. |
| **Schema** | Versioned, central definition of fields, annotations and behaviors (section 5). |
| **Behavior** | A named function plus metadata in the registry (section 5.4). |
| **Squash** | Reducing a set of entries to one state by folding them with merge rules. |
| **Layer** | The squashed state of one tier, or the defaults layer. |
| **Tombstone** | The Ion annotation `deleted::null` used as a delete value (section 6.7). |
| **Derived field** | A field not writable by contributions: *computed* (pure or impure) or *deferred*. |
| **Impure compute** | A computed field whose behavior performs IO (a side effect), runs inside P4 and request handling, and is strongly consistent; used sparingly (section 9.3.1). |
| **Effect log** | Append-only record of impure-compute outcomes (value or exhausted failure), keyed by input fingerprint; makes derivation replayable (section 9.3.1). |
| **Derived store** | Persistent store for deferred-field results (section 9.5). |
| **Model error** | A recorded violation of a validation rule by a document, stored with the document and returned on every read (section 10). |
| **Non-rejection flag** | A per-request marker (a *lenient write*) that records validation-rule failures as model errors instead of rejecting the request (section 10.2). |
| **Rejection audit log** | Optional, separate, audit-only record of rejected requests; never read by any merge or document (section 4.5). |

### 1.4 Implementations and ports

This repository holds the language-neutral specification and conformance vectors. Implementations are language
ports named `aduku-<lang>` (for example `aduku-py`, `aduku-java`, `aduku-rs`, `aduku-js`). A port conforms if it passes
the conformance tests in section 16 and the published vector set, and it MUST produce byte-identical documents and
model errors for identical ledgers and schema versions.

---

## 2. Architecture

```
 writers (backfill job, user, admin, ...)
        │  contribute entries
        ▼
 ┌───────────────────────────── admission (P0) ───────────────────────────────┐
 │ authenticate → authority/flag check → schema checks → dry-run derive and   │
 │ validate (no deferred values) → accept (assign seq) │ reject (error)        │
 └───────┬───────────────────────────────────────────────────┬───────────────┘
         ▼ accepted                                          ▼ rejected (optional)
 append-only LEDGER  (accepted entries only)      REJECTION AUDIT LOG (audit-only, capped)
         │
   read / refresh│
                 ▼
 P1 squash per tier ─► P2 merge layers (defaults ⊗ tier0 ⊗ tier1 ⊗ … ⊗ tierN)
                 ─► P3 apply tombstones ─► P4 computed (DAG: pure, or impure with recorded outcomes)
                 ─► P6 validate document ─► P7 project (as-of, redact, provenance)
                                                  ▲ overlay only (display; nothing reads it back)
        DERIVED STORE ◄── P5 deferred stage (stand-alone, impure): reads P1-P4 output, writes the store
                          (once │ per-fetch │ scheduled │ notification), retries, per-field failure policy
```

Components: **Ledger**, **Schema registry**, **Behavior registry**, **Engine** (phases P0–P7),
**Derived store**, **Deferred scheduler**, **Snapshot cache**.

---

## 3. Data model

### 3.1 Values

Documents use the Ion data model, which is a superset of JSON:

- Primitives: null (typed), bool, int, decimal, float, timestamp, string, symbol, blob, clob.
- Containers: **struct** (unordered field → value) and **list** (ordered values). Sexp is not used.
- **Annotations**: zero or more symbols prefixed to any value (`computed::"X"`).

A JSON profile MAY be offered. In the JSON profile annotations are carried as
`{"$ann": ["deleted"], "$value": null}`.

*Absent* (no field), *null* (an explicit null value) and *tombstone* (`deleted::null`) are three distinct states.

### 3.2 Paths

```
path     = "/" [ segment *( "/" segment ) ]
segment  = field-name / list-key
field-name = 1*( ALPHA / DIGIT / "_" / "-" )
list-key = "[" key-expr "]"      ; keyed list element, e.g. /items[sku="A1"]/qty
```

- A path addresses the *leaf or container* named by its last segment.
- Writing `/address/city` implicitly creates the struct `/address` if absent.
- Schema field patterns MAY use `*` for one segment and `**` for any depth.
- Paths MUST be normalized (no empty or `.`/`..` segments) before use.
- A struct written to a field merged field by field is spread over its child paths, so each of its keys MUST be a valid
  field name, and each child is checked like a write of its own (authority, derived fields, type).

### 3.3 Types

The schema assigns each path a type (section 5.2). Entries whose value does not conform MUST be rejected at admission.

---

## 4. The ledger

### 4.1 Entry

```
entry {
  id:             string      // globally unique (ULID/UUIDv7); MUST be immutable
  doc:            string      // document id
  seq:            int         // per-document, strictly increasing; assigned by ledger at commit
  tier:           symbol      // contributor tier, must exist in the schema's tier list
  path:           string      // see 3.2
  op:             symbol      // "set" | "delete" (core); extension ops in 4.3; "layer" is written only by compaction (15.3)
  value:          any         // omitted for delete; for delete the engine uses deleted::null
  contributor:    string      // authenticated identity (user id, job id, service id)
  tx_time:        timestamp   // wall-clock at commit (informational; never used for ordering)
  valid_time:     timestamp?  // OPTIONAL: when the fact became true in the world; a timestamp with an offset (BAD_VALID_TIME otherwise);
                              // read only by as_of valid-time reads (12.2, 15.4); part of the request hash only when given (4.2 item 4)
  idem_key:       string?     // OPTIONAL per-entry key; REQUIRED for non-idempotent merge behaviors (6.3); reuse is rejected
  request_key:    string?     // OPTIONAL caller-supplied retry key shared by the entries of one request (4.2 item 4)
  request_hash:   string?     // SHA-256 of the canonical request (4.2 item 4); set when request_key is present
  schema_version: int         // schema version in force at admission
  request_id:     string      // entries accepted together share it; a request is atomic (4.2)
  lenient:        bool?       // true if written with the non-rejection flag (section 10.2)
  meta:           struct?     // free-form (batch id, ticket, reason text)
  synthetic:      string?     // the compaction id, on the entries compaction writes in place of others (15.3)
  purged:         bool?       // true for the placeholder a purge leaves in place of an erased entry (15.5)
}
```

### 4.2 Invariants

1. Entries are **immutable and append-only**. Corrections are new entries. The only operations that change or remove an
   existing entry are **compaction** (15.3) and **purge** (15.5). Both are recorded operations and atomic; compaction is
   verified to change no answer before it commits, and a purge leaves a placeholder so that `seq` stays continuous.
2. `seq` is the only ordering within a tier. `tx_time` MUST NOT affect merge results.
3. **Only accepted contributions become entries.** A rejected request records **nothing** in the ledger. When rejection
   auditing is enabled (section 4.5) it is written to the separate rejection audit log, which is never read by any fold.
4. **Retries are no-ops.** A request MAY carry a caller-supplied `request_key`. The request hash is SHA-256 over the canonical
   form of `(doc, contributor identity, lenient flag, entries)` (each entry as tier, path, op, value, idem_key, meta, and
   `valid_time` only when the entry carries one; `expected_seq` excluded). If the ledger already holds a request with the same `(doc, contributor, request_key)`:
   - same request hash: the request is a **duplicate**. It is not admitted, nothing is validated, audited or written, and the
     response is the same shape as the original success: the original `(id, seq)` list and `request_id`, the model-error
     changes recorded for that request, and the latest materialized view. A retry therefore never changes the response shape
     or the ledger.
   - different request hash: rejected with `REQUEST_KEY_REUSED`; the original is kept.

   Without a `request_key` no duplicate detection is done and every request is new, so a deliberate repeat (set a value,
   another writer changes it, set it back) is always applied. Separately, an entry whose `idem_key` was already seen for the
   document makes its request fail with `DUPLICATE_IDEM_KEY`.
5. Appends to a document are serialized (single-writer per document, or optimistic with `expected_seq`).
   Appends to different documents are independent.
6. **A request is atomic: all of its entries are accepted or none are.** There is no partial acceptance. Accepted
   entries of one request are committed together with consecutive `seq` numbers and share a `request_id`. The request is
   the unit of acceptance, rejection, audit and model-error attribution. If storage fails during the commit, no entry of the
   request becomes visible and the caller receives a `STORAGE_ERROR`, which is not a rejection.

### 4.3 Operations

Core (v1): `set` (create or update — the engine does not distinguish, it combines) and `delete`
(value is the tombstone).

Extension (v1.1, MUST be declared by the schema per field before use):
`reset::null` (revert the field to its default), `increment::N` (counter), `append::V` (ordered log append).
Each extension op requires a merge behavior that defines it (section 6.4).

### 4.4 Tiers

- The schema declares an ordered tier list, lowest to highest: `tiers: [backfill, user, admin]`.
- Tier names, count and order are defined by the schema author; `backfill < user < admin` is only an illustration used in
  examples. The engine assumes nothing about particular names. Presets (ready-made tier lists for common cases) MAY be
  offered for scaffolding, but a preset is only a starting schema and has no special meaning to the engine.
- One tier order applies to every field of a schema in v1; per-field ordering is future work (section 17, item 2).
- The tier set is extensible; adding a tier at any rank is a schema change (section 14).
- A contributor identity maps to one or more *permitted tiers*; the ledger MUST reject an entry whose
  tier is not permitted for the contributor.
- A schema version that drops or renames a tier used by any entry is refused (`TIER_IN_USE`) when it is activated, when a
  document is pinned to it (for that document's entries) and when a service starts with it already active or pinned.

### 4.5 Rejection audit log

Rejected requests are not ledger entries. Whether they are kept anywhere is a configurable, audit-only concern.

**Setting** (schema level per document type, with an optional per-document override; default **off**):

```ion
rejection_audit: { enabled: false, mode: full,          // full | metadata_only
                   retention: { max_entries: 1000, max_age: "90d" } }   // both optional
```

- When `enabled: false`, a rejected request leaves no record.
- When `enabled: true`, each rejected request is appended to the **rejection audit log**, a store separate from the
  ledger, with: document id, contributor, tier, `lenient` flag, the attempted entries (values subject to `redact`),
  every rejection reason, schema version, and time.
- `mode` (default `full`): with `mode: metadata_only` the log keeps who, when, tier, `lenient` flag, the attempted paths
  and operations, every reason and the schema version, but never the attempted values. Use it for sensitive data.
- The log MUST NOT be read by any merge, validation, model-error calculation, derivation or document read, MUST NOT
  consume `seq` numbers, and MUST NOT affect any document. Deleting the log MUST NOT change any document.
- It is readable only through the audit interface (section 12.4) under access control at least as strict as the document's.
- Auditing is best effort: if writing the audit record fails, the request's rejection is still returned and the failure is
  logged; auditing never changes a request's outcome.
- With `mode: metadata_only` the record omits the attempted values (see the setting above).

**Retention** caps the log so it cannot grow without bound (and so spam is not stored forever):

- `max_entries`: keep at most N rejected requests per document; when exceeded, the oldest are pruned first.
- `max_age`: prune records older than the duration.
- If both are set, a record is pruned when **either** limit is exceeded.
- If neither is set while auditing is enabled, the schema MUST be refused at load (an unbounded log is not allowed).
- Pruning removes the oldest records first (by recorded time), runs after each audit write and on demand, is permanent and
  recorded as a count in the log's own metadata; it never touches the ledger.
- Changing a retention setting applies at the next prune; lowering it may remove existing records.

---

## 5. The schema

### 5.1 Principles

1. The schema is the **single source of truth** for field semantics. No behavior may live only in a client.
2. The schema is **versioned and immutable per version**; changing it creates a new version (section 14).
3. Annotations are **declarative references** to behaviors; the schema never embeds engine logic.
4. Schema load MUST fail closed on: unknown behavior, dependency cycle, type error, unreachable tier, a validation
   rule, computed field or default that depends on a deferred field (sections 9.1, 10.5), an impure compute without `reason`,
   `timeout`, `retry` and `deadline` or with an unknown `failure_mode` (section 9.3.1), a dynamic default that depends on
   itself (section 8), `rejection_audit` enabled without a retention limit, and arguments of a built-in validator that are
   of the wrong kind or range (`BAD_BEHAVIOR_ARGS`, for example a non-numeric `range` bound or `max_len` without `n`).

### 5.2 Structure (Ion illustration)

Parameterized types such as `list<string>` are not valid Ion syntax, so they are written as quoted strings.

```ion
schema::{
  id: "customer", version: 7,
  tiers: [backfill, user, admin],
  lenient: { allowed: [backfill] },                           // who may send the non-rejection flag
  impure_failure_mode: reject_on_failure,                     // document-type default; a field's failure_mode overrides it
  rejection_audit: { enabled: true,
                     retention: { max_entries: 1000, max_age: "90d" } },

  fields: {
    "/name":     { type: string, merge: lww,   validate: [non_empty, max_len::{n:120}] },
    "/age":      { type: int,    merge: max,   validate: [range::{min:0, max:150}] },
    "/tags":     { type: "list<string>", merge: union },
    "/address":  { type: struct,  merge: struct },            // field-wise
    "/address/city": { type: string, merge: lww },
    "/country":  { type: string, merge: lww },
    "/spouses":  { type: "list<string>", merge: lww },          // whole-list replacement
    "/currency": { type: string, merge: lww, default: "USD", on_delete: absent },
    "/plan":     { type: string, merge: lww, default: "free",
                   authority: [admin] },                       // only admin may contribute

    "/displayName": { type: string,
                      computed: { macro: upper, args: ["/name"] } },
    "/label":       { type: string,
                      computed: { macro: custom::"acme.label@3",
                                  depends: ["/name", "/address/city"] } },

    "/taxId":       { type: string,                              // impure: use sparingly (section 9.3.1)
                      computed: { impure: { hook: "tax.lookup@2", depends: ["/country", "/name"],
                                            reason: "issued by the registry; needed before checkout",
                                            timeout: "2s", retry: { max: 3, backoff: "200ms" }, deadline: "6s",
                                            recompute_by: [admin],
                                            failure_mode: reject_on_failure } } },

    "/creditScore": { type: int,
                      deferred: { hook: "credit.fetch_score@1", depends: ["/name"],
                                  refresh: once, timeout: "2s", invalidate_by: [admin] } },
    "/fxRate":      { type: decimal,
                      deferred: { hook: "fx.rate@2", depends: ["/currency"],
                                  refresh: per_fetch, ttl: "5m", on_failure: serve_stale,
                                  invalidate_by: [admin] } },

    "/*":        { merge: lww }                                // fallback rule
  },

  document_validate: [ "acme.max_spouses_by_country@1" ]     // pure; no deferred inputs
}
```

### 5.3 Annotation vocabulary

| Annotation | Phase | Meaning |
|---|---|---|
| `type` | P0 | Ion type or `struct` / `list<T>`; checked at admission and after derive. |
| `merge` | P1/P2 | Combine rule for old vs new (section 6). |
| `default` | P2 | Static value placed in the defaults layer (section 8). |
| `on_delete` | P3 | `absent` (default) or `default`: what a tombstone on a defaulted field means (section 8). |
| `authority` | P0 | Contributor classes/tiers allowed to write the field (section 11). |
| `validate` | P0/P6 | Contribution-level and document-level validators (section 10). |
| `computed` | P4 | Pure macro over declared dependencies (section 9), or an `impure` hook with retries and recorded outcomes (section 9.3.1). |
| `failure_mode` | P4 | Field-level, impure computes only: `reject_on_failure` (default) or `model_error_on_failure` (section 9.3.1). |
| `impure_failure_mode` | P4 | Schema-level default for `failure_mode` (section 9.3.1). |
| `on_missing` | P4 | Computed and impure fields: `null` (default), `absent` or `error`, what the field does when an input has no value (section 9.4). |
| `recompute_by` | P4 | Impure computes: the tiers or roles allowed to call `recompute` (section 9.3.1). |
| `deferred` | P5 | IO hook with refresh policy and persistence (section 9.5). Requires `invalidate_by`: the tiers or roles allowed to call `hook.invalidate` and `scheduler.run` for the field. |
| `redact` | P7 | Read-time projection rule (e.g. role-based masking). |
| `lenient` | P0 | Schema-level: tiers/contributors allowed to use the non-rejection flag (section 10.2). |
| `rejection_audit` | P0 | Schema-level: enable the rejection audit log and its retention caps (section 4.5). |
| `doc` | — | Human description; no behavior. |

A field MUST NOT carry both `computed` (pure or impure) and `deferred`, and a derived field MUST NOT carry `merge` or `default`.

### 5.4 Behavior registry

Every behavior referenced by a schema is registered:

```
behavior {
  name:        string          // e.g. "lww", "upper", "acme.label"
  version:     int
  kind:        merge | computed | deferred | validate | redact
  phase:       P0..P7
  purity:      pure | io       // computed/merge/validate MUST be pure; deferred is io
  signature:   typed input and output schemas
  laws:        [associative, commutative, idempotent, ...]   // claimed algebraic laws (merge only)
  impl:        reference to code (preset | sandboxed module)
}
```

- **Presets** ship with the engine (section 9.3) and are versioned with it.
- **Custom** behaviors are registered by operators, referenced as `custom::"name@version"`, and run in the
  sandbox (section 13).
- The engine MUST refuse a `merge` behavior that does not declare `associative`.
- Behavior versions are pinned by the schema version; upgrading is a schema change.

---

## 6. Merge semantics

### 6.1 The combine function

Each merge behavior is a binary function over a field:

```
combine(path, old, new) -> value
```

`old` is the accumulated value from earlier folds (or *absent*); `new` is the incoming value. Within a fold,
`new` is always the later element in (tier, seq) order.

### 6.2 Order and the squash equivalence law

Define the total order on accepted entries: `(tier rank, seq)` ascending, where tier rank is the position in the schema's
one tier list (the same for every field in v1).

```
Squash(t)   = fold(combine) over accepted entries with tier t, in seq order, from absent
Merged(d)   = Defaults ⊗ Squash(t0) ⊗ Squash(t1) ⊗ … ⊗ Squash(tN)      (⊗ = field-wise overlay using combine)
```

**Equivalence law:** `Merged(d)` MUST equal the single left fold of `combine` over all accepted entries
sorted by `(tier rank, seq)`, starting from the defaults layer. This holds if and only if every `combine` is
**associative** with *absent* as left identity. This is why every merge behavior MUST declare associativity
and SHOULD be property-tested (section 16).

Consequences: tiers can be squashed independently and cached; late arrivals only invalidate the
affected tier's snapshot; a low-tier backfill arriving late can never override a higher tier's earlier value
under `lww`, because order is `(tier, seq)`, not arrival time alone.

### 6.3 Standard merge behaviors

| Name | combine(old, new) | Associative | Commutative | Idempotent | Notes |
|---|---|---|---|---|---|
| `lww` | `new` | yes | no | yes | Last in `(tier, seq)` wins. Default fallback. |
| `max` / `min` | `max(old,new)` / `min(old,new)` | yes | yes | yes | Total order on the type required; a schema using them on an unordered type is refused at load. |
| `union` | set union, order of first appearance | yes | no (order) | yes | List treated as a set by value equality or `key:` expr. |
| `intersection` | set intersection | yes | yes | yes | Rarely useful across tiers; allowed. |
| `struct` | recurse field-by-field per child rules | yes | per child | per child | Default for struct-typed fields. |
| `append` | `old ++ new` | yes | no | no | Ordered log; v1.1 op. |
| `sum` | `old + new` | yes | yes | **no** | Counter. Only safe with exactly-once entries (idem_key mandatory). |
| `first_wins` | `old` if present else `new` | yes | no | yes | Write-once fields. |
| `custom::…` | user-defined | MUST declare | declares | declares | Sandboxed, pure. |

Behaviors that are not idempotent (`sum`, `append`) MUST require `idem_key` on entries for that field.

### 6.4 Extension operations

- `increment::N` pairs with `sum`; `append::V` pairs with `append`; `reset::null` pairs with any rule.
- The schema MUST list the ops a field accepts: `ops: [set, delete, increment]`. Others are rejected at admission.

### 6.5 Type conflicts

If `old` and `new` have different types, the field's rule applies only if it declares a coercion; otherwise the
entry is **rejected at admission** (it never reaches combine). Schema type changes are governed by section 14.

Admission also checks what a write really writes. A struct written through a parent field is spread over its child
paths, and each child must be declared by the schema (`UNKNOWN_PATH`), allow the `set` operation (`OP_NOT_ALLOWED`), admit
the writer (`NOT_AUTHORIZED`), conform to its type and be mergeable by its rule (`TYPE_MISMATCH`). A `delete` of a path
needs the authority of every declared field beneath it, because it wipes them. A value with no canonical Ion form (a
timestamp whose UTC instant falls outside the years 1 to 9999, or a name Ion reads as a symbol id such as `$10`) is a
`TYPE_MISMATCH`.

### 6.6 Lists

- `union` / `intersection`: sets; identity is value equality, or a declared `key` expression for structs.
- `append`: ordered log, no reordering.
- `lww`: the list is an **atomic** value; the newer list replaces the older.
- Concurrent insertion/removal/reordering into a shared ordered list is out of scope for v1.

### 6.7 Tombstones (delete)

A delete's value is `deleted::null`. Rules, evaluated in `(tier, seq)` order:

1. `combine(old, tombstone)` = tombstone, for every rule, regardless of `old`.
2. `combine(tombstone, v)` = `recreated(v)` for a later non-tombstone `v` (**re-create**), where `v` is the rule applied to
   *absent*. `recreated(v)` is a value that remembers the reset: `combine(x, recreated(v))` = `recreated(v)` for any earlier `x`,
   and `combine(recreated(u), v)` = `recreated(rule(u, v))`. Without the marker a squashed layer would forget that it
   contains a delete, and the equivalence law (6.2) would fail: with `max`, the layers `0` then `[delete, -1]` must give
   `-1` (the delete discards the `0`), but merging a plain `-1` into `0` gives `0`. With the marker, `combine` stays
   associative over *absent*, values, tombstones and recreated values.
3. A tombstone at tier *t* therefore suppresses all lower-tier values and earlier same-tier values, but a
   later same-tier or higher-tier contribution re-creates the field.
4. A lower-tier write never resurrects a higher-tier delete (it sorts earlier in the fold).
5. A tombstone on a struct path deletes the whole subtree; later writes beneath it re-create only the written leaves.
6. Tombstones and the reset marker are **retained** in layers during P1/P2 so they can win; they are **removed** at P3
   (a `recreated(v)` becomes the plain value `v`, a tombstone makes the field *absent*), unless a default exists and
   `on_delete: default` is set; default is `absent`.

---

## 7. Lifecycle (phase pipeline)

| Phase | Name | Input → Output | Pure? |
|---|---|---|---|
| **P0** | Admission | request → accepted (entries appended) or rejected (error; optional audit record) | pure + auth; dry-run may call impure computes |
| **P1** | Squash tiers | accepted entries → one layer per tier | pure |
| **P2** | Merge layers | defaults + layers → merged document (tombstones kept) | pure |
| **P3** | Apply tombstones | merged → document with deleted fields removed | pure |
| **P4** | Computed | document → + computed fields (DAG order); depends only on non-deferred fields | pure; impure computes do IO with recorded outcomes (9.3.1) |
| **P6** | Validate document | document → ok / violations; sees no deferred value | pure |
| **P7** | Project | document (+ derived-store snapshot overlay) → response (as-of, redaction, provenance) | pure |
| **P5** | Deferred (stand-alone) | P1-P4 output (+ other deferred values) → derived store records | IO (confined) |

P5 is not in the P0-P4-P6-P7 chain. It reads the output of P1-P4 and writes only to the derived store. Nothing in
P0-P4, P6 or P7 consumes a deferred value to compute or validate; P7 attaches stored deferred values and their status to
the response for display only.

Ordering constraints:

- P1–P3 depend only on the ledger and schema version. Their result is cacheable by `(doc, watermark seq, schema version)`.
- Dependencies are one-way (section 9.1): a deferred field MAY depend on plain, computed and other deferred fields; a
  computed field, validator or `default` MUST NOT depend on a deferred field.
- A computed field whose dependency is *absent* follows its `on_missing` policy (section 9.4). A deferred field waits
  until every deferred field it depends on has a value (section 9.5).
- **Admission dry-run:** P0 derives the document that would result from the request (P1–P4 only; P5 is never run),
  including any impure computes (section 9.3.1), and evaluates validators on it (section 10). The only IO in P0 is the
  impure-compute calls; a request is never rejected because of a deferred field. P5 values are unavailable to validation.

---

## 8. Defaults

1. A field's `default` is a **static Ion value**, or a pure macro (item 7). It populates the **defaults layer**, the lowest layer in P2.
2. Any contribution to the field replaces the default under the field's merge rule. The default is the lowest layer of the
   fold (6.2), so under `lww` a contribution simply replaces it, while under `max` the larger of the default and the
   contributions wins.
3. Defaults MUST NOT be applied to derived fields.
4. Defaults for a struct-typed field apply per child (`/address/country`), not as a whole struct, unless the struct
   field itself declares a default.
5. A tombstone beats the default (field becomes absent) unless the field sets `on_delete: default`, in which case the
   field takes its default. Deleting a struct falls back child by child, each child by its own `on_delete`. Only a
   field with an exact path has a default (a wildcard pattern is refused at load, `DEFAULT_ON_PATTERN`). `reset::null`
   always reverts to the default.
6. Defaults are part of the schema version; changing a default changes merged documents on next read (it is
   not stored in the ledger). Systems that need stability MUST pin the schema version per document (section 14).
7. **Dynamic defaults.** A `default` MAY instead be a pure macro over declared paths (`default: { macro: upper, args: ["/name"] }`).
   It is evaluated after P3, only for a field that is absent after the merge (or tombstoned with `on_delete: default`), so it
   never takes part in the merge and any contribution replaces it. It MAY depend on plain and computed fields, MUST NOT
   depend on a deferred field or on itself (cycles refused at load), and the field stays writable (it is not a derived field).
   Its `explain` winner is `"default"`. Static defaults remain the only kind in Feature 001.

---

## 9. Derived fields

### 9.1 Dependency graph

- Every derived field declares its `depends` (computed) or `depends` (deferred) list of paths.
- At schema load, the engine builds a directed graph over derived fields and their dependencies, rejects
  **cycles**, and computes a topological order.
- **Dependency direction (taint rule).** Computed fields depend only on plain and other computed fields. Deferred fields
  may depend on plain, computed and deferred fields. A computed field, a validator (field or document level) or a
  `default` that depends on a deferred field is refused at load. Deferred-ness therefore only flows outward to
  other deferred fields and to display (P7). An impure compute is a computed field for this purpose: it may depend on
  plain, computed (pure or impure) fields, and other computed fields and validators may depend on it.
- Evaluation MUST pass **only the declared dependency values** to the behavior (section 13).

### 9.2 Non-writability (authority invariant)

**No contribution can override a derived field.** The ledger MUST reject, at P0, any entry whose path is a
derived field (or beneath one) with reason `FIELD_NOT_WRITABLE`. The request fails with an error and nothing is
appended; it is written only to the rejection audit log, and only if auditing is enabled (section 4.5). This rejection
cannot be bypassed by the non-rejection flag.

If a field **becomes** derived in a later schema version, historic accepted entries for it are retained but
**shadowed**: they are ignored by folds under the new schema and visible in provenance as `shadowed`.

**Why an entry is left out.** An entry the version in force cannot use is left out of the document, listed by seq in `skipped`,
and given one reason, worked out from the ledger and the version each time and never stored (so it cannot disagree with them):
`tier_not_in_schema` (its tier is not in the schema), `now_derived` (its field, or one above it, is now computed or deferred),
`no_longer_conforms` (it was admitted under another version and its value does not conform to the type now in force, a
parameterized type such as a list of strings included; a validation rule is a model error, not a shadow) or `cannot_be_merged`
(the merge rule cannot take its value). The checks run in that order and the first that applies names the reason. Reading
under the version that wrote the entries leaves out none. `get` returns the reasons with the document (`shadowed`), `explain`
lists those that touch the path (`shadowed_by_schema`; its `shadowed` keeps the meaning of 12.3) and the dry run of 14 counts them
per reason.

### 9.3 Computed fields

- Evaluated at P4, **after the full merge** (P1–P3).
- Behavior MUST be **pure and deterministic**: no clock, randomness, network, filesystem, environment, or
  access to fields outside `depends`. The one exception is a declared impure compute (9.3.1).
- **Presets** (initial set; all pure): `upper`, `lower`, `trim`, `concat`, `coalesce`, `len`, `sum`, `min`,
  `max`, `round`, `format_ts`, `hash`, `join`, `if`, `eq`. Preset names are reserved.
- **Custom macros** use a sandboxed expression or module language (CEL or WASM recommended) with resource limits. In `aduku-py` they are
  expressions in a small CEL-syntax subset with deterministic step and size limits (feature 002, `contracts/expression-language.md`).
- Results are **never written to the ledger**. They MAY be memoized keyed by
  `(doc, input fingerprint, behavior version)`.
- Computed values are presented with the annotation `computed::` (projection MAY strip it).

### 9.3.1 Impure computes (strongly consistent, use sparingly)

An impure compute is a computed field whose behavior has a side effect (a call to another system). It exists for cases
where the client cannot proceed without the value and no pure computation can produce it. It is not a deferred field:
it runs inside request handling, and its value, or its failure, is part of the same response.

```ion
"/taxId": { type: string,
            computed: { impure: { hook: "tax.lookup@2", depends: ["/country", "/name"],
                                  reason: "tax id issued by the registry; required before checkout",
                                  timeout: "2s", retry: { max: 3, backoff: "200ms" }, deadline: "6s",
                                  recompute_by: [admin],
                                  failure_mode: reject_on_failure } } }   // or model_error_on_failure
```

1. **Position.** P4, in the same DAG as pure computes. Other computed fields and validators MAY depend on it. It MUST NOT
   depend on a deferred field. The hook receives only declared dependencies, MUST be idempotent (it is retried), and runs
   outside the pure sandbox (section 13) with scoped credentials.
2. **When it runs.** In the P0 dry-run of every request whose inputs change its fingerprint, and in a read that finds no
   recorded outcome. The client receives the materialized value, or the rejection, synchronously.
3. **Retries.** Bounded `retry` with backoff inside `timeout` per attempt and `deadline` overall. Exhaustion produces a
   failure outcome. The hook is never run while the storage write lock is held: the engine derives at an observed
   watermark, calls hooks, then takes the lock and re-checks the watermark; if it changed, it re-derives (outcomes with an
   unchanged fingerprint are reused, so no repeat call).
4. **Recorded outcomes (effect log).** Each outcome is recorded as `{doc, field, input_fingerprint, hook_version, outcome:
   value | failure, attempts, effect_seq}` in the append-only effect log, which is separate from the ledger and holds no
   contributions. Derivation is a pure function of `(ledger, effect log, schema version)`: it reuses the latest recorded
   outcome for `(doc, field, input_fingerprint, hook_version)` and makes no call. `rebuild` and re-derivation therefore make
   no IO when outcomes exist. Outcomes produced by a **rejected** request are discarded.
5. **On exhaustion** (`failure_mode`, set on the field or once for the whole document type with `impure_failure_mode: ...` at schema level; a field setting overrides the schema setting; default `reject_on_failure`):

   | Value | Effect |
   |---|---|
   | `reject_on_failure` (default) | The request is rejected with `IMPURE_COMPUTE_FAILED`; nothing is recorded. On a **read** or re-derivation the failure is a model error instead (a read is never rejected). |
   | `model_error_on_failure` | The request is accepted. A model error `(impure:<path>, <path>)` is recorded, attributed to the request. The field follows `on_missing`. Dependent computes follow `on_missing`; validators that depend on the missing value are not evaluated and add no further error, so the failure is the one error. This error is exempt from the no-new-errors rule (10.3); every other new error still counts. |

6. **Recovery.** Fix the cause (correct the input data with a new contribution, or restore the dependent system) and
   recompute: `recompute(doc, field?)`, permitted to the tiers and roles in the field's `recompute_by` (without a field it covers only the fields the caller may recompute), calls the hook again with the current inputs and appends a new outcome that
   supersedes the earlier one. A successful outcome removes the model error on the next derivation. A changed input
   fingerprint triggers a new call on its own.
7. **Use sparingly.** The schema MUST give each impure compute a `reason`, `timeout`, `retry`, `deadline` and `recompute_by` (the tiers or roles allowed to call `recompute`, the same vocabulary as `authority`; any other caller gets `NOT_AUTHORIZED`). `schema
   check` reports the inventory of impure computes. Prefer a pure compute or a deferred field.
8. **Retention and access (proposed defaults).** The latest outcome per `(doc, field, input_fingerprint, hook_version)` is kept
   as long as the ledger entries it derives from exist, because replay needs it. Superseded outcomes are kept for audit,
   capped by `max_entries` per key and `max_age` (defaults 5 and "90d"), and pruning them never changes a derivation.
   Outcomes can hold sensitive values, so reading the effect log needs access at least as strict as the document's and
   values honor `redact`.
9. **Determinism.** Time and external state enter derivation only through recorded outcomes. Two readers with the same
   ledger, effect log and schema version get identical documents and model errors.

### 9.4 Missing and pending dependencies

`on_missing` (per computed, impure or deferred field): `null` (default) | `absent` | `error`. Because computed fields cannot
depend on deferred fields, a pending dependency matters only to deferred fields (section 9.5).

For a computed or impure field, an input is *missing* when its path has no value (never set and no default, or deleted
without a default fallback), or its value is null, which includes the null result of another computed field. A macro that does
not accept missing inputs is then not run and `on_missing` decides: `null` puts `computed::null` in the document, `absent`
leaves the field out, `error` leaves it out and adds the model error `computed:<path>`, which counts against a request like
any other new model error. `coalesce`, `if`, `eq` and `join` accept missing inputs: they run, receive null, and their result is
the value. An impure field whose inputs are missing does not call its hook. A failed impure call is the model error
`impure:<path>`; the field then follows `on_missing` (`error` adds no second error).

A pure macro that errors, exceeds a limit or returns a value of the wrong type is a fault, never a null: a request that would
cause it is rejected with `COMPUTE_FAULT`, and a read raises it.

### 9.5 Deferred fields

A deferred field's value is produced by an **IO hook** and stored in the **derived store**.

```
hook(input: { dependency path → value }, ctx: { doc, field, attempt, deadline }) -> value | error
```

- Hooks receive only declared dependencies plus metadata. They MAY perform IO. They MUST be idempotent.
- Output MUST conform to the field's `type`; otherwise it is treated as an error. An answer of no value or null is an error too: a stored deferred value is never null.

**Refresh policies** (`refresh:`):

| Policy | Behavior | Persisted |
|---|---|---|
| `once` | Computed before the first full fetch if no persisted value exists; thereafter served from the store. Optional `on_dependency_change: recompute`. | Yes |
| `per_fetch` | Invoked on every fetch, except that a stored value is reused while its inputs are unchanged and it is younger than `ttl` (a changed input always causes a call); the value is returned and MAY be stored as last-known-good. | Optional |
| `scheduled` | Refreshed by the engine's scheduler in **batches across documents** on an interval or cron; fetch serves the stored value. | Yes |
| `notification` | Linked to a topic; a notification marks the affected documents' stored value out of date and makes no call; the next `scheduler.run` for the source recomputes it (a read serves the stored value meanwhile). | Yes |

Policies MAY be combined (e.g. `refresh: [once, scheduled::{every:"1h"}]`).

**Derived store record:**

```
derived_value { doc, field, value, as_of, input_fingerprint, hook_version,
                schema_version, status: ok|stale|pending|error, error?, attempts }
```

**Failure handling:** see the failure policy below (`on_failure`).
Hooks have `timeout`, bounded retries with backoff, and a circuit breaker per hook (one per hook name and version, shared by every field and document that uses it). Deferred calls on the
read path MUST NOT make a read exceed the fetch deadline, which covers the whole read and all its fields; on timeout the unfinished fields follow `on_failure`, and a field waiting on an unfinished upstream is `pending`.

**Scheduler:** batches documents per hook, honors hook rate limits, is idempotent, and records every run. On the read path a source's rate limit is never waited for: a read that finds no token makes no call and the field follows `on_failure` for that read.
Batch hooks MAY declare `batch: { max: N }` and receive many inputs per call.

**Deferred-to-deferred dependencies (promises).** A deferred field's value is a promise until its hook succeeds. A hook is
invoked only when every deferred field it depends on has a value (`status: ok` or accepted stale); until then the field is
`pending`. Dependencies on plain and computed fields are read from the P1–P4 output. The scheduler runs the deferred
graph in topological order, so a hook never runs with an unresolved deferred input.

**Failure policy** (`on_failure`, per field, with a document-type default). Applies after retries are exhausted:

| Value | Effect |
|---|---|
| `error` (default) | Field status is `error`; the write that triggered it is **not** affected. |
| `serve_stale` | Last good value is returned with `status: stale`. |
| `absent` | Field is shown absent with `status: error`. |

A failed call leaves no value and is not repeated on every read: a `once` field is asked again when it is invalidated or its inputs change, a `per_fetch` field at its next `ttl`, a scheduled field at its next slot (a refusal by an open breaker or a rate limit is not a call and does not delay this).

A deferred failure never rejects a contribution and is never a model error. It is a **deferred status** held in the
derived store (`status`, `error`, `attempts`), shown with the field, and cleared by a later successful recompute
(manual `hook.invalidate`, schedule, notification or dependency change). Dependent deferred fields stay `pending` while
an upstream field is in `error` unless it is stale-served. Deferred status is not part of rebuild equality
(section 16, item 6).

**Retries and IO.** Every IO call has a timeout, bounded retries with backoff and a circuit breaker; the retry
envelope belongs to the deferred stage, not to the hook.

**Isolation:** hook results are **never** merged as contributions, **never** validated for model errors, and **never**
inputs to computed fields, validators or defaults. They live in the derived store and P7 overlays them on the response for
display, preserving the guarantee that the ledger and P1–P4, P6 are pure and replayable.

---

## 10. Validation, model errors and rejection

### 10.1 Levels

| Level | When | Scope | On failure |
|---|---|---|---|
| **V0** | P0, before append | Shape, type, path exists in schema, op allowed, authority, tier, flag permission (**schema errors**) | **Reject** the request; never bypassable |
| **V1** | P0, before append | Field `validate` behaviors on the incoming value | Reject, unless lenient (10.2) |
| **V2** | P0 dry-run and every derivation, after P3 | Field validators on the merged value; `document_validate` cross-field rules | Reject, unless lenient; otherwise **model error** |
| **V3** | After P4 | Constraints on computed values, including impure ones that have a value | Same as V2 |

- A **rejected request** returns an error listing **every** reason of the first failing stage and records nothing in the ledger.
  Stages: V0 (all schema errors, reported together); only if V0 is clean, V1-V3 (all rule failures, reported together).
  Rule checks need a resolvable document, so they are not run on a request that has schema errors.
- A **model error** is a validation-rule violation of a document that was accepted (lenient write) or that arose when the
  document was re-derived (10.4). Model errors are stored with the derived document and returned on every read. Each
  records the request that introduced it (`introduced_by: request_id`), or the schema version for violations found on
  re-derivation. Attribution is a pure result of replay: requests are walked in order, each evaluated under the schema
  version recorded on it; an error that first appears after a request is attributed to that request, and an error that
  first appears when the active (or pinned) version differs from the last request's version is attributed to that version.
  An error keeps its attribution until it disappears; if it reappears it is a new error. A rebuild therefore reproduces
  attribution exactly.
- Validation is evaluated **once per request, on the document that results from applying all of its entries**. Intermediate
  states inside a request are never evaluated, so entries within one request MAY depend on each other.
- Validators are pure behaviors. They see only the merged and computed document (including recorded impure values), never deferred values (10.5).
- An impure compute whose retries are exhausted is handled by 9.3.1 item 5; under `model_error_on_failure` it is recorded as a model error.
- A validator fault (as opposed to a violation) is an error to the caller and is never treated as "no violations": at admission
  the request is rejected, on a read the read fails.

### 10.2 Non-rejection flag (lenient writes)

- A request MAY carry the non-rejection flag (`lenient: true`). The flag only affects V1–V3 failures: they are recorded
  as model errors instead of rejecting the request. **Schema errors (V0) always reject.**
- Only tiers or contributors listed in the schema's `lenient.allowed` MAY use the flag (permitted when the contributor is
  listed, or when every tier used in the request is listed); any other flagged request is
  rejected with `LENIENT_NOT_PERMITTED`, even if the data would have been valid.
- Accepted lenient entries carry `lenient: true` for provenance. A lenient request MAY carry a reason or ticket in `meta`;
  v1 does not require it (a later schema version may). Only the schema decides who may use the flag; there is no
  per-document override.
- Example: a country allows one spouse; a backfill with two spouses is recorded with a model error and surfaced with the
  document so the user can be nudged to fix it.

### 10.3 No-new-errors rule

For a document with existing model errors, an **unflagged** request is accepted only if the set of model errors after it
introduces none that did not exist before. A request that reduces the set, or leaves it unchanged, is accepted. The comparison uses the **net effect of the whole
request**: a request that fixes one error and introduces another is rejected. A
request that would introduce a new model error is rejected with `MODEL_ERROR_INTRODUCED`.

- Model error identity is `(rule id, path scope)`. Two violations of the same rule at the same scope are the same error.
- A **lenient** request from a permitted tier is **exempt** from this rule; its new violations are recorded as model errors.

A document with no entries is checked like any other: a rule that needs a field (`at_least_one`, or a computed field with `on_missing: error`) already reports on it, so
that error is not *new* when the first request arrives; the request is still rejected if it adds a different one.

### 10.4 Re-evaluation on schema or rule change

- Every derived document view records the schema version (and validation-rule versions) it was derived under.
- On read, if that version differs from the active (or pinned) version, the engine **discards** the cached derived view
  and re-derives from the ledger under the current version, recomputing model errors.
- Violations found are recorded as model errors and returned with the document. Previously accepted entries are never
  retroactively rejected or removed, and the ledger is not modified.
- Activating a version touches no document: each one re-derives on its next read. An operator MAY ask for an eager `rebuild`
  with the activation, and MAY first run a **dry run** (14) that derives documents under the current and the target version
  over the same entries and recorded outcomes and reports what would differ; a dry run writes nothing and calls no hook or source.

### 10.6 The rule library

Field rules (arguments in braces; each reports nothing for a value of a kind it does not apply to, names the field and the rule in
its message and never repeats the value): `non_empty`, `range{min,max}`, `max_len{n}`, `min_len{n}`, `pattern{regex}`,
`one_of{values}`, `unique_items`. `pattern` is a small subset (literals, `.`, classes and ranges, groups, alternation, `*` `+` `?`
`{m,n}`; no back-references or look-around) matched against the whole value in time proportional to its length.
Document rules over a list of paths (a path has a value if it is present and not null): `all_or_none{paths}`,
`at_least_one{paths}`, `at_most_one{paths}`, `ordered{first,second}` (a violation when the first is greater than the second or the
two cannot be ordered; nothing when either has no value). A deployment may register further field and document rules with
declared arguments; a schema that uses a rule with an undeclared, missing or ill-typed argument is refused at load.

### 10.5 Deferred values and validation

Deferred-field values MUST NOT be inputs to any validation rule, and a deferred field itself is never validated for model
errors. A schema declaring a validator that depends on a deferred field MUST be refused at load. Validation is therefore a
pure function of the ledger and schema version. Deferred failures are reported as deferred status (section 9.5), not as
model errors.

---

## 11. Authority

- Each field MAY declare `authority: [tier or role, …]`. Default: any permitted tier may write.
- Derived fields have an implicit authority of `engine` (no contributor).
- P0 checks the contributor's permitted tiers and the field's authority; failure is `NOT_AUTHORIZED`.
- Authority is checked **per entry at admission** against the schema version in force.
- Use of the non-rejection flag is a separate permission (`lenient.allowed`, section 10.2); failure is `LENIENT_NOT_PERMITTED`.
- Authority on a path and on its descendants follows the most specific declaration: a child without its own `authority` takes
  the nearest list above it, and a child's own list replaces its parent's (it does not add to it). `explain` and provenance obey redaction in the same way (details when `redact` is designed).
- A `NOT_AUTHORIZED` reason names the field and the rule, not the tiers and roles that may write it; `explain` and the schema check
  report the effective list and the field that declared it. The non-rejection flag never bypasses authority.
- Authority is not a substitute for tier priority: authority decides *who may write*; tier decides *who wins*.

---

## 12. Interfaces

### 12.1 Write

```
append(doc, entries[], { expected_seq?, lenient?, request_key? })
  -> ok:       [{ id, seq }], request_id, duplicate: bool, model_errors_added?, model_errors_resolved?, view   // view = latest materialized view
  -> rejected: error { code, reasons: [{ entry_index, code, rule?, path?, message }] }   // nothing recorded
```
A request is **always atomic**: if any entry is rejected, none is appended. There is no non-atomic mode, so every model
error and audit record can be attributed to one request rather than to a subset of its entries. A rejected request is one
audit record (section 4.5) when auditing is enabled.

### 12.2 Read

```
get(doc, {
  as_of?:        seq | tx_time | valid_time,    // time-travel
  consistency?:  cached | fresh,                // fresh forces per_fetch hooks and P1–P6 recompute
  include?:      [provenance, pending],
  tiers?:        [..]                           // debug: merge a subset of tiers
}) -> { document, model_errors: [ ... ], status, skipped: [seq], shadowed: [ { seq, entry, tier, path, reason, detail } ], provenance? }
```
A read normally makes no IO. The one exception is an impure compute with no recorded outcome for its current input
fingerprint (for example after a schema change): the read calls the hook once, records the outcome (section 9.3.1) and
returns a failure as a model error, never as a rejection.

A read may also call the sources of deferred fields (section 9.5): those whose `once` value is missing or whose `per_fetch`
value is past its `ttl` or was made for other inputs (every `per_fetch` value with `consistency: fresh`). All such calls share
one fetch deadline for the whole read; a field not finished by then follows its `on_failure`, and a failure is a status in
`deferred`, never a rejection or a model error. `deferred` lists the status of every deferred field; `include: pending` adds
what each pending field waits for. A write never calls a source, and `rebuild` never does.

**Time travel.** `as_of` names exactly one point (more, or none, is `AS_OF_AMBIGUOUS`): a `seq` (the document after that
entry, nothing after it; a seq beyond the last reads the current state, below the first the empty document), a `tx_time`
(the document at the highest seq recorded at or before it; recording times need not grow with seq, so this is a maximum
over all entries) or a `valid_time` (the entries whose `valid_time` is after it are left out, entries with no `valid_time`
always take part, the rest are folded in the usual order). The answer says the seq it resolved to and how many entries took
part. A time-travel read uses the schema version the document has now, writes nothing, and calls no hook or source: an
impure compute uses the outcome recorded for its inputs at that point (none recorded: the model error
`NO_RECORDED_OUTCOME`), and deferred fields are listed with status `unavailable` and no value. A point inside a compacted
range is answered from the archived originals while they are kept; once they are expired it is refused with
`HISTORY_COMPACTED` (and `earliest_seq`) unless it can still be answered exactly (15.4).

### 12.3 Explain

```
explain(doc, path) -> {
  path,
  value,                                                      // absent as null
  winner:   entry id | "default" | "computed" | "deferred" | null,   // null: nothing sets the path; an impure compute also names its effect-log record (effect_seq)
  winner_seq: seq of the winning entry | null,
  rule:     behavior name@version,
  inputs:   [ entry ids / dependency values that contributed ],
  shadowed: [ entry ids ],
  shadowed_by_schema: [ { seq, entry, tier, path, reason, detail } ],   // entries this version leaves out that touch the path (9.2)
  model_errors: [ model errors whose scope includes this path ]
}
```
The winner is the entry after which the path's value last changed, folding the entries that touch it in merge order. An
entry is an input if deriving without it gives another value there (the winner is always an input); the rest are shadowed.
A path the schema does not declare is `UNKNOWN_PATH`. `explain` accepts the same `as_of` as a read. After a compaction
`explain` is answered from the archived originals while they are kept; once they are expired a synthetic entry (15.3) is
one entry: an input if any of its sources was, shadowed if none was, and the answer lists its source ids and says the originals
are no longer kept. A purged entry (15.5) is never the winner, an input or shadowed; the answer lists the purged entries that
touched the path with when and why they were erased.

### 12.4 Admin

`list_entries`, `rebuild(doc|all)`, `compact(doc, policy)`, `verify_archive(doc)`, `expire_archive(doc, through_seq)`,
`purge(doc, scope, reason)` (permitted only to the tiers and roles the schema lists in `purge_by`; `NOT_AUTHORIZED` otherwise),
`purge_log(doc)`, `schema.put/activate`, the operations of 14 (`schema_changes`, `plan_schema`, `activate_schema`, `pin_document`, `unpin_document`, `plan_migration`, `migrate`, `schema_history`), `hook.invalidate(doc?, field)`,
`scheduler.run(hook)` (both permitted only to the tiers and roles a field lists in `invalidate_by`; `NOT_AUTHORIZED` otherwise), `list_rejections(doc, { since?, limit? })`, `set_rejection_audit(doc, enabled|inherit)`,
`prune_rejections(doc?)`, `recompute(doc, field?)`, `list_effects(doc, field?)`, `prune_effects(doc?)`.

`rebuild` derives each document from the ledger alone and compares it with the cached view and with the saved per-tier
layers (15.1), byte for byte. A current view or layer that differs is reported and repaired; a missing one, one from another
schema version or epoch, or a layer that merely lags, is derived afresh and stored (not a difference); a document that cannot
be derived is reported and does not stop the others. It never writes to the ledger and calls no deferred source; it calls no impure hook while the recorded outcomes cover the inputs
and otherwise fills a missing outcome once (9.3.1).

### 12.5 Error codes (non-exhaustive)

`FIELD_NOT_WRITABLE`, `NOT_AUTHORIZED`, `TIER_NOT_PERMITTED`, `UNKNOWN_PATH`, `TYPE_MISMATCH`,
`OP_NOT_ALLOWED`, `VALIDATION_FAILED`, `MODEL_ERROR_INTRODUCED`, `COMPUTE_FAULT`, `IMPURE_COMPUTE_FAILED`, `LENIENT_NOT_PERMITTED`, `DUPLICATE_IDEM_KEY`, `REQUEST_KEY_REUSED`, `IDEM_KEY_REQUIRED`, `SEQ_CONFLICT`, `SCHEMA_INACTIVE`, `BAD_VALID_TIME`, `AS_OF_AMBIGUOUS`, `HISTORY_COMPACTED`, `MIXED_SCHEMA_RANGE`, `IDEM_KEY_MISSING`,
`COMPACTION_CHECK_FAILED`, `LEDGER_CHANGED`, `PURGE_REASON_REQUIRED`, `UNKNOWN_ENTRY`, `BREAKING_CHANGE_NOT_ACKNOWLEDGED`, `NOT_PINNED`, `STORAGE_ERROR` (an error, not a rejection). Refusals of an activation or a pin that are schema errors carry `TIER_IN_USE`, `HISTORY_COMPACTED`, `SCHEMA_STATE_CHANGED` or `SCHEMA_VERSION_CHANGED` (14).

---

## 13. Sandboxing and security

1. Computed, merge and validate behaviors run with **no ambient authority**: no network, filesystem, process,
   environment, wall clock or randomness; fixed CPU and memory limits; deterministic numeric semantics.
2. Behaviors see **only declared inputs**. The engine MUST enforce this by construction (pass a projection),
   not by convention.
3. Impure-compute and deferred hooks run **outside** the sandbox boundary used for pure behaviors, with their own credentials
   scoped per hook, egress allow-lists, timeouts, and audit logging.
4. Redaction (`redact`) is applied at P7 on every read path, including `explain` and provenance.
5. The rejection audit log, model errors and provenance can contain sensitive values; access control MUST be at least as
   strict as the document's, and attempted values in the audit log MUST honor `redact`.
6. Custom behaviors MUST be reviewed/signed before registration; their versions are pinned by the schema.

---

## 14. Schema versioning and evolution

- Every schema version is **immutable**. `activate(version)` switches the version for new admissions and, by default,
  for reads.
- Merged results are a pure function of `(ledger, schema version)`. Re-reading under a new version discards any cached
  derived view and re-runs P1–P7, recomputing model errors (10.4).
- **Immutable across restarts.** A version is *served* from its first activation or pin. The store keeps a fingerprint of every
  served version, whole and per field, so that registering a version again with other content is refused
  (`SCHEMA_VERSION_CHANGED`, naming up to five fields that differ) while identical content is accepted. A version that was only
  registered, never served, may still be changed.
- **One place decides which version serves.** The active version and the pins are kept in the store, read through a generation
  that changes with them, so every process on the store serves the same version, and each change is one transaction with its
  history record. A process that lacks the stored active version refuses reads and writes with `SCHEMA_INACTIVE` naming it.
- Changes are classed:

| Change | Class | Effect |
|---|---|---|
| Add field (plain, computed or deferred), add tier at top, declare a role, change `rejection_audit` | Compatible | None to existing docs, except new defaults/derived fields appear. |
| Remove a field | Visible | New writes to it are refused; its recorded values still show in documents, merged by last-write-wins and no longer governed by a type, rule or authority. Nothing is shadowed. |
| Change default or `on_delete` | Visible | Docs without a contribution change on read. |
| Change merge rule | **Breaking** | Folds change; stored snapshots MUST be invalidated and rebuilt. Entries the new rule cannot take are shadowed (`cannot_be_merged`). |
| Change type / tighten constraint | **Breaking** | Entries admitted under another version that no longer conform are *shadowed* (`no_longer_conforms`), never refused, and counted by the dry run. |
| Add or tighten a validation rule, add, change or remove a document rule | Visible | Existing entries are kept; documents gain model errors on next read (10.4). |
| Add, change or remove an `authority` list, `ops` or `redact` (11) | Visible | Applies to entries admitted from then on; recorded entries stay. A child without a list also takes a parent's new list. |
| Make field derived | **Breaking** | Historic entries shadowed (`now_derived`, 9.2). |
| Make a derived field plain | **Breaking** | The field takes recorded writes again. |
| Change a computed field's macro, arguments or `on_missing` | Visible | Recomputed on the next read. |
| Change an impure compute's hook version or `depends` (or its call settings) | Visible | Recorded outcomes for the old version or fingerprint are not reused; the next derivation that needs the value calls the hook again (a read may therefore do IO once). |
| Change a deferred field's source, `depends`, `refresh`, `ttl`, `on_failure` or `invalidate_by` | Visible | The stored value is reused only while it answers the same question (9.5). |
| Change `failure_mode` / `impure_failure_mode` / `deferred_on_failure` / leniency | Visible | Applies from the next request or derivation; existing model errors and recorded outcomes stay. |
| Add, change or remove `purge_by` (15.5) | Visible | Applies to purges asked from then on. |
| Change merge rules, types, tiers or which fields are derived, on a document with compacted history (15.3) | **Breaking** | The document is derived from the archived originals; once they are expired the version is refused for it (`HISTORY_COMPACTED`). A version that only changes validation, computed or deferred rules reuses the compacted entries. |
| Insert/reorder/drop/rename a tier below the top | **Breaking** | `(tier, seq)` order changes; entries of a tier the schema lacks are shadowed (`tier_not_in_schema`). |
| Drop or rename a tier used by existing entries | **Breaking** | Refused at activation or pin while any entry (of that document, for a pin) uses it (`TIER_IN_USE`). |
| Any other difference | Visible, **not classified** | Listed as such, so that nothing is missed silently. |

- **Change list.** A function of two schemas alone lists every difference with its kind, path, class and one sentence that never
  holds a stored value, in a fixed order, and marks the changes that can shadow entries and for which reasons. When a field changes
  in several ways each is its own entry; a renamed field is a removal and an addition. The overall class is the worst of them.
- **Dry run.** `plan_schema(target)` derives each document under the current and the target version over the same entries and
  recorded outcomes and reports, per document, whether it differs, the model errors added and resolved (by rule and path), the
  skipped entries before and after, the shadowed entries per reason, whether the target would call a hook, and a document that
  cannot be derived (with its code). It writes nothing and calls no hook or source. Every document is examined unless a list of
  documents or a limit is given (a limit takes the first N by a hash of the name, the same N each time); the report says how many
  were examined and left out. Documents pinned to a version are left out: an activation does not touch them. What the dry run
  reports as unchanged reads unchanged after the activation, and what it reports as changed reads as reported.
- **Activation** is an operator's act: the operator is named and recorded. A breaking target is refused
  (`BREAKING_CHANGE_NOT_ACKNOWLEDGED`, naming the class and the first breaking changes) unless the operator acknowledges it; a
  compatible or visible one needs no acknowledgement. The checks of the table (`TIER_IN_USE`, `HISTORY_COMPACTED`), an
  unregistered version (`SCHEMA_INACTIVE`) and a stale expected generation (`SCHEMA_STATE_CHANGED`) refuse it and record
  nothing. Activating the active version is a no-op. Activation touches no document and its cost does not grow with their
  number; an eager `rebuild` MAY be asked for, refreshes the saved layers and views of the unpinned documents, reports those that
  changed and those it could not derive, never writes to the ledger, and the activation stands whatever it finds.
- **Rollback** is activating an older version again. It is allowed whenever the checks above pass, needs no acknowledgement, is
  recorded as a rollback with its class, changes no entry, and every document reads byte for byte as it did before.
- **Pins and migration.** A document MAY be pinned to a version (`doc.pinned_schema`): it is read and written under that version
  whatever is active, and a pin is refused for the reasons an activation is. A migration moves a pinned document to a chosen
  version (the active one by default), unpinning it when that is the active version, in one transaction with its record and
  without touching its entries. A dry run for one document is available. A batch works over a list or every pinned document,
  reports progress, goes on past a document that cannot be derived under the target (reported, left on its version) and is safe to
  run again after a stop: what moved is reported and left alone. Migrating a document that is not pinned is refused
  (`NOT_PINNED`).
- **History.** Every bootstrap, activation, rollback, pin, unpin and migration is a record (who, when, from and to version,
  document if any, class) that is never changed or removed and never read by a derivation. Anyone with access to the library or
  command line MAY act; the schema lists nobody (as for `rebuild` and `compact`).

---

## 15. Storage, snapshots, compaction and purge

### 15.1 Layout

- **Ledger**: append-only, partitioned by document, ordered by `seq`; changed only by compaction (15.3) and purge (15.5).
- **Snapshots (saved layers)**: one per document and tier, `{doc, tier, tier_seq, schema_version, rules_fingerprint, epoch, state}`,
  where `state` is the canonical text of the tier's folded layer (a list of `{p: path, s: value}`, with `t: true` for a tombstone,
  `r: true` for a reset mark and `a: true` for absent, sorted by path text) and `tier_seq` is the highest seq of that tier it covers.
  Saved layers are disposable: they never change a result, only the cost of getting it (15.2).
- **Epoch**: a per-document number that grows with each compaction and purge; a layer or view of another epoch is not used.
- **Archive**: the originals a compaction replaced, until an operator expires them (15.3).
- **Purge log**: append-only, never read by a derivation (15.5).
- **Derived store**: section 9.5.
- **Effect log**: append-only impure-compute outcomes, separate from the ledger (section 9.3.1).
- **Rejection audit log** (optional): separate, capped by retention (section 4.5); never read by derivation.
- **Model errors**: stored with the derived document view, keyed by schema version.

### 15.2 Incremental recomputation

On append of an accepted entry at tier *t*, in the transaction that commits it, the layer of *t* (and of any tier whose saved
layer is missing or stale) is saved with the cached view; other tiers' layers are reused as they are. A layer is used only if,
for every tier with entries up to the view's watermark, a row of the same schema version, rules fingerprint and epoch covers
exactly up to that tier's last seq. A reader that finds saved layers that fit continues from them, folding only the entries
after the watermark; if they do not fit, or cannot be read or saved, the document is derived from the ledger and the result is
the same. An impure compute is re-called only when its input fingerprint changes (or on `recompute`); otherwise its recorded
outcome is reused.

### 15.3 Compaction

- Compaction replaces the accepted entries up to a **request boundary** (a request is never split) with **one synthetic `layer`
  entry per tier**: its value is the tier's folded layer, its `seq` the highest of its sources, `synthetic` the compaction id. The
  range must have been written under one schema version (`MIXED_SCHEMA_RANGE`). A field with a non-idempotent merge behavior is
  compacted only with its idempotency keys (`IDEM_KEY_MISSING`); the keys of every source stay recorded, so a repeat is still
  `DUPLICATE_IDEM_KEY`.
- The originals go to the **archive**; the compaction also records the model errors (with who introduced each) and the skipped
  seqs a replay of the requests had computed, the ids of its sources, and a request index, so that nothing a replay shows and no
  retry answer changes.
- Compaction MUST NOT change `Merged(d)`, its model errors, status, skipped and conflicting paths, or any `explain` answer while the
  archive keeps the originals. Before it commits it derives the document, model errors, skipped and conflicting paths and the
  `explain` answer of every path the range touched from the compacted ledger alone, compares them with the ledger as it is, and
  refuses (`COMPACTION_CHECK_FAILED`, naming the first differences) if any differs; it MAY refuse a case it cannot prove equal.
- Compaction applies to ledger entries only. It MUST NOT remove or alter effect-log outcomes, derived-store records, rejection
  audit records or the request records that make a retry return its original answer.
- `expire_archive(doc, through_seq)` deletes the archived originals of the compactions at or below a seq; it is recorded and
  cannot be undone. The synthetic entries, their source ids, the request index and the retired idempotency keys stay.
- A schema version that folds differently (see the change table in 14) is applied to a compacted document by deriving from the
  archived originals; once they are expired it is refused (`HISTORY_COMPACTED`).

### 15.4 Time

- `seq` orders; `tx_time` records. `valid_time` (optional) supports bitemporal reads (`as_of: {valid_time}`) by filtering
  entries before folding; entries with no `valid_time` always take part.
- **Late arrivals** (e.g. backfill) are modeled by **tier**, not by timestamps: a backfill entry arriving today sorts below
  earlier user entries in `(tier, seq)` and cannot clobber them under `lww`.
- After the archive of a compaction is expired, a time-travel point inside its range is answered exactly only if: its seq is at or
  above the watermark; its `tx_time` is earlier than the first or not earlier than the last recording time of the range; or it is
  a `valid_time` and none of the compacted entries carried one. Anything else is `HISTORY_COMPACTED`.

### 15.5 Purge

- `purge(doc, scope, reason)` erases contributions for legal reasons: a scope is a path (every contribution at or below it,
  optionally of one tier) or a list of entry ids. The schema lists who may purge in `purge_by` (tiers and roles; nobody by default)
  and a reason is required. Physical removal is the only mode in v1.
- It removes the erased values, `meta` and `valid_time` from the ledger, the archive, the compacted layers, the saved layers and
  views, the effect-log inputs and outputs and the derived values of the fields that read the erased paths, and the attempted
  values in the rejection audit records, in one transaction. A struct written above the path keeps the rest of its value. If the
  originals of a compacted range were expired, the whole compacted contribution at the path is erased and the purge log says so.
- The erased entry stays as a **placeholder** (id, seq, tier, path, operation, contributor, `tx_time`, request, schema version,
  lenient flag and idempotency key stay; the value, `meta`, `valid_time` and the request hash are erased) and has no effect on any
  derivation. The stored hash of every request that held an erased value goes too (a hash of a value is still a copy of it); a
  retry of such a request is a duplicate with its original answer.
- Each purge is recorded in the **purge log** (who, when, reason, document, scope, counts, erased entry ids), never a value. The
  log is never read by a derivation and cannot be purged. A purge is not a tombstone: a tombstone is an entry that keeps the
  history and says a field is deleted.

---

## 16. Conformance and testing

An implementation conforms if it passes:

1. **Law tests** (property-based) for every merge behavior: associativity; and commutativity/idempotence where declared.
2. **Squash equivalence**: for random entry sets, per-tier-squash-then-merge equals the flat `(tier, seq)` fold.
3. **Order tolerance**: permuting arrival *within the constraint of fixed `(tier, seq)`* yields identical results; changing only
   `tx_time` yields identical results.
4. **Authority**: no sequence of accepted entries changes a derived field's value except via its behavior.
5. **Sandbox**: a computed behavior given extra undeclared paths in the input document cannot observe them.
6. **Replay**: `rebuild` from the ledger reproduces cached snapshots (views and per-tier layers) bit-for-bit.
7. **Rejection and atomicity**: a request with any rejected entry appends none of its entries; accepted requests commit
   their entries with consecutive `seq`; a rejected request leaves the ledger and every document unchanged; with auditing off nothing is
   recorded anywhere; with it on exactly one audit record exists; deleting the audit log changes no document.
8. **Retention**: after pruning, the audit log never exceeds `max_entries` per document or holds records older than `max_age`.
9. **Model errors**: unflagged requests never add a model error to a document that has any; lenient requests from permitted
   tiers always succeed on validation failures; V0 failures always reject, flagged or not.
10. **Version change**: after a schema or rule version change, the first read equals a fresh derivation under the new
    version, and the ledger is byte-identical.
11. **Deferred isolation**: no validator, computed field or default can observe a deferred value; a schema with such a
    dependency fails to load. A deferred hook with a deferred dependency is not invoked until that dependency has a value.
    A deferred failure never rejects a request, adds no model error and does not change the output of `rebuild` for P1–P4, P6.
12. **Impure computes**: a rebuild with a populated effect log makes zero hook calls and equals the cached view; a hook that
    fails on every attempt follows `failure_mode` (`reject_on_failure` leaves no ledger entry and no effect record, `model_error_on_failure`
    commits with exactly one model error); `recompute` after a fix removes the model error; a hook with `depends` outside
    its declaration cannot observe other fields; no hook call happens while the write lock is held.
13. **Retries**: re-submitting a request with the same `request_key`, payload and identity adds no entry and returns a response of
    identical shape (original entries, latest view); the same key with another payload is `REQUEST_KEY_REUSED`; the same payload
    without a key is applied as a new request; a reused entry `idem_key` rejects its request.
14. **Rejection stages**: a request with a schema error and a rule violation lists only the schema errors; after they are fixed it
    lists the rule violations.
15. **Vectors**: the worked examples below, plus a published vector set (`conformance/*.ion`).

### 16.1 Normative worked example

Tiers: `backfill < user < admin`. Defaults: `currency: "USD"`, `plan: "free"`. Rules: `/name lww`, `/age max`,
`/tags union`, `/address struct`, `/currency lww`, `/plan lww`; computed `displayName = upper(name)`,
`label = macro(name, address.city)`; deferred `creditScore` (once → 712), `fxRate` (per_fetch → 0.92).

Entries in `seq` order (`B`=backfill, `U`=user, `A`=admin):

| seq | tier | path | value |
|---|---|---|---|
| 1 | B | /name | "A. Smith" |
| 2 | B | /phone | "555-0100" |
| 3 | B | /age | 30 |
| 4 | B | /tags | ["x"] |
| 5 | U | /name | "Alice" |
| 6 | B | /address/zip | "78701" |
| 7 | U | /age | 28 |
| 8 | U | /tags | ["y"] |
| 9 | A | /tags | ["z"] |
| 10 | U | /name | "Alice S." |
| 11 | A | /phone | `deleted::null` |
| 12 | A | /address/city | "Paris" |
| 13 | U | /currency | "EUR" |

A further request, admin sets `/displayName = "HACKED"`, is **rejected** with `FIELD_NOT_WRITABLE`. It is not a ledger entry
and consumes no `seq`. If `rejection_audit.enabled` is true there is exactly one audit record for it; otherwise none.

Layers after P1:

```
Squash(backfill) = { name:"A. Smith", phone:"555-0100", age:30, tags:["x"], address:{zip:"78701"} }
Squash(user)     = { name:"Alice S.", age:28, tags:["y"], currency:"EUR" }
Squash(admin)    = { tags:["z"], phone:deleted::null, address:{city:"Paris"} }
Defaults         = { currency:"USD", plan:"free" }
```

After P2 (tombstone retained) → after P3 (tombstone applied):

```
{ currency:"EUR", plan:"free", name:"Alice S.", age:30,
  tags:["x","y","z"], address:{ zip:"78701", city:"Paris" } }
```

(`age` = max(30, 28); `tags` = union in tier order; `plan` untouched default; `phone` removed.)

After P4, and the P5 overlay shown by P7:

```
{ currency:"EUR", plan:"free", name:"Alice S.", age:30, tags:["x","y","z"],
  address:{ zip:"78701", city:"Paris" },
  displayName: computed::"ALICE S.", label: computed::"Alice S. (Paris)",
  creditScore: deferred::712, fxRate: deferred::0.92 }
```

### 16.2 Normative model-error example

Schema v7 plus: `lenient.allowed: [backfill]`; `/country lww`; `/spouses list<string> lww`; `/age range 0..150`;
document rule `max_spouses_by_country` (1 for `US`). Requests in order:

| # | Writer | Request | Result |
|---|---|---|---|
| 1 | backfill, lenient | one request: `/country="US"`, `/spouses=["A","B"]` | Accepted (seq 1–2, one `request_id`). Model error **E1** `(max_spouses_by_country, /spouses)`. |
| 2 | user | `/name="Pat"` | Accepted (seq 3). Errors unchanged: {E1}. |
| 3 | user | `/age=-5` | **Rejected** `MODEL_ERROR_INTRODUCED` (would add `(range, /age)`). Nothing recorded. |
| 4 | user, lenient | `/age=-5` | **Rejected** `LENIENT_NOT_PERMITTED` (user may not use the flag). |
| 5 | backfill, lenient | `/age=-5` | Accepted (seq 4). Errors: {E1, **E2** `(range, /age)`}; the no-new-errors rule is skipped. |
| 6 | user | `/spouses=["A"]` | Accepted (seq 5). E1 resolved. Errors: {E2}. |
| 7 | schema v8 adds `/name max_len 2` ("Pat" is 3 characters); read | | Cached view discarded and re-derived; errors: {E2, **E3** `(max_len, /name)`}. Ledger unchanged. |

---

## 17. Decided questions and future work

Every question below was decided on 2026-10-18. The Decision line says what is in v1 and what is future work.

1. **Cross-document derivation.** Fields computed across documents (joins, aggregates, references) and their
   invalidation graph.
   **Decision (2026-10-18): not in v1.** A document is derived only from its own ledger. Data from another document is
   brought in as a normal contribution by an integration job; a deferred field may call another aduku document as a
   workaround. A reference-field feature with invalidation events is a possible later addition.
2. **Per-field precedence.** Per-field tier ordering, trust scores or time-decay instead of one global tier order. Must
   preserve the equivalence law (6.2).
   **Decision (2026-10-18): one tier order per schema in v1**; tiers are defined by the schema author. Use `authority` for field-level exceptions. Per-field reordering and trust scores or time decay are later, separate rule types that must preserve the equivalence law (6.2).
3. **Ordered-list CRDTs** for concurrent list edits.
   **Decision (2026-10-18): out of scope for v1** (lists are replaced whole, treated as sets, or append-only logs). A keyed-list model (items with stable keys, merged per key) is the planned later step.
4. **Dynamic defaults** (a pure macro over other fields) and their ordering with merge.
   **Decision (2026-10-18): allowed, as a pure macro over other fields** (section 8, item 7). Lands with the computed-fields feature; Feature 001 keeps static defaults.
5. **Operational semantics of `once`** when dependencies later change (default: keep persisted; `on_dependency_change`
   opt-in).
   **Decision (2026-10-18): keep the persisted value by default; `on_dependency_change: recompute` is an opt-in per field.** Examples: a partner account id must never change; a geocode of `/address` opts in.
6. **Multi-region replication** of the ledger: `seq` assignment (single leader per document vs hybrid logical clocks)
   and its effect on order tolerance.
   **Decision (2026-10-18): not in v1** (single logical writer per document). First multi-region design: one leader region per document with routed writes.
7. **Standardized schema vocabulary**: express the annotations as a JSON Schema vocabulary / Ion Schema extension with
   a registry URI, to enable multiple implementations.
   **Decision (2026-10-18): direction is a JSON Schema vocabulary** (annotations published with a registry URI) so other tools and ports can validate schema files. v1 keeps aduku's own Ion schema file; the vocabulary and a mapping come after the format settles.
8. **Subtree authority and redaction** composition rules.
   **Decision (2026-10-18): the most specific rule wins** (a child's setting overrides its parent's), and `explain` and provenance obey redaction. Finish the details when `redact` is designed.
9. **Streaming reads**: change feed of merged-document diffs derived from ledger appends.
   **Decision (2026-10-18): not in v1**; its own later feature, built on the ledger and cache.
10. **Model error granularity.** Identity is `(rule, path scope)`, so worsening an existing error (a third spouse where two
    already violate the limit) is not detected as new. Consider a severity or count in the identity.
   **Decision (2026-10-18): keep `(rule, path scope)` in v1**, with the blind spot documented. Later, each rule MAY define what "worse" means.
11. **Rejection audit log scope.** Global (per-type) caps, sampling, metadata-only mode, and access-control model.
   **Decision (2026-10-18): v1 has per-document caps (`max_entries`, `max_age`) and a metadata-only mode** (section 4.5). Global caps and sampling are later; access is at least as strict as the document's.
12. **Schema-level vs per-document `lenient` override** and whether lenient writes should require a reason code.
   **Decision (2026-10-18): the schema alone decides** who may use the flag (no per-document override). A lenient write MAY carry a reason or ticket in `meta`; the schema may make it required in a later version.

---

## Appendix A. Changelog

- **0.13**: Schema versioning and evolution as built in feature 006: versions are immutable across restarts by fingerprint (`SCHEMA_VERSION_CHANGED`), the active version and the pins live in the store with a history, the classification table of 14 grows (a removed field is visible; unclassified differences are listed), the change list, the dry run, the acknowledgement of a breaking activation, rollback, per-document pins and migration, and the reasons for shadowed entries (9.2) shown with `get` and `explain` (12.2, 12.3); type tightening shadows instead of refusing; new codes in 12.5.
- **0.12**: Snapshots, time travel, compaction and purge as built in feature 005: saved per-tier layers with an epoch (15.1, 15.2), `as_of` by seq, `tx_time` and `valid_time` with the rules of 12.2 and 15.4, compaction with one `layer` entry per tier, an archive, its check and its expiry (15.3), and purge with `purge_by`, placeholders and the purge log (15.5); the append-only rule of 4.2 names compaction and purge as its only exceptions; `valid_time` joins the request hash only when given (4.1, 4.2).

- **0.11**: Rule library and authority as clarified for feature 004: the rules of 10.6, authority inheritance and the wording of `NOT_AUTHORIZED` reasons (11).
- **0.10**: Deferred fields as clarified and built for feature 003 (12.2 now says reads may call deferred sources): `invalidate_by` is required on every deferred field, a `per_fetch` value is reused only while its inputs are unchanged, the circuit breaker is per hook, the fetch deadline covers the whole read, and a notification only marks values out of date (9.5, 5.3, 12.4).
- **0.9**: Computed fields as built in feature 002: null counts as a missing input, `on_missing: error` model errors count against a request, `COMPUTE_FAULT`, `on_missing` and `recompute_by` in the annotation table (5.3, 9.4, 12.5).
- **0.8**: Impure computes declare `recompute_by` (who may call `recompute`), decided while clarifying feature 002 (9.3.1).
- **0.7**: Matched the spec to the first implementation: admission checks struct children, child authority on delete and values with no canonical Ion form (6.5); tier drop refused also at pin and at service start (4.4); built-in validator arguments checked at load (5.1); `explain` shape and winner definition (12.3); `rebuild` semantics (12.4). Example 16.2 step 7 uses `max_len 2`, since "Pat" has 3 characters.
- **0.6**: Decided all section 17 questions (see their Decision lines). Tiers are user-defined with optional presets, and one
  tier order applies per schema (4.4, 6.2). Dynamic defaults as pure macros (8.7). Most specific authority rule wins (11).
  Retries are no-ops through a caller-supplied `request_key` and request hash (4.1, 4.2, 12.1, `REQUEST_KEY_REUSED`).
  Rejections list the reasons of the first failing stage: schema errors, then rule failures (10.1). Rejection audit
  `metadata_only` mode (4.5). Optional lenient reason in `meta` (10.2). Model-error attribution by replay (10.1). Effect-log
  retention and access (9.3.1), reads and `explain` for impure computes (12.2, 12.3), compaction and the effect log (15.3).
  Also: best-effort audit writes and oldest-first pruning (4.5), storage failure during commit (4.2), flag permission rule
  (10.2), validator faults (10.1), max/min only on ordered types (6.3), tier drop refused at activation (14).
  Parameterized types are written as quoted strings in schema files (5.2).

- **0.5**: Added impure computes (9.3.1): a computed field with a side effect inside P4, strongly consistent, with retries,
  recorded outcomes in an effect log (so derivation stays replayable), `failure_mode: reject_on_failure | model_error_on_failure` (default reject, overridable per field or per document type), `recompute`,
  and a "use sparingly" declaration. Deferred fields remain stand-alone (0.4) and never affect the document.

- **0.4**: P5 (deferred) is a stand-alone stage outside the P1–P4/P6/P7 chain. One-way dependency rule: computed fields,
  validators and defaults cannot depend on deferred fields; deferred fields can depend on plain, computed and deferred
  fields and run only when their deferred inputs have values (promises). Deferred failures use a per-field `on_failure`
  policy and are reported as deferred status, never as model errors and never as a reason to reject a write.

- **0.3**: Project renamed to aduku. Removed the prototype status appendix (the explainer prototype is archived
  outside this repository). Added section 1.4 on language ports.
- **0.2**: Rejected contributions are no longer ledger entries. Added the optional rejection audit log with
  count/time retention (4.5); validation redesigned around model errors, the non-rejection flag, the no-new-errors rule
  and re-evaluation on schema change (section 10); deferred values excluded from validation; per-field `on_delete`
  for defaults; requests are always atomic (no partial acceptance); new interfaces, error codes, conformance tests and
  worked example 16.2.
- **0.1**: Initial draft.
