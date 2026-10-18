"""SC-005: a request with at least one invalid entry changes nothing."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aduku.errors import RejectedError
from aduku.model import EntryInput
from tests.service_helpers import ALICE, make_service, put

VALID = st.one_of(
    st.builds(lambda v: put("/name", v), st.text(max_size=8)),
    st.builds(lambda v: put("/age", v), st.integers()),
    st.builds(lambda v: put("/tags", tuple(v)), st.lists(st.text(max_size=3), max_size=3)),
)
INVALID = st.one_of(
    st.builds(lambda v: put("/nope", v), st.integers()),
    st.builds(lambda v: put("/age", v), st.text(max_size=5)),
    st.builds(lambda v: put("/name", v), st.integers()),
    st.just(put("/displayName", "HACKED")),
    st.just(EntryInput(tier="admin", path="/name", op="set", value="x")),
)


@given(
    good=st.lists(VALID, max_size=4),
    bad=st.lists(INVALID, min_size=1, max_size=3),
    seed=st.lists(VALID, min_size=1, max_size=3),
)
def test_invalid_request_changes_nothing(good: list, bad: list, seed: list) -> None:  # type: ignore[type-arg]
    service = make_service()
    service.append("d1", seed, contributor=ALICE)
    before = (service.list_entries("d1"), service.last_seq("d1"))
    mixed = [*good, *bad]
    with pytest.raises(RejectedError):
        service.append("d1", mixed, contributor=ALICE)
    assert (service.list_entries("d1"), service.last_seq("d1")) == before
    # and the next accepted request continues the sequence without a gap
    result = service.append("d1", [put("/age", 1)], contributor=ALICE)
    assert [seq for _, seq in result.entries] == [before[1] + 1]
