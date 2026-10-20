"""Feature 006 (R-4): the fold skips an entry written under another version whose value no longer conforms to the type in force."""

from aduku.behaviors.registry import BehaviorRegistry
from aduku.engine.derive import derive, prepare
from aduku.ion_codec import FrozenStruct
from aduku.schema.loader import load_schema
from tests.helpers import make_entry

BEHAVIORS = BehaviorRegistry.with_builtins()


def schema(fields: str, version: int):  # type: ignore[no-untyped-def]
    return load_schema(
        f'schema::{{id: "t", version: {version}, tiers: [backfill, user, admin], fields: {{{fields}}}}}', BEHAVIORS
    )


def entry(seq: int, path: str, value: object, version: int, tier: str = "user"):  # noqa: ANN201
    return make_entry("d", seq, request_id=f"r{seq}", tier=tier, path=path, value=value, schema_version=version)


V1 = schema('"/age": {type: int, merge: max}, "/tags": {type: "list<string>", merge: union}', 1)
V2 = schema('"/age": {type: string, merge: lww}, "/tags": {type: "list<string>", merge: union}', 2)


def test_an_entry_of_another_version_that_no_longer_conforms_is_skipped() -> None:
    entries = [entry(1, "/age", 30, 1), entry(2, "/age", "thirty", 2)]
    result = derive(V2, BEHAVIORS, entries, prepare(V2, BEHAVIORS))
    assert result.state.skipped == (1,) and result.document["age"] == "thirty"
    alone = derive(V2, BEHAVIORS, entries[:1], prepare(V2, BEHAVIORS))
    assert "age" not in alone.document and alone.state.skipped == (1,)


def test_an_entry_written_under_the_version_in_force_is_never_checked() -> None:
    result = derive(V2, BEHAVIORS, [entry(1, "/age", 30, 2)], prepare(V2, BEHAVIORS))
    assert result.state.skipped == () and result.document["age"] == 30


def test_the_same_ledger_under_the_version_that_wrote_it_skips_nothing() -> None:
    entries = [entry(1, "/age", 30, 1), entry(2, "/tags", ("a",), 1)]
    assert derive(V1, BEHAVIORS, entries, prepare(V1, BEHAVIORS)).state.skipped == ()


def test_a_value_that_still_conforms_is_kept() -> None:
    entries = [entry(1, "/tags", ("a", "b"), 1)]
    result = derive(V2, BEHAVIORS, entries, prepare(V2, BEHAVIORS))
    assert result.state.skipped == () and result.document["tags"] == ("a", "b")


def test_a_struct_write_is_checked_per_child_and_skipped_whole() -> None:
    v1 = schema(
        '"/a": {type: struct, merge: struct}, "/a/x": {type: int, merge: max}, "/a/y": {type: int, merge: max}', 1
    )
    v2 = schema(
        '"/a": {type: struct, merge: struct}, "/a/x": {type: int, merge: max}, "/a/y": {type: string, merge: lww}', 2
    )
    entries = [entry(1, "/a", FrozenStruct({"x": 1, "y": 2}), 1), entry(2, "/a", FrozenStruct({"x": 3}), 1)]
    result = derive(v2, BEHAVIORS, entries, prepare(v2, BEHAVIORS))
    assert result.state.skipped == (1,) and result.document["a"]["x"] == 3
    assert derive(v1, BEHAVIORS, entries, prepare(v1, BEHAVIORS)).state.skipped == ()
