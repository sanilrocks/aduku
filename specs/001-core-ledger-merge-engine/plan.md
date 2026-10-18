# Implementation Plan: Core Ledger and Merge Engine

**Branch**: `001-core-ledger-merge-engine` | **Date**: 2026-10-18 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-core-ledger-merge-engine/spec.md`

## Summary

Build the first slice of `aduku-py`: an append-only ledger with atomic contribution requests (P0 admission), and a
pure derivation pipeline (P1 squash, P2 merge, P3 tombstones, P6 validation, minimal P7 projection) driven by a
central Ion schema and a behavior registry. Model errors, the non-rejection flag, version-change re-derivation, the
optional capped rejection audit log and `explain` are included. Computed fields (P4), deferred fields (P5), the
sandbox, snapshots, compaction and time-travel are other features.

Technical approach: a pure-Python library (`aduku`) in `ports/py/`, organised as one pure module per phase over
immutable dataclasses, with all IO (SQLite, clock, audit log) behind a storage interface at the edge. Correctness is
established test-first by property tests of the algebraic laws, the normative examples of SPEC 16.1 and 16.2 as
golden tests, and language-neutral conformance vectors kept at the repository root so later ports can reuse them.

## Technical Context

**Language/Version**: Python 3.12 (`uv`-managed; 3.12.3 present locally)

**Primary Dependencies**: `amazon.ion` (Ion documents, annotations, text/binary codec), a ULID library for entry and request ids (behind an injected `IdSource`). Core has no framework
dependency. Dev only: `pytest`, `hypothesis`, `ruff`, `mypy`.

**Storage**: SQLite in WAL mode behind a `Storage` interface (stdlib `sqlite3`). Tables: `entries` (ledger),
`doc_seq` (per-document counter), `requests` (one row per request; unique request key per document and
contributor), `views` (derived-view cache incl. model errors), `rejection_audit`, `audit_meta` and `audit_override`
(separate, audit-only). In-memory implementation of the same interface for tests.

**Testing**: pytest; hypothesis for associativity, commutativity, idempotence, squash equivalence, order tolerance
(including `tx_time` changes) and atomicity; golden tests from SPEC 16.1 and 16.2; replay test (`rebuild` equals
cache); conformance vectors in `conformance/*.ion`.

**Target Platform**: Linux, macOS and Windows with CPython 3.12+; embedded library plus a thin CLI.

**Project Type**: library with CLI

**Performance Goals**: SC-006, a read of a document with up to 1,000 accepted entries in under 1 second uncached.

**Constraints**: Deterministic, byte-identical output (canonical serialization, R-3). No wall-clock, randomness or IO
in P0 checks, P1-P3, P6, P7 (clock enters only at commit and audit pruning). Rejected requests write nothing to the
ledger.

**Scale/Scope**: Per-document ledgers of up to thousands of entries; single logical writer per document (spec
assumption). Tens of schema fields. Documents are independent.

**Assumptions the user can change** (recorded per request, see also R-1):

- Python code lives in `ports/py/` in this repo until stable, then is split into its own `aduku-py` repository. Shared
  conformance vectors stay in `conformance/` at the repo root and are consumed by the port's tests.
- A minimal set of validator presets (`non_empty`, `range`, `max_len`) ships because the normative examples use them;
  the general library of validation rule types is Feature 4.
- Custom merge behaviors are registered Python callables with declared laws; the sandboxed macro language is Feature 2.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design: no violations.*

| Principle | Status | How the plan complies |
|---|---|---|
| I. Ledger Is Truth | Pass | `entries` is append-only (no UPDATE/DELETE issued; a trigger forbids both). A request commits in one transaction or not at all. Rejection audit is a separate table and a separate module that the engine never imports. |
| II. Schema Is Source of Truth | Pass | All field semantics come from the loaded schema bound to registry behaviors; no per-field special cases in the engine. |
| III. Determinism and Purity | Pass | Phase modules are pure functions of `(entries, schema)`; clock and storage injected only at the edge. The constitution also allows an effect log as an input (impure computes); it is empty in this feature, so derivation takes no such input yet. |
| IV. IO Is Confined | Pass (n/a) | No deferred hooks in this feature; IO limited to the storage and clock interfaces. |
| V. Algebraic Laws (NON-NEGOTIABLE) | Pass | Every merge behavior declares laws; load refuses a missing `associative`; hypothesis suite per behavior and for squash equivalence. |
| VI. Derived Fields Never Writable | Pass (partial) | `FIELD_NOT_WRITABLE` check is implemented in P0 against schema derived-field declarations (parsed, not evaluated) and is not bypassable by the flag. Evaluation is Features 2 and 3. |
| VII. Explainability | Pass | `explain(doc, path)` returns winner, rule@version, superseded entries or default, and scoped model errors. |
| VIII. Sandboxed Behaviors | Pass (scoped) | Behaviors receive only `(path, old, new)` or a validation view of the document; no ambient handles passed. Real sandboxing of custom code is Feature 2. |
| IX. Immutable Schema Versions | Pass | Schema objects are frozen; the registry rejects re-registering a version with different content; activation selects a version. |
| X. Test-First and Conformance (NON-NEGOTIABLE) | Pass, with one recorded deviation | Tasks order tests before implementation per phase, and each behavior had a failing test first. The 16.1 golden test (T080) is the exception: it was written last as the acceptance gate and never observed failing (see Complexity Tracking). |
| XI. Simplicity | Pass | Non-goals stay out. The only extra seam is the `Storage` interface, which the handoff mandates and the tests need. |

Scope and technology constraints: stable key ordering (R-3); pure phases do not read time; port conformance via
shared vectors (R-9). No violations, so Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/001-core-ledger-merge-engine/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── library-api.md
│   ├── schema-format.md
│   ├── cli.md
│   └── conformance-vectors.md
├── checklists/
└── tasks.md             # Phase 2 output (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
conformance/                      # language-neutral vectors shared by all ports (SPEC 16, item 12)
├── customer.schema.ion           # schema of the worked example and the quickstart
├── worked-example-16-1.ion
├── model-errors-16-2.ion
├── atomicity.ion  retries.ion  schema-load-failures.ion  defaults-on-delete.ion  tombstones.ion  explain.ion
├── clock-early.ion  clock-late.ion   # differ only in the harness clock; must give identical results
└── README.md

ports/py/                         # aduku-py; split into its own repo once stable
├── pyproject.toml                # uv, ruff, mypy, pytest config
├── src/aduku/
│   ├── model.py                  # Entry, Request, Tier, ModelError, Reason, ... (frozen dataclasses)
│   ├── paths.py                  # path parsing and normalization (SPEC 3.2)
│   ├── ion_codec.py              # Ion <-> internal values, tombstone, canonical serialization
│   ├── errors.py                 # error codes (SPEC 12.5), RejectedError, StorageError
│   ├── log.py                    # structured log events (admission decisions, rebuilds)
│   ├── schema/
│   │   ├── loader.py             # parse Ion schema, fail closed (SPEC 5.1, FR-009/011/021/028)
│   │   ├── model.py              # immutable Schema, FieldSpec
│   │   └── registry.py           # versioned schema registry, activate, pin
│   ├── behaviors/
│   │   ├── registry.py           # behavior registry with laws metadata
│   │   ├── merge.py              # lww, max, min, union, struct
│   │   └── validate.py           # non_empty, range, max_len; validator protocol
│   ├── engine/
│   │   ├── admission.py          # P0: schema checks, dry-run, lenient, no-new-errors
│   │   ├── request_hash.py       # canonical request hash for retries (SPEC 4.2 item 4)
│   │   ├── squash.py             # P1
│   │   ├── merge_layers.py       # P2
│   │   ├── tombstones.py         # P3
│   │   ├── defaults.py           # the defaults layer and delete fallbacks
│   │   ├── document.py           # assemble the nested document from path values
│   │   ├── validate.py           # P6: model errors
│   │   ├── project.py            # P7 (minimal): document + model errors
│   │   ├── derive.py             # composes P1-P3 and keeps the derivation state
│   │   ├── model_errors.py       # dry-run extension, attribution by version-aware replay (R-7), no-new-errors
│   │   └── explain.py            # winner, rule, inputs and shadowed entries, worked out on demand
│   ├── storage/
│   │   ├── base.py               # Storage protocol, Clock protocol
│   │   ├── memory.py
│   │   └── sqlite.py
│   ├── audit.py                  # rejection audit log + retention (separate from engine)
│   ├── service.py                # Aduku facade: append, get, explain, rebuild, audit admin
│   └── cli.py
└── tests/
    ├── conftest.py               # hypothesis profiles dev and ci
    ├── oracle.py                 # reference flat fold used by property tests
    ├── fixtures.py               # fixture behaviors (for example max_spouses_by_country)
    ├── conformance_runner.py     # runs conformance/*.ion cases
    ├── unit/
    ├── property/                 # hypothesis law and equivalence tests
    ├── golden/                   # SPEC 16.1 and 16.2 via conformance vectors
    └── integration/              # sqlite, atomicity, replay, concurrency
```

**Structure Decision**: single library project under `ports/py/` with a separate root-level `conformance/` folder.
One pure module per engine phase mirrors SPEC section 7 and keeps Principle III checkable by import rules
(`engine/*` and `schema/*` must not import `storage`, `audit`, `time`, `random` or `sqlite3`; enforced by a test).

## Complexity Tracking

One recorded deviation, found by `/speckit-analyze` after implementation (2026-10-18): Principle X says tests MUST be
written and observed failing before implementation. The golden test for the worked example 16.1 (T080) was written last,
as the acceptance gate, and was green at its first run, so it was never observed failing. Mitigation: every behavior it
combines (defaults, tombstones, squash, overlay, the rejected derived-field write) had its own failing test and vector
first. Remedy for the next feature: write the worked-example vector first, or state in the plan that the end-to-end
vector is an acceptance gate and not a test-first artifact.
