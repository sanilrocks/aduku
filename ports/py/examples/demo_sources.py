"""Sources for trying deferred fields from the command line (`aduku --behaviors demo_sources ...`).

The library registers behaviors in code, so the command line asks for a module with a `register(behaviors)` function.
The names are the conformance fixtures: `fx.score@1` (answers 712), `fx.rate@1` (answers the decimal 0.92),
`fx.limit@1` (ten times the score it is given), `fx.batch@1` (a batch source for up to 50 documents) and
`sanctions.check@1`. Each call is written to standard error, so a walkthrough can see when a source is asked. Set
`DEMO_DOWN=1` to make `fx.rate@1` fail.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from typing import Any

from aduku.behaviors.registry import BehaviorRegistry


def _called(name: str, detail: str = "") -> None:
    print(f"demo: {name} called{detail}", file=sys.stderr)


def _score(inputs: dict[str, Any], timeout: float) -> int:
    _called("fx.score")
    return 712


def _rate(inputs: dict[str, Any], timeout: float) -> Decimal:
    _called("fx.rate")
    if os.environ.get("DEMO_DOWN") == "1":
        raise RuntimeError("the rate service is down")
    return Decimal("0.92")


def _limit(inputs: dict[str, Any], timeout: float) -> int:
    _called("fx.limit")
    return int(inputs["/creditScore"]) * 10


def _batch(items: list[dict[str, Any]], timeout: float) -> list[str]:
    _called("fx.batch", f" with {len(items)} item(s)")
    return ["band-" + str(item.get("/country")) for item in items]


def _sanctions(inputs: dict[str, Any], timeout: float) -> str:
    _called("sanctions.check")
    return "clear"


def register(behaviors: BehaviorRegistry) -> None:
    behaviors.register_hook("fx.score", 1, _score)
    behaviors.register_hook("fx.rate", 1, _rate)
    behaviors.register_hook("fx.limit", 1, _limit)
    behaviors.register_hook("fx.batch", 1, _batch, batch_max=50)
    behaviors.register_hook("sanctions.check", 1, _sanctions)
