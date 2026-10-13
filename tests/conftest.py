from __future__ import annotations

from pathlib import Path

import pytest

from fleet_routing.config import SMALL
from fleet_routing.data import Dataset, generate, load


@pytest.fixture(scope="session")
def small(tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    out: Path = tmp_path_factory.mktemp("data")
    generate(SMALL, out)
    return load(out)
