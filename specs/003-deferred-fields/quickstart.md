# Quickstart: Validate Deferred Computed Fields

A run guide proving the feature end to end. Details: [data-model.md](data-model.md), [contracts/](contracts/). It assumes
Features 001 and 002 are in place (`ports/py/`, `uv sync` done).

## Automated validation

```bash
cd ports/py
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest tests/unit                 # planner, schedule, breaker, loader checks, purity import test
uv run pytest tests/golden               # SPEC 16.1 with creditScore and fxRate; every vector in ../../conformance
uv run pytest tests/property --hypothesis-profile=ci   # core output independent of the store; planner order independence
uv run pytest tests/integration          # reads, ttl, chains, scheduler, notifications, failures, deadline, lock discipline
```

Expected: all green; the 16.1 output matches the vector exactly (SC-010); the core output is identical for an empty,
full and changing store (SC-001); no source call during `rebuild`.

## Manual walkthrough with the CLI

The example schema `examples/deferred.schema.ion` has a `once` field (`creditScore`), a `per_fetch` field (`fxRate`, ttl
5 minutes, `serve_stale`), a chained field (`creditLimit`, from `creditScore`), a `scheduled` field (`riskBand`, batch
source) and a `notification` field (`sanctionsFlag`). `examples/demo_sources.py` registers the sources, which count their calls
and can be made to fail by setting `DEMO_DOWN=1`.

```bash
cd ports/py
rm -f /tmp/aduku-demo3.db*
export ADUKU_DB=/tmp/aduku-demo3.db
export ADUKU_SCHEMA=examples/deferred.schema.ion
export ADUKU_BEHAVIORS=demo_sources PYTHONPATH=examples
uv run aduku schema check examples/deferred.schema.ion        # lists the deferred inventory
uv run aduku append d1 --contributor alice --tier user --set /name='"Alice"' --set /currency='"EUR"' --set /country='"US"'
uv run aduku get d1
```

Expected: the document shows `creditScore:deferred::712`, `fxRate:deferred::0.92` and `creditLimit:deferred::7120`, each
`ok` in the `deferred` section; the write itself made no call.

```bash
uv run aduku get d1                                            # served from the store: the demo prints calls so far
uv run aduku append d1 --contributor alice --tier user --set /name='"Alicia"'
uv run aduku get d1                                            # creditScore unchanged (once keeps it)
uv run aduku get d1 --fresh                                    # fxRate fetched again, ttl ignored
uv run aduku deferred list d1                                  # as_of, attempts, invalidated; never a value
```

Expected: `creditScore` stays 712 with no new call; `--fresh` makes one `fx.rate` call.

```bash
DEMO_DOWN=1 uv run aduku get d1 --fresh --include pending      # fxRate shows deferred::0.92 with status stale
uv run aduku append d1 --contributor alice --tier user --set /name='"Alina"'     # accepted: a source outage never rejects
uv run aduku get d1 --format json | grep -c model_errors                         # no model error was added
uv run aduku invalidate d1 /creditScore --contributor eve --tier user            # exit 1: NOT_AUTHORIZED
uv run aduku invalidate d1 /creditScore --contributor root --tier admin          # marked: 1
uv run aduku get d1                                                              # creditScore fetched again (stderr: demo: fx.score called)
uv run aduku scheduler run risk.band@1 --contributor root --tier admin           # batches; a second run does nothing
uv run aduku scheduler run sanctions.check@1 --contributor root --tier admin     # the first value is the scheduler's
uv run aduku notify sanctions.updated d1                                         # marks only d1 (marked: 1); no call yet
uv run aduku scheduler run sanctions.check@1 --contributor root --tier admin     # recomputes d1
uv run aduku rebuild                                                             # core unchanged, zero source calls
```

Expected: each step matches its comment; `rebuild` reports no differences and the demo's call counter does not move.

## Success criteria map

| Criterion | Where it is shown |
|---|---|
| SC-001, SC-009 | property tests and the loader vectors; `rebuild` in the walkthrough |
| SC-002 | `DEMO_DOWN=1` append is accepted with no model error |
| SC-003 | `fx.slow@1` integration test against a manual clock |
| SC-004, SC-005 | `once` and chain integration tests; the `creditLimit` chain order |
| SC-006, SC-007 | scheduler and notification integration tests, batch counts |
| SC-008 | breaker unit and integration tests with a manual clock |
| SC-010 | golden test of 16.1 |
| SC-011 | single-flight integration test with two threads |
