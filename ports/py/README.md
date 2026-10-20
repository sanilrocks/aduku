# aduku-py

The Python port of **aduku**: an append-only ledger of contributions merged into documents by a central annotated schema
(Document Oriented Design): the core ledger and merge engine (feature 001) computed fields (feature 002), deferred fields (feature 003), the validation rule library with per-field authority (feature 004) and saved layers, time travel, compaction and purge (feature 005). The specification is
[`docs/SPEC.md`](../../docs/SPEC.md); the shared test vectors are in [`conformance/`](../../conformance).

## Install

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
cd ports/py
uv sync          # amazon.ion, python-ulid, and the dev tools (pytest, hypothesis, ruff, mypy)
```

## Use

```python
from aduku import Aduku, EntryInput, Identity, SchemaRegistry, SqliteStorage

registry = SchemaRegistry()
registry.put(registry.load("../../conformance/customer.schema.ion"))
registry.activate(7)
db = Aduku(SqliteStorage("demo.db"), registry)

db.append("d1", [EntryInput("user", "/name", "set", "Alice")], contributor=Identity("alice", ("user",)))
print(db.get("d1").document)
print(db.explain("d1", "/name").winner)
```

### Computed fields

A field can be computed from others (`computed: {macro: upper, args: ["/name"]}`); it appears in the document as
`computed::"ALICE"` and nobody can write it. Custom macros are small expressions registered by name; impure computes call a
registered hook, record every outcome in an effect log and never call it again for the same inputs:

```python
behaviors = BehaviorRegistry.with_builtins()
behaviors.register_macro("acme.label", 1, ["name", "city"], 'name + " (" + city + ")"')
behaviors.register_hook("tax.lookup", 1, lambda inputs, timeout: "tax-" + inputs["/country"])
db = Aduku(SqliteStorage("demo.db"), registry, behaviors=behaviors)  # a schema may name both
db.recompute("d1", "/taxId", caller=Identity("root", ("admin",)))  # only who the schema's recompute_by lists
```

The command line takes the same registrations from a module: `aduku --behaviors demo_hooks ...` (see
[`examples/demo_hooks.py`](examples/demo_hooks.py)). The macro language is in
[`contracts/expression-language.md`](../../specs/002-computed-fields/contracts/expression-language.md).

### Deferred fields

A field can be filled by a source that does IO (a score, a rate). It never touches the merge: the value lives in a separate
store and is shown as `deferred::712` with a status (`ok`, `stale`, `pending`, `error`). Each field says when it is
fetched: `once` (first read, then kept), `per_fetch` (on read, reused within a `ttl`), `scheduled` (by `scheduler_run`, in
batches) or `notification` (marked by `notify`, recomputed by the next run). A failing source only changes a status.

```python
behaviors.register_hook("fx.rate", 1, lambda inputs, timeout: Decimal("0.92"))
behaviors.register_hook(
    "risk.band", 1, band_for_many, batch_max=50, rate_limit=2.0
)  # one call carries up to 50 documents
db = Aduku(SqliteStorage("demo.db"), registry, behaviors=behaviors, fetch_deadline=5.0)
db.get("d1").deferred  # the status of every deferred field
db.get("d1", consistency="fresh")  # ignore the ttl of per_fetch fields
db.scheduler_run("risk.band@1", caller=Identity("root", ("admin",)))  # only who the field's invalidate_by lists
db.notify("sanctions.updated", ["d1"])  # marks values out of date; the next run recomputes them
```

The command line has the same operations (`aduku get --fresh --include pending`, `invalidate`, `scheduler run`, `notify`,
`deferred list`); try them with [`examples/demo_sources.py`](examples/demo_sources.py) and
[`examples/deferred.schema.ion`](examples/deferred.schema.ion), as in
[`quickstart.md`](../../specs/003-deferred-fields/quickstart.md).

### Validation rules and authority

Rules check values and the whole document; authority says who may write a field. A schema uses them like this:

```
"/country": { type: string, merge: lww, validate: [pattern::{regex: "[A-Z]{2}"}] },
"/plan":    { type: string, merge: lww, authority: [admin], validate: [one_of::{values: ["free", "pro"]}] },
"/billing": { type: struct, merge: struct, authority: [finance] },        // its children inherit [finance]
document_validate: [at_most_one::{paths: ["/personalName", "/companyName"]}]
```

Field rules: `non_empty`, `range`, `max_len`, `min_len`, `pattern` (a small linear-time subset), `one_of`, `unique_items`. Document
rules: `all_or_none`, `at_least_one`, `at_most_one`, `ordered`. A rejected request lists every reason without repeating a value;
a permitted lenient write records model errors instead; authority is never bypassed by the flag. A deployment adds its own
rule with typed arguments, which the loader checks on every use:

```python
behaviors.register_validate(
    "acme.tax_id",
    1,
    check,
    args=[ArgSpec("country", "text"), ArgSpec("strict", "bool", required=False)],
    applies="field",
)
```

`aduku schema check` lists every field's effective authority and rules; `explain` says who may write a path and where that was
declared. Try it with [`examples/rules.schema.ion`](examples/rules.schema.ion) and [`examples/demo_rules.py`](examples/demo_rules.py), as in
[`quickstart.md`](../../specs/004-validation-authority/quickstart.md); the rules are in
[`contracts/rules.md`](../../specs/004-validation-authority/contracts/rules.md).

## Fast reads, time travel, compaction and purge (feature 005)

```python
from aduku import AsOf   # get(doc, as_of=AsOf(seq=7)); also AsOf(tx_time=...) and AsOf(valid_time=...)
db.get("acme", as_of=AsOf(seq=7))                      # the document right after entry 7, with its model errors
db.get("acme", as_of=AsOf(valid_time=march_first))     # only what was true then (entries with no valid_time always take part)
db.compact("acme", through_seq=10_000)                 # one `layer` entry per tier; the originals go to the archive
db.verify_archive("acme"); db.expire_archive("acme", through_seq=10_000)
db.purge("acme", PurgeScope(path="/email"), reason="ticket 4711", caller=dpo)   # the schema's `purge_by` says who may
```

Saved per-tier layers make a cold start and an append fold only what is new; they are a cache, so losing them changes
no result (`Aduku(..., layers=False)` runs without them), and `rebuild` compares them with the ledger byte for byte. A
compaction derives the document, its model errors, its skipped paths and the `explain` answer of every touched path from the
compacted ledger and refuses if anything differs; a purge leaves a placeholder per entry, cleans every store that holds the
value and logs the act without it. The command line has `get --as-of`, `explain --as-of`, `append --valid-time`, `compact`,
`archive verify|expire`, `purge` and `purge-log`. Try [`examples/demo_history.py`](examples/demo_history.py), as in
[`quickstart.md`](../../specs/005-snapshots-compaction-time-travel/quickstart.md).

The full API is in
[`contracts/library-api.md`](../../specs/001-core-ledger-merge-engine/contracts/library-api.md) and, for computed fields and
deferred fields, the `contracts/library-api.md` of
[feature 002](../../specs/002-computed-fields/contracts/library-api.md) and
[feature 003](../../specs/003-deferred-fields/contracts/library-api.md). `MemoryStorage` and
`SqliteStorage` implement the same storage contract; there is also a command line, `uv run aduku --help`
([`contracts/cli.md`](../../specs/001-core-ledger-merge-engine/contracts/cli.md)).

## Check

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest                                          # unit, integration, golden and property tests
uv run pytest tests/property --hypothesis-profile=ci   # 1,000 examples per property (SC-002, SC-003)
```

