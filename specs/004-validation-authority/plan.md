# Implementation Plan: Validation Rule Library and Per-Field Authority

**Branch**: `004-validation-authority` | **Date**: 2026-10-19 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/004-validation-authority/spec.md`

## Summary

Extend `aduku-py` with a larger library of validation rules and a complete per-field authority. Four new field rules
(`min_len`, `pattern`, `one_of`, `unique_items`) and four new document rules (`all_or_none`, `at_least_one`, `at_most_one`,
`ordered`) join the existing three; a rule is registered with a typed argument declaration, so the loader checks every use
(unknown, missing, wrong-kind and out-of-range arguments, paths that do not exist, paths that are deferred, a rule in the
wrong place) in one generic pass instead of per-rule code. Authority gains inheritance (a field takes the nearest list above
it, its own list replaces it), a delete is checked against the effective authority of every declared field beneath, rejection
reasons name the field and the rule but not the allowed list, and `explain` and `schema check` report the effective list and
where it was declared.

Technical approach: the rule checks stay pure behaviors of the existing `validate` kind (`Subject` in, violations out), so
admission (V1), the dry run (V2/V3), re-evaluation on a schema change, the lenient flag and the no-new-errors rule need no
change. New pure code: a linear-time matcher for the `pattern` subset (`behaviors/pattern.py`), the rules themselves, and an
argument declaration (`ArgSpec`) on registration. Authority is one pure lookup on the schema (`authority_for`) used by
admission, `explain` and the schema check.

## Technical Context

**Language/Version**: Python 3.12 (as Features 001 to 003)

**Primary Dependencies**: none new at runtime. The `pattern` language is matched by an own Thompson-style matcher
(research R-2): the standard `re` module has back-references and look-around, whose meaning other ports cannot match and
whose worst case is exponential. Dev only: unchanged.

**Storage**: none new. Rules and authority are schema and engine concerns; the ledger, views and model errors are unchanged.

**Testing**: `pytest` with `hypothesis`; test-first (constitution X). New: unit tests for every rule and for the pattern
matcher (including a differential property test against `re` on the shared subset), loader tests for every refusal code, authority
tests (inheritance, override, wildcard, role, delete) at unit and service level, property tests that a rejected request
leaves the ledger unchanged and that the lenient flag never passes authority, shared conformance vectors for rules and
authority, and the unchanged 16.1/16.2 vectors.

**Target Platform**: Linux/macOS/Windows, same as Feature 001

**Project Type**: library with a thin CLI (extends `ports/py/`)

**Performance Goals**: a worst-shape pattern against a 10,000-character value in under 100 ms (SC-008); the added cost of rules
on a derivation is proportional to the number of rule declarations and the size of the values they read, with no extra pass
over the ledger.

**Constraints**: `engine/`, `schema/`, `behaviors/` stay pure (no clock, no IO); rules see only the merged and computed
document (Constitution VIII), never a deferred value; messages never contain a submitted value; no change to the stage order
or atomicity of Feature 1.

**Scale/Scope**: tens of rule declarations per schema; pattern length at most 500 characters, repeat counts at most 255.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How the plan complies |
|---|---|---|
| I. Ledger Is Truth | Pass | A rejected request records nothing (unchanged stage order, tested again for the new rules); model errors stay in the derived view, never in the ledger. |
| II. Schema Is Source of Truth | Pass | Rules, arguments, versions and authority lists are declared in the schema and bound to registered behaviors; argument checks come from the registration, not from loader special cases. |
| III. Determinism and Purity | Pass | Rules and the pattern matcher are pure functions of their inputs; the matcher is linear in the value and has no state between calls; the purity import test covers the new module. |
| IV. IO Is Confined | Pass | No IO is added. |
| V. Algebraic Laws (NON-NEGOTIABLE) | Pass | No merge rule is added or changed. Rules do not take part in merging. |
| VI. Derived Fields Never Writable | Pass | Authority is checked after the derived-path check; no list can make a derived path writable; tests cover derived paths under every authority shape. |
| VII. Explainability | Pass | `explain` gains the effective authority and its source; model errors keep their rule and scope. |
| VIII. Sandboxed Behaviors | Pass | A rule gets the value (field rule) or the merged and computed document (document rule) and its own arguments; deferred values are never in that document, and a schema that points a rule at one is refused. |
| IX. Immutable Schema Versions | Pass | Rules are pinned by version in the schema; re-evaluation uses the version the schema names, not the newest registered. |
| X. Test-First and Conformance (NON-NEGOTIABLE) | Pass | Tasks put failing tests and vectors first; the 16.1 and 16.2 vectors are run unchanged. |
| XI. Simplicity | Pass | Redaction, cross-document rules and a full regular-expression engine stay out. One own matcher and one argument declaration are added; see Complexity Tracking. |

Scope and technology constraints: stable key ordering and canonical text are unchanged; the new schema syntax, codes and
vectors are specified in `contracts/` so other ports can implement them. No violations.

## Project Structure

### Documentation (this feature)

```text
specs/004-validation-authority/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── schema-format.md         # delta: rule declarations, arguments, authority inheritance, load codes
│   ├── rules.md                 # the rule library: each rule, its arguments, what it reports, the pattern subset
│   ├── library-api.md           # delta: register_validate with arguments, Explanation authority, reasons
│   ├── cli.md                   # delta: schema check inventory, explain
│   └── conformance-vectors.md   # delta: new vectors and the explain fields
├── checklists/
└── tasks.md             # Phase 2 output (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
conformance/
├── validation-rules.ion         # new: every field rule, good and bad values, lenient, no value in messages
├── document-rules.ion           # new: the four document rules, together-satisfying requests, no-new-errors, version change
├── authority.ion                # new: tiers, roles, inheritance, override, wildcard, delete, several entries, lenient
├── schema-load-failures.ion     # extended: rule argument, place and path errors, bad pattern, empty authority
└── README.md                    # new vectors and fixture rules documented

