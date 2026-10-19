# aduku

Specification and conformance for **aduku**, an implementation of Document Oriented Design: an append-only ledger of
contributions merged into documents by a central annotated schema. Language ports are `aduku-py`, `aduku-java`,
`aduku-rs`, `aduku-js`.

## Read first

1. `docs/notes.md`: decisions with reasons, prior art, pending items. Start here for context.
2. `docs/SPEC.md`: the authoritative specification. Do not contradict it without updating it.
3. `.specify/memory/constitution.md`: governing principles; plans and tasks must comply.
4. `specs/002-computed-fields/spec.md`: the current feature (feature 001, `specs/001-core-ledger-merge-engine/`, is implemented).

## Workflow

Spec Kit (hyphenated commands: `/speckit-constitution`, `-specify`, `-clarify`, `-plan`, `-tasks`, `-analyze`,
`-implement`, `-converge`). Run Claude Code from this folder. Prompts for the features still to be specified are in `docs/prompts/specify-features.md`.
Current step: feature 002 (computed fields) is implemented, analyzed and converged (tasks T001 to T078 done, including the code-review fixes of Phase 13); next, commit it. Feature 001 is implemented. Features 3 to 6 are still to be specified (`docs/prompts/specify-features.md`).

## Rules

- Keep `docs/SPEC.md`, the feature specs and the constitution consistent; when one changes, check the others.
- Specs describe what and why; the tech stack belongs in plans (`docs/prompts/plan-input.md`).
- Rejected requests are not ledger entries; requests are atomic (see SPEC 4.2, 4.5, 10).
- `../archive/` holds an unrelated explainer animation; ignore it.
- Do not commit or create branches unless asked.
