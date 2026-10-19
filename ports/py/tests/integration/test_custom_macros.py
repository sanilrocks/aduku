"""US3: custom macros in a schema: results, load-time checks, and faults that are never null (FR-010, FR-011)."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import ComputeFault, ErrorCode, RejectedError, SchemaError
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.base import SequentialIds, Storage
from tests.service_helpers import ALICE, put, schema_text

BASE = '"/name": {type: string, merge: lww}, "/city": {type: string, merge: lww}, "/a": {type: int, merge: lww}, "/b": {type: int, merge: lww}'


def behaviors_with(*macros: tuple) -> BehaviorRegistry:  # type: ignore[type-arg]
    behaviors = BehaviorRegistry.with_builtins()
    behaviors.register_macro("acme.label", 1, ["name", "city"], 'name + " (" + city + ")"')
    behaviors.register_macro("acme.divide", 1, ["a", "b"], "a / b")
    behaviors.register_macro("acme.wrong", 1, ["a"], "a + 1")  # an int, whatever the field says
    for macro in macros:
        behaviors.register_macro(*macro)
    return behaviors


def service(backend: Storage, fields: str, behaviors: BehaviorRegistry | None = None) -> Aduku:
    behaviors = behaviors or behaviors_with()
    registry = SchemaRegistry()
    registry.put(load_schema(schema_text(fields=BASE + ", " + fields), behaviors))
    registry.activate(1)
    return Aduku(backend, registry, behaviors=behaviors, ids=SequentialIds("s"))


LABEL = '"/label": {type: string, computed: {macro: "acme.label@1", args: ["/name", "/city"]}}'


def test_a_custom_macro_gives_its_result_like_a_preset(backend: Storage) -> None:
    s = service(backend, LABEL)
    s.append("d1", [put("/name", "Alice S."), put("/city", "Paris")], contributor=ALICE)
    document = s.get("d1").document
    assert document["label"].value == "Alice S. (Paris)" and document["label"].annotations == ("computed",)
    assert s.explain("d1", "/label").rule == "acme.label@1"


def test_a_missing_input_skips_a_macro_that_does_not_accept_it(backend: Storage) -> None:
    s = service(backend, LABEL)
    s.append("d1", [put("/name", "Alice S.")], contributor=ALICE)
    assert s.get("d1").document["label"].value is None


def test_an_unregistered_macro_or_version_is_refused_at_load() -> None:
    behaviors = behaviors_with()
    for macro in ("acme.nope", "acme.label@2"):
        text = schema_text(fields=BASE + f', "/x": {{type: string, computed: {{macro: "{macro}", args: ["/name"]}}}}')
        with pytest.raises(SchemaError) as caught:
            load_schema(text, behaviors)
        assert "UNKNOWN_BEHAVIOR" in caught.value.codes


def test_a_macro_fault_rejects_the_request_with_compute_fault_and_records_nothing(backend: Storage) -> None:
    s = service(backend, '"/q": {type: int, computed: {macro: "acme.divide@1", args: ["/a", "/b"]}}')
    s.append("d1", [put("/a", 6), put("/b", 3)], contributor=ALICE)
    with pytest.raises(RejectedError) as caught:
        s.append("d1", [put("/b", 0)], contributor=ALICE)
    assert caught.value.code is ErrorCode.COMPUTE_FAULT and "acme.divide@1" in str(caught.value)
    assert "division" in str(caught.value) and [e.seq for e in s.list_entries("d1")] == [1, 2]
    assert s.get("d1").document["q"].value == 2  # the document is unchanged


def test_a_read_that_hits_a_fault_raises_it_and_never_returns_null(backend: Storage) -> None:
    plain = service(backend, "")
    plain.append("d1", [put("/a", 1), put("/b", 0)], contributor=ALICE)  # accepted: nothing computed yet
    behaviors = behaviors_with()
    registry = plain.schemas
    registry.put(
        load_schema(
            schema_text(
                version=2,
                fields=BASE + ', "/q": {type: int, computed: {macro: "acme.divide@1", args: ["/a", "/b"]}}',
            ),
            behaviors,
        )
    )
    later = Aduku(backend, registry, behaviors=behaviors, ids=SequentialIds("late"))
    registry.activate(2)
    with pytest.raises(ComputeFault) as caught:
        later.get("d1")
    assert caught.value.macro == "acme.divide@1"


def test_a_result_of_the_wrong_type_is_a_fault(backend: Storage) -> None:
    s = service(backend, '"/w": {type: string, computed: {macro: "acme.wrong@1", args: ["/a"]}}')
    with pytest.raises(RejectedError) as caught:
        s.append("d1", [put("/a", 1)], contributor=ALICE)
    assert caught.value.code is ErrorCode.COMPUTE_FAULT and "type" in str(caught.value)


def test_a_macro_over_its_step_limit_is_stopped_and_reported(backend: Storage) -> None:
    heavy = ("acme.heavy", 1, ["a"], " + ".join(["a"] * 100), False, {"steps": 30})
    behaviors = behaviors_with()
    behaviors.register_macro(heavy[0], heavy[1], heavy[2], heavy[3], accepts_missing=heavy[4], limits=heavy[5])
    s = service(backend, '"/h": {type: int, computed: {macro: "acme.heavy@1", args: ["/a"]}}', behaviors)
    with pytest.raises(RejectedError) as caught:
        s.append("d1", [put("/a", 1)], contributor=ALICE)
    assert caught.value.code is ErrorCode.COMPUTE_FAULT and "step" in str(caught.value)
    assert s.list_entries("d1") == []


def test_a_fault_in_one_field_does_not_change_the_value_of_another(backend: Storage) -> None:
    s = service(
        backend,
        LABEL + ', "/q": {type: int, computed: {macro: "acme.divide@1", args: ["/a", "/b"]}}',
    )
    s.append("d1", [put("/name", "Al"), put("/city", "Rome"), put("/a", 4), put("/b", 2)], contributor=ALICE)
    assert s.get("d1").document["label"].value == "Al (Rome)"
    with pytest.raises(RejectedError):
        s.append("d1", [put("/b", 0)], contributor=ALICE)
    assert s.get("d1").document["label"].value == "Al (Rome)" and s.get("d1").document["q"].value == 2
