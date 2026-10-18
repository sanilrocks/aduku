from hypothesis import HealthCheck, settings

settings.register_profile("dev", max_examples=50, deadline=None)
settings.register_profile(
    "ci",
    max_examples=1000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("dev")


import pathlib  # noqa: E402
from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402

from aduku.storage.base import Storage  # noqa: E402
from aduku.storage.memory import MemoryStorage  # noqa: E402
from aduku.storage.sqlite import SqliteStorage  # noqa: E402


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, tmp_path: pathlib.Path) -> Iterator[Storage]:
    """A fresh storage of each kind; service-level tests run against both."""
    storage: Storage = MemoryStorage() if request.param == "memory" else SqliteStorage(str(tmp_path / "t.db"))
    yield storage
    storage.close()
