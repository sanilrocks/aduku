# Implementation Plan: Schema Versioning and Evolution

**Branch**: `006-schema-evolution` | **Date**: 2026-10-20 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/006-schema-evolution/spec.md`

## Summary

Extend `aduku-py` so that schema versions are immutable across restarts, a change between two versions is classified and previewed
before it is activated, documents can be pinned and migrated in a planned way, and every entry left out of a document says why.

Technical approach, in four layers that can ship in order:

1. **Shared schema state.** The active version, the document pins, the fingerprint of every served version and a history of every
   change move from the in-memory `SchemaRegistry` into the storage the ledger already uses (four small tables, one generation
   counter). The registry keeps holding the versions the application supplies; it reads the active version and pins through a state
   store when bound to a service, and writes through it, so the existing calls (`activate`, `pin`, `version_for`) keep working and
   become durable and shared. A registry used without a service behaves exactly as today.
2. **Change classification (pure).** `schema/diff.py` compares two `Schema` objects field by field and gives each difference a path,
   a description and a class (compatible, visible, breaking) from one table that mirrors docs/SPEC.md section 14; anything outside the
   table is "visible, not classified".
3. **Dry run and activation.** A dry run derives each examined document under the target version with the engine's own replay and
   compares with what the document reads now; it writes nothing and calls nothing. `activate_schema` adds the acknowledgement for a
   breaking change, the history record, an optional eager rebuild, and a rollback that is just another activation.
4. **Pinning, migration and reasons.** `pin`, `unpin` and `migrate` (single, batch, resumable) on top of layer 1; a pure
   `engine/shadow.py` that names why an entry is skipped, shown with the document, in `explain` and in the dry run. One new rule in
   the fold: an entry written under another version whose value no longer conforms to the type now in force is skipped (this is
   what makes "no longer conforms" a reason; it cannot change any ledger that never changed a type).

The engine modules stay pure; the new edge work (state tables, the state store, operator calls) sits in the service and storage.

## Technical Context

**Language/Version**: Python 3.12 (as Features 001 to 005)

**Primary Dependencies**: none new. Dev only: unchanged.

**Storage**: SQLite (WAL) and the in-memory backend behind the one storage interface. New tables `schema_versions`, `schema_state`,
`doc_pins`, `schema_history` (insert-only). Details in [data-model.md](data-model.md).

**Testing**: `pytest` with `hypothesis`; test-first (constitution X): unit tests for the classification table (every row, plus the
unclassified default), the shadow reasons and the dry-run comparison; property tests that the dry run predicts what a read gives
after activation and that activate-rollback-activate is byte-identical; integration tests on both backends for the state store
(two services on one storage, restarts, a changed version refused), pins, batch migration (stop and resume) and operator calls;
shared conformance vectors for classification, the dry run, shadow reasons and pins; the 16.1/16.2 and Features 1 to 5 vectors
unchanged.

**Target Platform**: Linux/macOS/Windows, same as Feature 001

**Project Type**: library with a thin CLI (extends `ports/py/`)

**Performance Goals**: SC-005: activation of a compatible version touches no document, so its time does not depend on the number of
documents; a dry run limited to 1,000 of 10,000 documents takes no more than a tenth of an unlimited one. Reading the shared state on
a request costs one indexed read of a single row (the generation) and reloads the active version and the pins only when it changed.

**Constraints**: `engine/` and `schema/` stay pure (no clock, no IO; the state store is an injected interface); a dry run writes
nothing and calls nothing; activation, pin and migration are single transactions with their history record; no value appears in logs
or in the history; no change to any merged result of Features 1 to 5.

**Scale/Scope**: tens of schema versions, thousands of pinned documents, millions of documents examined by a dry run (linear).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How the plan complies |
|---|---|---|
| I. Ledger Is Truth | Pass | No entry is ever written, changed or removed by a schema change; reasons are derived, not stored; the history is a separate insert-only log, never read by a derivation. |
| II. Schema Is the Single Source of Truth | Pass | Classification reads the schema content only; no semantics move into the service. |
| III. Determinism and Purity | Pass | Classification, shadow reasons and the dry-run comparison are pure functions of (schemas, entries, effect log); the clock enters only at the service edge for the history. |
| IV. IO Is Confined | Pass | A dry run and an eager rebuild report a document that needs an impure hook call instead of calling it (the rebuild keeps its Feature 2 rule: a missing outcome is filled once on a real read). |
| V. Algebraic Laws (NON-NEGOTIABLE) | Pass | No merge rule changes. |
| VI. Derived Fields Never Writable | Pass | A field that becomes derived shadows older entries (reason "now derived"); nothing new becomes writable. |
| VII. Explainability | Pass | `explain` lists skipped entries with their reasons; the history says who changed what and when. |
| VIII. Sandboxed Behaviors | Pass | No behavior sees more. |
| IX. Immutable Schema Versions | Pass, strengthened | A served version's fingerprint is remembered; changed content under a served number is refused after a restart. Breaking changes ship with the classification, the dry run and an optional rebuild, as the principle requires. |
| X. Test-First and Conformance (NON-NEGOTIABLE) | Pass | Failing tests and vectors first; Feature 5's recorded deviation is not repeated (tasks put each test before its code). |
| XI. Simplicity | Pass, justified | Per-field precedence and multi-region seq stay out. The four tables and the state store are justified in Complexity Tracking. |

Scope and technology constraints: canonical text and stable ordering for the change list; new codes, commands and vectors are in
`contracts/` for other ports. Re-check after design: no violation.

## Project Structure

### Documentation (this feature)

```text
specs/006-schema-evolution/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── changes.md               # the classification table and the change list format
│   ├── library-api.md           # delta: activate_schema, plan_schema, pin, unpin, migrate, history, reasons
│   ├── schema-state.md          # the state store interface and the stored tables
│   ├── cli.md                   # delta: schema diff/plan/activate/history, pin, unpin, migrate, --schema repeatable
│   └── conformance-vectors.md   # delta: new steps and vectors
├── checklists/
└── tasks.md             # Phase 2 output (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
conformance/
├── schema-changes.ion       # new: one case per row of the classification table, the overall class, the unclassified default
├── schema-dry-run.ion       # new: dry run vs the real activation, limits, pinned documents left out, rollback byte-identical
├── schema-shadow.ion        # new: one entry of each shadow reason, shown with the document and in explain
├── schema-pins.ion          # new: pin, write under the pin, migrate, batch and resume
└── README.md                # new steps documented

