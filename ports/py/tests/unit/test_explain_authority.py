"""Feature 004, FR-022: `explain` says who may write a path and which field declared it."""

from aduku.storage.memory import MemoryStorage
from tests.rule_helpers import build

FIELDS = """
  "/plan": {type: string, merge: lww, authority: [admin]},
  "/free": {type: string, merge: lww},
  "/billing": {type: struct, merge: struct, authority: [finance, ops]},
  "/billing/note": {type: string, merge: lww},
  "/billing/limit": {type: int, merge: max, authority: [admin]},
  "/w": {type: struct, merge: struct},
  "/w/*": {type: string, merge: lww, authority: [ops]},
  "/shout": {type: string, computed: {macro: upper, args: ["/free"]}, authority: [admin]}
"""


def explained(path: str):  # type: ignore[no-untyped-def]
    return build(MemoryStorage(), FIELDS).explain("d1", path)


def test_a_field_with_its_own_list() -> None:
    result = explained("/plan")
    assert (result.authority, result.authority_from) == (("admin",), "/plan")


def test_a_field_that_inherits_names_the_parent_that_declared_the_list() -> None:
    result = explained("/billing/note")
    assert (result.authority, result.authority_from) == (("finance", "ops"), "/billing")


def test_a_field_that_overrides_names_itself() -> None:
    result = explained("/billing/limit")
    assert (result.authority, result.authority_from) == (("admin",), "/billing/limit")


def test_a_field_with_no_list_is_open() -> None:
    result = explained("/free")
    assert (result.authority, result.authority_from) == (None, None)


def test_a_path_under_a_wildcard_field_with_a_list() -> None:
    result = explained("/w/anything")
    assert (result.authority, result.authority_from) == (("ops",), "/w/*")


def test_the_rest_of_the_explanation_is_unchanged() -> None:
    result = explained("/plan")
    assert (result.path, result.winner, result.value) == ("/plan", None, None)


def test_a_derived_path_has_no_authority_because_nobody_can_write_it() -> None:
    result = explained("/shout")
    assert (result.authority, result.authority_from) == (None, None)
