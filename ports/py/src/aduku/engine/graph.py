"""The dependency graph of derived fields (research R-5). Pure.

Nodes are the derived fields (computed, pure or impure, and deferred); an edge goes from a field to each derived field it
depends on, where a dependency on a path beneath a derived field counts as a dependency on that field, and a dependency
on a path above a derived field (a struct that holds it) counts as one too. `order` gives the
evaluation order of the computed fields, the same for any declaration order in the schema file.
"""

from __future__ import annotations

from aduku.ion_codec import Annotated, FrozenStruct
from aduku.paths import PathError, parse_path, parse_pattern
from aduku.schema.model import FieldSpec, Schema

__all__ = ["dependency_paths", "edges", "find_cycles", "order"]


def dependency_paths(spec: FieldSpec) -> tuple[str, ...]:
    """The paths a derived field reads, in declaration order."""
    if spec.computed is not None:
        return spec.computed.paths
    if spec.impure is not None:
        return spec.impure.depends
    raw = spec.derived_spec
    if isinstance(raw, Annotated):
        raw = raw.value
    if isinstance(raw, FrozenStruct) and isinstance(raw.get("depends"), tuple):
        return tuple(str(p) for p in raw["depends"] if isinstance(p, str))
    return ()


def edges(schema: Schema) -> dict[str, list[str]]:
    """For each derived field (by pattern), the derived fields it depends on, in path order and without repeats."""
    derived = {spec.pattern: spec for spec in schema.fields if spec.derived}
    where = {name: parse_pattern(name) for name in derived}
    found: dict[str, list[str]] = {name: [] for name in sorted(derived)}
    for name, spec in derived.items():
        for path_text in dependency_paths(spec):
            try:
                path = parse_path(path_text)
                above = schema.derived_field_at_or_above(path)
            except PathError:
                continue
            targets = [above.pattern] if above is not None else []
            # a struct read whole holds every derived field beneath it, but a field is not its own input
            targets += sorted(
                other
                for other, at in where.items()
                if other != name and len(at) > len(path) and at[: len(path)] == path
            )
            for target in targets:
                if target in derived and target not in found[name]:
                    found[name].append(target)
    return found


def find_cycles(schema: Schema) -> list[list[str]]:
    """Every loop among derived fields, each as `[a, b, ..., a]`, once per set of fields, in a stable order."""
    graph = edges(schema)
    reported: set[frozenset[str]] = set()
    loops: list[list[str]] = []
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished

    for start in graph:  # depth first without recursion: a long chain of fields must not exhaust the stack
        if state.get(start) is not None:
            continue
        state[start] = 1
        trail = [start]
        todo = [iter(graph[start])]
        while todo:
            name = trail[-1]
            nxt = next(todo[-1], None)
            if nxt is None:
                state[name] = 2
                trail.pop()
                todo.pop()
            elif state.get(nxt) == 1:
                loop = [*trail[trail.index(nxt) :], nxt] if nxt in trail else [name, nxt]
                if frozenset(loop) not in reported:
                    reported.add(frozenset(loop))
                    loops.append(loop)
            elif state.get(nxt) is None:
                state[nxt] = 1
                trail.append(nxt)
                todo.append(iter(graph[nxt]))
    return loops


def order(schema: Schema) -> tuple[FieldSpec, ...]:
    """The computed fields in evaluation order: dependencies first, ties by path in code-point order.

    A schema with a cycle is refused at load, so none is expected here; fields left in a cycle are omitted.
    """
    computed = {spec.pattern: spec for spec in schema.fields if spec.derived == "computed"}
    graph = edges(schema)
    waiting = {name: {dep for dep in graph[name] if dep in computed} for name in computed}
    result: list[FieldSpec] = []
    while waiting:
        ready = sorted(name for name, deps in waiting.items() if not deps)
        if not ready:
            break
        for name in ready:
            result.append(computed[name])
            del waiting[name]
        for deps in waiting.values():
            deps.difference_update(ready)
    return tuple(result)
