"""Plan performance goals for rules: a worst-shape pattern on a long value, and 50 rule declarations on a big document (SC-008)."""

import statistics
import time

from aduku.behaviors.pattern import compile_pattern
from aduku.storage.memory import MemoryStorage
from tests.rule_helpers import USER, build, set_

RULES = ['pattern::{regex: "[a-z0-9-]+"}', "min_len::{n: 1}", 'one_of::{values: ["v0", "v1", "v2", "v3"]}']


def fields(count: int, with_rules: bool) -> str:
    out = []
    for n in range(count):
        rule = f", validate: [{RULES[n % 3]}]" if with_rules else ""
        out.append(f'"/f{n}": {{type: string, merge: lww{rule}}}')
    return ", ".join(out)


def test_a_worst_shape_pattern_checks_a_ten_thousand_character_value_in_under_a_hundred_milliseconds() -> None:
    value = "a" * 10_000
    for text in ("(a|aa)*b", "(a*)*b", "((a|b)*)*c"):
        started = time.perf_counter()
        compile_pattern(text).matches(value)
        assert time.perf_counter() - started < 0.1, text


def median_ms(call, repeats: int = 7) -> float:  # type: ignore[no-untyped-def]
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def test_fifty_rule_declarations_add_under_fifty_milliseconds_to_deriving_a_thousand_entry_document() -> None:
    plain = build(MemoryStorage(), fields(50, False), prefix="p")
    ruled = build(MemoryStorage(), fields(50, True), prefix="r")
    for service in (plain, ruled):
        for batch in range(20):  # 1,000 entries: 50 fields written 20 times over
            entries = [set_(f"/f{n}", f"v{(n + batch) % 3}") for n in range(50)]
            service.append("d1", entries, contributor=USER)
    assert ruled.get("d1").model_errors == ()
    base, with_rules = median_ms(lambda: plain.rebuild("d1")), median_ms(lambda: ruled.rebuild("d1"))
    assert with_rules - base < 50.0, (base, with_rules)
