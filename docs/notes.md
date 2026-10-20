# aduku working notes

Context that lives outside `SPEC.md`: decisions with their reasons, prior art, ideas not yet in the spec, and what is
pending. Written 2026-10-18 so a new session can pick up without the original conversation.

## 1. What this is

**aduku** (Tamil *adukku*, "to put in order") implements **Document Oriented Design (DOD)**: writers submit
contributions to a typed Ion/JSON document; contributions are kept as an append-only ledger; a central annotated schema
says how fields are validated, merged, defaulted, computed and enriched; the current document is derived from the ledger.
Ports are named `aduku-py`, `aduku-java`, `aduku-rs`, `aduku-js`. `docs/SPEC.md` is authoritative.

## 2. Decision log (why things are the way they are)

| Decision | Reason |
|---|---|
| Order is `(tier, seq)`, never wall-clock time | Late backfills must not override earlier higher-tier values; makes results independent of write races. |
| Every merge rule must be associative | Squash-per-tier-then-merge must equal one flat fold; enables caching and compaction. |
| Rejected requests are **not** ledger entries | Avoids storing data the system refused; the ledger stays a clean basis for replay. |
| Optional separate audit log for rejections, off by default, capped by count and/or age | Audit is sometimes needed, but unbounded storage of rejected spam is not acceptable. Enabling it without a cap is refused. |
| Requests are **always atomic** | Partial acceptance makes audit hard and model-error attribution needs ablation. A request is the unit of acceptance, rejection and attribution. |
| Validation runs once per request on the document that results from all its entries | Entries in one request may depend on each other; intermediate states are not meaningful. |
| Non-rejection ("lenient") flag records rule-breaking data as **model errors** | Backfilled or legacy data can violate rules (for example, two spouses where one is allowed) and must still be kept so users can be nudged to fix it. |
| Only schema-permitted tiers or contributors may use the flag; schema errors are never bypassable | Otherwise anyone could slip invalid data past validation. |
| Unflagged requests must add no new model errors; reducing or leaving them unchanged is fine; flagged requests from permitted tiers are exempt | Errors never grow through ordinary writes, but a document with a legacy problem stays editable. A strict "must reduce errors" reading would block unrelated edits. |
| Schema or rule version change: discard cached view, re-derive from ledger, recompute model errors | Old entries are never retroactively rejected; documents show how they stand against today's rules. |
| Deferred (IO) values never feed validation | Keeps validation pure and replayable. |
| Delete versus default is per-field (`on_delete: absent` default, or `default`) | A delete is an explicit decision and should normally win; some fields want to revert. |
| A retry is a no-op: caller-supplied `request_key` plus identical request hash and identity means the request is not admitted and returns the original result with the latest materialized view; no key means no duplicate detection | A retry must never change outcome or response shape, but a pure payload hash would drop a deliberate repeat (set Alice, other writer sets Bob, set Alice again) (2026-10-18). |
| P5 (deferred) is a stand-alone stage; deferred values never feed computed fields, validators or defaults; deferred fields may depend on plain, computed and deferred fields and run only when their deferred inputs have values | Keeps admission, merge and validation pure and replayable; IO is the only impure part. |
| Rule library and authority as built (feature 004): a child field without an `authority` list inherits the nearest list above it and its own list replaces the parent's; a `NOT_AUTHORIZED` reason names the field and the rule but not the allowed list; `pattern` is a small own linear-time subset (no back-references, look-around, shortcuts or flags); a null counts as no value for rules; `ordered` treats values that cannot be ordered as a violation; a rule that needs a field already reports on an empty document, so a first write is not blocked by it | Keeps the rules portable and bounded in time, and one lookup for authority in admission, `explain` and the schema check. Settled while clarifying and building feature 004 (2026-10-20). |
| Deferred stage as built (feature 003): `once` keeps its value when inputs change (opt-in `on_dependency_change: recompute`); a `per_fetch` value is reused only while its inputs are unchanged and it is younger than `ttl`; a failed attempt is not repeated before its `ttl`, schedule, `invalidate` or changed input; `invalidate_by` is required on every deferred field; the circuit breaker is per source and lives in memory of one service; one fetch deadline covers a whole read; a read never waits for a source's rate limit; a notification only marks values out of date and the next `scheduler_run` recomputes them; a null answer is a failure | Keeps reads fast and bounded, a down source from being hammered, and recovery an explicit, authorized act. Settled while clarifying and planning feature 003 (2026-10-19). |
| Deferred failures never reject a write and are never model errors; per-field `on_failure` (`error`, `serve_stale`, `absent`) reports them as deferred status, cleared by recompute | Model errors and admission must be pure functions of ledger and schema; an outside service outage must not decide what is accepted. Reject mode was considered and dropped for deferred fields (2026-10-18). |
| **Impure compute** is a separate, rare construct inside P4: strongly consistent, retried, outcomes recorded in an effect log; `failure_mode: reject_on_failure` (default) or `model_error_on_failure`, overridable per field or per document type; recovery by `recompute` | Some customer cases need the value before the client can proceed. Recording outcomes keeps derivation and rebuild replayable and keeps IO out of reads of cached state (2026-10-18). |
| Computed and deferred fields cannot be written by any contribution | Derived values must stay consistent with their inputs; the write is rejected with `FIELD_NOT_WRITABLE` and cannot be bypassed by the flag. |

## 3. Prior art (checked 2026-10-18; searches were light, not a patentability opinion)

