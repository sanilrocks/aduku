# Contract: Change classification

`schema_changes(old, new) -> ChangeList` is pure: two schemas in, a list out, the same list in the same order each time. It reads no
store. The class table below is also in docs/SPEC.md section 14.

## Format of a change

`{kind, path, description, class, classified, detail}`; `path` is the field pattern (or `""` for a top-level setting, or the tier name
for a tier), `description` is one short sentence that never contains a stored value (a default or an argument is named, not
shown, when it could hold data), `class` is `compatible`, `visible` or `breaking`, `classified` is false for "not classified".
Sorted by `(path, kind)`. `overall` is the worst class (`breaking` > `visible` > `compatible`); an empty list is `compatible`.

## The table

| Kind | Difference | Class |
|---|---|---|
| `field_added` | a field pattern in the new version only (plain, computed or deferred) | compatible |
| `field_removed` | a field pattern in the old version only: new writes to it are refused, and its recorded values still show in documents, merged by last-write-wins and no longer governed by a type, rule or authority | visible |
| `default_changed` | `default` or `on_delete` added, changed or removed | visible |
| `merge_changed` | merge behavior name or version differs | breaking |
| `type_changed` | `type` differs | breaking |
| `validate_changed` | a field `validate` rule added, removed, or its name, version or arguments differ | visible |
| `authority_changed` | `authority` list, `ops` or `redact` differs | visible |
| `became_derived` | plain becomes computed or deferred | breaking |
| `became_plain` | computed or deferred becomes plain | breaking |
| `computed_changed` | macro, arguments or `on_missing` differ | visible |
| `impure_changed` | hook, version, `depends`, timeout, retry, deadline, `recompute_by` or `failure_mode` differ | visible |
| `deferred_changed` | hook, `depends`, `refresh`, `ttl`, `on_failure`, `invalidate_by` or rate settings differ | visible |
| `tier_added_top` | a tier appended after the old ones, the old order unchanged | compatible |
| `tier_changed` | a tier inserted below the top, reordered, renamed or dropped | breaking |
| `roles_added` / `roles_removed` | a declared role added / removed | compatible / visible |
| `settings_changed` | `lenient`, `purge_by`, `impure_failure_mode`, `deferred_on_failure` | visible |
| `audit_changed` | `rejection_audit` | compatible |
| `document_validate_changed` | a document rule added, removed or changed | visible |
| `other` | any other difference | visible, `classified: false` |

When a field changes in several ways, each is its own change. A field renamed is a removal and an addition (two changes).

## Which changes can shadow entries

The pure list marks, on each change that can shadow entries, the reasons it can give: `became_derived` (`now_derived`),
`type_changed` (`no_longer_conforms`), `merge_changed` (`cannot_be_merged`: a stored value the new merge cannot take) and `tier_changed` (`tier_not_in_schema`). A removed field shadows nothing. The number of entries per reason is only known with a store, so it is in the dry-run report, not in the pure list.
