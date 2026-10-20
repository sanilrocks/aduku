# Contract: Conformance Vectors, delta to Features 001 to 003

Case format, steps and canonical text are unchanged. Additions:

## New cases

| Case | Covers |
|---|---|
| `validation-rules` | each field rule with good and bad values (accept, reject with the rule and field named, no value in the reasons); a lenient write by a permitted tier recorded as model errors; two bad values listed together; rules that do not apply to a kind of value |
| `document-rules` | each document rule satisfied, broken, and satisfied only by the entries of one request together; null as no value; `ordered` with incomparable kinds; the no-new-errors rule; a schema version that adds a rule (model error attributed to the version, ledger unchanged) |
| `authority` | tier, role, inheritance, override, wildcard, a delete across declared children, several entries, the lenient flag (never passing authority) and `LENIENT_NOT_PERMITTED`, derived paths, and `explain` with `authority` and `authority_from` |
| `schema-load-failures` (extended) | `RULE_WRONG_PLACE`, `BAD_BEHAVIOR_ARGS` for each argument problem and for bad patterns, `UNKNOWN_RULE_PATH`, `DEPENDS_ON_DEFERRED` through a path argument, `DUPLICATE_RULE_SCOPE`, `EMPTY_AUTHORITY` |

## Steps

`explain` expectations may name `authority` (a list or `null`) and `authority_from`. A rejected `append` may list
`reasons: [{ path, rule }]`; the harness also checks that no reason text contains the submitted value.

The 16.1 and 16.2 vectors are unchanged.
