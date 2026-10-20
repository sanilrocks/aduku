# Contract: Command line, delta to Features 001 to 005

Global options as before, except `--schema` may be repeated or name a directory (every `*.ion` file in it is a version). A store with no
schema state adopts the highest version among the files it is given as its active one (a single file: that file's version); a store that
holds state serves what it was told to, whatever the files say.

```text
aduku schema diff OLD NEW                     # two files; no store; prints the change list and the overall class
aduku schema plan --target V [--limit N] [--doc D ...]       # the dry run; prints the report
aduku schema activate V --operator NAME [--acknowledge-breaking] [--rebuild]
aduku schema history [--limit N] [--doc D]
aduku pin DOC V --operator NAME
aduku unpin DOC --operator NAME
aduku migrate [DOC ...] [--to V] --operator NAME [--dry-run] [--batch N]
```

- Exit 0 on success, 1 on a refusal (the code is printed, as for a rejected request), 2 on a usage error (a missing `--operator`
  included).
- `schema diff` prints `{old, new, overall, changes: [{kind, path, description, class, classified}]}`; exit 0 whatever the class.
- `schema plan` prints the counts (`examined`, `left_out`, `changed`, `unchanged`, `failed`, `would_call_hook`) and, per changed
  document, what differs; it never prints a value, only whether a document, its model errors or its skipped entries differ and the
  names and counts of errors and shadow reasons.
- `schema activate` of a breaking version without `--acknowledge-breaking` prints the first breaking changes and exits 1.
- `migrate --dry-run` prints the per-document dry run and moves nothing.
- `get` prints `shadowed` (seq, entry, reason, detail) beside `skipped`; `explain` prints the shadowed entries that touch the path.
