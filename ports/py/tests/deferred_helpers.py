"""Builders for the tests of deferred fields (feature 003): a service on a manual clock with fixture sources."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aduku.behaviors.registry import BehaviorRegistry
from aduku.hooks import HookRunner
from aduku.ion_codec import Annotated
from aduku.schema.loader import load_schema
from aduku.schema.registry import SchemaRegistry
from aduku.service import Aduku
from aduku.storage.base import SequentialIds, Storage
from tests.fixtures import CountingHook, ManualClock, register_fixtures, register_sources
from tests.service_helpers import schema_text

START = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
PLAIN = (
    '"/name": {type: string, merge: lww}, "/country": {type: string, merge: lww}, '
    '"/currency": {type: string, merge: lww, default: "USD"}, '
    '"/shout": {type: string, computed: {macro: upper, args: ["/name"]}}'
)


def deferred(
    hook: str = "fx.score@1",
    depends: str = '"/name"',
    refresh: str = "once",
    type_: str = "int",
    invalidate_by: str = "[admin]",
    **options: str,
) -> str:
    """The text of one deferred field; extra options are added inside `deferred:` (`on_missing` goes on the field)."""
    on_missing = options.pop("on_missing", None)
    body = f'hook: "{hook}", depends: [{depends}], refresh: {refresh}, invalidate_by: {invalidate_by}'
    for key, value in options.items():
        body += f", {key}: {value}"
    head = f"type: {type_}" + (f", on_missing: {on_missing}" if on_missing else "")
    return f"{{{head}, deferred: {{{body}}}}}"


class Built:
    """A service, its clock, its behaviors and the storage, so a test can reach any of them."""

    def __init__(self, service: Aduku, clock: ManualClock, behaviors: BehaviorRegistry, storage: Storage) -> None:
        self.service = service
        self.clock = clock
        self.behaviors = behaviors
        self.storage = storage

    def calls(self, source: str) -> int:
        name, _, version = source.partition("@")
        behavior = self.behaviors.get(name, int(version or 1))
        assert behavior is not None
        return int(behavior.impl.calls)  # type: ignore[attr-defined]

    def stored(self, doc: str = "d1") -> dict[str, Any]:
        with self.storage.read() as tx:
            return {r.field: r for r in tx.get_derived(doc)}

    def add_schema(self, text: str, activate: bool = True) -> None:
        schema = load_schema(text, self.behaviors)
        self.service.schemas.put(schema)
        if activate:
            self.service.schemas.activate(schema.version)


def build(
    storage: Storage,
    fields: str,
    *,
    top: str = "",
    sources: list[str] | None = None,
    extra: Callable[[BehaviorRegistry], None] | None = None,
    real_time: bool = False,
    prefix: str = "x",
    **service_options: Any,
) -> Built:
    behaviors = BehaviorRegistry.with_builtins()
    register_fixtures(behaviors)
    register_sources(behaviors, sources)
    if extra is not None:
        extra(behaviors)
    clock = ManualClock(START)
    registry = SchemaRegistry()
    registry.put(
        load_schema(
            schema_text(fields=PLAIN + ", " + fields, lenient=f"{top} lenient: {{allowed: [backfill]}},"), behaviors
        )
    )
    registry.activate(1)
    service = Aduku(
        storage,
        registry,
        clock=clock,
        ids=SequentialIds(prefix),
        behaviors=behaviors,
        **(
            {}  # real monotonic time and real sleeps: for tests of deadlines and timeouts (the wall clock stays manual)
            if real_time
            else {
                "monotonic": clock.monotonic,
                "hook_runner": HookRunner(sleep=clock.sleep, monotonic=clock.monotonic),
                "sleep": clock.sleep,
            }
        ),
        **service_options,
    )
    return Built(service, clock, behaviors, storage)


def counting(name: str, answer: Callable[[dict[str, object], int], object]) -> Callable[[BehaviorRegistry], None]:
    """An `extra` that registers a counting hook `name@1`."""

    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook(name, 1, CountingHook(name, answer))

    return register


def deferred_value(result: Any, key: str) -> Any:
    """The value of a deferred field in a read result (without its annotation), or None if it is left out."""
    value = result.document.get(key)
    return value.value if isinstance(value, Annotated) else value


class Delayed:
    """A source that takes `delay` real seconds to answer (or never, with `delay=None` until `release` is set)."""

    def __init__(self, name: str, delay: float | None, answer: object = 1) -> None:
        import threading

        self.name = name
        self.delay = delay
        self.answer = answer
        self.calls = 0
        self.release = threading.Event()

    def __call__(self, inputs: dict[str, object], timeout: float) -> object:
        import time

        self.calls += 1
        if self.delay is None:
            self.release.wait(30)
        else:
            time.sleep(self.delay)
        return self.answer


def delayed(name: str, delay: float | None, answer: object = 1) -> Callable[[BehaviorRegistry], None]:
    def register(behaviors: BehaviorRegistry) -> None:
        behaviors.register_hook(name, 1, Delayed(name, delay, answer))

    return register


def together(*extras: Callable[[BehaviorRegistry], None]) -> Callable[[BehaviorRegistry], None]:
    def register(behaviors: BehaviorRegistry) -> None:
        for extra in extras:
            extra(behaviors)

    return register
