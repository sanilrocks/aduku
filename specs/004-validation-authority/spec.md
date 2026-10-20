# Feature Specification: Validation Rule Library and Per-Field Authority

**Feature Branch**: `004-validation-authority`

**Created**: 2026-10-19

**Status**: Draft

**Depends on**: Features 001 (core ledger and merge engine), 002 (computed fields) and 003 (deferred fields), all implemented;
docs/SPEC.md sections 10 (10.1 to 10.5) and 11 (authority), with 5.3 (annotation vocabulary) and 12.3 (explain).

**Input**: User description: "Validation rule library and per-field authority. Add the rest of the validation rule types and finish per-field authority. Why: Reject bad or unauthorized contributions early, and detect when valid contributions yield an invalid document, without silently fixing it. Stories: type and constraint validators run at admission (a library of rule types beyond the three that exist: non_empty, range, max_len); per-field authority limits who may write (tiers or roles; the most specific declaration wins for a path and its descendants; a delete needs the authority of every declared field beneath the path); cross-field document validators run after merge; violations become rejections or model errors as defined in Feature 1 (non-rejection flag, no-new-errors rule); validators on computed values (never deferred values). Features 1 to 3 already have the validator levels, the non-rejection flag, the no-new-errors rule, model errors with attribution, re-evaluation on a schema change, authority on fields and the three rule types, so this feature adds the further rule types, completes authority (roles, inheritance, delete rule, explain and error reporting) and the author-facing way to register custom validators with declared arguments."

## Clarifications

### Session 2026-10-19

- Q: Does a child field without its own authority list inherit its parent's? → A: Yes. A child without a list uses the nearest list above it; a child with its own list uses only that list, which replaces the parent's (the most specific declaration wins).
- Q: What does `ordered` do with two values that cannot be ordered against each other? → A: It is a violation of the rule (the values are not "in order"); the rule reports nothing only when either path has no value.
- Q: Does a null count as "has a value" for the document rules? → A: No. Null counts as no value, the same as an absent or deleted path (as for a missing input in Feature 2).
- Q: Does a rejection for lack of authority name the tiers and roles that may write the field? → A: No. The reason names the field and the rule only ("not authorized to write /plan"); the allowed list is shown by `explain` and the schema check.
- Q: Which pattern language does `pattern` use? → A: A small documented subset: literals, `.`, character classes and ranges, groups, alternation and the repeaters `*`, `+`, `?` and `{m,n}`; no back-references or look-around; it matches the whole value. Anything beyond it is a custom rule.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Reject bad values at the door with a library of rule types (Priority: P1)

A schema author attaches rules to a field, for example "a country code is exactly two capital letters", "a plan is one of
free, pro or team", "a name has at least one character", "the tags of a document are all different". Today the library has
three rules (`non_empty`, `range`, `max_len`). This feature adds four more field rules: `min_len`, `pattern`, `one_of` and
`unique_items`. A contribution whose value breaks a rule is rejected before it is recorded, with a reason that names the
field and the rule but never repeats the value. A contribution flagged as lenient by a permitted tier is recorded instead,
and the broken rule becomes a model error shown with the document.

**Why this priority**: Rejecting bad data at admission is the first line of defence and the reason the rule library exists.

**Independent Test**: Declare one field per rule type. Submit a good and a bad value for each: good values are accepted, bad
values are rejected with a reason that names the field and the rule and does not contain the value. Repeat the bad values
as a lenient write by a permitted tier: they are recorded and each shows as a model error.

**Acceptance Scenarios**:

1. **Given** a field with `min_len`, **When** a value shorter than the minimum arrives, **Then** the request is rejected with
   a reason naming the field and `min_len`; a value of exactly the minimum length is accepted.
2. **Given** a field with `pattern`, **When** a value arrives that does not match, **Then** the request is rejected; a
   matching value is accepted; a value that is not text is left to the type check.
3. **Given** a field with `one_of`, **When** a value outside the list arrives, **Then** the request is rejected; each listed
   value is accepted.
