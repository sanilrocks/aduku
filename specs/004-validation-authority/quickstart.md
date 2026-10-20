# Quickstart: Validate the Rule Library and Authority

A run guide proving the feature end to end. Details: [data-model.md](data-model.md), [contracts/](contracts/). It assumes Features 001
to 003 are in place (`ports/py/`, `uv sync` done).

## Automated validation

```bash
cd ports/py
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest tests/unit          # pattern matcher, every rule, ArgSpec checks, loader refusals, authority_for
uv run pytest tests/golden        # 16.1 and 16.2 unchanged; validation-rules, document-rules, authority vectors
uv run pytest tests/property --hypothesis-profile=ci   # pattern vs re; rejected request leaves no trace; lenient never passes authority
uv run pytest tests/integration   # service-level rules, authority matrix, lenient, no-new-errors, version change, custom rules
```

Expected: all green; the 16.1 and 16.2 vectors match exactly (SC-010).

## Manual walkthrough with the CLI

`examples/rules.schema.ion` uses every rule and a parent/child authority; `examples/demo_rules.py` registers a custom rule
`acme.tax_id@1`.

```bash
cd ports/py
rm -f /tmp/aduku-demo4.db*
export ADUKU_DB=/tmp/aduku-demo4.db ADUKU_SCHEMA=examples/rules.schema.ion ADUKU_BEHAVIORS=demo_rules PYTHONPATH=examples
uv run aduku schema check examples/rules.schema.ion        # rules and effective authority per field
uv run aduku append d1 --contributor alice --tier user --set /country='"US"' --set /tags='["a","b"]'
uv run aduku append d1 --contributor alice --tier user --set /country='"usa"'      # exit 1: pattern (the value is not repeated)
uv run aduku append d1 --contributor alice --tier user --set /plan='"pro"'         # exit 1: NOT_AUTHORIZED, no list in the reason
uv run aduku append d1 --contributor root --tier admin --set /plan='"pro"'         # accepted
uv run aduku explain d1 /billing/note                       # authority [finance], declared by /billing
uv run aduku append d1 --contributor alice --tier user --set /personalName='"Pat"' --set /companyName='"Acme"'   # exit 1: at_most_one
uv run aduku append d1 --contributor alice --tier user --set /street='"1 Main"'                                    # exit 1: all_or_none
uv run aduku append d1 --contributor alice --tier user --set /taxId='"DE123"' --set /country='"US"'                # taxId: acme.tax_id strict (5+ chars) is fine
```

Expected: each step matches its comment.

## Success criteria map

| Criterion | Where it is shown |
|---|---|
| SC-001, SC-002 | the rule unit tests and the `validation-rules` and `document-rules` vectors |
| SC-003 | lenient and no-new-errors cases for every rule type |
| SC-004, SC-005 | the `authority` vector and the authority matrix |
| SC-006 | the `schema-load-failures` vector |
| SC-007 | the custom-rule fault test |
| SC-008 | the pattern performance test |
| SC-009 | the version-change test against a rebuild |
| SC-010 | the golden tests of 16.1 and 16.2, and `explain` of the 16.1 schema |
