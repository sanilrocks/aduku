"""US2: authority with inheritance, override, roles, wildcards, the delete rule and reasons that do not list the allowed
tiers (FR-017 to FR-020, FR-024)."""

from aduku.errors import ErrorCode
from aduku.model import Identity
from aduku.storage.base import Storage
from tests.rule_helpers import ADMIN, FINANCE, JOB, USER, build, never_shows, rejection, remove, set_

FIELDS = """
  "/plan": {type: string, merge: lww, authority: [admin]},
  "/free": {type: string, merge: lww},
  "/billing": {type: struct, merge: struct, authority: [finance]},
  "/billing/note": {type: string, merge: lww},
  "/billing/limit": {type: int, merge: max, authority: [admin]},
  "/billing/tax": {type: struct, merge: struct},
  "/billing/tax/rate": {type: int, merge: max},
  "/w": {type: struct, merge: struct},
  "/w/*": {type: string, merge: lww, authority: [ops]},
  "/shout": {type: string, computed: {macro: upper, args: ["/free"]}, authority: [admin]}
"""
OPS = Identity("op", ("user",), ("ops",))
BOTH = Identity("both", ("user", "admin"), ("finance",))


def authority_reasons(error):  # type: ignore[no-untyped-def]
    return sorted((r.path, r.rule) for r in error.reasons if r.code is ErrorCode.NOT_AUTHORIZED)


def test_the_tier_in_the_list_may_write_and_another_may_not(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/plan", "pro", "admin")], contributor=ADMIN)
    error = rejection(service, [set_("/plan", "pro-secret")])
    assert error.code is ErrorCode.NOT_AUTHORIZED and authority_reasons(error) == [("/plan", "authority")]
    assert error.reasons[0].message == "not authorized to write /plan"
    never_shows(error, "pro-secret", "admin")  # neither the value nor the allowed list


def test_a_role_in_the_list_is_enough_whatever_the_tier(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/billing/note", "n")], contributor=FINANCE)
    assert authority_reasons(rejection(service, [set_("/billing/note", "n2")])) == [("/billing/note", "authority")]


def test_a_child_without_a_list_takes_its_parents_through_several_levels(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/billing/tax/rate", 5)], contributor=FINANCE)
    assert authority_reasons(rejection(service, [set_("/billing/tax/rate", 6)])) == [("/billing/tax/rate", "authority")]


def test_a_childs_own_list_replaces_its_parents(backend: Storage) -> None:
    service = build(backend, FIELDS)
    assert authority_reasons(rejection(service, [set_("/billing/limit", 5)], FINANCE)) == [
        ("/billing/limit", "authority")
    ]
    service.append(
        "d1", [set_("/billing/limit", 5, "admin")], contributor=ADMIN
    )  # the parent's list does not apply to it
    assert service.get("d1").document["billing"]["limit"] == 5


def test_a_wildcard_field_with_a_list_applies_to_the_paths_it_matches(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/w/anything", "x")], contributor=OPS)
    assert authority_reasons(rejection(service, [set_("/w/anything", "y")])) == [("/w/anything", "authority")]


def test_a_field_with_no_list_is_open_to_any_permitted_tier(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/free", "x")], contributor=USER)


def test_a_delete_needs_the_authority_of_every_declared_field_beneath_the_path(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/billing/limit", 1, "admin")], contributor=ADMIN)
    error = rejection(service, [remove("/billing")], FINANCE)  # /billing is theirs, /billing/limit is not
    assert authority_reasons(error) == [("/billing", "authority")] and "/billing/limit" in error.reasons[0].message
    assert "wipes" in error.reasons[0].message
    error = rejection(service, [remove("/billing", "admin")], ADMIN)  # the admin tier is not in /billing's list
    assert ("/billing", "authority") in authority_reasons(error)
    service.append("d1", [remove("/billing", "admin")], contributor=BOTH)  # role finance and tier admin cover both
    assert "billing" not in service.get("d1").document


def test_a_delete_of_a_path_with_no_declared_children_needs_only_its_own_authority(backend: Storage) -> None:
    service = build(backend, FIELDS)
    service.append("d1", [set_("/billing/note", "n")], contributor=FINANCE)
    service.append("d1", [remove("/billing/note")], contributor=FINANCE)


def test_every_unauthorized_entry_is_listed_and_nothing_is_recorded(backend: Storage) -> None:
    service = build(backend, FIELDS)
    error = rejection(service, [set_("/plan", "pro"), set_("/free", "ok"), set_("/billing/note", "n")])
    assert authority_reasons(error) == [("/billing/note", "authority"), ("/plan", "authority")]
    assert service.list_entries("d1") == []


def test_the_lenient_flag_never_passes_an_authority_check(backend: Storage) -> None:
    service = build(backend, FIELDS)
    error = rejection(service, [set_("/plan", "pro", "backfill")], JOB, lenient=True)  # the tier may use the flag
    assert error.code is ErrorCode.NOT_AUTHORIZED and service.list_entries("d1") == []
    flagged = rejection(service, [set_("/free", "x")], lenient=True)  # a tier that may not use the flag
    assert flagged.code is ErrorCode.LENIENT_NOT_PERMITTED


def test_a_derived_path_is_unwritable_whatever_its_authority_list(backend: Storage) -> None:
    service = build(backend, FIELDS)
    error = rejection(service, [set_("/shout", "x", "admin")], ADMIN)
    assert error.code is ErrorCode.FIELD_NOT_WRITABLE
    assert rejection(service, [set_("/shout", "x", "backfill")], JOB, lenient=True).code is ErrorCode.FIELD_NOT_WRITABLE