4. **Given** a list field with `unique_items`, **When** a list with a repeated item arrives, **Then** the request is
   rejected; a list of different items (and an empty list) is accepted.
5. **Given** a request with two bad values on two fields, **When** it is submitted, **Then** it is rejected and the answer
   lists both reasons, and nothing is recorded.
6. **Given** a lenient request from a permitted tier with a bad value, **When** it is submitted, **Then** it is accepted, the
   entry is marked lenient, and the document carries one model error naming the field and the rule.
7. **Given** a rule declared with an argument the rule does not take, a missing required argument or an argument of the wrong
   kind, **When** the schema is loaded, **Then** it is refused with a stable code naming the field and the rule.
8. **Given** a rule applied to a value of a kind it does not apply to (for example `min_len` on a number), **When** a value
   arrives, **Then** the rule reports no violation and the type check is what decides.

---

### User Story 2 - Only the right people may write a field (Priority: P1)

A schema author lists, per field, which tiers or roles may write it, for example only `admin` may write `/plan`. A
contribution from anyone else is rejected before it is recorded. A field with no list can be written by any permitted tier.
A field without its own list takes the list of the nearest field above it that has one; a field with its own list uses only
that. Deleting a path needs the authority of every declared field beneath it, so a delete cannot be used to wipe a field its
author may not touch. Authority decides who may write; tier decides who wins. Rejections say which field and which rule was
not satisfied, and `explain` and the schema check show the authority that applies to a path and where it was declared.

**Why this priority**: Authority is the second line of defence, and it is incomplete today: roles work, but a child field
does not take its parent's list and the reporting is thin.

**Independent Test**: Declare `/plan` writable by `admin` only and a parent struct `/billing` writable by `finance`, with
children that declare nothing and one child that declares its own list. Submit writes and deletes from contributors with
various tiers and roles, and compare each outcome with the stated rule. Ask `explain` and the schema check which authority
applies.

**Acceptance Scenarios**:

1. **Given** a field whose authority lists `admin`, **When** a contributor with the `admin` tier writes it, **Then** the
   request is accepted; **When** a contributor with only the `user` tier writes it, **Then** it is rejected with
   `NOT_AUTHORIZED`.
2. **Given** an authority entry that names a role, **When** a contributor who holds that role (in any tier) writes the field,
   **Then** the request is accepted.
3. **Given** a parent field with an authority list and a child that declares none, **When** a contributor not on the
   parent's list writes the child, **Then** it is rejected; **When** one on the list writes it, **Then** it is accepted.
4. **Given** a child that declares its own authority list, **When** a contributor on the parent's list but not on the child's
   writes the child, **Then** it is rejected (the most specific declaration wins), and a contributor on the child's list but
   not on the parent's is accepted for the child.
5. **Given** a delete of a path with declared fields beneath it, **When** the contributor lacks the authority of any one of
   them, **Then** the request is rejected and the reason names that field.
6. **Given** a request with several entries, **When** one lacks authority, **Then** the whole request is rejected, every
   unauthorized entry is listed, and nothing is recorded.
7. **Given** the non-rejection flag, **When** a flagged request lacks authority, **Then** it is still rejected (authority is
   never bypassed by the flag), and a flagged request from a tier that may not use the flag is rejected with
   `LENIENT_NOT_PERMITTED`.
8. **Given** a path, **When** `explain` is asked about it, **Then** the answer says which tiers and roles may write it and the
   field that declared that list (or that anyone permitted may).
9. **Given** a schema, **When** the schema check runs, **Then** it lists every field with its effective authority and where
   it comes from.
10. **Given** an authority entry that names something that is neither a tier nor a declared role, **When** the schema is
    loaded, **Then** it is refused (as in Feature 1).

---

### User Story 3 - Check rules that span several fields (Priority: P2)

A schema author states rules over more than one field, for example "if one of the address fields is given, all must be",
"at least one contact (phone or email) is required", "not both a personal and a company name", "the start date is not after
the end date". This feature adds four document rules for the common cases: `all_or_none`, `at_least_one`, `at_most_one` and
`ordered`. They are checked on the merged document, after all the entries of a request have been applied, so the entries of
one request may depend on each other. A violation rejects the request; from a permitted lenient write it is a model error;
and a document that already has model errors only accepts unflagged requests that add no new one.

