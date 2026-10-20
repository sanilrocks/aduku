"""Feature 006 (US1): the active version and the pins are shared through the store; versions stay what they were served as."""

from __future__ import annotations

import pytest

from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.model import Identity
from aduku.storage.base import Storage
from tests.schema_helpers import Env, field_text, versions_text
from tests.service_helpers import put

USER = Identity("alice", ("user",))


def texts() -> list[str]:
    return versions_text(
        field_text(), field_text(', "/nick": {type: string, merge: lww}'), field_text(', "/z": {type: int, merge: max}')
    )


def test_two_services_on_one_storage_see_each_others_activation_and_pin(backend: Storage) -> None:
    env = Env(backend, *texts())
    first = env.service(active=1)
    second = env.service(active=1)
    first.schemas.activate(2, operator="alice")
    assert second.schemas.active().version == 2
    second.schemas.pin("d", 3, operator="bob")
    assert first.schemas.version_for("d").version == 3 and first.schemas.version_for("e").version == 2


def test_a_second_process_with_a_different_choice_is_brought_into_line_with_the_store(backend: Storage) -> None:
    env = Env(backend, *texts())
    env.service(active=1)
    late = env.service(active=3)  # built later, would have chosen 3; the store says 1
    assert late.schemas.active().version == 1


def test_a_restart_with_identical_content_is_accepted_and_serves_the_same(backend: Storage) -> None:
    env = Env(backend, *texts())
    first = env.service(active=2)
    first.append("d", [put("/name", "A")], contributor=USER)
    again = env.service(active=2)
    assert again.get("d").document == first.get("d").document and again.schemas.active().version == 2


def test_a_restart_with_changed_content_under_a_served_number_is_refused_naming_the_fields(backend: Storage) -> None:
    env = Env(backend, *texts())
    env.service(active=1)
    edited = Env(
        backend,
        *versions_text(field_text("", base='"/name": {type: string, merge: lww}, "/age": {type: int, merge: min}')),
    )
    with pytest.raises(SchemaError) as caught:
        edited.service(active=1)
    assert (
        caught.value.codes == ("SCHEMA_VERSION_CHANGED",)
        and "/age" in str(caught.value)
        and "/name" not in str(caught.value)
    )


def test_a_registered_but_never_activated_version_can_still_be_changed(backend: Storage) -> None:
    env = Env(backend, *texts())
    env.service(active=1)  # versions 2 and 3 were registered, never served
    changed = list(texts())
    changed[1] = versions_text(field_text(), field_text(', "/other": {type: int, merge: max}'))[1]
    service = Env(backend, *changed).service(active=1)
    service.schemas.activate(2, operator="a")  # now it is served, and fixed
    edited_again = list(changed)
    edited_again[1] = texts()[1]
    with pytest.raises(SchemaError) as caught:
        Env(backend, *edited_again).service(active=1)
    assert caught.value.codes == ("SCHEMA_VERSION_CHANGED",)


def test_a_stored_active_version_missing_from_the_registry_fails_reads_and_writes_naming_it(backend: Storage) -> None:
    env = Env(backend, *texts())
    env.service(active=3)
    thin = env.service(only=[1, 2])
    with pytest.raises(RejectedError) as read:
        thin.get("d")
    with pytest.raises(RejectedError) as write:
        thin.append("d", [put("/name", "A")], contributor=USER)
    for caught in (read, write):
        assert caught.value.code is ErrorCode.SCHEMA_INACTIVE and "3" in str(caught.value)


def test_a_request_adds_one_generation_read_and_no_reload_without_a_change(backend: Storage) -> None:
    env = Env(backend, *texts())
    service = env.service(active=1)
    service.append("d", [put("/name", "A")], contributor=USER)
    state = service.schemas._state
    assert state is not None
    counts = {"generation": 0, "read": 0}
    generation, read = state.generation, state.read

    def counted_generation() -> int:
        counts["generation"] += 1
        return generation()

    def counted_read():  # type: ignore[no-untyped-def]
        counts["read"] += 1
        return read()

    state.generation = counted_generation  # type: ignore[method-assign]
    state.read = counted_read  # type: ignore[method-assign]
    for _ in range(3):
        service.get("d")
    assert counts == {"generation": 3, "read": 0}
    service.append("d", [put("/name", "B")], contributor=USER)
    assert counts["generation"] == 4 and counts["read"] == 0
    env.service(active=1).schemas.activate(2, operator="x")  # another process
    service.get("d")
    assert counts["read"] == 1


def test_schema_state_false_keeps_the_choice_in_the_process(backend: Storage) -> None:
    env = Env(backend, *texts())
    first = env.service(active=1, schema_state=False)
    second = env.service(active=2, schema_state=False)
    assert first.schemas.active().version == 1 and second.schemas.active().version == 2
    with backend.read() as tx:
        assert tx.schema_state("t") == (0, None, {})


def test_the_service_reports_the_fingerprint_of_every_served_version(backend: Storage) -> None:
    env = Env(backend, *texts())
    db = env.service(active=1)
    assert list(db.served_versions()) == [1]
    db.activate_schema(2, operator="alice", acknowledge_breaking=True)
    served = db.served_versions()
    assert list(served) == [1, 2] and served[2] == db.schemas.get(2).content_fingerprint
    assert env.service().served_versions() == served  # what another process sees
