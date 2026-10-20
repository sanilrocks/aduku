"""The canonical text of a layer (feature 005, research R-2). Pure.

A layer maps a path to a fold state: a plain value, TOMBSTONE, Recreated(value) or ABSENT. It is saved as a canonical Ion list
of structs sorted by path text, so that every port writes the same bytes for the same layer:

    {p: "/name", s: "Ann"}            a plain value
    {p: "/name", t: true}             a tombstone
    {p: "/name", r: true, s: "Ann"}   a value written after a tombstone (the reset mark is kept)
    {p: "/name", a: true}             absent

Structs and flags rather than annotations, so that a contributed value that happens to carry an annotation can never be read
as a reset mark.
"""

from __future__ import annotations

from typing import Any

from aduku.behaviors.merge import Recreated
from aduku.ion_codec import ABSENT, TOMBSTONE, FrozenStruct, IonError, parse, to_canonical
from aduku.paths import Path, PathError, format_path, parse_path

__all__ = ["Layer", "decode_layer", "encode_layer"]

Layer = dict[Path, Any]  # a path -> fold state (a plain value, TOMBSTONE, Recreated(value) or ABSENT)


def encode_layer(layer: Layer) -> str:
    """Canonical text of `layer`, sorted by path text."""
    rows: list[tuple[str, FrozenStruct]] = []
    for path, state in layer.items():
        text = format_path(path)
        fields: dict[str, Any] = {"p": text}
        if state is TOMBSTONE:
            fields["t"] = True
        elif state is ABSENT:
            fields["a"] = True
        elif isinstance(state, Recreated):
            fields["r"] = True
            fields["s"] = state.value
        else:
            fields["s"] = state
        rows.append((text, FrozenStruct(fields)))
    rows.sort(key=lambda row: row[0])
    return to_canonical(tuple(row for _, row in rows))


def decode_layer(text: str) -> Layer:
    """The layer a text stands for. Raises IonError for text that is not a layer."""
    parsed = parse(text)
    if not isinstance(parsed, tuple):
        raise IonError("a layer is a list")
    layer: Layer = {}
    for row in parsed:
        if not isinstance(row, FrozenStruct) or not isinstance(row.get("p"), str):
            raise IonError("a layer row is a struct with a path")
        try:
            path = parse_path(row["p"])
        except PathError as error:
            raise IonError(f"a layer row has a bad path: {error}") from error
        if row.get("t") is True:
            layer[path] = TOMBSTONE
        elif row.get("a") is True:
            layer[path] = ABSENT
        elif row.get("r") is True:
            layer[path] = Recreated(row.get("s"))
        else:
            layer[path] = row.get("s")
    return layer
