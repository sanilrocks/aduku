"""Feature 006 (research R-1): a registry bound to a shared schema state reads and writes through it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.model import HistoryRecord, StateChange
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry, SchemaStateSnapshot, StateConflict
from tests.schema_helpers import field_text, versions_text

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class FakeState:
    """An in-memory SchemaState that counts its reads."""

    def __init__(self) -> None:
        self.gen = 0
        self.act: int | None = None
        self.pins: dict[str, int] = {}
        self.served: dict[int, tuple[str, str]] = {}
        self.log: list[StateChange] = []
        self.generation_reads = 0
        self.full_reads = 0

    def generation(self) -> int:
        self.generation_reads += 1
        return self.gen

    def read(self) -> SchemaStateSnapshot:
        self.full_reads += 1
        return SchemaStateSnapshot(self.gen, self.act, dict(self.pins), dict(self.served))

    def apply(self, change: StateChange, *, expect_generation: int | None, check=None) -> int:  # type: ignore[no-untyped-def]
        if check is not None:
            check()
        if expect_generation is not None and expect_generation != self.gen:
            raise StateConflict("stale")
        for version, fingerprint, parts in change.served:
            self.served.setdefault(version, (fingerprint, parts))
        self.pins.update(change.pins)
        if change.pin_doc is not None:
            if change.pin_version is None:
                self.pins.pop(change.pin_doc, None)
            else:
                self.pins[change.pin_doc] = change.pin_version
        if change.active is not None:
            self.act = change.active
        self.gen += 1
        self.log.append(change)
        return self.gen

    def history(self, *, limit: int | None = None, doc: str | None = None) -> list[HistoryRecord]:
        return []


def registry(*versions: int) -> SchemaRegistry:
    behaviors = BehaviorRegistry.with_builtins()
    texts = versions_text(
        field_text(), field_text(', "/nick": {type: string, merge: lww}'), field_text(', "/z": {type: int, merge: max}')
    )
    result = SchemaRegistry()
    for text in texts:
        schema = load_schema(text, behaviors)
        if not versions or schema.version in versions:
            result.put(schema)
    return result


def bound(state: FakeState, *versions: int, active: int | None = None) -> SchemaRegistry:
    result = registry(*versions)
    if active is not None:
        result.activate(active)
    result.bind(state, lambda: NOW)
    return result


def test_an_unbound_registry_behaves_as_before() -> None:
    plain = registry()
    plain.activate(2)
    plain.pin("d", 1)
    assert plain.active().version == 2 and plain.version_for("d").version == 1 and plain.version_for("x").version == 2


def test_binding_an_empty_store_adopts_the_registrys_active_version_and_pins_as_a_bootstrap() -> None:
    state = FakeState()
    local = registry()
    local.activate(2)
    local.pin("d", 1)
    local.bind(state, lambda: NOW)
    assert (state.gen, state.act, state.pins) == (1, 2, {"d": 1})
    assert state.log[0].action == "bootstrap" and state.log[0].operator == "bootstrap"
    assert sorted(version for version, _, _ in state.log[0].served) == [1, 2]


def test_binding_an_empty_store_with_nothing_active_writes_nothing() -> None:
    state = FakeState()
    registry().bind(state, lambda: NOW)
    assert state.gen == 0 and state.log == []


def test_when_the_store_holds_state_the_stored_state_wins() -> None:
    state = FakeState()
    first = bound(state, active=1)
    first.pin("d", 1)
    second = registry()
    second.activate(3)  # this process would have chosen version 3
    second.bind(state, lambda: NOW)
    assert second.active().version == 1 and second.version_for("d").version == 1 and state.gen == 2


def test_activation_writes_through_with_operator_class_and_note() -> None:
    state = FakeState()
    local = bound(state, active=1)
    local.activate(2, operator="alice", change_class="visible", note="{n:1}")
    change = state.log[-1]
    assert (state.act, change.action, change.operator, change.change_class, change.note) == (
        2,
        "activate",
        "alice",
        "visible",
        "{n:1}",
    )
    assert (change.from_version, change.to_version, change.at) == (1, 2, NOW)
    local.activate(1, operator="alice", rollback=True)
    assert state.log[-1].action == "rollback" and state.act == 1


def test_a_version_is_recorded_as_served_once() -> None:
    state = FakeState()
    local = bound(state, active=1)
    local.activate(2, operator="a")
    local.activate(1, operator="a", rollback=True)
    assert [sorted(v for v, _, _ in c.served) for c in state.log] == [[1], [2], []]


def test_pin_and_unpin_write_through_and_record_the_document() -> None:
    state = FakeState()
    local = bound(state, active=1)
    local.pin("d", 2, operator="bob")
    assert (
        state.pins == {"d": 2}
        and state.log[-1].action == "pin"
        and state.log[-1].doc == "d"
        and state.log[-1].operator == "bob"
    )
    local.pin("d", None, operator="bob")
    assert state.pins == {} and state.log[-1].action == "unpin"


def test_another_process_changes_are_seen_on_the_next_call() -> None:
    state = FakeState()
    mine = bound(state, active=1)
    other = bound(state)
    other.activate(2, operator="x")
    other.pin("d", 3, operator="x")
    assert mine.active().version == 2 and mine.version_for("d").version == 3 and mine.version_for("e").version == 2


def test_the_state_is_reloaded_only_when_the_generation_changed() -> None:
    state = FakeState()
    local = bound(state, active=1)
    reads = state.full_reads
    for _ in range(5):
        local.active()
        local.version_for("d")
    assert state.full_reads == reads  # unchanged generation, no reload
    assert state.generation_reads >= 10
    state.gen += 1
    state.act = 2
    assert local.active().version == 2 and state.full_reads == reads + 1


def test_a_stored_active_version_the_registry_lacks_is_inactive_and_named() -> None:
    state = FakeState()
    bound(state, active=3)
    thin = bound(state, 1, 2)
    with pytest.raises(RejectedError) as caught:
        thin.active()
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE and "3" in str(caught.value)
    with pytest.raises(RejectedError):
        thin.version_for("any")


def test_a_pinned_version_the_registry_lacks_is_inactive_for_that_document_only() -> None:
    state = FakeState()
    full = bound(state, active=1)
    full.pin("d", 3, operator="a")
    thin = bound(state, 1, 2)
    assert thin.version_for("other").version == 1
    with pytest.raises(RejectedError):
        thin.version_for("d")


def test_a_stale_generation_on_activation_is_refused_as_schema_state_changed() -> None:
    state = FakeState()
    local = bound(state, active=1)
    with pytest.raises(SchemaError) as caught:
        local.activate(2, operator="a", expect_generation=state.gen - 1)
    assert caught.value.codes == ("SCHEMA_STATE_CHANGED",) and state.act == 1


def test_a_version_changed_after_it_was_served_is_refused_at_bind_naming_the_fields() -> None:
    state = FakeState()
    bound(state, active=1)
    edited = SchemaRegistry()
    behaviors = BehaviorRegistry.with_builtins()
    edited.put(load_schema(versions_text(field_text(', "/extra": {type: int, merge: max}'))[0], behaviors))
    with pytest.raises(SchemaError) as caught:
        edited.bind(state, lambda: NOW)
    assert caught.value.codes == ("SCHEMA_VERSION_CHANGED",) and "/extra" in str(caught.value)


def test_put_refuses_changed_content_for_a_served_version_but_not_for_one_never_served() -> None:
    state = FakeState()
    local = bound(state, 1, 2, active=1)
    behaviors = BehaviorRegistry.with_builtins()
    changed_served = load_schema(versions_text(field_text(', "/extra": {type: int, merge: max}'))[0], behaviors)
    fresh = SchemaRegistry()
    fresh.bind(state, lambda: NOW)
    with pytest.raises(SchemaError) as caught:
        fresh.put(changed_served)
    assert caught.value.codes == ("SCHEMA_VERSION_CHANGED",)
    # version 2 was registered but never served: another process may still change it
    other = SchemaRegistry()
    other.bind(state, lambda: NOW)
    other.put(
        load_schema(versions_text(field_text(), field_text(', "/different": {type: int, merge: max}'))[1], behaviors)
    )
    assert local.versions() == (1, 2)


def test_the_low_level_activation_records_the_class_of_the_change() -> None:
    state = FakeState()
    local = bound(state, active=1)
    local.activate(2)
    assert state.log[-1].change_class == "compatible"  # version 2 only adds /nick
    local.activate(1, rollback=True)
    assert state.log[-1].change_class == "visible"
    assert state.log[0].change_class is None  # the first activation replaces nothing
