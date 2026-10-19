# Implementation Plan: Computed Fields and Non-Writability

**Branch**: `002-computed-fields` | **Date**: 2026-10-19 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/002-computed-fields/spec.md`

## Summary

Add phase P4 to `aduku-py`: computed fields evaluated over the merged document in dependency order, shown with the
`computed::` annotation, never stored in the ledger and never writable (the P0 rejection of Feature 001 stays and gains
tests for the new field kinds). Pure computes use a closed library of preset macros and registered custom macros written
in a small expression language (a subset of CEL's syntax) that runs in a sandbox by construction, with deterministic limits.
Impure computes call registered hooks outside the pure engine, with timeout, bounded retry and deadline; every outcome is
written to an append-only effect log, and derivation reads recorded outcomes instead of calling hooks, so rebuild and
repeated reads make no calls. `recompute`, `list_effects` and `prune_effects` are the admin operations.

Technical approach: P4 is one new pure module (`engine/compute.py`) between P3 and P6, fed by an effect-log view; the
expression language is a new pure package (`expr/`); hooks run in an edge module (`hooks.py`) that the service calls
before it takes the write lock, so a request is derived at an observed watermark, hooks are called, and the write
transaction re-checks the watermark. The effect log is a new append-only table next to the ledger. Existing vectors that
contain `displayName` change in the same commit.

## Technical Context

**Language/Version**: Python 3.12 (as Feature 001)

**Primary Dependencies**: none new at runtime. The expression language is implemented in this package (research R-1:
the CEL library evaluated in a spike lacks the string functions and a cost limit). Dev only: unchanged (`pytest`,
`hypothesis`, `ruff`, `mypy`).

**Storage**: SQLite (WAL) and the in-memory backend, behind the existing storage interface, extended with an
append-only `effects` table and a per-document effects watermark stored with the cached view.

**Testing**: `pytest` with `hypothesis`; test-first (constitution X). New: unit tests for the expression language and
every preset, property tests for dependency-order independence and cache-versus-scratch equality, integration tests with
a counting hook, golden tests for the SPEC 16.1 computed output, and shared conformance vectors (new fixture macro and
hooks).

**Target Platform**: Linux/macOS/Windows, same as Feature 001

**Project Type**: library with a thin CLI (extends `ports/py/`)

**Performance Goals**: uncached read of a 1,000-entry document with 5 computed fields in under 1 second (SC-007); P4
adds work proportional to the number of computed fields, not to the ledger.

**Constraints**: derivation stays a pure function of (ledger, effect log, schema version) with no clock and no IO inside
`engine/`, `expr/`, `schema/` or `behaviors/`; a hook is never called while the write lock is held; limits are
deterministic (steps and sizes, not wall-clock time).

**Scale/Scope**: tens of computed fields per document type; one effect-log record per impure field per distinct input.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How the plan complies |
|---|---|---|
| I. Ledger Is Truth | Pass | Computed values are never entries. The effect log is a separate append-only table that holds no contributions and is not read by merge or validation. A rejected request writes neither entries nor outcomes. |
| II. Schema Is Source of Truth | Pass | Macro, hook, dependencies, `on_missing`, `failure_mode` and `recompute_by` are declared in the schema and bound to registered behaviors; the engine has no per-field special cases. |
| III. Determinism and Purity | Pass | P4 is a pure function of the merged document and the recorded outcomes. Time and external state enter only as recorded outcomes. Macro limits are step and size budgets, so a fault is the same on every machine. |
| IV. IO Is Confined | Pass | The only new IO is the impure hook call in `hooks.py` (edge), with timeout, bounded retry and credentials owned by the hook. The pure packages keep the import-rule test. |
| V. Algebraic Laws (NON-NEGOTIABLE) | Pass | Computed fields have no merge rule; the laws of Feature 001 are untouched. Property tests assert order independence of the dependency graph and determinism of evaluation. |
| VI. Derived Fields Never Writable | Pass | P0 rejects any entry on or beneath a computed path with `FIELD_NOT_WRITABLE`, not bypassable by the flag; tests cover pure and impure fields, children, deletes and mixed requests. |
| VII. Explainability | Pass | `explain` on a computed path names the macro or hook, version, dependency values and the effect-log record. |
| VIII. Sandboxed Behaviors | Pass | A macro sees only its declared dependencies (they are the only variables in the expression); the language has no clock, randomness or IO; limits are enforced by the evaluator. Hooks are the declared, audited exception. |
| IX. Immutable Schema Versions | Pass | Unchanged. Hook and macro versions are pinned by the schema; a changed hook version means new fingerprints and new calls. |
| X. Test-First and Conformance (NON-NEGOTIABLE) | Pass | Tasks order failing tests first, including the 16.1 computed output; the worked example vector is written before the P4 implementation this time (Feature 001 deviation remedied). |
| XI. Simplicity | Pass | Deferred fields, redaction, snapshots and CEL itself stay out; the expression language is a deliberately small subset. See Complexity Tracking. |

Scope and technology constraints: stable key ordering and canonical text (Feature 001, R-3) apply to the `computed::`
annotation; the new language is specified in `contracts/expression-language.md` so other ports can implement it or
swap in CEL for the same subset. No violations.

## Project Structure

### Documentation (this feature)

```text
specs/002-computed-fields/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── expression-language.md   # the sandboxed language, its functions and limits
│   ├── schema-format.md         # delta to Feature 001: computed and impure declarations
│   ├── library-api.md           # delta: recompute, effects, registration, Explanation
│   ├── cli.md                   # delta: recompute, effects, schema check inventory
│   └── conformance-vectors.md   # delta: fixture macro and hooks, hook_calls step
├── checklists/
└── tasks.md             # Phase 2 output (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
conformance/
├── customer.schema.ion          # unchanged: the simple worked-example schema (displayName only)
├── worked-example-16-1.ion      # expected document gains displayName and label (computed::); its schema is inline
├── computed-fields.ion          # new: FIELD_NOT_WRITABLE on computed paths
├── computed-on-missing.ion      # new: on_missing and its model error
├── impure-computes.ion          # new: hook outcomes, failure modes, recompute, rebuild with zero calls
└── README.md                    # fixture macro and hooks documented

