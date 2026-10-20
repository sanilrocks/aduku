# Contract: CLI, delta to Features 001 to 003

| Command | Change |
|---|---|
| `aduku schema check FILE` | adds, per field, `authority` (the effective list or `null`), `authority_from` (the declaring pattern or `null`) and `rules` (name, version, arguments); adds `document_rules`; reports the new load codes with exit 2 |
| `aduku explain DOC PATH` | adds `authority` and `authority_from` |
| `aduku append ...` | a rejection for lack of authority prints `NOT_AUTHORIZED: not authorized to write <field>` (no list) and exits 1 |

Exit codes are unchanged (0 ok, 1 rejected request, 2 usage or schema error).
