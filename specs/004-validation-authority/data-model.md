# Data Model: Validation Rule Library and Per-Field Authority

Entities from the spec with fields and rules. Feature 1 to 3 entities (entry, request, view, model error, violation) are
unchanged.

## ArgSpec (registration)

| Field | Type | Rule |
|---|---|---|
| `name` | text | The argument's name in the schema. |
| `kind` | `int` \| `number` \| `text` \| `bool` \| `path` \| `paths` \| `values` \| `pattern` | `int` is a whole number; `number` is int, decimal or float; `path` is a declared path; `paths` is a list of them; `values` is a list of text, numbers, booleans and null; `pattern` is text in the pattern subset. |
| `required` | bool | Default true. |
| `min`, `max` | number or null | Bounds for `int`/`number`; for `paths`/`values`, `min` is the least number of items. |
| `choices` | list or null | Allowed values for `text`. |

## Rule registration (Behavior of kind `validate`)

| Field | Type | Rule |
|---|---|---|
| `name`, `version` | text, int | Unique together. A preset or reserved name is refused as for macros. |
| `applies` | `field` \| `document` \| `either` | Where a schema may use it. Feature 1 registrations are `either`. |
| `args` | list of ArgSpec | Declared arguments; an argument not listed is refused. |
| `check_args` | function or null | Extra problems for combinations the declaration cannot express (for example `min` greater than `max`). |
| `impl` | function | `Subject -> list[Violation]`; pure. |

## Rule declaration (in a schema)

A field's `validate: [name@version::{args}, ...]` or the schema's `document_validate: [...]`. Checked at load against the
registration: found (`UNKNOWN_BEHAVIOR`), right place (`RULE_WRONG_PLACE`), arguments (`BAD_BEHAVIOR_ARGS`), paths declared
(`UNKNOWN_RULE_PATH`) and not deferred (`DEPENDS_ON_DEFERRED`), no two uses of one document rule with the same first path
(`DUPLICATE_RULE_SCOPE`).

## The library

| Rule | Place | Arguments | Reports a violation when |
|---|---|---|---|
| `non_empty` | field | none | text, list or struct of length 0 |
| `range` | field | `min`, `max` (number, optional) | a number outside the bounds |
| `max_len` | field | `n` (int, at least 0) | text or list longer than `n` |
| `min_len` | field | `n` (int, at least 0) | text or list shorter than `n` |
| `pattern` | field | `regex` (pattern) | text that does not match the whole of `regex` |
| `one_of` | field | `values` (at least one) | any value whose canonical form is not in `values` |
| `unique_items` | field | none | a list with two items of the same canonical form |
| `all_or_none` | document | `paths` (at least 2) | some but not all paths have a value |
| `at_least_one` | document | `paths` (at least 1) | no path has a value |
| `at_most_one` | document | `paths` (at least 2) | more than one path has a value |
| `ordered` | document | `first`, `second` (paths) | both have a value and `first > second`, or they cannot be ordered |

A path has a value if it is present in the merged and computed document and not null. A field rule is skipped for a kind of
value it does not apply to. Messages state the constraint and never the value.

## Authority list and effective authority

| Concept | Rule |
|---|---|
| Authority list | `authority: [name, ...]` on a field, each a tier or a declared role; empty is refused (`EMPTY_AUTHORITY`); an unknown name is `UNKNOWN_TIER`. |
| Effective authority of a path | The list of the first field, from the path's own field up through its parents' patterns, that declares one; `None` (any permitted tier) if none does. |
| Declared by | The pattern of that field. |
| May write | One of the entry's tier or the contributor's roles is in the effective list. |
| Delete | The path's effective list and the effective list of every declared field beneath it must all be satisfied. |

## Explanation (extended) and schema check

`Explanation.authority: tuple[str, ...] | None` and `Explanation.authority_from: str | None`. The schema check lists, for each
field: `authority`, `authority_from` and its rules; and the document rules.

## Violation and model error scope for document rules

Rule id = the rule's name; path scope = the first path argument; identity (rule, path scope) as in Feature 1.
