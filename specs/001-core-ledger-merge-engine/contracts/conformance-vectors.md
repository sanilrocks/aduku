# Contract: Conformance Vectors

Language-neutral test cases in `conformance/*.ion` that every port MUST pass with byte-identical output (SPEC 1.4, 16;
constitution Scope). A change that alters merge results MUST change vectors in the same commit.

## Case format

```ion
case::{
  name: "worked-example-16-1",
  schema: { ... },                       // inline schema (contracts/schema-format.md) plus registered fixture behaviors
  steps: [
    { append: { doc: "d1", contributor: {id:"job", tiers:[backfill]}, lenient: false,
                entries: [ {tier: backfill, path: "/name", value: "A. Smith"} ] },
      expect: { accepted: [ {seq: 1} ] } },
    { append: { ... }, expect: { rejected: { code: FIELD_NOT_WRITABLE, reasons: [ {path: "/displayName"} ] } } },
    { get: "d1", expect: { document: '{address:{city:"Paris",zip:"78701"},age:30, ...}',
                           model_errors: [] } },
    { explain: { doc: "d1", path: "/age" }, expect: { rule: "max@1", winner_seq: 3 } },
    { activate_schema: 8 },
    { rebuild: "d1", expect: { identical: true } }
  ]
}
```

- `expect.document` and `expect.model_errors` are compared as canonical Ion text, defined in the next section.
- A `load_schema: '''<schema text>'''` step loads a schema with the case's fixtures registered; `expect` is `{ok: true}` or
  `{problems: [CODE, ...]}`, and every listed code must be among the problem codes reported. Ports compare codes, never
  message text. Codes: `UNKNOWN_BEHAVIOR`, `MERGE_NOT_ASSOCIATIVE`, `MERGE_TYPE_UNSUITABLE`, `DEPENDS_ON_DEFERRED`,
  `DEPENDENCY_CYCLE`, `DEFAULT_TYPE_MISMATCH`, `ON_DELETE_NEEDS_DEFAULT`, `UNKNOWN_TIER`, `DUPLICATE_TIER`,
  `DERIVED_HAS_MERGE_OR_DEFAULT`, `COMPUTED_AND_DEFERRED`, `AUDIT_RETENTION_REQUIRED`, `RETENTION_NOT_POSITIVE`,
  `OP_NOT_SUPPORTED`, `UNKNOWN_OPERATION`, `DEFAULT_ON_PATTERN`, `BAD_BEHAVIOR_ARGS`.
- Fixture behaviors (for example `acme.max_spouses_by_country@1`) are named in a `fixtures:` list and each port provides
  its own implementation under that name; their semantics are documented in `conformance/README.md`.
- `tx_time` is supplied by the harness clock and is excluded from comparisons; the vector set includes a pair of cases
  differing only in clock values that MUST give identical results.

## Canonical Ion text (normative)

Every port MUST produce exactly these bytes for documents and model errors; spec FR-019 refers to this section.

1. **Format**: Ion text, UTF-8, no insignificant whitespace (none outside string literals).
2. **Structs**: fields sorted ascending by the Unicode code points of the field name; duplicate field names do not occur.
3. **Lists**: element order preserved.
4. **Annotations**: written immediately before the annotated value, in their original order, each as a bare symbol when
   it is a valid identifier and quoted otherwise.
5. **Scalars**: `null` is untyped `null` (typed nulls such as `null.string` become `null`); booleans `true`/`false`; integers in
   base 10 with no leading zeros or `+`; decimals keep their written digits and exponent (`1.50` stays `1.50`, never
   `1.5`; exponent 0 is written `100.`, a positive exponent as `1d3`, a negative one with a decimal point);
   timestamps in UTC with their written precision (`2020T`, `2020-02T`, `2020-02-03T`, `2020-01-01T10:20Z`,
   `2020-01-01T09:20:30.5Z`; a known offset is converted to `Z`, while an unknown offset (`-00:00`) stays `-00:00` because
   it is not the same as UTC; fractional seconds keep every digit written, however fine,
   e.g. `2026-01-01T10:00:00.123456789Z`); strings in double quotes where only `\"`, `\\`, `\n`, `\r`,
   `\t`, `\b`, `\f`, `\0` and `\xHH` (other control characters U+0001 to U+001F and U+007F) are escaped and every other
   character, including non-ASCII, is written as itself in UTF-8.
6. **Depth**: values nested more than 64 levels deep have no canonical form; they are refused, never written.
7. **Outside the vector set** until a second port settles their rendering: binary floating-point values, blobs, clobs,
   s-expressions. Vectors MUST NOT contain them.
8. **Symbols** stay symbols: written bare when they are valid identifiers and not `null`, `true`, `false` or `nan`, otherwise in single
   quotes (this applies to field names and annotations too). Strings and symbols are different values.
9. **Floats** (outside the vector set) are written as a mantissa and an integer exponent with no `+` sign and no leading zeros,
   for example `1.5e0`, `1e16`, `1.5e-7`, `nan`, `+inf`.

Research R-3 records why this form was chosen; this section is the definition.

## Initial set (this feature)

| File | Source |
|---|---|
| `worked-example-16-1.ion` | SPEC 16.1, up to P3 (P4/P5 expectations omitted until Features 2 and 3), plus the rejected `FIELD_NOT_WRITABLE` request and audit-on/off variants |
| `model-errors-16-2.ion` | SPEC 16.2 steps 1-7 |
| `tombstones.ion` | SPEC 6.7 rules and User Story 5 scenarios |
| `defaults-on-delete.ion` | User Story 4 (both `on_delete` settings) |
| `atomicity.ion` | User Story 1 scenario 4; request with interdependent entries |
| `retries.ion` | same request key and payload twice (no-op, same response shape); key reused with other payload; same payload without a key applied again |
| `schema-load-failures.ion` | non-associative custom merge, audit without retention, validator on deferred field |
