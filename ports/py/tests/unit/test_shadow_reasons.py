"""Feature 006 (FR-018, R-4): why an entry is left out of a document. Pure."""

from aduku.behaviors.merge import MergeTypeError
from aduku.behaviors.registry import BehaviorRegistry, Laws
from aduku.engine.derive import derive, prepare
from aduku.engine.shadow import shadow_reasons
from aduku.ion_codec import FrozenStruct
from aduku.schema.loader import load_schema
from tests.helpers import make_entry

BEHAVIORS = BehaviorRegistry.with_builtins()


def _picky(path: object, old: object, new: object, ctx: object) -> object:
    if new == "bad":
        raise MergeTypeError("this value cannot be merged here")
    return new


BEHAVIORS.register_merge("t.picky", 1, _picky, Laws(associative=True))


def schema(fields: str, version: int = 2):  # type: ignore[no-untyped-def]
    return load_schema(
        f'schema::{{id: "t", version: {version}, tiers: [backfill, user, admin], fields: {{{fields}}}}}', BEHAVIORS
    )


def entry(seq: int, path: str, value: object, tier: str = "user", version: int = 1, **kw: object):  # noqa: ANN201
    return make_entry("d", seq, request_id=f"r{seq}", tier=tier, path=path, value=value, schema_version=version, **kw)  # type: ignore[arg-type]


def reasons(sch, entries):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN201
    ready = prepare(sch, BEHAVIORS)
    folded = derive(sch, BEHAVIORS, entries, ready)
    return folded, [
        (r.seq, r.reason, r.detail) for r in shadow_reasons(sch, BEHAVIORS, ready, entries, folded.state.skipped)
    ]


NAME = '"/name": {type: string, merge: lww}'


def test_a_tier_the_schema_lacks() -> None:
    _, found = reasons(schema(NAME), [entry(1, "/name", "a"), entry(2, "/name", "b", tier="ghost")])
    assert found == [(2, "tier_not_in_schema", "ghost")]


def test_a_field_that_became_computed_or_deferred() -> None:
    sch = schema(NAME + ', "/shout": {type: string, computed: {macro: upper, args: ["/name"]}}')
    _, found = reasons(sch, [entry(1, "/name", "a"), entry(2, "/shout", "stale")])
    assert found == [(2, "now_derived", "computed /shout")]


def test_a_struct_written_over_a_field_that_became_derived_is_one_reason_for_the_whole_entry() -> None:
    sch = schema(
        '"/addr": {type: struct, merge: struct}, "/addr/city": {type: string, merge: lww},'
        ' "/addr/loud": {type: string, computed: {macro: upper, args: ["/addr/city"]}}'
    )
    _, found = reasons(sch, [entry(1, "/addr", FrozenStruct({"city": "x", "loud": "Y"}))])
    assert found == [(1, "now_derived", "computed /addr/loud")]


def test_a_value_that_no_longer_conforms_to_the_type() -> None:
    sch = schema(NAME + ', "/age": {type: string, merge: lww}')
    folded, found = reasons(sch, [entry(1, "/name", "a"), entry(2, "/age", 7), entry(3, "/age", "seven")])
    assert found == [(2, "no_longer_conforms", "string")] and folded.document["age"] == "seven"


def test_a_parameterized_type_is_checked_as_a_whole() -> None:
    sch = schema('"/tags": {type: "list<string>", merge: union}')
    _, found = reasons(sch, [entry(1, "/tags", ("a", 2)), entry(2, "/tags", ("a", "b"))])
    assert found == [(1, "no_longer_conforms", "list<string>")]


def test_an_entry_written_under_the_version_in_force_is_never_checked() -> None:
    sch = schema('"/age": {type: string, merge: lww}')
    folded, found = reasons(sch, [entry(1, "/age", 7, version=2)])
    assert found == [] and folded.document["age"] == 7  # admission checked it; the fold does not check it again


def test_a_struct_write_is_checked_child_by_child() -> None:
    sch = schema(
        '"/addr": {type: struct, merge: struct}, "/addr/city": {type: string, merge: lww}, "/addr/n": {type: int, merge: max}'
    )
    _, found = reasons(sch, [entry(1, "/addr", FrozenStruct({"city": 5, "n": 1}))])
    assert found == [(1, "no_longer_conforms", "string")]


def test_values_the_rule_cannot_merge() -> None:
    sch = schema('"/m": {merge: "t.picky@1"}')
    _, found = reasons(sch, [entry(1, "/m", 1, version=2), entry(2, "/m", "bad", version=2)])
    assert found == [(2, "cannot_be_merged", "t.picky")]


def test_the_checks_run_in_a_fixed_order_and_an_entry_gets_one_reason() -> None:
    sch = schema(NAME + ', "/shout": {type: string, computed: {macro: upper, args: ["/name"]}}')
    _, found = reasons(sch, [entry(1, "/shout", 5, tier="ghost")])
    assert found == [(1, "tier_not_in_schema", "ghost")]  # the tier first, then derived, then type, then merge


def test_nothing_is_shadowed_under_the_version_that_wrote_the_entries() -> None:
    sch = schema(NAME, version=1)
    folded, found = reasons(sch, [entry(1, "/name", "a"), entry(2, "/name", "b")])
    assert found == [] and folded.state.skipped == ()


def test_only_the_skipped_seqs_get_a_reason() -> None:
    sch = schema(NAME)
    ready = prepare(sch, BEHAVIORS)
    entries = [entry(1, "/name", "a"), entry(2, "/name", 5)]
    assert [r.seq for r in shadow_reasons(sch, BEHAVIORS, ready, entries, ())] == []
    assert [r.seq for r in shadow_reasons(sch, BEHAVIORS, ready, entries, (2,))] == [2]
