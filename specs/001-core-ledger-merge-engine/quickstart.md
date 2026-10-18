# Quickstart: Validate the Core Ledger and Merge Engine

A run guide proving the feature end to end. Details: [data-model.md](data-model.md), [contracts/](contracts/).

## Prerequisites

- Python 3.12 and `uv` installed.
- Code in `ports/py/` (created by the implementation tasks).

## Setup

```bash
cd ports/py
uv sync            # installs amazon.ion and dev tools (pytest, hypothesis, ruff, mypy)
```

## Automated validation

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest tests/unit tests/golden            # worked examples 16.1 and 16.2 and every vector in ../../conformance
uv run pytest tests/property --hypothesis-profile=ci   # 1,000+ ledgers: squash equivalence, order tolerance, laws
uv run pytest tests/integration                  # sqlite atomicity, concurrency, replay, audit retention
```

Expected: all green; the golden tests match the vectors exactly (SC-001), property tests show 0 mismatches
(SC-002, SC-003).

## Manual walkthrough with the CLI

```bash
rm -f /tmp/aduku-demo.db*
export ADUKU_DB=/tmp/aduku-demo.db
export ADUKU_SCHEMA=../../conformance/customer.schema.ion
uv run aduku schema check ../../conformance/customer.schema.ion
uv run aduku append d1 --contributor job-1 --tier backfill --set /name='"A. Smith"' --set /age=30
uv run aduku append d1 --contributor alice --tier user --set /name='"Alice S."' --set /age=28
uv run aduku get d1
```

Expected: `name` is `"Alice S."` (user beats backfill), `age` is `30` (max rule), `plan` is `"free"` (default).

```bash
uv run aduku append d1 --contributor root --tier admin --set /displayName='"HACKED"'    # exit 1: FIELD_NOT_WRITABLE
uv run aduku entries d1                                                                 # still 4 entries (seq 1 to 4); no seq consumed
uv run aduku explain d1 /age                                                            # max rule, supplied by seq 2 (the 30)
```

Model errors (US7): the customer schema lets `backfill` use the flag and has `range 0..150` on `/age`.

```bash
uv run aduku append d2 --contributor job-1 --tier backfill --set /age=-5               # exit 1: VALIDATION_FAILED
uv run aduku append d2 --contributor job-1 --tier backfill --lenient --set /age=-5     # exit 0: kept; model_errors_added has range
uv run aduku get d2                                                                     # model_errors: range at /age, with the request that introduced it
uv run aduku append d2 --contributor alice --tier user --lenient --set /age=7           # exit 1: LENIENT_NOT_PERMITTED
```

The SPEC 16.2 example itself needs the fixture rule `max_spouses_by_country`, which only the test harness registers; it
runs as `conformance/model-errors-16-2.ion`.

```bash
uv run aduku rebuild d1        # expected: identical (exit 0); `aduku rebuild` with no document checks every document
```

## Success checks mapped to the spec

| Check | Spec |
|---|---|
| Worked example output matches vectors | SC-001, US2 |
| Rejected request leaves 0 entries and changes no document | SC-005, US1 |
| Audit on: one record per rejection; off: none; table dropped: documents unchanged | SC-010, FR-027 |
| Pruning keeps `max_entries`/`max_age` | SC-011 |
| Lenient write keeps the violation as a model error on every read | SC-008, US7 |
| First read after a schema bump reflects new rules; second read does not re-derive | SC-009, US8 |
| 1,000-entry document reads in under 1 second | SC-006 |
