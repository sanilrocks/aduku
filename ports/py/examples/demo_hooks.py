"""Behaviors for trying computed fields from the command line (`aduku --behaviors demo_hooks ...`).

The library registers behaviors in code, so the command line asks for a module with a `register(behaviors)` function.
The names are the conformance fixtures: `acme.label@1` (a custom macro) and the hooks `fx.echo@1`, `fx.fail@1` and
`fx.flaky@1`. `fx.flaky` fails the first time it is called in a process, so a `recompute` right after shows a retry work.
"""

from __future__ import annotations

from typing import Any

from aduku.behaviors.registry import BehaviorRegistry

_seen: set[str] = set()


def _echo(inputs: dict[str, Any], timeout: float) -> str:
    return "tax-" + str(inputs["/country"])


def _fail(inputs: dict[str, Any], timeout: float) -> str:
    raise RuntimeError("the registry is down")


def _flaky(inputs: dict[str, Any], timeout: float) -> str:
    key = repr(sorted(inputs.items()))
    if key not in _seen:
        _seen.add(key)
        raise RuntimeError("try again")
    return _echo(inputs, timeout)


def register(behaviors: BehaviorRegistry) -> None:
    behaviors.register_macro("acme.label", 1, ["name", "city"], 'name + " (" + city + ")"')
    behaviors.register_hook("fx.echo", 1, _echo)
    behaviors.register_hook("fx.fail", 1, _fail)
    behaviors.register_hook("fx.flaky", 1, _flaky)
