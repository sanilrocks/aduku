# Research: Validation Rule Library and Per-Field Authority

Spec clarifications (2026-10-19) are taken as given: a child without an authority list inherits the nearest list above it and
its own list replaces the parent's; `ordered` reports a violation for values that cannot be ordered and nothing when either
path has no value; null counts as no value for the document rules; a `NOT_AUTHORIZED` reason names the field and the rule but
not the allowed list; `pattern` is a small linear-time subset matched against the whole value.

## R-1. A typed argument declaration on registration

- **Decision**: `register_validate(name, version, check, check_args=None, *, applies="either", args=None)`. `args` is a list of
  `ArgSpec(name, kind, required=True, min=None, max=None, choices=None, items=None)` with kinds `int`, `number`, `text`,
  `bool`, `path`, `paths`, `values` (a list of scalars) and `pattern`. When `args` is given the registry builds `check_args`
  from it: unknown names, missing required names, wrong kinds, values out of `min`/`max`/`choices`, and a `paths` list shorter
  than `min` items are problems. An explicit `check_args` still works (the existing three rules keep theirs until converted;
  both may be given and then both run). `applies` is `field`, `document` or `either` (default, so Feature 1 registrations are
  unchanged). The loader reads the `path`/`paths` arguments of a use to check that each path is declared and not deferred.
- **Rationale**: eight new rules plus custom ones all need the same checks, and the loader can only find the paths a rule reads
  (for the deferred refusal and the unknown-path check) if the rule says which arguments are paths. Today it guesses with keys
  named `depends` and `args`.
- **Alternatives considered**: a `check_args` function per rule (drift, and no path discovery); JSON-Schema-like argument schemas
  (heavier than eight kinds).

## R-2. The `pattern` language and its matcher

