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
