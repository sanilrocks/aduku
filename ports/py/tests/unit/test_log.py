import logging

import pytest

from aduku.log import emit


class Boom:
    calls = 0

    def __str__(self) -> str:
        Boom.calls += 1
        return "boom"


def test_event_is_one_json_line(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="aduku"):
        emit("admission", doc="d1", decision="rejected")
    assert [r.getMessage() for r in caplog.records] == ['{"decision": "rejected", "doc": "d1", "event": "admission"}']


def test_nothing_is_built_when_the_level_is_off(caplog: pytest.LogCaptureFixture) -> None:
    Boom.calls = 0
    with caplog.at_level(logging.WARNING, logger="aduku"):
        emit("admission", detail=Boom())
    assert Boom.calls == 0 and caplog.records == []
