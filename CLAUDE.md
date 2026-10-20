# aduku

Specification and conformance for **aduku**, an implementation of Document Oriented Design: an append-only ledger of
contributions merged into documents by a central annotated schema. Language ports are `aduku-py`, `aduku-java`,
`aduku-rs`, `aduku-js`.

## Read first

1. `docs/notes.md`: decisions with reasons, prior art, pending items. Start here for context.
2. `docs/SPEC.md`: the authoritative specification. Do not contradict it without updating it.
3. `.specify/memory/constitution.md`: governing principles; plans and tasks must comply.
4. `specs/005-snapshots-compaction-time-travel/spec.md`: the current feature (features 001 to 004, in `specs/001-core-ledger-merge-engine/` to `specs/004-validation-authority/`, are implemented).

## Workflow

Spec Kit (hyphenated commands: `/speckit-constitution`, `-specify`, `-clarify`, `-plan`, `-tasks`, `-analyze`,
`-implement`, `-converge`). Run Claude Code from this folder. Prompts for the features still to be specified are in `docs/prompts/specify-features.md`.
Current step: feature 005 (snapshots, time travel, compaction and purge) is implemented (tasks T001 to T070 done); next `/speckit-analyze` and `/speckit-converge`. Features 001 to 004 are implemented and committed. Feature 6 is still to be specified (`docs/prompts/specify-features.md`).

## Rules

- Keep `docs/SPEC.md`, the feature specs and the constitution consistent; when one changes, check the others.
- Specs describe what and why; the tech stack belongs in plans (`docs/prompts/plan-input.md`).
- Rejected requests are not ledger entries; requests are atomic (see SPEC 4.2, 4.5, 10).
- `../archive/` holds an unrelated explainer animation; ignore it.
- Do not commit or create branches unless asked.
