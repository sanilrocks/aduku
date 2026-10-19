"""Run every shared vector in conformance/ against both storage backends."""

import pathlib

import pytest

from aduku.storage.base import Storage
from tests.conformance_runner import CONFORMANCE_DIR, build_service, load_case, run_case

CASES = sorted(p for p in CONFORMANCE_DIR.glob("*.ion") if not p.name.endswith(".schema.ion"))
# vectors written first and not yet satisfiable; each entry names the task that makes it pass (then delete the entry)
WAITING: dict[str, str] = {}


def _marked(path: pathlib.Path):  # type: ignore[no-untyped-def]
    reason = WAITING.get(path.stem)
    marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
    return pytest.param(path, id=path.stem, marks=marks)


@pytest.mark.parametrize("path", [_marked(p) for p in CASES])
def test_vector(path: pathlib.Path, backend: Storage) -> None:
    case = load_case(path.read_text())
    run_case(case, build_service(case, backend))
