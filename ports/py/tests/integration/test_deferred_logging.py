"""FR-028: structured events for source calls, breaker changes, scheduler runs and notifications, with no value and no
error text in them."""

import json
import logging

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.storage.base import Storage
from tests.deferred_helpers import build, deferred
from tests.fixtures import CountingHook
from tests.service_helpers import ALICE, ROOT, put

SECRET_VALUE = 424242
SECRET_TEXT = "password=hunter2"


def events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "aduku"]


def test_events_carry_the_facts_and_never_a_value_or_an_error_text(
    backend: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="aduku")
    state = {"down": False}

    def answer(inputs: dict[str, object], attempt: int) -> object:
        if state["down"]:
            raise RuntimeError(SECRET_TEXT)
        return SECRET_VALUE

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook("fx.secret", 1, CountingHook("fx.secret", answer), batch_max=None)

    ttl = chr(34) + "5m" + chr(34)
    fields = (
        f'"/now": {deferred(hook="fx.secret@1", refresh="per_fetch", ttl=ttl, retry="{max: 0}")}, '
        f'"/later": {deferred(hook="fx.secret@1", refresh="[scheduled::{every: " + chr(34) + "1h" + chr(34) + "}, notification::{topic: " + chr(34) + "t" + chr(34) + "}]", retry="{max: 0}")}'
    )
    built = build(backend, fields, extra=register, sources=[])
    for n in range(7):
        built.service.append(f"d{n}", [put("/name", f"N{n}")], contributor=ALICE)
    built.service.get("d0")  # a call that succeeds
    state["down"] = True
    for n in range(1, 7):  # failures until the breaker opens
        built.service.get(f"d{n}")
    built.service.scheduler_run("fx.secret@1", caller=ROOT)
    built.service.notify("t", ["d0"])
    seen = events(caplog)
    names = {e["event"] for e in seen}
    assert {"deferred_call", "breaker", "scheduler_run", "notification"} <= names
    call = next(e for e in seen if e["event"] == "deferred_call")
    assert {"doc", "field", "source", "attempts", "result"} <= set(call)
    assert next(e for e in seen if e["event"] == "breaker")["state"] == "open"
    run = next(e for e in seen if e["event"] == "scheduler_run")
    assert {"source", "attempted", "succeeded", "failed", "skipped", "calls"} <= set(run)
    text = json.dumps(seen)
    assert str(SECRET_VALUE) not in text and "hunter2" not in text and "password" not in text