ports/py/src/aduku/
├── schema/
│   ├── diff.py              # NEW, pure: Change, ChangeClass, diff(old, new), overall_class
│   ├── model.py             # + Schema.content_fingerprint and parts (per-field digests)
│   └── registry.py          # + the SchemaState interface, bind(), generation-checked active()/version_for(), write-through
├── engine/
│   ├── shadow.py            # NEW, pure: shadow_reasons(schema, entries, skipped) -> reasons
│   ├── derive.py            # fold_entries: skip an entry written under another version whose value no longer conforms
│   ├── dryrun.py            # NEW, pure: compare two derivations of one document -> DocumentChange
│   └── explain.py           # + skipped entries with reasons
├── storage/
│   ├── base.py              # + Tx schema-state methods
│   ├── memory.py, sqlite.py # tables, insert-only history trigger
├── schema_state.py          # NEW, edge: the adapter that binds a registry to a storage
├── service.py               # + activate_schema, plan_schema, pin_document, unpin_document, migrate, schema_history;
│                            #   skipped reasons on DocumentResult
├── model.py                 # + Change, DryRunReport, DocumentChange, MigrationReport, HistoryRecord, Shadowed
├── errors.py                # + codes (see contracts/library-api.md)
└── cli.py                   # + schema diff|plan|activate|history, pin, unpin, migrate, repeatable --schema

ports/py/tests/
├── unit/                    # classification table, shadow reasons, dry-run comparison, fingerprints
├── property/                # dry run predicts the read; rollback is byte-identical; classification is symmetric in what it lists
├── integration/             # both backends: shared state across services, restart, refused change, pins, migration, CLI
└── golden/                  # the new vectors; 16.1/16.2 and Features 1 to 5 unchanged
```

**Structure Decision**: extend the single library under `ports/py/`. Pure logic goes in `schema/diff.py` and `engine/{shadow,dryrun}.py`;
the only impure additions are the state adapter, the service operations and the storage tables.

## Complexity Tracking

| Choice | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Shared schema state in the store (R-1) | The clarification makes activation and pins shared and atomic across processes; the history must be durable anyway | Keeping them in each process cannot make a rollback one act, nor stop two processes admitting under different versions |
| Per-field fingerprints of a served version (R-2) | To refuse a changed version and name which fields changed without keeping the schema text | A single digest says "changed" but not where; keeping the full text contradicts the assumption that the store does not hold schemas |
| A dry run built on the engine's replay (R-5) | The prediction must equal the real read (SC-002) | A separate "predictor" would drift from the read path |
| The conformance rule in the fold (R-4) | "No longer conforms" must be a real shadow reason, not only a label | Reporting it without skipping the entry would leave documents violating the new type |
