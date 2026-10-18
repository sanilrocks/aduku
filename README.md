# aduku

Specification and conformance for **aduku** (from the Tamil *adukku*, "to put in order"), an implementation of
Document Oriented Design: an append-only ledger of contributions merged into documents by a central, annotated schema.

Language ports are named `aduku-<lang>`: `aduku-py`, `aduku-java`, `aduku-rs`, `aduku-js`.

## Layout

| Path | Purpose |
|---|---|
| `docs/SPEC.md` | The authoritative specification (Draft 0.7) |
| `docs/notes.md` | Decisions with reasons, prior art, pending items |
| `docs/prompts/` | Prompts for the features still to be specified, and the tech-stack input for planning |
| `conformance/` | Language-neutral test vectors every port must pass byte for byte |
| `ports/py/` | `aduku-py`, the Python port (see its README) |
| `specs/001-core-ledger-merge-engine/` | Feature 001: spec, plan, tasks, contracts, quickstart, checklists |
| `.specify/` | [Spec Kit](https://github.com/github/spec-kit) scaffolding; `memory/constitution.md` is the project constitution |
| `.claude/skills/` | Spec Kit skills for Claude Code (`/speckit-*`) |

## Where things stand

Constitution ratified. Feature 001 (core ledger and merge engine) is implemented in `ports/py/`: all tests pass against
the shared vectors. Features 2 to 6 are still to be specified, in the order in `docs/prompts/specify-features.md`.

To try it: `cd ports/py && uv sync && uv run pytest`, then the walkthrough in
`specs/001-core-ledger-merge-engine/quickstart.md`.

## Workflow

Run the commands from this folder, in this order. Command names follow your installed Spec Kit version.

1. Once per project: `/speckit-constitution`.
2. Per feature: `/speckit-specify` (the feature's prompt) → `/speckit-clarify` → `/speckit-plan` (with
   `docs/prompts/plan-input.md`) → `/speckit-checklist` (optional) → `/speckit-tasks` → `/speckit-analyze` (optional) →
   `/speckit-implement` → `/speckit-converge` (repeat until it reports "Converged.").

Specs say what and why, never the tech stack; the stack lives in the plan. `docs/SPEC.md` stays the reference each prompt cites.

## License

MIT, see [LICENSE](LICENSE). Each port carries its own copy (for example `ports/py/LICENSE`).
