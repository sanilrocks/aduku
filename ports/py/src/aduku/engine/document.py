"""Assemble the nested document from path values. Pure."""

from __future__ import annotations

from typing import Any

from aduku.behaviors.merge import SchemaMergeContext
from aduku.ion_codec import FrozenStruct
from aduku.paths import Path, Segment

__all__ = ["assemble"]


def _key(segment: Segment) -> str:
    return segment.name if segment.key is None else f"{segment.name}[{segment.key}]"


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