ports/py/src/aduku/
├── behaviors/
│   ├── pattern.py               # NEW, pure: parse and match the pattern subset in time linear in the value
│   ├── validate.py              # + min_len, pattern, one_of, unique_items, all_or_none, at_least_one, at_most_one, ordered;
│   │                            #   ArgSpec declarations for every rule (the three existing ones included)
│   └── registry.py              # + ArgSpec, Behavior.args and .applies, register_validate(..., args=, applies=)
├── schema/
│   ├── model.py                 # + Schema.authority_for(path) -> (list, declared by)
│   └── loader.py                # generic argument check from ArgSpec; RULE_WRONG_PLACE, UNKNOWN_RULE_PATH;
│                                #   path arguments joined to the deferred refusal; EMPTY_AUTHORITY
├── engine/
│   ├── admission.py             # effective authority, delete rule over effective lists, reasons without the list
│   ├── explain.py               # + authority and where it was declared
│   └── validate.py              # (unchanged contract) rule fault handling covers the new rules
├── model.py                     # + Explanation.authority, Explanation.authority_from
└── cli.py                       # schema check: effective authority and rules per field; explain prints authority

ports/py/tests/
├── unit/                        # pattern matcher, every rule, ArgSpec checks, loader refusals, authority_for
├── property/                    # pattern vs re on the subset; rejected request leaves no trace; lenient never passes authority
├── integration/                 # service-level rules, authority matrix, lenient, no-new-errors, version change, custom rules
└── golden/                      # 16.1 and 16.2 unchanged; new vectors
```

**Structure Decision**: extend the single library under `ports/py/`. Rules stay in `behaviors/` (pure); the only new module is
the matcher. No new storage, edge module or stage.

## Complexity Tracking

| Choice | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| An own pattern matcher (research R-2) | The same pattern must mean the same in every port, and no input may take unbounded time | `re` has back-references and look-around (not portable) and can take exponential time on crafted patterns |
| A typed argument declaration on registration (research R-1) | Eight new rules and custom rules all need the same checks; per-rule `check_args` functions would repeat them | Hand-written `check_args` per rule: more code, drift between rules, and no way for the loader to find path arguments for the deferred refusal |
| Authority lookup on the schema (research R-6) | Admission, `explain` and the schema check must agree on the effective list | Three copies of the walk up the field patterns |
