"""US1 scenario 5, FR-009, FR-026, SC-011: explain on a computed path."""

from aduku.ion_codec import Annotated
from aduku.schema.loader import load_schema
from aduku.storage.base import Storage
from tests.service_helpers import ALICE, make_service, put, schema_text

COMPUTED = (
    '"/name": {type: string, merge: lww}, '
    '"/displayName": {type: string, computed: {macro: upper, args: ["/name"]}}, '
    '"/greeting": {type: string, computed: {macro: concat, args: [lit::"Hello, ", "/displayName"]}}'
)


def test_a_computed_path_is_explained_by_its_macro_and_inputs(backend: Storage) -> None:
    service = make_service(backend, schema=schema_text(fields=COMPUTED))
    service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    why = service.explain("d1", "/displayName")
    assert (why.winner, why.rule, why.winner_seq) == ("computed", "upper@1", None)
    assert why.dependencies == (("/name", "Alice"),)
    assert why.inputs == () and why.shadowed == () and why.effect_seq is None
    assert why.value == "ALICE"


def test_a_chained_field_lists_the_computed_value_it_used(backend: Storage) -> None:
    service = make_service(backend, schema=schema_text(fields=COMPUTED))
    service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    why = service.explain("d1", "/greeting")
    assert why.rule == "concat@1" and why.dependencies == (("/displayName", "ALICE"),)  # literals are not dependencies


def test_a_missing_input_is_shown_as_missing(backend: Storage) -> None:
    from aduku.ion_codec import ABSENT

    service = make_service(backend, schema=schema_text(fields=COMPUTED))
    why = service.explain("d1", "/displayName")
    assert why.dependencies == (("/name", ABSENT),) and why.value is None


def test_entries_made_unusable_by_the_field_becoming_computed_are_listed_as_shadowed(backend: Storage) -> None:
    service = make_service(
        backend, schema=schema_text(fields='"/name": {type: string, merge: lww}, "/shout": {type: string, merge: lww}')
    )
    service.append("d1", [put("/name", "pat"), put("/shout", "stale")], contributor=ALICE)
    new = load_schema(
        schema_text(
            version=2,
            fields='"/name": {type: string, merge: lww}, "/shout": {type: string, computed: {macro: upper, args: ["/name"]}}',
        )
    )
    service.schemas.put(new)
    service.schemas.activate(2)
    why = service.explain("d1", "/shout")
    stale = [e.id for e in service.list_entries("d1") if e.path == "/shout"]
    assert why.winner == "computed" and why.shadowed == tuple(stale) and why.value == "PAT"


def test_every_computed_path_is_explained_by_one_call(backend: Storage) -> None:
    service = make_service(backend, schema=schema_text(fields=COMPUTED))
    service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    document = service.get("d1").document
    computed = [k for k, v in document.items() if isinstance(v, Annotated)]
    assert sorted(computed) == ["displayName", "greeting"]
    for key in computed:
        why = service.explain("d1", "/" + key)
        assert why.winner == "computed" and why.rule and why.dependencies


def test_the_explanation_of_a_plain_field_has_no_dependencies(backend: Storage) -> None:
    service = make_service(backend, schema=schema_text(fields=COMPUTED))
    service.append("d1", [put("/name", "Alice")], contributor=ALICE)
    why = service.explain("d1", "/name")
    assert why.winner != "computed" and why.dependencies == ()
