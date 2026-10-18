# Contract: CLI (`aduku`)

Thin wrapper over the library, for validation and manual checks. Global options: `--db PATH` (env `ADUKU_DB`, default `aduku.db`),
`--schema PATH` (env `ADUKU_SCHEMA`), `--format json|ion` (default json). Exit codes: 0 ok, 1 rejected request (or a rebuild that found a difference or a document it could not derive), 2 usage or schema error.

| Command | Purpose | Output |
|---|---|---|
| `aduku schema check FILE` | Load and validate a schema | `ok` or every load error |
| `aduku append DOC --contributor ID --tier T [--lenient] [--request-key K] [--idem-key K] (--set PATH=VALUE \| --delete PATH)...` | Submit one atomic request | entries `[(id, seq)]` and model-error changes, or every rejection reason |
| `aduku get DOC` | Read the merged document | document and `model_errors` |
| `aduku explain DOC PATH` | Why a field has its value | winner, rule, superseded entries |
| `aduku entries DOC [--since SEQ]` | List ledger entries | entries |
| `aduku rebuild [DOC]` | Re-derive and compare with the cache | `identical`, `checked`, `differing`, `refreshed`, `failed`; exit 1 when a cached view differed or a document could not be derived |
| `aduku audit list DOC` / `audit prune [DOC]` / `audit set DOC enabled\|disabled\|inherit` | Rejection audit admin | records / pruned count |

Rejected requests print the reasons list on stderr (JSON on stdout when `--format json`) and exit 1; nothing is written
to the ledger.