Individual pieces exist; I found nothing combining all of them under one schema-driven model.

- MDM survivorship rules (field-level source priority, golden record): [skyMDM](https://docs.skypoint.ai/docs/skymdm-merge),
  [HAPI FHIR MDM](https://hapifhir.io/hapi-fhir/apidocs/hapi-fhir-server-mdm/ca/uhn/fhir/mdm/svc/MdmSurvivorshipSvcImpl.html),
  [US 10055545](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/10055545),
  [EP 2608074](https://data.epo.org/gpi/EP2608074A3), [i2 Analyze merge](https://docs.i2group.com/analyze/4.4.4/c_merge.html).
- Schema-driven per-field merge for JSON: [jsonmerge](https://pypi.org/project/jsonmerge/).
- Event store with deterministic merge: [eventcore git-mergeable store](https://git.johnwilger.com/Slipstream/eventcore/pulls/390).
- CRDT JSON documents: [Automerge](https://docs.rs/crate/automerge/0.11.0) (no schema-declared per-field strategies found).
- Not found: computed fields contributions can never override with sandboxed macros; deferred fields with IO hooks and
  refresh policies in the schema; one x-path ledger over Ion documents with squash by tier then across tiers.
- Suggested follow-ups: Google Patents search for "survivorship" with "computed"/"derived field"; Reltio and Informatica
  calculated attributes; academic work on layered or priority CRDTs; collisions for the name "Document Oriented Design".

## 4. Ideas not yet in the spec

1. **Phase pipeline with a behavior registry** as the core extension model (partly in SPEC sections 5.4 and 7).
2. **Dependency graph with incremental recomputation** for derived fields (spreadsheet-style), including external events
   as inputs.
3. **Field authority** as a general model (who may write each field); computed immutability is one case.
4. **Per-field precedence** (admin wins here, most recent wins there, trust score, time decay) while preserving the
   equivalence law.
5. **More operation types** via Ion annotations: `increment::`, `append::`, `reset::` (declared in SPEC 4.3, v1.1).
6. **Bitemporal reads** (valid time versus transaction time) for retroactive corrections.
7. **Explainability API** and schema-version replay (partly in SPEC 12.3 and 14).
8. **Portable schema vocabulary** (JSON Schema vocabulary, decided direction, SPEC 17.7) and sandboxed macro language (CEL or WASM) so
   all ports share one schema and one conformance suite.
9. **Tier presets** for scaffolding (for example `backfill < user < admin`, `import < partner < first-party`, or a
   two-tier `default < override`); tiers are always user-defined (SPEC 4.4), presets are only starting schemas.
10. **Model error granularity**: identity is `(rule, path scope)`, so worsening an error is not detected (SPEC 17.10).

## 5. Where the work stands

- Spec Kit initialized (`specify init --here --integration claude`). Commands are hyphenated: `/speckit-constitution`,
  `/speckit-specify`, `/speckit-clarify`, `/speckit-plan`, `/speckit-checklist`, `/speckit-tasks`, `/speckit-analyze`,
  `/speckit-implement`, `/speckit-converge`.
- Constitution **v1.4.0** (11 principles) ratified 2026-10-18.
- Feature `001-core-ledger-merge-engine`: specified and clarified (FR-001 to FR-033, SC-001 to SC-013), planned, tasked (86 tasks), analyzed and implemented in
  `ports/py/` (T001 to T086 done). Both checklists are ticked. One recorded deviation from Principle X (plan.md, Complexity Tracking).
- Feature `002-computed-fields`: specified, clarified (5 questions), planned, tasked (70 tasks) and implemented in `ports/py/`
  (T001 to T070 done). Decisions with reasons are in its `research.md` (custom macros are sandboxed expressions in a CEL
  subset with deterministic limits; impure hooks run outside the write lock; outcomes go to an append-only effect log).
- `docs/SPEC.md` is Draft 0.9.
- The earlier Manim explainer animation and its voice assets are in `../archive/` and are not needed.

## 6. Pending

- [x] Run `/speckit-plan` for feature 001 (done 2026-10-18; artifacts in `specs/001-core-ledger-merge-engine/`).
- [x] Feature 001 forward-compat: the view cache key gained an effect-log watermark (`effects_seq`) with feature 002.
- [x] `/speckit-tasks` and `/speckit-analyze` done (2026-10-18; third analysis pass had no critical or medium findings).
      Decided before tasks: request keys for retries (R-10), rejection reasons reported in stages (schema errors first),
      metadata-only audit scenario (US1 scenario 7, SC-013).
- [x] Feature 001 checklists reviewed and implemented.
- [ ] Feature 002: run `/speckit-analyze` and `/speckit-converge` on the implementation.
- [x] **Where the first Python port's code lives**: `ports/py/` here until stable, then its own `aduku-py` repo
      (taken as the default in the plan; change freely). Conformance vectors stay in root `conformance/`.
- [x] `docs/prompts/specify-features.md` Features 2, 3 and 4 prompts updated for model errors and the stand-alone
      deferred stage (2026-10-18).
- [ ] Features 3 to 6 still to be specified, in the order in `docs/prompts/specify-features.md`.
- [x] `aduku/` is its own git repository (`git init -b main`, 2026-10-18); nothing is committed yet.
- [x] Open questions in `docs/SPEC.md` section 17: all 12 decided on 2026-10-18 (see the Decision lines there).
- [ ] Check the name "Document Oriented Design" for collisions before using it publicly.
