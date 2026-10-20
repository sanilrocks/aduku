# Quickstart: validating Feature 6

Prerequisites: `cd ports/py && uv sync`. The walkthrough uses three small schema files you write beside the database.

## Automated

```bash
cd ports/py
uv run pytest tests/unit tests/property tests/integration tests/golden -q
uv run pytest tests/property -q -k "dry_run or rollback or classification" --hypothesis-profile=ci
uv run ruff format --check . && uv run mypy src
```

## Manual walkthrough

```bash
mkdir /tmp/evo && cd /tmp/evo
# v1: /name lww, /age max. v2: v1 plus /email (compatible) and a default for /name (visible).
# v3: v2 with /age computed from /name (breaking). Write them as v1.ion, v2.ion and v3.ion, for example v1:
#   schema::{id: "demo", version: 1, tiers: [backfill, user, admin],
#     fields: {"/name": {type: string, merge: lww}, "/age": {type: int, merge: max}}}
export ADUKU_DB=/tmp/evo/demo.db
one() { uv run --project ~/workspace/wonder/aduku/ports/py aduku --schema /tmp/evo/v1.ion "$@"; }
a() { uv run --project ~/workspace/wonder/aduku/ports/py aduku --schema /tmp/evo "$@"; }

a schema diff v1.ion v2.ion                       # overall: visible (field_added /email, default_changed /name)
a schema diff v2.ion v3.ion                       # overall: breaking (became_derived /age)
one append d1 --contributor u --tier user --set '/name="Ann"' --set '/age=3'   # an empty store adopts what it is given: version 1
a schema history                                  # [bootstrap -> 1]; from here the store decides, whatever the files hold
a schema plan --target 3                          # d1: the document changes, one entry would be shadowed (now_derived)
a schema activate 3 --operator alice              # refused: BREAKING_CHANGE_NOT_ACKNOWLEDGED, exit 1
a schema activate 3 --operator alice --acknowledge-breaking --rebuild     # changed: [d1]
a get d1                                          # shadowed: [{seq: 2, reason: now_derived}]
a schema activate 1 --operator alice              # rollback: d1 reads as before; the history shows it
a schema history
a pin d1 1 --operator alice; a schema activate 3 --operator alice --acknowledge-breaking; a get d1    # d1 stays on version 1
a migrate d1 --to 3 --operator alice --dry-run
a migrate d1 --to 3 --operator alice              # moved: [d1]; a get now shows the shadowed entry
```

An empty store takes the highest version among the files it is given (a single file is its own highest); a store that already
holds state serves what it was told to, whatever the files say.

## Success criteria map

| Criterion | Where it is checked |
|---|---|
| SC-001 | `tests/unit/test_schema_diff.py`; `conformance/schema-changes.ion` |
| SC-002 | `tests/property/test_dry_run_predicts_the_read.py`; `tests/integration/test_schema_plan.py` |
| SC-003 | `tests/property/test_rollback_is_byte_identical.py` |
| SC-004 | `tests/integration/test_schema_state.py` (restart with identical and with changed content) |
| SC-005 | `tests/integration/test_schema_activation_cost.py` |
| SC-006 | `tests/unit/test_shadow_reasons.py`; `conformance/schema-shadow.ion` |
| SC-007 | `tests/integration/test_schema_migration.py` (1,000 pinned documents, stopped and restarted) |
