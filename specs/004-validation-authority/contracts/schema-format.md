# Contract: Schema Format, delta to Features 001 to 003

## Rule declarations

```
"/country": { type: string, merge: lww,
              validate: [pattern::{regex: "[A-Z]{2}"}, one_of::{values: ["US", "DE", "FR"]}] },
"/tags":    { type: "list<string>", merge: union, validate: [unique_items, max_len::{n: 20}] },

document_validate: [
  all_or_none::{paths: ["/address/street", "/address/city", "/address/zip"]},
  at_least_one::{paths: ["/phone", "/email"]},
  at_most_one::{paths: ["/personalName", "/companyName"]},
  ordered::{first: "/validFrom", second: "/validTo"}
]
```

A use is `name`, `"name@version"`, `name::{args}` or `'name@version'::{args}` (the form of Feature 1; a name with a dot is a quoted symbol when it has arguments, such as `'acme.tax_id'::{country: "US"}`, and a string when it has none). The arguments are those the rule
declares; see `rules.md`.

## Authority

```
"/billing":       { type: struct, merge: struct, authority: [finance] },
"/billing/note":  { type: string, merge: lww },                       // inherits [finance]
"/billing/limit": { type: int, merge: max, authority: [admin] },       // its own list: only admin, not finance
"/plan":          { type: string, merge: lww, authority: [admin] }
```

A field without `authority` takes the first list found at its own pattern and then at the patterns of its parents; a field with
its own list uses only that list. Entries are tiers or roles the schema declares; an empty list is refused.

## Load-time checks (each names the field or `document_validate` and the rule; every problem is reported)

| Code | Cause |
|---|---|
| `UNKNOWN_BEHAVIOR` | the rule or version is not registered as a validator (unchanged) |
| `RULE_WRONG_PLACE` | a document rule on a field, or a field rule in `document_validate` |
| `BAD_BEHAVIOR_ARGS` | an undeclared, missing, wrong-kind or out-of-range argument; a bad or unsupported `pattern`; a `paths` list shorter than the rule needs; `min` greater than `max` (unchanged code, new causes) |
| `UNKNOWN_RULE_PATH` | a `path`/`paths` argument names a path no field declares |
| `DEPENDS_ON_DEFERRED` | a rule on, or over, a deferred field (now found through the rule's path arguments) |
| `DUPLICATE_RULE_SCOPE` | two uses of one document rule with the same first path |
| `EMPTY_AUTHORITY` | `authority: []` |
| `UNKNOWN_TIER` | an authority entry that is neither a tier nor a declared role (unchanged) |

## Compatibility

Existing schemas load unchanged: the three rules keep their names, arguments and messages. A rule that takes no arguments (`non_empty`, `unique_items`) now refuses one, as `range` and `max_len` always did (a misspelt or stray argument is a mistake, not something to ignore); a schema that passed one to `non_empty` must drop it. `non_empty`, `range` and `max_len` are field rules, so a schema that used one in `document_validate` (it has no value to check there) is now refused with `RULE_WRONG_PLACE`; none exists in the shared vectors or tests. A schema whose child fields relied
on having no list under a parent that has one now gets the parent's list (a visible change: see SPEC 14).
