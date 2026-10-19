"""Feature 002, research R-2 and contracts/expression-language.md: deterministic limits for custom macros."""

import pytest

from aduku.errors import SchemaError
from aduku.expr.limits import DEFAULTS, MAXIMUM, Limits


def test_platform_defaults_and_maximum() -> None:
    assert (DEFAULTS.steps, DEFAULTS.expression_chars, DEFAULTS.depth) == (10_000, 2_000, 32)
    assert (DEFAULTS.value_bytes, DEFAULTS.input_bytes) == (64 * 1024, 256 * 1024)
    assert (MAXIMUM.steps, MAXIMUM.expression_chars, MAXIMUM.depth) == (100_000, 10_000, 64)
    assert (MAXIMUM.value_bytes, MAXIMUM.input_bytes) == (1024 * 1024, 256 * 1024)


def test_no_limits_given_means_the_defaults() -> None:
    assert Limits.for_macro() == DEFAULTS


def test_a_registration_may_lower_or_raise_up_to_the_maximum() -> None:
    lowered = Limits.for_macro(steps=500, depth=8)
    assert (lowered.steps, lowered.depth) == (500, 8) and lowered.value_bytes == DEFAULTS.value_bytes
    raised = Limits.for_macro(steps=50_000)
    assert raised.steps == 50_000
    assert Limits.for_macro(steps=MAXIMUM.steps).steps == MAXIMUM.steps


@pytest.mark.parametrize(
    "change",
    [
        {"steps": 100_001},
        {"expression_chars": 10_001},
        {"depth": 65},
        {"value_bytes": 1024 * 1024 + 1},
        {"input_bytes": 256 * 1024 + 1},
    ],
)
def test_a_limit_above_the_maximum_is_refused(change: dict[str, int]) -> None:
    with pytest.raises(SchemaError):
        Limits.for_macro(**change)


@pytest.mark.parametrize("change", [{"steps": 0}, {"depth": -1}, {"value_bytes": 0}])
def test_a_limit_must_be_positive(change: dict[str, int]) -> None:
    with pytest.raises(SchemaError):
        Limits.for_macro(**change)


def test_an_unknown_limit_name_is_refused() -> None:
    with pytest.raises(SchemaError):
        Limits.for_macro(minutes=5)  # type: ignore[arg-type]
