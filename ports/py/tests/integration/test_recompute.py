"""US7 scenarios 7, 9, 10 and FR-025: `recompute` calls a hook again, for the callers the schema permits."""

import logging

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ErrorCode, RejectedError
from aduku.ion_codec import Annotated
from aduku.model import Identity
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.base import SequentialIds, Storage
from tests.fixtures import CountingHook
from tests.service_helpers import ALICE, ROOT, put, schema_text

AUDITOR = Identity("aud", ("user",), ("auditor",))


def impure(path: str, hook: str, by: str = "admin", retry: int = 0) -> str:
    return (
        f'"{path}": {{type: string, computed: {{impure: {{hook: "{hook}", depends: ["/country"], reason: "registry",'
        f' timeout: "1s", retry: {{max: {retry}, backoff: "1ms"}}, deadline: "3s", recompute_by: [{by}],'
        f" failure_mode: model_error_on_failure}}}}}}"
    )


class Switch:
    """A hook that fails while `down` is set and answers with a counter once it is up."""

    def __init__(self) -> None:
        self.down = True
        self.count = 0

    def __call__(self, inputs: dict[str, object], timeout: float) -> object:
        if self.down:
            raise RuntimeError("down")
        self.count += 1
        return f"{inputs['/country']}-{self.count}"


def build(backend: Storage, switch: Switch, top: str = "") -> Aduku:
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_hook("fx.switch", 1, switch)
    behaviors.register_hook("fx.other", 1, CountingHook("fx.other", lambda i, n: "other"))
    fields = (
        '"/country": {type: string, merge: lww}, '
        + impure("/taxId", "fx.switch@1")
        + ", "
        + impure("/audit", "fx.other@1", by="auditor")
    )
    registry = SchemaRegistry()
    registry.put(load_schema(schema_text(fields=fields).replace("tiers:", f"{top} tiers:"), behaviors))
    registry.activate(1)
    return Aduku(backend, registry, behaviors=behaviors, ids=SequentialIds("r"))


def value(service: Aduku, key: str = "taxId") -> object:
    got = service.get("d1").document.get(key)
    return got.value if isinstance(got, Annotated) else got


def effects(service: Aduku, field: str | None = None):  # type: ignore[no-untyped-def]
    return service.list_effects("d1", field)


def test_7_recompute_replaces_the_failure_and_clears_the_model_error(backend: Storage) -> None:
    switch = Switch()
    service = build(backend, switch, top="roles: [auditor],")
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert [e.rule_id for e in service.get("d1").model_errors] == ["impure:/taxId"] and value(service) is None
    switch.down = False  # the registry is back
    report = service.recompute("d1", "/taxId", caller=ROOT)
    assert report.recomputed == ("/taxId",) and report.skipped == () and report.failed == ()
    assert service.get("d1").model_errors == () and value(service) == "US-1"
    first, second = effects(service, "/taxId")
    assert (first.outcome, second.outcome) == ("failure", "value") and second.effect_seq > first.effect_seq
    assert dict(report.effect_seqs) == {"/taxId": second.effect_seq}


def test_a_recompute_that_fails_again_is_recorded_and_reported(backend: Storage) -> None:
    switch = Switch()
    service = build(backend, switch, top="roles: [auditor],")
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    report = service.recompute("d1", "/taxId", caller=ROOT)
    assert report.recomputed == () and report.failed == (("/taxId", "error"),)
    assert len(effects(service, "/taxId")) == 2 and value(service) is None


def test_9_a_caller_the_schema_does_not_list_is_refused_and_nothing_is_called(backend: Storage) -> None:
    switch = Switch()
    service = build(backend, switch, top="roles: [auditor],")
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    before = len(effects(service))
    with pytest.raises(RejectedError) as caught:
        service.recompute("d1", "/taxId", caller=ALICE)
    assert caught.value.code is ErrorCode.NOT_AUTHORIZED and len(effects(service)) == before


def test_the_list_may_name_a_role(backend: Storage) -> None:
    switch = Switch()
    service = build(backend, switch, top="roles: [auditor],")
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    assert service.recompute("d1", "/audit", caller=AUDITOR).recomputed == ("/audit",)
    with pytest.raises(RejectedError):  # the auditor may recompute /audit but not /taxId
        service.recompute("d1", "/taxId", caller=AUDITOR)


def test_10_a_recompute_without_a_field_covers_only_the_permitted_fields(backend: Storage) -> None:
    switch = Switch()
    switch.down = False
    service = build(backend, switch, top="roles: [auditor],")
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    report = service.recompute("d1", caller=AUDITOR)
    assert report.recomputed == ("/audit",) and report.skipped == ("/taxId",)
    both = service.recompute("d1", caller=Identity("both", ("admin",), ("auditor",)))
    assert both.recomputed == ("/audit", "/taxId") and both.skipped == ()


def test_a_field_with_a_missing_input_cannot_be_recomputed(backend: Storage) -> None:
    switch = Switch()
    switch.down = False
    service = build(backend, switch, top="roles: [auditor],")
    report = service.recompute("d1", "/taxId", caller=ROOT)
    assert report.recomputed == () and report.failed == (("/taxId", "missing_input"),) and effects(service) == []


def test_only_an_impure_field_can_be_recomputed(backend: Storage) -> None:
    service = build(backend, Switch(), top="roles: [auditor],")
    for path in ("/country", "/nope"):
        with pytest.raises(RejectedError) as caught:
            service.recompute("d1", path, caller=ROOT)
        assert caught.value.code is ErrorCode.UNKNOWN_PATH


def test_a_recompute_is_logged_without_values(backend: Storage, caplog: pytest.LogCaptureFixture) -> None:
    switch = Switch()
    switch.down = False
    service = build(backend, switch, top="roles: [auditor],")
    service.append("d1", [put("/country", "US")], contributor=ALICE)
    with caplog.at_level(logging.INFO, logger="aduku"):
        service.recompute("d1", "/taxId", caller=ROOT)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert '"event": "recompute"' in text and '"event": "hook_call"' in text and "/taxId" in text
    assert "US-" not in text and "root" in text  # who asked, but never a value