- **Decision**: the subset of the spec: literal characters (`\` escapes a metacharacter), `.`, classes `[abc]`, `[a-z]`,
  `[^...]`, groups `( )`, alternation `|`, and the repeaters `*` `+` `?` `{m}` `{m,}` `{m,n}`. No back-references, look-around,
  anchors (a pattern always matches the whole value), shortcuts such as `\d`, or flags. `behaviors/pattern.py` parses to a
  syntax tree, builds a Thompson NFA and runs it by simulating the set of states over the value, so matching costs
  O(length of value × states). Limits, refused at load: pattern length at most 500, a repeat count at most 255, the NFA at most
  5,000 states (nested counted repeats multiply). `.` matches any one character except newline.
- **Rationale**: simulation never backtracks, so no pattern can take exponential time (SC-008), and the small grammar is easy
  to port. The limits keep compile time and memory bounded as well.
- **Alternatives considered**: host `re` with a timeout (not portable, not deterministic); RE2 bindings (a native
  dependency); a derivative-based matcher (more code for the same guarantee).

## R-3. Where the new rules run

- **Decision**: all are `validate` behaviors with the existing `Subject` contract, so they run at V1 (incoming value, field
  rules), V2 and V3 (merged and computed document), in re-evaluation on a schema change, and under the lenient flag and the
  no-new-errors rule, with no change to the engine. Field rules return no violation for a kind of value they do not apply to
  (`min_len` on a number, `pattern` on a non-text, `unique_items` on a non-list). A rule on a field with no value is not run
  (the field is not in the document).
- **Rationale**: this is the Feature 1 design; the spec adds rules, not stages.
- **Alternatives considered**: a new stage for the cross-field rules (not needed: V2 already sees the whole document).

## R-4. Document rules: paths, "has a value", scope

- **Decision**: a document rule takes its paths from arguments (`paths: ["/a", "/b"]` for `all_or_none`, `at_least_one`,
  `at_most_one`; `first` and `second` for `ordered`). A path has a value if it is present in the merged and computed document
  and is not null (clarification 3); computed annotations are stripped before the rule sees the document (existing). The
  violation's path scope is the first path of the rule (a valid path, so `explain` of that path lists the error), and its rule
  id is the rule name; model error identity is therefore (rule, first path). Two declarations of one rule with the same first
  path are refused at load (`DUPLICATE_RULE_SCOPE`) because they would be the same error. Two different document rules over the same paths are separate errors (their rule ids differ), so they are allowed. Two uses of one rule that start at the same path are refused on purpose: they would be one model error, and the author can express both constraints by ordering the paths differently or by a custom rule.
- **Rationale**: scope must be a path for `explain` to find it; the duplicate case is the only place identity could collide.
- **Alternatives considered**: a scope made of all the paths joined (not a path, so invisible to `explain`); one scope per
  path (several errors for one cause).

## R-5. `ordered`: comparison

- **Decision**: values are comparable when both are numbers (int, decimal or float, compared by value), both are text (code
  point order), or both are timestamps (by instant); anything else is not comparable and is a violation (clarification 2).
  Equal values are in order (`first <= second`). Booleans are not numbers.
- **Rationale**: this is the order the `max` and `min` merge rules use for these kinds, so authors learn one order.
- **Alternatives considered**: strict `<` (an unset range with equal ends is a common valid case); cross-kind coercion (hides
  errors).

## R-6. Authority: inheritance and the effective list

- **Decision**: `Schema.authority_for(path)` returns the tiers and roles that may write `path` and the field pattern that
  declared them: it walks from the path to its first segment and takes the first `field_for(prefix)` that has an `authority`
  (so the field itself, then its parents; wildcard patterns count like any other pattern). `None` means any permitted tier.
  Admission, `explain` and the schema check all call it. A contributor may write when one of the entry's tier or the
  contributor's roles is in the list (as today). A delete checks the effective list of the path and of every declared field
  beneath it, and the reason names the first field not satisfied.
- **Rationale**: one lookup guarantees the three places agree; walking prefixes implements "the most specific declaration
  wins" and the inheritance of clarification 1.
- **Alternatives considered**: copying the parent's list into each child at load (the schema file would no longer show what the
  author wrote, and `explain` could not name the declaring field).

## R-7. Reasons that do not reveal the list

- **Decision**: `Reason` for authority has code `NOT_AUTHORIZED`, `rule = "authority"`, `path` = the field and the message
  `not authorized to write <field>` (for a delete that wipes a child: `not authorized to write <child>, which this delete
  wipes`). The allowed list appears only in `explain` and `schema check`.
- **Rationale**: clarification 4. A caller who needs to know who may write asks `explain`, which a deployment can protect.
- **Alternatives considered**: showing the list only to insiders (a second policy to maintain).

## R-8. Load-time checks and codes

- **Decision**: reuse `UNKNOWN_BEHAVIOR` (an unregistered rule or version) and `BAD_BEHAVIOR_ARGS` (argument problems, one
  message per problem); add `RULE_WRONG_PLACE` (a document rule on a field, or a field rule in `document_validate`),
  `UNKNOWN_RULE_PATH` (a path argument that no field declares), `DUPLICATE_RULE_SCOPE` and `EMPTY_AUTHORITY`; keep
  `DEPENDS_ON_DEFERRED` (now found through the `path`/`paths` arguments instead of key names) and `UNKNOWN_TIER` for an
  authority entry that is neither a tier nor a role.
- **Rationale**: stable, per-cause codes for the shared load-failure vectors; no new code where one exists.
- **Alternatives considered**: one `BAD_RULE` code (hard to test per cause).

## R-9. Custom rules

- **Decision**: a custom rule is registered with `register_validate(..., args=[ArgSpec...], applies=...)` and used in a schema
  as `name@version::{args}` (the existing form). Its version is pinned by the schema: `_run` resolves the version the schema
  names, so a newer registered version never changes an older schema's documents. A rule that raises, or returns something
  other than a list of `Violation`, is a `ValidatorFault` (existing), reported as a rejection at admission and as a failed read
  otherwise.
- **Rationale**: these properties exist from Feature 1; the work is the argument declaration and tests that the new rules and a
  custom rule share them.
- **Alternatives considered**: none worth recording.

## R-10. `explain` and the schema check

- **Decision**: `Explanation` gains `authority` (tuple of tiers and roles, or `None` for any permitted tier) and
  `authority_from` (the field pattern that declared it, or `None`). The schema check lists, per field, `authority`,
  `authority_from` and its `validate` rules (name, version, arguments), and the `document_validate` rules. The CLI prints
  both.
- **Rationale**: spec FR-022, FR-023; the data comes from `authority_for` and the schema.
- **Alternatives considered**: a separate `authority` command (two places to look).

## R-11. Existing fixtures and tests that change

- **Decision**: reasons no longer list the allowed tiers; no existing test asserts the old text, so the new tests assert the new
  wording (a search of `ports/py/tests` found none); the three built-in rules are re-declared with `ArgSpec` (their messages and codes are unchanged, so the 16.1 and
  16.2 vectors do not change). Fixture rules in the shared vectors use the new rules by name.
- **Rationale**: constitution workflow (vectors change in the same change as behavior).
- **Alternatives considered**: keeping the old wording behind a switch (the clarification rules it out).

## R-12. Conformance

- **Decision**: three new vectors (`validation-rules`, `document-rules`, `authority`) and an extended
  `schema-load-failures`, using only existing step kinds (`append`, `get`, `explain`, `load_schema`, `rebuild`,
  `activate_schema`); `explain` expectations may name `authority` and `authority_from`. Cases never put a submitted value in an
  expected message: the harness checks that a rejection's reasons do not contain it.
- **Rationale**: ports compare codes and structured results, not message text.
- **Alternatives considered**: new step kinds (not needed).
