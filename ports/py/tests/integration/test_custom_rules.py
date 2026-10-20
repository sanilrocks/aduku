"""US4: a custom rule with declared arguments runs like a library rule, is pinned by version, and a crash is a fault (FR-013 to FR-015, SC-007)."""

import pytest

from aduku.behaviors.registry import ArgSpec, BehaviorRegistry
from aduku.engine.validate import ValidatorFault
from aduku.errors import ErrorCode, SchemaError
from aduku.model import Violation
from aduku.storage.base import Storage
from tests.rule_helpers import JOB, USER, build, never_shows, rejection, set_

FIELDS = """
  "/taxId": {type: string, merge: lww, validate: ['acme.tax_id'::{country: "DE"}]},
  "/a": {type: int, merge: lww}, "/b": {type: int, merge: lww}, "/name": {type: string, merge: lww}
"""
RULES = """'acme.balanced'::{paths: ["/a", "/b"]}"""


def tax_id(subject):  # type: ignore[no-untyped-def]
    if isinstance(subject.value, str) and not subject.value.startswith(subject.args["country"]):
        return [Violation("acme.tax_id", subject.path, "must start with the country code")]
    return []


def balanced(subject):  # type: ignore[no-untyped-def]
    a, b = subject.document.get("a"), subject.document.get("b")
    if a is not None and b is not None and a + b != 0:
        return [Violation("acme.balanced", "/a", "the two amounts must cancel out")]
    return []


def register(behaviors: BehaviorRegistry) -> None:
    behaviors.register_validate(
        "acme.tax_id",
        1,
        tax_id,
        args=(ArgSpec("country", "text"), ArgSpec("strict", "bool", required=False)),
        applies="field",
    )
    behaviors.register_validate(
        "acme.balanced", 1, balanced, args=(ArgSpec("paths", "paths", min=2),), applies="document"
    )


def test_a_custom_field_rule_and_a_custom_document_rule_run_at_the_same_stages_as_library_rules(
    backend: Storage,
) -> None:
    service = build(backend, FIELDS, RULES, register=register)
    service.append("d1", [set_("/taxId", "DE123"), set_("/a", 5), set_("/b", -5)], contributor=USER)
    error = rejection(service, [set_("/taxId", "FR999")])
    assert [(r.rule, r.path) for r in error.reasons] == [
        ("acme.tax_id", "/taxId")
    ] and error.code is ErrorCode.VALIDATION_FAILED
    never_shows(error, "FR999")
    assert [(r.rule, r.path) for r in rejection(service, [set_("/b", 1)]).reasons] == [("acme.balanced", "/a")]
    result = service.append(
        "d2", [set_("/taxId", "FR999", "backfill")], contributor=JOB, lenient=True
    )  # the flag works too
    assert [(e.rule_id, e.path_scope) for e in result.model_errors_added] == [("acme.tax_id", "/taxId")]


@pytest.mark.parametrize(
    "use",
    [
        """'acme.tax_id'::{country: "DE", extra: 1}""",
        """'acme.tax_id'::{strict: true}""",
        """'acme.tax_id'::{country: 7}""",
        """'acme.tax_id'::{country: "DE", strict: "yes"}""",
        """'acme.tax_id@9'::{country: "DE"}""",
    ],
)
def test_each_way_of_breaking_the_declaration_is_refused_when_the_service_is_built(backend: Storage, use: str) -> None:
    fields = FIELDS.replace("""'acme.tax_id'::{country: "DE"}""", use)
    with pytest.raises(SchemaError) as caught:
        build(backend, fields, register=register)
    assert set(caught.value.codes) & {"BAD_BEHAVIOR_ARGS", "UNKNOWN_BEHAVIOR"}


def faulty(how: str):  # type: ignore[no-untyped-def]
    state = {"on": False}

    def rule(subject):  # type: ignore[no-untyped-def]
        if state["on"] and subject.value == "boom":
            if how == "raises":
                raise RuntimeError("the rule crashed on secret-data")
            return "not a list"
        return []

    return state, rule


@pytest.mark.parametrize("how", ["raises", "returns-the-wrong-thing"])
def test_a_rule_that_crashes_rejects_the_request_and_fails_the_read_it_never_reads_as_no_violations(
    backend: Storage, how: str
) -> None:
    state, rule = faulty("raises" if how == "raises" else "wrong")

    def with_rule(behaviors: BehaviorRegistry) -> None:
        behaviors.register_validate("acme.fragile", 1, rule, args=(), applies="field")

    fields = '"/code": {type: string, merge: lww, validate: ["acme.fragile"]}'
    service = build(backend, fields, register=with_rule)
    service.append("d1", [set_("/code", "fine")], contributor=USER)
    state["on"] = True
    error = rejection(service, [set_("/code", "boom")])
    assert (
        error.code is ErrorCode.VALIDATION_FAILED
        and "acme.fragile" in str(error)
        and len(service.list_entries("d1")) == 1
    )
    if how == "raises":  # the fault names the kind of error the rule raised, never what it said (it may hold a value)
        assert "RuntimeError" in str(error) and "secret-data" not in str(error)
        assert all("secret-data" not in r.message for r in error.reasons)
    with backend.transaction() as tx:
        tx.drop_view("d1")
    cold = build(backend, fields, register=with_rule, prefix="cold")
    state["on"] = False
    cold.append("d1", [set_("/code", "boom")], contributor=USER)  # recorded while the rule was healthy...
    state["on"] = True
    with backend.transaction() as tx:
        tx.drop_view("d1")
    reader = build(backend, fields, register=with_rule, prefix="reader")
    with pytest.raises(ValidatorFault) as fault:
        reader.get("d1")  # ...and now a read fails instead of returning a document as if it were valid
    assert "secret-data" not in str(fault.value)


def test_a_rule_sees_the_merged_and_computed_document_and_never_a_deferred_value(backend: Storage) -> None:
    seen: list[dict[str, object]] = []

    def spy(subject):  # type: ignore[no-untyped-def]
        seen.append(dict(subject.document))
        return []

    def with_spy(behaviors: BehaviorRegistry) -> None:
        behaviors.register_validate("acme.spy", 1, spy, args=(), applies="document")
        behaviors.register_hook("h", 1, lambda inputs, timeout: 1)

    fields = (
        '"/name": {type: string, merge: lww}, "/shout": {type: string, computed: {macro: upper, args: ["/name"]}}, '
        '"/score": {type: int, deferred: {hook: "h@1", depends: ["/name"], refresh: once, invalidate_by: [admin]}}'
    )
    service = build(backend, fields, '"acme.spy"', register=with_spy)
    service.append("d1", [set_("/name", "alice")], contributor=USER)
    service.get("d1")
    assert seen and all("score" not in doc for doc in seen)  # a deferred value is never in the document a rule sees
    assert seen[-1]["shout"] == "ALICE"  # a computed value is, as its plain value


def test_the_version_a_schema_names_is_the_one_that_runs(backend: Storage) -> None:
    def both(behaviors: BehaviorRegistry) -> None:
        behaviors.register_validate("acme.pinned", 1, lambda s: [], args=(), applies="field")
        behaviors.register_validate(
            "acme.pinned",
            2,
            lambda s: [Violation("acme.pinned", s.path, "version two always objects")],
            args=(),
            applies="field",
        )

    fields = '"/code": {type: string, merge: lww, validate: ["acme.pinned@1"]}'
    service = build(backend, fields, register=both)
    service.append(
        "d1", [set_("/code", "x")], contributor=USER
    )  # version 2 is registered but this schema names version 1
    assert service.get("d1").model_errors == ()
