# Contract: CLI, delta to Features 001 and 002

| Command | Purpose | Output |
|---|---|---|
| `aduku get DOC [--fresh] [--include pending]` | (extended) read the document | deferred values print as `deferred::<value>`; a `deferred` section lists `path`, `status`, `as_of`, `error`, `attempts` (and `waiting_for`, `reason` with `--include pending`) |
| `aduku invalidate DOC FIELD --contributor ID --tier T [--role R]...` | Mark a deferred value out of date (`DOC` of `*` means every document that holds one) | records marked; exit 0, or 1 when refused (`NOT_AUTHORIZED`, `UNKNOWN_PATH`) |
| `aduku scheduler run HOOK --contributor ID --tier T [--role R]...` | Run due refreshes for a source (`name@version`) | the report (`attempted`, `succeeded`, `failed`, `skipped`, `calls`, `reason`); exit 0, or 1 when refused |
| `aduku notify TOPIC [DOC]...` | Deliver a notification | records marked; exit 0 |
| `aduku deferred list DOC` | Stored state of each deferred field | `field`, `status`, `as_of`, `attempts`, `invalidated` (never a value) |
| `aduku explain DOC PATH` | (extended) | for a deferred path: `winner` `deferred`, source, `dependencies`, `as_of`, `status` |
| `aduku schema check FILE` | (extended) | adds the `deferred` inventory (field, hook, depends, refresh, ttl, on_failure, invalidate_by) and the new load codes |

Exit codes: unchanged (0 ok, 1 rejected or refused request, 2 usage or schema error). Sources are registered by the
program that hosts the library; the CLI loads them with `--behaviors MODULE` (env `ADUKU_BEHAVIORS`), for example
`ports/py/examples/demo_sources.py`. `get` calls due sources, so a CLI read can print `deferred::` values on the first run
and the same values on later runs without a call.
