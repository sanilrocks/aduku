# Quickstart: Validate Computed Fields and Non-Writability

A run guide proving the feature end to end. Details: [data-model.md](data-model.md), [contracts/](contracts/). It assumes
Feature 001 is in place (`ports/py/`, `uv sync` done).

## Automated validation

```bash
cd ports/py
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest tests/unit                 # expression language, presets, graph order, loader checks
uv run pytest tests/golden               # SPEC 16.1 with displayName and label; every vector in ../../conformance
uv run pytest tests/property --hypothesis-profile=ci   # order independence, cache vs scratch, determinism
uv run pytest tests/integration          # counting hook, failure modes, recompute, lock discipline, rebuild
```

Expected: all green; the 16.1 output matches the vector exactly (SC-001); property tests show 0 mismatches (SC-003, SC-004).

## Manual walkthrough with the CLI

The example schema `examples/computed.schema.ion` has two pure computed fields (`displayName`, `label`) and one impure one
(`taxId`). `examples/demo_hooks.py` registers the custom macro `acme.label@1` and the hook `fx.echo@1`, which the schema names.

```bash
cd ports/py
rm -f /tmp/aduku-demo2.db*
export ADUKU_DB=/tmp/aduku-demo2.db
export ADUKU_SCHEMA=examples/computed.schema.ion
export ADUKU_BEHAVIORS=demo_hooks PYTHONPATH=examples        # or: aduku --behaviors demo_hooks ... with PYTHONPATH=examples
uv run aduku schema check examples/computed.schema.ion        # lists the computed fields and the impure inventory
uv run aduku append d1 --contributor job-1 --tier backfill --set /name='"A. Smith"' --set /address/city='"Paris"'
uv run aduku append d1 --contributor alice --tier user --set /name='"Alice S."'
uv run aduku get d1
```

Expected: the document has `displayName:computed::"ALICE S."` and `label:computed::"Alice S. (Paris)"`.

```bash
uv run aduku append d1 --contributor root --tier admin --set /displayName='"HACKED"'    # exit 1: FIELD_NOT_WRITABLE
uv run aduku entries d1                                                                 # still seq 1 to 3; nothing consumed
uv run aduku explain d1 /displayName       # winner computed, rule upper@1, dependencies: /name = "Alice S."
uv run aduku append d1 --contributor alice --tier user --set /name='"Bob"'
uv run aduku get d1                        # displayName "BOB", label "Bob (Paris)"
```

Impure computes (the hook is called once per distinct `/country`):

```bash
uv run aduku append d2 --contributor alice --tier user --set /country='"US"'            # calls fx.echo@1 once
uv run aduku get d2                        # taxId:computed::"tax-US", from the recorded outcome
uv run aduku effects list d2               # one record: field, hook, outcome value, attempts 1 (never the value)
uv run aduku rebuild d2                    # identical; no new call, so still one record
uv run aduku recompute d2 /taxId --contributor alice --tier user    # exit 1: NOT_AUTHORIZED (the schema lists admin)
uv run aduku recompute d2 /taxId --contributor root --tier admin    # exit 0: a second record supersedes the first
uv run aduku effects list d2
```

Schema errors: a schema whose computed fields depend on each other, or on a path nobody declares, is refused at load, with
every problem listed:

```bash
cat > /tmp/bad.schema.ion <<'ION'
schema::{id: "t", version: 1, tiers: [a], fields: {
  "/x": {type: string, computed: {macro: upper, args: ["/y"]}},
  "/y": {type: string, computed: {macro: upper, args: ["/x"]}},
  "/z": {type: string, computed: {macro: upper, args: ["/nope"]}}}}
ION
uv run aduku schema check /tmp/bad.schema.ion      # exit 2: UNKNOWN_DEPENDENCY and DEPENDENCY_CYCLE (/x -> /y -> /x)
```

Missing inputs (`on_missing`), the failure modes of a hook and the macro faults need schemas with other settings; they are
exercised by `conformance/computed-on-missing.ion`, `conformance/impure-computes.ion` and the integration tests.

## Success checks mapped to the spec

| Check | Spec |
|---|---|
| 16.1 document with computed values matches the vector | SC-001, US1 |
| Writes to computed paths rejected, 0 entries, 0 seqs | SC-002, US2 |
| Cache, scratch and both backends give identical computed text | SC-003 |
| An input change reaches every dependent computed field | SC-004 |
| Cycles, undeclared and deferred dependencies refused at load | SC-005, US4 |
| Macro cannot see beyond its inputs; limit overruns are stopped | SC-006, US3 |
| 1,000 entries with 5 computed fields read in under 1 second | SC-007 |
| Zero hook calls after the first outcome, across 100 repeats | SC-008, US6 |
| Failure modes behave as specified; recompute clears the error | SC-009, US7 |
| A slow hook does not delay another document's write | SC-010 |
| One `explain` call answers for a computed path | SC-011 |
