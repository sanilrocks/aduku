"""Principle V: every merge behavior declares its laws and they hold, including with absent and tombstone."""

from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aduku.behaviors.merge import MergeContext, Recreated, step, unwrap
from aduku.behaviors.registry import Behavior, BehaviorRegistry
from aduku.ion_codec import ABSENT, TOMBSTONE, FrozenStruct, to_canonical

BUILTINS = BehaviorRegistry.with_builtins()
LWW = BUILTINS.get("lww", 1)
assert LWW is not None


class FixedContext(MergeContext):
    """Children of a struct merge with one rule."""

    def __init__(self, rule: Behavior) -> None:
        self.rule = rule

    def step(self, path: tuple, old: Any, new: Any) -> Any:  # type: ignore[type-arg, override]
        return step(self.rule, path, old, new, self)


STATES = st.sampled_from([ABSENT, TOMBSTONE])
ints = st.integers(-5, 5)
words = st.text(alphabet="abc", max_size=2)
lists = st.lists(st.one_of(ints, words), max_size=4).map(tuple)
structs = st.dictionaries(st.sampled_from(["a", "b", "c"]), ints, max_size=3).map(FrozenStruct)


def with_states(values: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    """Plain values, absent, a tombstone, and values re-created after a tombstone (what a squashed layer can hold)."""
    return st.one_of(STATES, values, values.map(Recreated))


DOMAINS: dict[str, st.SearchStrategy[Any]] = {
    "lww": with_states(st.one_of(ints, words, lists, structs)),
    "max": with_states(ints),
    "min": with_states(ints),
    "union": with_states(lists),
    "struct": with_states(structs),
}
NORMALIZED = {
    "lww": st.one_of(ints, words, lists, structs),
    "max": ints,
    "min": ints,
    "union": st.lists(st.one_of(ints, words), max_size=4, unique_by=lambda v: to_canonical(v)).map(tuple),
    "struct": structs,
}


def names() -> list[str]:
    return sorted(b.name for b in BUILTINS.all() if b.kind == "merge")


def behavior(name: str) -> Behavior:
    found = BUILTINS.get(name, 1)
    assert found is not None
    return found


def fold(name: str) -> Callable[[Any, Any], Any]:
    rule = behavior(name)
    ctx = FixedContext(LWW)
    return lambda old, new: step(rule, (), old, new, ctx)


def test_the_built_in_rules_are_registered() -> None:
    assert names() == ["lww", "max", "min", "struct", "union"]


@pytest.mark.parametrize("name", ["lww", "max", "min", "union", "struct"])
def test_every_built_in_declares_associativity(name: str) -> None:
    assert behavior(name).laws.associative


@pytest.mark.parametrize("name", ["lww", "max", "min", "union", "struct"])
def test_associative(name: str) -> None:
    combine = fold(name)

    @given(DOMAINS[name], DOMAINS[name], DOMAINS[name])
    def check(a: Any, b: Any, c: Any) -> None:
        assert combine(combine(a, b), c) == combine(a, combine(b, c))

    check()


@pytest.mark.parametrize("name", ["lww", "max", "min", "union", "struct"])
def test_absent_is_the_identity(name: str) -> None:
    combine = fold(name)

    @given(NORMALIZED[name])
    def check(x: Any) -> None:
        assert combine(ABSENT, x) == x
        assert combine(x, ABSENT) == x

    check()
    assert combine(ABSENT, ABSENT) is ABSENT
    assert combine(ABSENT, TOMBSTONE) is TOMBSTONE


COUNTEREXAMPLES = {
    "lww": (1, 2),
    "union": ((1,), (2,)),
    "struct": (FrozenStruct({"a": 1}), FrozenStruct({"a": 2})),
}


@pytest.mark.parametrize("name", ["lww", "max", "min", "union", "struct"])
def test_declared_commutativity(name: str) -> None:
    combine = fold(name)
    if behavior(name).laws.commutative:

        @given(NORMALIZED[name], NORMALIZED[name])
        def check(a: Any, b: Any) -> None:
            assert combine(a, b) == combine(b, a)

        check()
    else:  # not declared, so it must really be order-sensitive
        a, b = COUNTEREXAMPLES[name]
        assert combine(a, b) != combine(b, a)


@pytest.mark.parametrize("name", ["lww", "max", "min", "union", "struct"])
def test_declared_idempotence(name: str) -> None:
    combine = fold(name)
    if not behavior(name).laws.idempotent:
        assert name == "struct"  # idempotence of a struct merge depends on its children's rules
        return

    @given(NORMALIZED[name])
    def check(x: Any) -> None:
        assert combine(x, x) == x

    check()


@given(STATES | ints, ints)
def test_a_tombstone_beats_everything_before_it(old: Any, value: int) -> None:
    for name in ("lww", "max", "min"):
        assert fold(name)(old, TOMBSTONE) is TOMBSTONE
        recreated = fold(name)(TOMBSTONE, value)  # re-created by a later write, and the reset is remembered
        assert recreated == Recreated(value) and unwrap(recreated) == value


@given(ints, ints, ints)
def test_squashing_a_delete_then_a_write_equals_the_flat_fold(before: int, after: int, other: int) -> None:
    """The case that needs the reset marker: max(before, [delete, after]) is `after`, not max(before, after)."""
    combine = fold("max")
    flat = combine(combine(before, TOMBSTONE), after)
    squashed = combine(before, combine(TOMBSTONE, after))
    assert flat == squashed and unwrap(flat) == after
    assert unwrap(combine(combine(before, TOMBSTONE), after)) == after
    assert combine(combine(other, combine(before, TOMBSTONE)), after) == combine(
        other, combine(combine(before, TOMBSTONE), after)
    )


def test_commutativity_and_idempotence_are_declared_where_they_hold() -> None:
    assert behavior("max").laws.commutative and behavior("min").laws.commutative
    assert not behavior("lww").laws.commutative and not behavior("union").laws.commutative
    assert all(behavior(n).laws.idempotent for n in ("lww", "max", "min", "union"))
