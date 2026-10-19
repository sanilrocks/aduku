"""Feature 003, research R-7: the metadata a source is registered with (FR-031)."""

import pytest

from aduku.behaviors.registry import BehaviorRegistry
from aduku.errors import SchemaError


def test_a_hook_registered_the_feature_002_way_has_no_source_options() -> None:
    behavior = BehaviorRegistry.with_builtins().register_hook("tax.lookup", 2, lambda inputs, timeout: "x")
    assert (behavior.kind, behavior.purity) == ("hook", "io")
    assert (behavior.batch_max, behavior.rate_limit, behavior.with_context) == (None, None, False)


def test_a_source_may_declare_a_batch_maximum_a_rate_limit_and_a_context() -> None:
    behavior = BehaviorRegistry.with_builtins().register_hook(
        "risk.band", 1, lambda items, timeout: [], batch_max=50, rate_limit=2.5, with_context=True
    )
    assert (behavior.batch_max, behavior.rate_limit, behavior.with_context) == (50, 2.5, True)


@pytest.mark.parametrize("options", [{"batch_max": 0}, {"batch_max": -3}, {"rate_limit": 0}, {"rate_limit": -1.0}])
def test_nonsense_options_are_refused(options: dict[str, float]) -> None:
    with pytest.raises(SchemaError):
        BehaviorRegistry.with_builtins().register_hook("h", 1, lambda inputs, timeout: 1, **options)  # type: ignore[arg-type]


def test_the_reserved_names_and_duplicates_are_still_refused() -> None:
    registry = BehaviorRegistry.with_builtins()
    registry.register_hook("h", 1, lambda inputs, timeout: 1, batch_max=2)
    with pytest.raises(SchemaError):
        registry.register_hook("h", 1, lambda inputs, timeout: 1)
    with pytest.raises(SchemaError, match="RESERVED_MACRO_NAME"):
        registry.register_hook("upper", 1, lambda inputs, timeout: 1, rate_limit=1)
