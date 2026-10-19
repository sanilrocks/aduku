"""US1: computed values follow their inputs on every read, are identical everywhere and are never stored (FR-005, FR-006)."""

import pathlib

from aduku.ion_codec import Annotated, to_canonical
from aduku.storage.base import SequentialIds, Storage
from aduku.storage.memory import MemoryStorage
from aduku.storage.sqlite import SqliteStorage
from tests.service_helpers import ALICE, delete, make_service, put, schema_text

COMPUTED = (
    '"/name": {type: string, merge: lww}, "/address": {type: struct, merge: struct}, '
    '"/address/city": {type: string, merge: lww}, '
    '"/displayName": {type: string, computed: {macro: upper, args: ["/name"]}}, '
    '"/greeting": {type: string, computed: {macro: concat, args: [lit::"Hello, ", "/displayName"]}}, '
    '"/cityUpper": {type: string, computed: {macro: upper, args: ["/address/city"]}}'
)


def svc(backend: Storage, **kwargs):  # type: ignore[no-untyped-def]
    return make_service(backend, schema=schema_text(fields=COMPUTED), **kwargs)


def test_a_changed_input_updates_dependents_on_the_next_read_and_nothing_else(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/name", "Alice"), put("/address/city", "Paris")], contributor=ALICE)
    first = service.get("d1").document
    assert first["displayName"] == Annotated(("computed",), "ALICE")
    assert first["greeting"] == Annotated(("computed",), "Hello, ALICE")  # a chain
    service.append("d1", [put("/name", "Bob")], contributor=ALICE)
    second = service.get("d1").document
    assert second["displayName"] == Annotated(("computed",), "BOB")
    assert second["greeting"] == Annotated(("computed",), "Hello, BOB")
    assert second["cityUpper"] == first["cityUpper"] == Annotated(("computed",), "PARIS")


def test_deleting_an_input_makes_the_dependents_null(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    service.append("d1", [delete("/name")], contributor=ALICE)
    document = service.get("d1").document
    assert "name" not in document
    assert document["displayName"] == Annotated(("computed",), None) == document["greeting"]


def test_the_text_is_identical_on_a_cold_service_and_after_a_rebuild(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/name", "Alice"), put("/address/city", "Paris")], contributor=ALICE)
    warm = to_canonical(service.get("d1").document)
    assert 'displayName:computed::"ALICE"' in warm
    with backend.transaction() as tx:
        tx.drop_view("d1")
    cold = svc(backend, ids=SequentialIds("cold"))
    assert to_canonical(cold.get("d1").document) == warm
    assert cold.rebuild("d1").identical
    assert to_canonical(cold.get("d1").document) == warm


def test_both_backends_give_the_same_bytes(tmp_path: pathlib.Path) -> None:
    texts = []
    for storage in (MemoryStorage(), SqliteStorage(str(tmp_path / "x.db"))):
        service = svc(storage)
        service.append("d1", [put("/name", "Zoë"), put("/address/city", "Orléans")], contributor=ALICE)
        service.append("d1", [put("/name", "Zoé")], contributor=ALICE)
        texts.append(to_canonical(service.get("d1").document))
        storage.close()
    assert texts[0] == texts[1]


def test_no_ledger_entry_ever_holds_a_computed_value(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    service.get("d1")
    service.rebuild("d1")
    entries = service.list_entries("d1")
    assert [e.path for e in entries] == ["/name"]
    assert not any(isinstance(e.value, Annotated) or "ALICE" in repr(e.value) for e in entries)


def test_reading_writes_no_entries(backend: Storage) -> None:
    service = svc(backend)
    service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    for _ in range(3):
        service.get("d1")
    assert service.last_seq("d1") == 1


def test_a_struct_read_whole_reaches_a_macro_without_the_computed_annotations_inside_it(backend: Storage) -> None:
    import hashlib

    from aduku.ion_codec import FrozenStruct

    fields = (
        '"/order": {type: struct, merge: struct}, "/order/n": {type: string, merge: lww}, '
        '"/order/total": {type: string, computed: {macro: upper, args: ["/order/n"]}}, '
        '"/a_digest": {type: string, computed: {macro: hash, args: ["/order"]}}'
    )
    service = make_service(backend, schema=schema_text(fields=fields))
    service.append("d1", [put("/order/n", "x")], contributor=ALICE)
    plain = to_canonical(FrozenStruct({"n": "x", "total": "X"}))
    got = service.get("d1").document["a_digest"]
    assert got == Annotated(("computed",), hashlib.sha256(plain.encode("utf-8")).hexdigest())
