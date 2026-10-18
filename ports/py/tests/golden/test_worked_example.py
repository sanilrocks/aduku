"""SPEC 16.1 and 16.2 as golden tests (SC-001), the clock-only pair, and the audit setting on the rejected request."""

import pytest

from aduku.ion_codec import to_canonical
from aduku.storage.base import Storage
from tests.conformance_runner import CONFORMANCE_DIR, build_service, load_case, run_case
from tests.service_helpers import ALICE, make_service, put, schema_text

CUSTOMER = (CONFORMANCE_DIR / "customer.schema.ion").read_text()


@pytest.mark.parametrize("name", ["worked-example-16-1", "model-errors-16-2"])
def test_the_normative_examples_match_their_vectors(name: str, backend: Storage) -> None:
    case = load_case((CONFORMANCE_DIR / f"{name}.ion").read_text())
    run_case(case, build_service(case, backend))


def test_p1_gives_the_layers_of_the_worked_example() -> None:
    from aduku.behaviors.merge import unwrap
    from aduku.behaviors.registry import BehaviorRegistry
    from aduku.engine.derive import derive
    from aduku.paths import format_path
    from tests.helpers import make_entry

    rows = [
        ("backfill", "/name", "A. Smith"),
        ("backfill", "/phone", "555-0100"),
        ("backfill", "/age", 30),
        ("backfill", "/tags", ("x",)),
        ("user", "/name", "Alice"),
        ("backfill", "/address/zip", "78701"),
        ("user", "/age", 28),
        ("user", "/tags", ("y",)),
        ("admin", "/tags", ("z",)),
        ("user", "/name", "Alice S."),
        ("admin", "/phone", None),
        ("admin", "/address/city", "Paris"),
        ("user", "/currency", "EUR"),
    ]
    entries = [
        make_entry(seq=n, tier=tier, path=path, value=value, op="delete" if value is None else "set")
        for n, (tier, path, value) in enumerate(rows, 1)
    ]
    from aduku.schema.loader import load_schema

    schema = load_schema(CUSTOMER)
    result = derive(schema, BehaviorRegistry.with_builtins(), entries)

    def flat(tier: str) -> dict[str, str]:
        return {format_path(p): to_canonical(unwrap(v)) for p, v in result.state.layers[tier].items()}

    assert flat("backfill") == {
        "/name": '"A. Smith"',
        "/phone": '"555-0100"',
        "/age": "30",
        "/tags": '["x"]',
        "/address/zip": '"78701"',
    }
    assert flat("user") == {"/name": '"Alice S."', "/age": "28", "/tags": '["y"]', "/currency": '"EUR"'}
    assert flat("admin") == {"/tags": '["z"]', "/phone": "deleted::null", "/address/city": '"Paris"'}


@pytest.mark.parametrize("enabled", [True, False])
def test_the_rejected_write_has_one_audit_record_when_auditing_is_on_and_none_when_off(
    enabled: bool, backend: Storage
) -> None:
    from aduku.errors import RejectedError

    audit = "rejection_audit: {enabled: true, mode: full, retention: {max_entries: 10}}," if enabled else ""
    service = make_service(backend, schema=schema_text(audit=audit))
    service.append("d1", [put("/name", "x")], contributor=ALICE)
    with pytest.raises(RejectedError):
        service.append("d1", [put("/displayName", "HACKED")], contributor=ALICE)
    assert len(service.list_rejections("d1")) == (1 if enabled else 0)
    assert len(service.list_entries("d1")) == 1  # no seq consumed


def test_cases_that_differ_only_in_the_clock_give_identical_results() -> None:
    def outcome(name: str) -> tuple[str, list[tuple[object, ...]]]:
        case = load_case((CONFORMANCE_DIR / f"{name}.ion").read_text())
        service = build_service(case)
        run_case(case, service)
        entries = [
            (e.seq, e.tier, e.path, e.op, to_canonical(e.value), e.contributor, e.request_id)
            for e in service.list_entries("d1")
        ]
        return to_canonical(service.get("d1").document), entries

    early, late = outcome("clock-early"), outcome("clock-late")
    assert early == late
    case_early = load_case((CONFORMANCE_DIR / "clock-early.ion").read_text())
    case_late = load_case((CONFORMANCE_DIR / "clock-late.ion").read_text())
    assert case_early.clock != case_late.clock  # the clocks really do differ
