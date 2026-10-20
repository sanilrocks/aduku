# Contract: The Rule Library

Each rule is a pure function of its subject and arguments. A field rule is given the value at one path; a document rule the merged
and computed document. A violation has the rule's name, the path scope and a message that states the constraint and never the value.

| Rule | Place | Arguments | Violation when | Message (constraint only) |
|---|---|---|---|---|
| `non_empty` | field | none | text, list or struct of length 0 | `must not be empty` |
| `range` | field | `min`, `max` (numbers, each optional) | a number below `min` or above `max` | `must be at least N` / `must be at most N` |
| `max_len` | field | `n` (whole number, 0 or more) | text or list longer than `n` | `must have at most N items or characters` |
| `min_len` | field | `n` (whole number, 0 or more) | text or list shorter than `n` | `must have at least N items or characters` |
| `pattern` | field | `regex` | text that does not match the whole of `regex` | `must match the pattern` |
| `one_of` | field | `values` (one or more scalars) | a value whose canonical form is not listed | `must be one of the allowed values` |
| `unique_items` | field | none | a list with two items of the same canonical form | `must not repeat an item` |
| `all_or_none` | document | `paths` (two or more) | some but not all of the paths have a value | `these fields go together: all or none` |
| `at_least_one` | document | `paths` (one or more) | none of the paths has a value | `at least one of these fields is needed` |
| `at_most_one` | document | `paths` (two or more) | more than one of the paths has a value | `at most one of these fields may be given` |
| `ordered` | document | `first`, `second` | both have a value and `first > second` (`must not be after the other field`), or they cannot be ordered (`these fields cannot be compared`) | as stated |

- A field rule reports nothing for a kind of value it does not apply to. Booleans are not numbers.
- A `paths` list may not name a path twice (refused at load). A path has a value if it is present and not null; a null is no value for a field rule too, so a field rule is not run for it. A document rule over paths with no value reports only what its row says.
- Document rules report with the rule's name and the first path as the scope.
- `ordered`: numbers compare by value, text by code point, timestamps by instant; equal values are in order; any other pair
  is not comparable.
- `unique_items` and `one_of` compare by canonical form: `1` and `1.0` differ; two equal structs are the same.

## The `pattern` subset

```
pattern   := branch ('|' branch)*
branch    := piece*
piece     := atom ('*' | '+' | '?' | '{' m '}' | '{' m ',' '}' | '{' m ',' n '}')?
atom      := char | '.' | class | '(' pattern ')'
class     := '[' '^'? (member | member '-' member)+ ']'     ; inside a class only \ [ ] are special: write them as \\ \[ \]
member    := any character except \ [ ]  ; or '\' followed by any one character
char      := any character except \ . [ ] ( ) | * + ? { }  ; or '\' followed by any one character (a literal)
```

The whole value must match (there are no anchors). `.` is any character except a newline. Limits: pattern length at most 500,
repeat counts at most 255, at most 5,000 matcher states; a pattern that breaks a limit or the grammar is refused at load.
Matching time is proportional to (length of value) x (matcher states).

## Registering a custom rule

```python
behaviors.register_validate(
    "acme.tax_id", 1, check,
    args=[ArgSpec("country", "text", required=True), ArgSpec("strict", "bool", required=False)],
    applies="field",
)
```

`check(subject) -> list[Violation]` is pure. `subject.args` holds the declared arguments; `subject.value` the value (field rule) or
`subject.document` the document (document rule). A rule that raises, or returns anything but a list of `Violation`, is a rule
fault; the fault names the rule and the kind of error it raised (for example `it raised RuntimeError`), never what the error said.