**Why this priority**: Cross-field rules catch what no single value can show. They build on the machinery Feature 1 already
has, so they are less urgent than the field rules and authority.

**Independent Test**: Declare the four document rules over a small set of fields. Submit requests that satisfy and break each,
including a request whose entries only satisfy the rule together. Confirm rejection, the lenient model error, the
no-new-errors rule on a document that already has an error, and the effect of activating a schema version that adds a rule.

**Acceptance Scenarios**:

1. **Given** `all_or_none` over three paths, **When** a request leaves one or two of them with a value, **Then** it is rejected;
   when it leaves none or all three, it is accepted.
2. **Given** `at_least_one` over two paths, **When** a request leaves neither with a value, **Then** it is rejected.
3. **Given** `at_most_one` over two paths, **When** a request leaves both with a value, **Then** it is rejected.
4. **Given** `ordered` over two paths, **When** a request leaves the first greater than the second, **Then** it is rejected; when
   either has no value, the rule has nothing to compare and reports no violation.
5. **Given** a request with two entries that together satisfy a rule but not one by one, **When** it is submitted, **Then** it is
   accepted (rules see the document that results from the whole request).
6. **Given** a document that already has a model error from one rule, **When** an unflagged request is submitted that does not
   add a new error, **Then** it is accepted; **When** it adds a different error, **Then** it is rejected with
   `MODEL_ERROR_INTRODUCED`; **When** a permitted lenient request adds one, **Then** it is recorded.
7. **Given** a schema version that adds a document rule, **When** a document written under the earlier version is read under the
   new one, **Then** the violation appears as a model error attributed to the schema version, and the ledger is unchanged.
8. **Given** a document rule over a path that does not exist in the schema, or over a deferred field, **When** the schema is
   loaded, **Then** it is refused.

---

### User Story 4 - Register a custom rule with declared arguments (Priority: P2)

A deployment may need a rule the library does not have, for example "a tax id matches the checksum of its country". The
author registers a rule under a name and version, says which arguments it takes and what kind they are, and uses it in a
schema like a library rule. The schema is refused at load if the rule is not registered, is given an argument it does not
declare or a value of the wrong kind, or leaves out a required argument. A rule that crashes (rather than reporting a
violation) is an engine fault: it is reported to the caller and is never treated as "no violations".

**Why this priority**: The library will never cover everything; this is the escape hatch, and it must be as safe as the
built-in rules.

**Independent Test**: Register a custom field rule and a custom document rule with declared arguments. Use them in a schema,
exercise accept, reject and lenient paths, then break the declaration in each way and check the load codes. Register a rule
that raises and confirm the request fails with a rule fault instead of being accepted.

**Acceptance Scenarios**:

1. **Given** a registered custom rule with a declared argument list, **When** a schema uses it correctly, **Then** it loads and
   the rule runs at the same stages as a library rule.
2. **Given** a schema that uses an unregistered rule or version, an undeclared argument, a missing required argument or an
   argument of the wrong kind, **When** it is loaded, **Then** it is refused with a stable code naming the field and the rule.
3. **Given** a custom rule that raises an error, **When** a request would run it, **Then** the request is rejected with a rule
   fault and nothing is recorded; **When** a read would run it, **Then** the read fails with the fault and no document is
   returned as if it were valid.
4. **Given** a custom rule, **When** it receives the document, **Then** it sees only the merged and computed document (never a
   deferred value) and, for a field rule, the value and the rule's arguments.
5. **Given** a rule version in the schema, **When** the registered rule is later replaced by another version, **Then** documents
   are re-checked under the version their schema names, not the newest registered.

---

### User Story 5 - Rules on computed values, never on deferred ones (Priority: P3)

