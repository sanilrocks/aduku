"""Run every shared vector in conformance/ against both storage backends."""

import pathlib

import pytest

from aduku.storage.base import Storage
from tests.conformance_runner import CONFORMANCE_DIR, build_service, load_case, run_case

CASES = sorted(p for p in CONFORMANCE_DIR.glob("*.ion") if not p.name.endswith(".schema.ion"))


@pytest.mark.parametrize("path", CASES, ids=lambda p: pathlib.Path(p).stem)
def test_vector(path: pathlib.Path, backend: Storage) -> None:
    case = load_case(path.read_text())
    run_case(case, build_service(case, backend))
