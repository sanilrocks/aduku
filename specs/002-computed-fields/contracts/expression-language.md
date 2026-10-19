# Contract: Expression Language for Custom Macros

A custom macro is an expression over its named parameters. The language is a deliberately small subset of CEL: the
syntax and meaning below match CEL, so a port may use a native CEL implementation for these expressions, provided the
limits and function library match.

## Values

`null`, `bool`, `int` (64-bit; overflow is a fault), `decimal` (exact), `string`, `list`, `map` (a struct). Floats and
timestamps are not supported: a custom macro whose declared dependency has type `float` or `timestamp` is refused at load
(`MACRO_INPUT_UNSUPPORTED`). Ordering (`<`, `<=`, `>`, `>=`) between different types is a fault, as in CEL; `==` and `!=` between different types are false (an `int` and a `decimal` compare by value).

## Syntax

```text
expr     := ternary
ternary  := or ( "?" expr ":" expr )?
or       := and ( "||" and )*
and      := rel ( "&&" rel )*
rel      := add ( ("==" | "!=" | "<" | "<=" | ">" | ">=" | "in") add )*
add      := mul ( ("+" | "-") mul )*
mul      := unary ( ("*" | "/" | "%") unary )*
unary    := ("!" | "-")* postfix
postfix  := primary ( "." IDENT ( "(" args? ")" )? | "[" expr "]" )*
primary  := literal | IDENT | IDENT "(" args? ")" | "(" expr ")" | "[" args? "]" | "{" pairs? "}"
```

Literals: integers, decimals (`1.50d`), double-quoted strings with the escapes `\n \t \" \\ \uXXXX`, `true`, `false`, `null`.
Identifiers are the macro's parameter names or function names. `.` on a map reads a key; `[]` indexes a list or map.

## Function library

The preset macros, callable as `upper(s)` or `s.upper()`: `upper lower trim concat coalesce len sum min max round
format_ts hash join if eq`, with the signatures of `behaviors/compute.py`. Also `size(x)` (same as `len`) and `has(m.key)`
(true if the key is present). There are no other functions: no clock, no randomness, no IO, no regular expressions.

## Evaluation

- Parameters are bound by position to the declared arguments. A missing argument is bound to `null` only if the macro
  declares `accepts_missing`; otherwise the macro is not run (see `on_missing`).
- `&&`, `||` and `?:` evaluate lazily; an error in an unused branch is not raised.

## Limits (deterministic)

| Limit | Default | Platform maximum |
|---|---|---|
| Steps (one per node evaluated and per element a function processes) | 10,000 | 100,000 |
| Expression length (characters) | 2,000 | 10,000 |
| Nesting depth | 32 | 64 |
| Size of values built (bytes of canonical text, summed) | 64 KiB | 1 MiB |
| Size of inputs (bytes of canonical text, summed) | 256 KiB | 256 KiB |

A chain of operators (`a + b + c + ...`, `x.f().g()...`) nests as deep as it is long; an expression whose tree is more than
200 levels high is refused whatever the limits above. A registration may set lower values for its macro and is refused for higher ones. Exceeding a limit, a type error, division
by zero, integer overflow, or a result that does not conform to the declared type is a `COMPUTE_FAULT` naming the macro and the
reason (the limit's name, never a value).

## Fixture

`acme.label@1`: params `name`, `city`; expression `name + " (" + city + ")"`; `accepts_missing=false`.
