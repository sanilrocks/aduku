# Contract: Command line, delta to Features 001 to 004

All commands keep the global options (`--db`, `--schema`, `--format`). Exit code 0 on success, 1 on a refusal (the code is printed),
2 on a usage error.

```text
aduku get DOC --as-of seq=N | tx=ISO8601 | valid=ISO8601
aduku explain DOC PATH --as-of ...
aduku rebuild [DOC]                      # also reports layers that differed or were refreshed
aduku compact DOC (--through-seq N | --older-than 90d)
aduku archive verify DOC
aduku archive expire DOC --through-seq N
aduku purge DOC (--path P [--scope-tier T] | --entry ID [--entry ID ...]) --reason TEXT --contributor C --tier T [--role R ...]
aduku purge-log DOC
aduku entries DOC [--since N]            # shows valid_time, synthetic and purged
aduku append ... [--valid-time ISO8601]  # applies to every --set/--delete of the request
```

- `--as-of` takes exactly one `kind=value`; anything else is exit 2 with `AS_OF_AMBIGUOUS` or a usage message.
- `get --as-of` prints the document, model errors, status, `as_of` (kind, point, resolved_seq, included, inside_request) and the
  deferred list.
- `compact` prints the watermark, entries replaced, synthetic entries written and paths checked; with nothing to do it prints
  `nothing to compact` and exits 0.
- `purge` prints the counts per store and the purge id, never a value. `--reason` is required (usage error when missing).
- `explain` prints purged marks and, for a synthetic entry, `originals: kept|expired` and its sources.
- `schema check` prints `purge_by`.
