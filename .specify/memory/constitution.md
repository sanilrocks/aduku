# aduku Constitution

## Core Principles

### I. Ledger Is Truth
Ledger entries MUST be immutable and append-only. Document state MUST always be derived from the ledger
and never edited in place; corrections MUST be new entries. The only operations that MAY change or remove an
existing entry are compaction and purge: each MUST be a recorded, atomic operation; a compaction MUST be verified,
before it commits, to change no document, model error or provenance answer, and MUST keep the replaced originals
in an archive until an operator expires them; a purge MUST leave a placeholder so that sequence numbers stay
continuous, MUST leave no copy of the erased values in any store, and MUST be logged without them. Only accepted contributions become entries: a
rejected request MUST record nothing in the ledger. A request MUST be accepted or rejected as a whole (atomic).
Rejected requests MAY be kept only in a separate, capped, audit-only log, enabled by schema setting, that MUST
NOT be read by any merge, validation or derivation.
Rationale: provenance, replay and reversibility depend on history never being rewritten, and refused data must
never influence a document or accumulate without bound. History cannot grow without bound either, and the law
sometimes requires data to be erased, so those two exceptions exist, and they are held to proof (compaction) and
to an audit trail (purge).

### II. Schema Is the Single Source of Truth
Field semantics (type, merge, default, computed, deferred, validate, authority) MUST be declared in the
versioned schema and bound to registered behaviors. Semantics MUST NOT live only in a client or as
engine special cases.
Rationale: one definition keeps every writer and reader consistent.

### III. Determinism and Purity
Admission checks, squash, merge, tombstone handling, computed fields, validation and projection MUST be
pure functions of (ledger, effect log, schema version). Wall-clock time, randomness and IO MUST NOT affect them
except through recorded impure-compute outcomes.
Rationale: identical inputs must always produce identical documents.

### IV. IO Is Confined
Side effects MUST occur only in (a) declared impure computes in P4, used sparingly where no pure computation is
possible, and (b) the stand-alone deferred stage (P5). Both run with timeouts, bounded retries, scoped credentials and
audit logging, and their results MUST NOT be merged as contributions.
Impure computes are strongly consistent: every outcome (value or exhausted failure) MUST be recorded in the effect log,
derivation and rebuild MUST reuse recorded outcomes and make no call, and exhaustion MUST follow the field's declared
policy (reject the request, or record a model error). Deferred fields MUST NOT be inputs to computed fields,
validators or defaults, MUST NOT cause a rejection or a model error (failures are deferred status), and MAY depend on
plain, computed and other deferred fields once those have values.
Rationale: external calls must not make the core merge, admission or validation unreplayable; the only impurity that
touches a document is explicit, rare and recorded.

### V. Algebraic Laws Are Mandatory (NON-NEGOTIABLE)
Every merge behavior MUST declare associativity, and commutativity or idempotence where claimed, and MUST
be covered by property-based tests. Squashing each tier and then merging the tiers MUST equal the flat
(tier, seq) fold.
Rationale: tier squash, caching and compaction are only correct if these laws hold.

### VI. Derived Fields Are Never Writable
No contribution MAY override a computed or deferred field. Such entries MUST be rejected at admission
with reason `FIELD_NOT_WRITABLE`; the rejection is not a ledger entry and is kept only in the optional rejection
audit log.
Rationale: derived values must stay consistent with their inputs regardless of who writes.

### VII. Explainability
Every value in a document MUST be traceable to entries, a rule and a schema version through `explain`.
Rationale: auditability is a core reason for the architecture.

### VIII. Sandboxed Behaviors
Pure behaviors MUST see only their declared dependencies and MUST have no ambient authority (no network,
filesystem, environment, clock or randomness).
Rationale: sandboxing makes custom behaviors safe to run and test.

### IX. Immutable Schema Versions
Schema versions MUST be immutable. Breaking changes (merge rule, type, tier order, making a field
derived) MUST ship with a dry-run diff and a rebuild.
Rationale: merged results must stay reproducible across schema changes.

### X. Test-First and Conformance (NON-NEGOTIABLE)
Law tests, order-tolerance tests, replay tests and the normative worked example (docs/SPEC.md 16.1) MUST
pass before any change merges to main. Tests MUST be written and observed failing before implementation.
Rationale: correctness here is mathematical and cannot be verified by inspection alone.

### XI. Simplicity
The v1 non-goals in docs/SPEC.md section 1.2 MUST stay out of scope until explicitly added by
amendment. New behavior MUST justify its complexity against the principles above.
Rationale: scope discipline protects the guarantees the design depends on.

## Scope and Technology Constraints

- docs/SPEC.md is the authoritative reference for semantics; plans and tasks MUST NOT contradict it
  without amending the spec.
- Output MUST be deterministic, including stable key ordering in serialized documents.
- The engine's phases (P0–P4, P6, P7) MUST NOT read wall-clock time; time and external state enter only through
  recorded impure-compute outcomes. P5 (deferred) is outside this chain; P7 only overlays stored deferred values for display.
- aduku is specified language-neutrally. Every port (`aduku-py`, `aduku-java`, `aduku-rs`, `aduku-js`, ...) MUST pass
  the shared conformance vectors and produce byte-identical results for identical ledgers and schema versions.

## Development Workflow and Quality Gates

- Each feature follows specify, clarify, plan, tasks, implement and converge, and is reviewed against this
  constitution.
- Open questions in docs/SPEC.md section 17 MUST be resolved or explicitly deferred during clarify before
  planning a feature that depends on them.
- A change that alters merge results MUST update conformance vectors in the same change.
- Structured logs MUST record admission decisions, hook runs and rebuilds for audit.

## Governance

This constitution supersedes plans, tasks and other development practices. Amendments require a written
rationale, an updated version number and updated conformance tests. Versioning follows semantic rules:
MAJOR for removing or redefining a principle in a backward-incompatible way, MINOR for adding a
principle or materially expanding guidance, PATCH for clarifications. Every plan and review MUST verify
compliance, and complexity MUST be justified. Runtime guidance lives in docs/SPEC.md.

**Version**: 1.5.0 | **Ratified**: 2026-10-18 | **Last Amended**: 2026-10-20