The golden tests run every vector in `conformance/` against both storage backends. A step-by-step walkthrough is in
[`quickstart.md`](../../specs/001-core-ledger-merge-engine/quickstart.md).

## Layout

| Path | What |
|---|---|
| `src/aduku/ion_codec.py` | Ion parsing and the canonical text every port must produce byte for byte |
| `src/aduku/schema/` | schema loader, immutable versions, the registry |
| `src/aduku/behaviors/` | merge rules, validators, the preset macros |
| `src/aduku/expr/` | the sandboxed expression language of custom macros (pure) |
| `src/aduku/engine/` | admission, squash, overlay, tombstones, computed fields, effects, validation, model errors, explain (pure; no clock, no IO) |
| `src/aduku/hooks.py` | the one module that calls out: runs impure hooks with timeout, retry and deadline |
| `src/aduku/storage/` | the storage contract, in-memory and SQLite |
| `src/aduku/service.py` | `Aduku`: append, get, explain, rebuild, recompute, the rejection audit, the effect log |
| `examples/` | behaviors for trying computed fields from the command line |
| `tests/` | `unit/`, `integration/`, `property/`, `golden/` |

## Where this code lives

This code sits in `ports/py/` of the aduku repository while the specification and the vectors are still changing. The
assumption (recorded in the plan, and open to change) is that once they are stable it moves to its own `aduku-py`
repository, with `conformance/` shared with the other ports (`aduku-java`, `aduku-rs`, `aduku-js`) as a Git submodule or
a pinned release.

## License

MIT, see [LICENSE](LICENSE).
