"""A custom validation rule for trying the rule library from the command line (`aduku --behaviors demo_rules ...`).

`acme.tax_id@1` is a field rule with two declared arguments: `country` (text, required) and `strict` (true or false, optional).
A tax id must start with the country code; with `strict` it must also be at least 5 characters long. The loader checks every
use of it against that declaration, so a schema that leaves out `country` or passes `strict: "yes"` does not load.
"""

from __future__ import annotations

from typing import Any

from aduku.behaviors.registry import ArgSpec, BehaviorRegistry
from aduku.model import Violation


def check_tax_id(subject: Any) -> list[Violation]:
    value = subject.value
    if not isinstance(value, str):
        return []
    if not value.startswith(subject.args["country"]):
        return [Violation("acme.tax_id", subject.path, "must start with the country code")]
    if subject.args.get("strict") and len(value) < 5:
        return [Violation("acme.tax_id", subject.path, "must be at least 5 characters")]
    return []


def register(behaviors: BehaviorRegistry) -> None:
    behaviors.register_validate(
        "acme.tax_id",
        1,
        check_tax_id,
        args=(ArgSpec("country", "text"), ArgSpec("strict", "bool", required=False)),
        applies="field",
    )
