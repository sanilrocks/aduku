"""contracts/library-api.md: the names a user imports from the package."""

import importlib


def test_the_contract_import_works() -> None:
    from aduku import Aduku, Clock, IdSource, SqliteStorage  # noqa: F401


def test_every_exported_name_resolves() -> None:
    package = importlib.import_module("aduku")
    assert {"Aduku", "SqliteStorage", "MemoryStorage", "Clock", "IdSource", "SchemaRegistry", "load_schema"} <= set(
        package.__all__
    )
    for name in package.__all__:
        assert getattr(package, name) is not None, name


def test_an_unknown_name_is_an_attribute_error() -> None:
    import pytest

    import aduku

    with pytest.raises(AttributeError):
        aduku.NoSuchThing  # noqa: B018


def test_every_type_the_computed_fields_contract_names_is_importable() -> None:
    """contracts/library-api.md (feature 002): the report and info types, the fault, and the hook runner."""
    import aduku
    from aduku import ComputeFault, EffectInfo, HookRunner, RecomputeReport  # noqa: F401

    assert {"ComputeFault", "EffectInfo", "HookRunner", "RecomputeReport"} <= set(aduku.__all__)


def test_every_type_the_deferred_fields_contract_names_is_importable() -> None:
    """contracts/library-api.md (feature 003): the status, info and report types."""
    import aduku
    from aduku import DeferredInfo, DeferredStatus, SchedulerReport  # noqa: F401

    assert {"DeferredInfo", "DeferredStatus", "SchedulerReport"} <= set(aduku.__all__)


def test_the_argument_declaration_of_a_rule_is_importable() -> None:
    """contracts/library-api.md (feature 004): rules are registered with ArgSpec arguments."""
    import aduku
    from aduku import ArgSpec  # noqa: F401

    assert "ArgSpec" in aduku.__all__


def test_every_name_the_schema_evolution_contract_uses_is_importable() -> None:
    """contracts/library-api.md (feature 006): the reports, the change list, the history record and the shadow reason."""
    import aduku
    from aduku import (  # noqa: F401
        ActivationReport,
        Change,
        ChangeList,
        DocumentChange,
        DryRunReport,
        HistoryRecord,
        MigrationProgress,
        MigrationReport,
        Shadowed,
        schema_changes,
    )

    assert {"ActivationReport", "ChangeList", "DryRunReport", "MigrationReport", "Shadowed", "schema_changes"} <= set(
        aduku.__all__
    )