ports/py/src/aduku/
├── expr/                        # NEW, pure: the sandboxed expression language
│   ├── parser.py                # tokens and grammar (contracts/expression-language.md)
│   ├── evaluator.py             # tree walk with step, depth and size budgets
│   └── limits.py                # Limits (defaults, platform maximum, per-macro lowering)
├── behaviors/
│   ├── registry.py              # + kinds "macro" and "hook"; register_macro, register_hook
│   └── compute.py               # NEW, pure: preset macros, their signatures and accepts_missing flags
├── schema/
│   ├── model.py                 # + ComputedSpec, ImpureSpec on FieldSpec
│   └── loader.py                # + parse and check computed declarations, on_missing, recompute_by, reserved names
├── engine/
│   ├── graph.py                 # NEW, pure: dependency graph, evaluation order, cycle loops (used by the loader and P4)
│   ├── compute.py               # NEW, pure: P4 (graph order, evaluation, on_missing, annotation, faults)
│   ├── effects.py               # NEW, pure: fingerprints, choosing the latest outcome, which calls are needed
│   ├── derive.py                # build_document unchanged; P4 composed after it
│   ├── model_errors.py          # extend/_evaluate: P4 then P6, effect view in, exempt impure errors
│   └── validate.py              # validators may depend on computed values (V3)
├── hooks.py                     # NEW, edge: HookRunner (timeout, retry, backoff, deadline); not imported by engine/
├── storage/
│   ├── base.py                  # + Tx effect methods, ViewRow.effects_seq (EffectRecord is in model.py, so the engine can use it)
│   ├── memory.py                # + effect log
│   └── sqlite.py                # + effects table, append-only triggers, view column
├── model.py                     # + Explanation fields, RecomputeReport, EffectInfo
├── errors.py                    # + COMPUTE_FAULT, IMPURE_COMPUTE_FAILED codes
├── service.py                   # admission with hook phase, recompute, list_effects, prune_effects, explain
├── log.py                       # (unchanged) events hook_call and recompute use it
└── cli.py                       # + recompute, effects list/prune, schema check inventory

ports/py/examples/
└── demo_hooks.py                # NEW: register(behaviors) with counting hooks, for the quickstart CLI walkthrough

ports/py/tests/
├── unit/                        # expr parser and evaluator, presets, graph order, loader checks
├── property/                    # declaration-order independence, cache vs scratch, evaluation determinism
├── integration/                 # counting hook, failure modes, recompute, lock discipline, rebuild
└── golden/                      # 16.1 with computed fields, new vectors
```

**Structure Decision**: extend the single library under `ports/py/`. One pure module per concern keeps constitution III
checkable by the existing import test (`expr/` is added to its list); the only module allowed to call out is `hooks.py`,
which lives outside `engine/`, `expr/`, `schema/` and `behaviors/`.

## Complexity Tracking

| Choice | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| A purpose-built expression language (research R-1) | SC-006 needs custom logic that provably cannot reach the clock, network or files, with deterministic limits | Registered Python callables cannot be sandboxed in-process; a CEL library lacks string functions and cost limits; WebAssembly adds a large runtime for one feature |
| A hook runner with a thread per attempt (research R-8) | Timeouts and a deadline per hook call, abandoning a call that does not return | Calling hooks inline cannot enforce a timeout; a pool would keep a stuck call's worker |
| An effect log table (research R-9) | Replayable derivation needs recorded outcomes | Re-calling hooks on every derivation would break determinism and rebuild |
