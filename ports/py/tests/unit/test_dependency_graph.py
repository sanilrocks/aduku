"""Feature 002, research R-5: the dependency graph of computed fields and its evaluation order."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.graph import find_cycles, order
from aduku.errors import SchemaError
from aduku.schema.loader import load_schema


def schema_with(fields: dict[str, str]):  # type: ignore[no-untyped-def]
    body = ", ".join(f'"{path}": {decl}' for path, decl in fields.items())
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_hook("h", 1, lambda inputs, timeout: "x")
    return load_schema(
        'schema::{id: "t", version: 1, tiers: [a, b], fields: {"/name": {type: string, merge: lww}, '
        '"/city": {type: string, merge: lww}, ' + body + "}}",
        behaviors,
    )


def computed(*deps: str) -> str:
    args = ", ".join(f'"{d}"' for d in deps)
    return f"{{type: string, computed: {{macro: concat, args: [{args}]}}}}"


A, B, C = "/a", "/b", "/c"
CHAIN = {A: computed("/name"), B: computed(A), C: computed(B, A)}


def names(schema) -> list[str]:  # type: ignore[no-untyped-def]
    return [spec.pattern for spec in order(schema)]


def test_dependencies_come_first() -> None:
    assert names(schema_with(CHAIN)) == [A, B, C]


def test_declaration_order_does_not_matter() -> None:
    forward = schema_with(CHAIN)
    reverse = schema_with(dict(reversed(list(CHAIN.items()))))
    assert names(forward) == names(reverse) == [A, B, C]


def test_independent_fields_are_ordered_by_path() -> None:
    schema = schema_with({"/z": computed("/name"), "/m": computed("/city"), "/b": computed("/name")})
    assert names(schema) == ["/b", "/m", "/z"]


def test_an_impure_field_depends_on_its_depends_list() -> None:
    impure = (
        '{type: string, computed: {impure: {hook: "h@1", depends: ["/a"], reason: "r", timeout: "1s", '
        'retry: {max: 1, backoff: "10ms"}, deadline: "3s", recompute_by: [a]}}}'
    )
    schema = schema_with({A: computed("/name"), "/z": impure})
    assert names(schema) == [A, "/z"]


def test_a_path_beneath_a_derived_field_depends_on_that_field() -> None:
    parent = "{type: struct, computed: {macro: coalesce, args: [lit::null]}}"
    schema = schema_with({"/p": parent, "/q": computed("/p/inner")})
    assert names(schema) == ["/p", "/q"]


def test_plain_fields_are_not_in_the_graph() -> None:
    assert names(schema_with({A: computed("/name")})) == [A]


@pytest.mark.parametrize(
    "fields, loop",
    [
        ({A: computed(A)}, [A, A]),
        ({A: computed(B), B: computed(A)}, [A, B, A]),
        ({A: computed(B), B: computed(C), C: computed(A)}, [A, B, C, A]),
    ],
)
def test_cycles_are_found_with_their_loop(fields: dict[str, str], loop: list[str]) -> None:
    body = ", ".join(f'"{path}": {decl}' for path, decl in fields.items())
    text = 'schema::{id: "t", version: 1, tiers: [a], fields: {"/name": {type: string, merge: lww}, ' + body + "}}"
    with pytest.raises(SchemaError) as caught:
        load_schema(text)
    message = next(p for p in caught.value.problems if p.startswith("DEPENDENCY_CYCLE"))
    assert " -> ".join(loop) in message


def test_find_cycles_on_a_valid_schema_is_empty() -> None:
    assert find_cycles(schema_with(CHAIN)) == []


def test_a_field_that_reads_a_struct_comes_after_the_computed_fields_inside_it() -> None:
    text = (
        'schema::{id: "t", version: 1, tiers: [a], fields: {"/order": {type: struct, merge: struct}, '
        '"/order/n": {type: string, merge: lww}, '
        '"/order/total": {type: string, computed: {macro: upper, args: ["/order/n"]}}, '
        '"/a_summary": {type: string, computed: {macro: hash, args: ["/order"]}}}}'
    )
    schema = load_schema(text, BehaviorRegistry.with_builtins())
    assert names(schema) == ["/order/total", "/a_summary"]  # not path order: the summary needs the total first


def test_a_computed_field_inside_a_struct_it_reads_does_not_depend_on_itself() -> None:
    text = (
        'schema::{id: "t", version: 1, tiers: [a], fields: {"/order": {type: struct, merge: struct}, '
        '"/order/n": {type: string, merge: lww}, '
        '"/order/summary": {type: string, computed: {macro: hash, args: ["/order"]}}}}'
    )
    assert names(load_schema(text, BehaviorRegistry.with_builtins())) == ["/order/summary"]


def test_a_very_long_chain_of_fields_is_checked_without_recursion() -> None:
    links = 1500
    fields = {f"/f{i:04d}": computed("/name" if i == 0 else f"/f{i - 1:04d}") for i in range(links)}
    assert len(names(schema_with(fields))) == links
    closing = {**fields, "/f0000": computed(f"/f{links - 1:04d}")}  # the first reads the last: one loop of all of them
    body = ", ".join(f'"{path}": {decl}' for path, decl in closing.items())
    text = 'schema::{id: "t", version: 1, tiers: [a], fields: {"/name": {type: string, merge: lww}, ' + body + "}}"
    with pytest.raises(SchemaError) as caught:
        load_schema(text, BehaviorRegistry.with_builtins())
    assert any(p.startswith("DEPENDENCY_CYCLE") for p in caught.value.problems)


# -- feature 003: the levels of deferred fields (FR-013, FR-014) ---------------------------------------------------


def deferred(*deps: str) -> str:
    listed = ", ".join(f'"{d}"' for d in deps)
    return f'{{type: string, deferred: {{hook: "h@1", depends: [{listed}], refresh: once, invalidate_by: [a]}}}}'


def level_names(schema) -> list[list[str]]:  # type: ignore[no-untyped-def]
    from aduku.engine.graph import deferred_levels

    return [[spec.pattern for spec in level] for level in deferred_levels(schema)]


def test_deferred_fields_are_levelled_by_their_deepest_deferred_dependency() -> None:
    fields = {
        "/d3": deferred("/d2", "/name"),
        "/d1": deferred("/name", A),
        "/d2": deferred("/d1"),
        "/d0": deferred("/city"),
        A: computed("/name"),
    }
    assert level_names(schema_with(fields)) == [["/d0", "/d1"], ["/d2"], ["/d3"]]


def test_the_levels_do_not_depend_on_declaration_order() -> None:
    fields = {"/x": deferred("/name"), "/y": deferred("/x"), "/z": deferred("/name")}
    forward = level_names(schema_with(fields))
    backward = level_names(schema_with(dict(reversed(list(fields.items())))))
    assert forward == backward == [["/x", "/z"], ["/y"]]


def test_a_deferred_field_that_reads_computed_and_impure_fields_is_on_level_zero() -> None:
    impure = (
        '{type: string, computed: {impure: {hook: "h@1", depends: ["/name"], reason: "r", timeout: "1s", '
        'retry: {max: 0}, deadline: "2s", recompute_by: [a]}}}'
    )
    fields = {A: computed("/name"), "/tax": impure, "/d": deferred(A, "/tax")}
    assert level_names(schema_with(fields)) == [["/d"]]


def test_a_schema_without_deferred_fields_has_no_levels() -> None:
    assert level_names(schema_with(CHAIN)) == []


def test_a_field_reading_a_struct_is_above_the_deferred_fields_inside_it() -> None:
    text = (
        'schema::{id: "t", version: 1, tiers: [a], fields: {"/order": {type: struct, merge: struct}, '
        '"/order/n": {type: string, merge: lww}, '
        f'"/order/rate": {deferred("/order/n")}, "/a_all": {deferred("/order")}}}}}'
    )
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_hook("h", 1, lambda inputs, timeout: "x")
    assert level_names(load_schema(text, behaviors)) == [["/order/rate"], ["/a_all"]]
