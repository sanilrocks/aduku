# Contract: CLI, delta to Feature 001

| Command | Purpose | Output |
|---|---|---|
| `aduku recompute DOC [FIELD] --contributor ID --tier T [--role R]...` | Call impure hooks again | `recomputed`, `skipped`, `failed`, `effect_seqs`; exit 0, or 1 when refused (`NOT_AUTHORIZED`) or a hook failed |
| `aduku effects list DOC [FIELD]` | Effect log records (metadata only, never a value or an error text) | `effect_seq`, `field`, `hook`, `outcome`, `attempts`, `recorded_at` |
| `aduku effects prune [DOC]` | Prune superseded records | pruned count |
| `aduku schema check FILE` | (extended) | adds `computed` (field, macro, args), `impure` (the inventory: field, hook, reason, timeout, retry, deadline, recompute_by) and the new load codes |
| `aduku get DOC` | (extended) | the document prints computed values as `computed::<value>` |
| `aduku explain DOC PATH` | (extended) | adds `dependencies` and `effect_seq`; `winner` is `computed` for a computed path |

Exit codes: unchanged (0 ok, 1 rejected request or a refused or failed `recompute`, 2 usage or schema error). Macros and hooks are registered by the program that hosts the library, so the CLI can only evaluate schemas whose behaviors are presets or are named in `--behaviors MODULE` (env `ADUKU_BEHAVIORS`; a module with a `register(behaviors)` function, for example `ports/py/examples/demo_hooks.py`).