A schema author may state a rule about a computed field, for example "the display name is at most 80 characters" or "the total
is not negative". It runs on the computed value in the same checks as other rules; a violation caused by a contribution
rejects it, as for any other rule. A rule may never read a deferred value: a schema that tries is refused. A deferred field is
itself never checked.

**Why this priority**: The machinery exists from Features 2 and 3; this story makes sure the new rule types honor it.

**Independent Test**: Declare each new rule type on a computed field and a document rule over computed fields; submit
contributions that make the computed value violate and satisfy them. Declare a rule over a deferred field of each kind and
check that the schema is refused.

**Acceptance Scenarios**:

1. **Given** a rule on a computed field, **When** a contribution makes the computed value violate it, **Then** the request is
   rejected (or recorded as a model error if lenient), naming the computed field.
2. **Given** a document rule over a computed field and a plain field, **When** a contribution changes either, **Then** the rule is
   checked on the new document.
3. **Given** a rule on, or over, a deferred field, **When** the schema is loaded, **Then** it is refused (`DEPENDS_ON_DEFERRED`).
4. **Given** a computed field that is missing because its inputs are missing, **When** a rule on it is checked, **Then** the rule
   has no value to check and reports no violation, adding no further error.

---

### Edge Cases

- A rule on a field with no value reports nothing; "required" is a separate rule (`non_empty` or `at_least_one`), and a field
  that is absent is not an empty value.
- A rule meets a value of a kind it does not apply to: it reports nothing, and the type check decides.
- Violations never repeat the value in the reason or the model error message (values can be sensitive).
- A null value is no value for a field rule too: field rules are not run for it (a typed field cannot hold null; this concerns `any`-typed fields and computed fields whose inputs are missing).
- A `paths` list that names a path twice is refused when the schema loads.
- A document with no entries is checked like any other: a rule that needs a field already reports on it, so that error is not new when the first request arrives; the request is rejected only if it adds a different error.
- Two violations of the same rule at the same path are one model error; a document rule over several paths is one error per
  rule and set of paths.
- `unique_items` compares items by their canonical form, so `1` and `1.0` are different and two equal structs are the same.
- A `pattern` that is not valid, or that uses a construct the supported subset does not have, is refused when the schema loads.
- A `pattern` is matched against the whole value; a pattern that could take unbounded time cannot be written.
- `one_of` with an empty list, `min_len` with a negative number, `ordered` with one path, and `all_or_none` with fewer than two
  paths are refused when the schema loads.
- `ordered` compares numbers with numbers, text with text, and timestamps with timestamps; two values of different kinds are a
  violation of the rule (they cannot be ordered).
- A delete of a path with no declared field beneath it needs only the authority of the path itself.
- An authority list on a wildcard field applies to every path it matches, and an exact declaration is more specific than a
  wildcard one.
- A field's own empty authority list is refused (use no list for "anyone permitted").
- An authority change in a new schema version applies to entries admitted from then on; entries already recorded stay.
- A request that mixes an unauthorized entry and a rule violation is rejected for the authority first (a schema-level error
  reported before rule checks, as in Feature 1).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST provide the field rules `min_len`, `pattern`, `one_of` and `unique_items` in addition to
  `non_empty`, `range` and `max_len`, each declared in the schema with named arguments and each checked at the stages the
  existing rules are (on the incoming value at admission, on the merged and computed value in every derivation).
- **FR-002**: System MUST provide the document rules `all_or_none`, `at_least_one`, `at_most_one` and `ordered`, declared in
  the schema over a list of paths, and checked on the merged and computed document after all the entries of a request have
  been applied.
- **FR-003**: `min_len` MUST accept a text or list whose length is at least the stated minimum, and report a violation for a
  shorter one.
- **FR-004**: `pattern` MUST accept a text that matches the whole of a stated pattern and report a violation otherwise; the
  pattern language is a documented subset with the same meaning in every port, and matching MUST NOT be able to take
  unbounded time.
- **FR-005**: `one_of` MUST accept a value equal to one of a stated list of values and report a violation for any other.
- **FR-006**: `unique_items` MUST accept a list whose items are all different (by canonical form) and report a violation for a
  list with a repeated item.
