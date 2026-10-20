# Quickstart: validating Feature 5

Prerequisites: `cd ports/py && uv sync`. The walkthrough uses `examples/history.schema.ion`; `examples/demo_history.py` runs the same
things through the library.

## Automated

```bash
cd ports/py
uv run pytest tests/unit tests/property tests/integration tests/golden -q              # everything
uv run pytest tests/property -q -k "layers or as_of or compaction or purge"            # the new properties
uv run pytest tests/property -q --hypothesis-profile=ci                                # 1,000 examples each
uv run ruff format --check . && uv run mypy src
```

## Manual walkthrough

```bash
cd ports/py
export ADUKU_DB=/tmp/demo.db ADUKU_SCHEMA=$PWD/examples/history.schema.ion     # `dpo` may purge; see the schema file
alias a='uv run aduku'

# 1. a document with some history (seq 1 to 7; seq 6 states a valid_time)
for n in 1 2 3 4 5; do a append acme --contributor u1 --tier user --set "/name=\"Acme $n\"" >/dev/null; done
a append acme --contributor job --tier backfill --set /age=7 --valid-time 2024-03-01T00:00:00Z >/dev/null
a append acme --contributor u1 --tier user --set '/email="a@example.com"' >/dev/null
a rebuild acme                    # identical: true; layers_differing and layers_refreshed are empty (the appends saved them)

# 2. time travel
a get acme --as-of seq=3          # name "Acme 3", as_of.resolved_seq 3
a get acme --as-of valid=2024-02-01T00:00:00Z     # leaves out seq 6 (valid from March); seq 7 states no time and takes part
a get acme --as-of seq=3 --as-of valid=2024-02-01T00:00:00Z     # AS_OF_AMBIGUOUS, exit 2

# 3. compaction
a explain acme /name > before.txt
a compact acme --through-seq 7    # watermark 7, replaced 7, synthetic 2, checked_paths 3
a explain acme /name > after.txt; diff before.txt after.txt     # no difference while the archive is kept
a archive verify acme             # checked: true, differences: []
a archive expire acme --through-seq 7
a get acme --as-of seq=3          # HISTORY_COMPACTED (exit 1): the earliest seq that can still be answered is shown

# 4. purge (`purge_by` lists the role dpo)
a purge acme --path /email --reason "ticket 4711" --contributor officer --tier admin --role dpo
a purge-log acme                  # one record, no value
a rebuild acme                    # identical: true
```

## Success criteria map

| Criterion | Where it is checked |
|---|---|
| SC-001 | `tests/integration/test_snapshot_performance.py` (100,000 entries; restart; first write; read) |
| SC-002 | `tests/property/test_layers_equal_replay.py`; vectors with layers on and off |
| SC-003 | `tests/integration/test_rebuild_layers.py` (damage a layer, a tier_seq and a view) |
| SC-004 | `tests/property/test_as_of_history.py`; counting hook/source doubles in `test_as_of_isolation.py` |
| SC-005 | `tests/property/test_compaction_invariance.py`; `test_compaction_check_refuses.py` |
| SC-006 | `tests/integration/test_compaction_service.py` (10,000 entries on 20 fields leave at most 61) |
| SC-007 | `tests/integration/test_purge_service.py` (every table scanned for a marker), `tests/property/test_purge_equals_derivation.py` |
| SC-008 | `tests/golden` with `layers=False`; conformance runner |
