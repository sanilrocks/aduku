# Contract: Library API, delta to Features 001 to 003

Python rendering; other ports expose the same operations. Everything not mentioned is unchanged.

## Registration

```python
behaviors.register_validate(name, version, check, check_args=None, *, args=None, applies="either") -> Behavior
ArgSpec(name, kind, required=True, min=None, max=None, choices=None)       # kind: int number text bool path paths values pattern
```

`args` makes the loader check every use of the rule (see `schema-format.md`). `applies` is `"field"`, `"document"` or `"either"`.
`check_args` still works and runs after the declared checks. Feature 1 registrations are unchanged.

## Authority

```python
schema.authority_for(path) -> tuple[tuple[str, ...], str] | None     # (tiers and roles, the pattern that declared them)
```

Admission rejects an entry whose tier and the contributor's roles are not in the effective list with `NOT_AUTHORIZED`:
`Reason(code=NOT_AUTHORIZED, rule="authority", path=<the entry's path>, message="not authorized to write <field pattern>")`; a delete that wipes a
child the contributor may not write names that child. The message never lists the allowed tiers and roles.

## Explain

`db.explain(doc, path)` returns `Explanation` with two new fields: `authority` (the tiers and roles that may write the path, or
`None` for any permitted tier) and `authority_from` (the field pattern that declared the list, or `None`). The rest of the answer
is unchanged. For a derived (computed or deferred) path both are `None`: nobody can write it, whatever list the schema gives it.

## Rejections and model errors

No new codes. A rule violation at admission is `VALIDATION_FAILED` (or `MODEL_ERROR_INTRODUCED` under the no-new-errors rule) with
a reason per violation: `rule` = the rule's name, `path` = the field (field rule) or the first path (document rule). A rule fault
is `RejectedError` with code `VALIDATION_FAILED` and the fault's message (the rule and the kind of error it raised, never what the error said), and a read raises `ValidatorFault`.

## Guarantees

- A rejected request leaves the ledger, the views and the model errors unchanged.
- The non-rejection flag never passes an authority check.
- No message, reason or model error contains a submitted value.
- Documents are re-checked under the rule versions their schema names.
