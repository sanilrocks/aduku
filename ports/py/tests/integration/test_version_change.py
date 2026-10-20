"""US8 and FR-009, FR-025, FR-026: a read under a different schema version re-derives; activation protects the ledger."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ErrorCode, RejectedError, SchemaError
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.base import SequentialIds, Storage
from tests.fixtures import register_fixtures
from tests.service_helpers import ALICE, JOB, delete, put, schema_text

BASE = """
  "/name": {type: string, merge: lww%s},
  "/age": {type: int, merge: lww, validate: [range::{min: 0, max: 150}]},
  "/country": {type: string, merge: lww},
  "/spouses": {type: "list<string>", merge: lww}
"""


def text(version: int, name_rule: str = "", tiers: str = "backfill, user, admin") -> str:
    fields = BASE % (f", validate: [{name_rule}]" if name_rule else "")
    found = schema_text(fields=fields, version=version).replace(
        "fields:", 'document_validate: ["acme.max_spouses_by_country@1"], fields:'
    )
    found = found.replace("tiers: [backfill, user, admin]", f"tiers: [{tiers}]")
    return found if "backfill" in tiers else found.replace("lenient: {allowed: [backfill]},", "")


def build(
    storage: Storage, *versions: str, active: int | None = None, prefix: str = "s", schema_state: bool = True
) -> Aduku:
    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    registry = SchemaRegistry()
    for body in versions:
        registry.put(load_schema(body, behaviors))
    service = Aduku(storage, registry, behaviors=behaviors, ids=SequentialIds(prefix), schema_state=schema_state)
    if active is not None:
        registry.activate(active)
    return service


V7 = text(7)
V8 = text(8, "max_len::{n: 2}")


def test_reading_with_no_active_version_is_schema_inactive(backend: Storage) -> None:
    service = build(backend, V7)
    with pytest.raises(RejectedError) as caught:
        service.get("d1")
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE


def test_a_document_cannot_be_pinned_to_a_version_that_is_not_registered(backend: Storage) -> None:
    service = build(backend, V7, active=7)
    with pytest.raises(RejectedError) as caught:
        service.schemas.pin("d1", 99)
    assert caught.value.code is ErrorCode.SCHEMA_INACTIVE


@pytest.mark.parametrize("tiers", ["user, admin", "legacy, user, admin"])
def test_activating_a_version_that_drops_or_renames_a_used_tier_is_refused(backend: Storage, tiers: str) -> None:
    service = build(backend, V7, text(9, tiers=tiers), active=7)
    service.append("d1", [put("/name", "x", tier="backfill")], contributor=JOB)
    with pytest.raises(SchemaError):
        service.schemas.activate(9)
    assert service.schemas.active().version == 7


def test_a_version_that_keeps_every_used_tier_can_be_activated(backend: Storage) -> None:
    service = build(backend, V7, text(9, tiers="backfill, user, admin, audit"), active=7)
    service.append("d1", [put("/name", "x", tier="backfill")], contributor=JOB)
    service.schemas.activate(9)
    assert service.get("d1").document["name"] == "x"


def test_a_document_cannot_be_pinned_to_a_version_that_lacks_a_tier_it_uses(backend: Storage) -> None:
    service = build(backend, V7, text(9, tiers="user, admin"), active=7)
    service.append("d1", [put("/name", "x", tier="backfill")], contributor=JOB)
    service.append("d2", [put("/name", "x")], contributor=ALICE)
    with pytest.raises(SchemaError):
        service.schemas.pin("d1", 9)
    service.schemas.pin("d2", 9)  # d2 never used the dropped tier


def test_an_error_whose_path_was_deleted_disappears(backend: Storage) -> None:
    service = build(backend, V7, active=7)
    service.append("d1", [put("/age", -5, tier="backfill")], contributor=JOB, lenient=True)
    assert [e.path_scope for e in service.get("d1").model_errors] == ["/age"]
    service.append("d1", [delete("/age", tier="backfill")], contributor=JOB)
    assert service.get("d1").model_errors == ()


def test_a_delete_of_an_unrelated_path_keeps_the_errors_that_still_fail(backend: Storage) -> None:
    service = build(backend, V7, active=7)
    service.append(
        "d1",
        [put("/country", "US", tier="backfill"), put("/spouses", ("A", "B"), tier="backfill")],
        contributor=JOB,
        lenient=True,
    )
    service.append("d1", [put("/name", "x")], contributor=ALICE)
    service.append("d1", [delete("/name")], contributor=ALICE)
    assert [e.rule_id for e in service.get("d1").model_errors] == ["max_spouses_by_country"]


# -- scenarios 1 and 2, and SPEC 16.2 step 7 ------------------------------------------------------------------------------


def named(storage: Storage):  # type: ignore[no-untyped-def]
    service = build(storage, V7, V8, active=7)
    service.append("d1", [put("/name", "Pat")], contributor=ALICE)
    service.append("d1", [put("/age", -5, tier="backfill")], contributor=JOB, lenient=True)
    return service


def test_1_a_tighter_rule_in_a_new_version_shows_as_a_model_error_attributed_to_the_version(backend: Storage) -> None:
    service = named(backend)
    before = service.list_entries("d1")
    assert [(e.rule_id, e.path_scope) for e in service.get("d1").model_errors] == [("range", "/age")]
    service.schemas.activate(8)
    result = service.get("d1")
    by_rule = {e.rule_id: e for e in result.model_errors}
    assert sorted(by_rule) == ["max_len", "range"]
    assert by_rule["max_len"].path_scope == "/name"
    assert (by_rule["max_len"].introduced_by_request, by_rule["max_len"].introduced_by_schema_version) == (None, 8)
    assert by_rule["range"].introduced_by_request is not None  # the older error keeps its request
    assert service.list_entries("d1") == before  # the ledger is untouched


def test_2_a_second_read_under_the_same_version_does_not_derive_again(backend: Storage) -> None:
    service = named(backend)
    service.schemas.activate(8)
    first = service.get("d1")
    derived = service.derivations
    assert service.get("d1") == first and service.derivations == derived


def test_the_first_read_after_a_change_equals_a_fresh_derivation(backend: Storage) -> None:
    service = named(backend)
    service.schemas.activate(8)
    changed = service.get("d1")
    with backend.transaction() as tx:
        tx.drop_view("d1")
    cold = build(backend, V7, V8, active=8, prefix="cold").get("d1")
    assert cold == changed


def test_reading_under_an_older_version_again_re_derives_and_drops_the_newer_error(backend: Storage) -> None:
    service = named(backend)
    service.schemas.activate(8)
    service.get("d1")
    service.schemas.activate(7)
    assert [e.rule_id for e in service.get("d1").model_errors] == ["range"]


def test_a_new_request_under_the_new_version_is_judged_by_its_rules(backend: Storage) -> None:
    service = named(backend)
    service.schemas.activate(8)
    service.append("d1", [put("/name", "Patricia")], contributor=ALICE)  # the same error again is not a new one
    with pytest.raises(RejectedError) as caught:  # a different rule failing is, and the document already has errors
        service.append("d1", [put("/country", "US"), put("/spouses", ("A", "B"))], contributor=ALICE)
    assert caught.value.code is ErrorCode.MODEL_ERROR_INTRODUCED


def test_a_read_that_has_to_derive_never_takes_the_write_lock(backend: Storage) -> None:
    from tests.integration.test_resilience import Counting

    counting = Counting(backend)
    service = build(counting, V7, V8, active=7)  # type: ignore[arg-type]
    service.append("d1", [put("/name", "Pat")], contributor=ALICE)
    service.schemas.activate(8)  # the stored view is now from another version
    writes = counting.writes
    for _ in range(3):
        assert [e.rule_id for e in service.get("d1").model_errors] == ["max_len"]
    assert counting.writes == writes


def test_a_service_refuses_an_active_version_that_drops_a_tier_the_ledger_uses(backend: Storage) -> None:
    # a database written before the store kept the active version: nothing stored, so the registry's own choice is adopted
    build(backend, V7, active=7, schema_state=False).append("d1", [put("/name", "x", tier="backfill")], contributor=JOB)
    from aduku.behaviors.registry import BehaviorRegistry
    from aduku.service import Aduku

    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    registry = SchemaRegistry()
    registry.put(load_schema(text(9, tiers="user, admin"), behaviors))
    registry.activate(9)  # before any service exists, so activation could not check the ledger
    with pytest.raises(SchemaError):
        Aduku(backend, registry, behaviors=behaviors)


def test_a_service_refuses_a_pin_that_drops_a_tier_the_document_uses(backend: Storage) -> None:
    from aduku.behaviors.registry import BehaviorRegistry
    from aduku.service import Aduku

    build(backend, V7, active=7, schema_state=False).append("d1", [put("/name", "x", tier="backfill")], contributor=JOB)
    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    registry = SchemaRegistry()
    registry.put(load_schema(V7, behaviors))
    registry.put(load_schema(text(9, tiers="user, admin"), behaviors))
    registry.activate(7)
    registry.pin("d1", 9)
    with pytest.raises(SchemaError):
        Aduku(backend, registry, behaviors=behaviors)


def test_when_the_store_holds_state_a_registrys_own_choice_is_ignored_even_if_it_could_not_serve_the_ledger(
    backend: Storage,
) -> None:
    """Feature 006: the store decides. A process whose files hold a newer version that drops a used tier still starts and serves
    the stored version, and can repair things (activate, roll back) instead of being locked out."""
    from aduku.behaviors.registry import BehaviorRegistry
    from aduku.service import Aduku

    build(backend, V7, active=7).append("d1", [put("/name", "x", tier="backfill")], contributor=JOB)
    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    registry = SchemaRegistry()
    registry.put(load_schema(V7, behaviors))
    registry.put(load_schema(text(9, tiers="user, admin"), behaviors))
    registry.activate(9)
    service = Aduku(backend, registry, behaviors=behaviors)
    assert service.schemas.active().version == 7 and service.get("d1").document["name"] == "x"
    with pytest.raises(SchemaError):
        service.activate_schema(9, operator="alice", acknowledge_breaking=True)
