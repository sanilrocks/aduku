# aduku-py

The Python port of **aduku**: an append-only ledger of contributions merged into documents by a central annotated schema
(Document Oriented Design). This is feature 001, the core ledger and merge engine. The specification is
[`docs/SPEC.md`](../../docs/SPEC.md); the shared test vectors are in [`conformance/`](../../conformance).

## Install

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
cd ports/py
uv sync          # amazon.ion, python-ulid, and the dev tools (pytest, hypothesis, ruff, mypy)
```

## Use

```python
from aduku.behaviors.registry import BehaviorRegistry
from aduku.model import EntryInput, Identity
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.sqlite import SqliteStorage

registry = SchemaRegistry()
registry.put(load_schema(open("../../conformance/customer.schema.ion").read()))
registry.activate(7)
db = Aduku(SqliteStorage("demo.db"), registry)

db.append("d1", [EntryInput("user", "/name", "set", "Alice")], contributor=Identity("alice", ("user",)))
print(db.get("d1").document)
print(db.explain("d1", "/name").winner)
```

The full API is in
[`contracts/library-api.md`](../../specs/001-core-ledger-merge-engine/contracts/library-api.md). `MemoryStorage` and
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
| `src/aduku/behaviors/` | merge rules and validators |
| `src/aduku/engine/` | admission, squash, overlay, tombstones, validation, model errors, explain (pure; no clock, no IO) |
| `src/aduku/storage/` | the storage contract, in-memory and SQLite |
| `src/aduku/service.py` | `Aduku`: append, get, explain, rebuild, the rejection audit |
| `tests/` | `unit/`, `integration/`, `property/`, `golden/` |

## Where this code lives

This code sits in `ports/py/` of the aduku repository while the specification and the vectors are still changing. The
assumption (recorded in the plan, and open to change) is that once they are stable it moves to its own `aduku-py`
repository, with `conformance/` shared with the other ports (`aduku-java`, `aduku-rs`, `aduku-js`) as a Git submodule or
a pinned release.

## License

MIT, see [LICENSE](LICENSE).