- **FR-007**: `all_or_none` MUST report a violation when some but not all of its paths have a value; `at_least_one` when none
  has; `at_most_one` when more than one has; `ordered` when the first value is greater than the second, or the two cannot be
  ordered. A path counts as having a value only if it is present and not null; a rule over paths with no value reports what is stated above and nothing else.
- **FR-008**: Every rule MUST report nothing for a value of a kind it does not apply to, and MUST name the field and the rule in
  a violation without repeating any value.
- **FR-009**: System MUST refuse at schema load, with a stable code naming the field and the rule: an unregistered rule or
  version, an undeclared argument, a missing required argument, an argument of the wrong kind or out of range, an invalid or
  unsupported pattern, and a document rule over fewer paths than it needs or over a path the schema does not declare.
- **FR-010**: System MUST reject a request at admission when any entry's value breaks a field rule or the resulting document
  breaks a rule, answer with every reason of the first failing stage, and record nothing; a request that is accepted records
  all of its entries. (Feature 1 stage order and atomicity are unchanged.)
- **FR-011**: System MUST apply the non-rejection flag and the no-new-errors rule to the new rules exactly as to the existing
  ones: a permitted lenient request records its violations as model errors, and an unflagged request on a document with model
  errors is accepted only if it adds none.
- **FR-012**: System MUST re-evaluate every rule, new and old, when a document is read under a schema version different from the
  one its view was derived under, attributing new violations to the schema version and leaving the ledger unchanged.
- **FR-013**: System MUST let a deployment register custom field rules and custom document rules by name and version, with a
  declared list of arguments (name, kind, required or optional, and allowed range where it applies), and MUST check a schema's
  use of them against that declaration when it loads.
- **FR-014**: System MUST treat a rule that raises an error as a rule fault: the request is rejected with a fault, a read
  fails with the fault, and the result is never treated as "no violations".
- **FR-015**: A rule MUST see only the merged and computed document (and, for a field rule, the value and its own arguments);
  it MUST NOT see a deferred value, and a schema in which a rule is declared on or over a deferred field MUST be refused
  (`DEPENDS_ON_DEFERRED`).
- **FR-016**: System MUST run rules on computed values (including recorded impure values) like any other value, and a rule on a
  computed field whose inputs are missing MUST report nothing.
- **FR-017**: A field MUST take the authority list of the nearest declaration above it (the path itself, then its parent
  patterns) that has one; a field that declares its own list uses only that list; a field with no list above it may be
  written by any permitted tier (SPEC 11, most specific declaration wins).
- **FR-018**: A contributor MUST be allowed to write a field when one of the tiers of the entry or one of the contributor's roles
  is in the field's effective authority list; otherwise the entry is rejected with `NOT_AUTHORIZED` and a reason naming the field and the rule only, not the tiers and roles that may write it (those are shown by `explain` and the schema check).
- **FR-019**: A delete of a path MUST require the authority of the path and of every declared field beneath it; the reason names
  the first field that is not satisfied.
- **FR-020**: Authority MUST be checked per entry at admission against the schema version in force, MUST NOT be bypassable by the
  non-rejection flag, and MUST be reported before any rule is checked.
- **FR-021**: System MUST refuse at schema load an authority list that is empty or that names something that is neither a tier
  nor a declared role.
- **FR-022**: `explain` for a path MUST report who may write it (the effective tiers and roles, or "any permitted tier") and the
  field that declared the list.
- **FR-023**: The schema check MUST list every field with its effective authority and the field it comes from, and the rules
  declared on each field and on the document.
- **FR-024**: System MUST keep derived (computed and deferred) fields unwritable by any contributor regardless of authority, as
  Feature 1 does.
- **FR-025**: System MUST add conformance vectors and tests for every rule and for the authority cases above, each written and
  seen to fail before the behavior is built.

### Key Entities

