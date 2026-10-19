# Contract: Schema File Format, delta to Feature 001

Feature 001 parsed `computed` declarations and made their paths unwritable. This feature evaluates them and adds the keys
below. Everything in [Feature 001's schema-format](../../001-core-ledger-merge-engine/contracts/schema-format.md) still applies.

## Pure computed field

```ion
"/displayName": { type: string, computed: { macro: upper, args: ["/name"] } },
"/label":       { type: string, computed: { macro: "acme.label@1", args: ["/name", "/address/city"] }, on_missing: absent },
"/greeting":    { type: string, computed: { macro: concat, args: [lit::"Hello, ", "/name", lit::"!"] } },
"/nick":        { type: string, computed: { macro: coalesce, args: ["/nickname", "/name"] } }
```

| Key | Rule |
|---|---|
| `computed.macro` | a preset (`upper lower trim concat coalesce len sum min max round format_ts hash join if eq`) or a registered macro, `name` or `name@version` (default 1). Preset names are reserved (`RESERVED_MACRO_NAME` when registering). Unknown name or version: `UNKNOWN_BEHAVIOR`. |
| `computed.args` | ordered; a path (string starting with `/`) or a literal (`lit::<scalar>`). Wrong count or types for the signature: `BAD_MACRO_ARGS`. |
| `on_missing` | `null` (default), `absent`, `error`; on the field, outside `computed` |
| `type` | the result type; a preset whose result type differs is `BAD_MACRO_ARGS` |

## Impure computed field

```ion
"/taxId": { type: string, on_missing: null,
            computed: { impure: { hook: "tax.lookup@2", depends: ["/country", "/name"],
                                  reason: "tax id issued by the registry; required before checkout",
                                  timeout: "2s", retry: { max: 3, backoff: "200ms" }, deadline: "6s",
                                  recompute_by: [admin], failure_mode: reject_on_failure } } }
```

`hook`, `depends`, `reason`, `timeout`, `retry`, `deadline` and `recompute_by` are required
(`IMPURE_DECLARATION_INCOMPLETE`). `failure_mode` is optional and may be written inside `impure` (as above) or, as Feature 001 allowed, on the field itself; the
first that is present wins: inside `impure`, then on the field, then the schema-level `impure_failure_mode` (default
`reject_on_failure`). Platform bounds: `timeout` at most 30s, `retry.max` at most 5, `deadline` at most 60s and at least `timeout`.

## Load failures added (all reported together as `SchemaError`)

`UNKNOWN_DEPENDENCY` (a path no field declares), `DEPENDENCY_CYCLE` (already; the message lists the loop),
`DEPENDS_ON_DEFERRED` (already), `BAD_MACRO_ARGS`, `MACRO_INPUT_UNSUPPORTED` (custom macro over a float or timestamp
dependency), `IMPURE_DECLARATION_INCOMPLETE`, `UNKNOWN_TIER` for a name in `recompute_by` that is neither a tier nor a
declared role, and `UNKNOWN_BEHAVIOR` for an unregistered macro or hook. A validator or computed field MAY depend on
another computed field, pure or impure.
