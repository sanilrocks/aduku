# /speckit.plan input (proposed defaults; change freely)

Applies to the feature currently being planned. The repo already uses Python 3.12 with uv.

- **Language/tooling:** Python 3.12, `uv`, `pytest`, `hypothesis` (property-based law tests), `ruff`, `mypy`.
- **Data model:** Amazon Ion via `amazon.ion` for documents and annotations; a JSON profile per SPEC 3.1.
- **Schema:** Ion schema file per SPEC 5.2, validated at load; parse into immutable dataclasses.
- **Architecture:** library first (`aduku` package) with a thin CLI; engine phases P0-P7 as separate pure modules;
  behavior registry with entry-point style registration; no framework dependency in core.
- **Reference storage:** SQLite (WAL) for ledger, snapshots and derived store behind a storage interface, so other
  backends can be added.
- **Sandbox (Feature 2):** evaluate CEL (`cel-python`) or WASM for custom macros; decide after a spike. Presets are
  plain Python functions with declared purity.
- **Deferred scheduler (Feature 3):** in-process scheduler with a pluggable executor; hooks are async callables.
- **Testing:** unit tests per behavior; hypothesis tests for associativity, commutativity, idempotence, squash
  equivalence and order tolerance; golden tests from SPEC 16.1; replay test from ledger.
- **Non-functional:** deterministic output (stable key ordering), no wall-clock in P0-P4/P6/P7, structured logs for
  audit.

**Ports:** this repo holds the language-neutral spec and conformance vectors. The first implementation is `aduku-py`
(Python, as above). Other ports (`aduku-java`, `aduku-rs`, `aduku-js`) will reuse the shared conformance vectors.
[DECISION NEEDED] Where the Python code lives: proposed default is `ports/py/` inside this repo until stable, then
split into its own `aduku-py` repository.