- **Field rule**: A named, versioned, pure check of one value, with declared arguments; reports zero or more violations.
- **Document rule**: A named, versioned, pure check of the merged and computed document over a list of paths.
- **Rule declaration**: The use of a rule in a schema: the rule's name and version, its arguments, and the field or document
  it applies to.
- **Rule registration**: A deployment's definition of a custom rule: name, version, kind (field or document), declared
  arguments and the check itself.
- **Violation**: What a rule reports: the rule, the path scope and a message without values.
- **Model error**: A violation of an accepted (lenient) or re-derived document, with the identity (rule, path scope) and its
  attribution to a request or schema version (Feature 1).
- **Authority list**: The tiers and roles allowed to write a field, declared on the field or inherited from the nearest
  declaration above it.
- **Effective authority**: The list that applies to a path after inheritance, with the field that declared it.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For each of the seven field rules, a set of good and bad values is accepted and rejected exactly as the rule's
  definition says in 100% of cases, and no reason or model error message contains a submitted value.
- **SC-002**: For each of the four document rules, requests that satisfy and break it are accepted and rejected as stated in
  100% of cases, including requests whose entries satisfy the rule only together.
- **SC-003**: A lenient request from a permitted tier records its violations as model errors in 100% of cases, and an unflagged
  request on a document with model errors is accepted only if it adds none, for every rule type.
- **SC-004**: Every authority case (tier, role, inheritance, override, wildcard, delete across declared children, several
  entries) gives the stated outcome in 100% of cases, and a rejected request leaves the ledger unchanged.
- **SC-005**: No request flagged lenient ever gets past an authority check, in 100% of cases.
- **SC-006**: Every faulty rule declaration (unregistered, undeclared or missing argument, wrong kind, bad pattern, bad paths)
  and every faulty authority list is refused at load with a stable code in 100% of cases, and no such schema is used for a read.
- **SC-007**: A rule that raises an error never results in an accepted request or a document returned as valid, in 100% of cases.
- **SC-008**: A pattern of the worst shape the language allows is checked against a value of 10,000 characters in under 100
  milliseconds.
- **SC-009**: Re-reading a document under a schema version with new rules shows the same model errors and attribution as a rebuild
  from the ledger, in 100% of cases.
- **SC-010**: `explain` and the schema check report the effective authority of every field of the SPEC 16.1 schema correctly,
  and the SPEC 16.1 document and its model-error example (16.2) are unchanged by this feature.

## Assumptions

- Out of scope: redaction (`redact`) and the redaction of authority information in `explain` and provenance, which wait for the
  redaction feature (docs/SPEC.md 17 item 8); snapshots, compaction and time travel (Feature 5); detailed schema evolution
  (Feature 6); cross-document rules (docs/SPEC.md 17 item 1).
- Features 1 to 3 supply the stages V0 to V3, the non-rejection flag and `LENIENT_NOT_PERMITTED`, the no-new-errors rule, model
  error identity and attribution, re-evaluation on a schema change, roles on contributors, and the refusal of rules that read
  deferred fields. This feature does not change them.
- The `pattern` language is a small documented subset (literals, character classes and ranges, `.`, groups, alternation and the
  repeaters `*`, `+`, `?` and `{m,n}`) without back-references or look-around, so that matching takes time proportional to the
  length of the value and every port can implement it identically; a pattern matches the whole value. Shortcuts such as `\d` and flags such as case-insensitivity are not in the subset.
- `one_of` compares by canonical form; its list may hold text, numbers, booleans and null.
- `ordered` compares two paths of the same comparable kind (numbers, text, timestamps); the exact comparison for decimals and
  timestamps follows the merge rules `max` and `min` of Feature 1.
- A rule's message names the constraint (for example "must have at least 2 characters") and never the value.
- Registered rules are supplied by the deployment as code, like merge rules, macros and hooks; the schema names them and pins
  their versions.
- A `NOT_AUTHORIZED` reason does not list the allowed tiers and roles; who may see them through `explain` and the schema check is a policy to settle with the redaction feature.
- The normative worked example in docs/SPEC.md 16.1 and the model-error example in 16.2 are the reference test cases and must
  not change.
