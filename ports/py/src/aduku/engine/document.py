"""Assemble the nested document from path values. Pure."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aduku.behaviors.merge import SchemaMergeContext
from aduku.ion_codec import ABSENT, Annotated, FrozenStruct
from aduku.paths import Path, Segment

__all__ = ["assemble", "key_of", "strip_computed", "value_at", "with_value"]


def _key(segment: Segment) -> str:
    return segment.name if segment.key is None else f"{segment.name}[{segment.key}]"


key_of = _key


def value_at(document: FrozenStruct, path: Sequence[Segment]) -> Any:
    """The value at a concrete path of an assembled document, or ABSENT."""
    node: Any = document
    for segment in path:
        key = _key(segment)
        if not isinstance(node, FrozenStruct) or key not in node:
            return ABSENT
        node = node[key]
    return node


def strip_computed(value: Any) -> Any:
    """The document without its `computed::` annotations, as validators and macros see it."""
    if isinstance(value, Annotated) and value.annotations == ("computed",):
        return strip_computed(value.value)
    if isinstance(value, FrozenStruct):
        changed = {k: strip_computed(v) for k, v in value.items()}
        return value if all(changed[k] is value[k] for k in value) else FrozenStruct(changed)
    return value


def with_value(document: FrozenStruct, path: Sequence[Segment], value: Any) -> FrozenStruct | None:
    """A copy of `document` with `value` at `path`, creating structs on the way; None if an ancestor is not a struct."""
    if not path:
        return None
    key = _key(path[0])
    if len(path) == 1:
        return FrozenStruct({**document, key: value})
    child = document.get(key, FrozenStruct())
    if not isinstance(child, FrozenStruct):
        return None
    inner = with_value(child, path[1:], value)
    return None if inner is None else FrozenStruct({**document, key: inner})


def assemble(values: dict[Path, Any], ctx: SchemaMergeContext, dropped: list[Path] | None = None) -> FrozenStruct:
    """Nest `values`. Shallower paths go first, so a value that is not a container wins over anything beneath it;
    a path left out for that reason is appended to `dropped`."""
    root: dict[str, Any] = {}
    for path in sorted(values, key=lambda p: (len(p), tuple((s.name, s.key or "") for s in p))):
        if not path:
            continue
        node = root
        for segment in path[:-1]:
            child = node.setdefault(_key(segment), {})
            if not isinstance(child, dict):
                if dropped is not None:
                    dropped.append(path)
                break  # an atomic value sits above this path: it wins, this write is not part of the document
            node = child
        else:
            value = values[path]
            last = _key(path[-1])
            if isinstance(value, FrozenStruct) and ctx.rule_for(path).field_wise:
                node.setdefault(last, {})  # a container: its children fill it in
            else:
                node[last] = value
    return _freeze(root)


def _freeze(node: dict[str, Any]) -> FrozenStruct:
    return FrozenStruct({key: _freeze(value) if isinstance(value, dict) else value for key, value in node.items()})
