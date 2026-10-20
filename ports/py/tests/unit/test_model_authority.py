"""Feature 004, FR-022: an explanation can say who may write a path and where that was declared."""

from aduku.model import Explanation


def test_an_explanation_has_no_authority_unless_it_is_given_one() -> None:
    plain = Explanation("/name", "Alice", None, None, None)
    assert (plain.authority, plain.authority_from) == (None, None)


def test_an_explanation_carries_the_list_and_where_it_was_declared() -> None:
    explained = Explanation("/billing/note", "x", None, None, None, authority=("finance",), authority_from="/billing")
    assert explained.authority == ("finance",) and explained.authority_from == "/billing"
