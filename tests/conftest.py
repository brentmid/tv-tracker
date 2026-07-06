import sys
from pathlib import Path

import pytest

# Make `import tvtracker` work when pytest is run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tvtracker import db  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "network: hits the real network (excluded by the pre-commit hook)"
    )


@pytest.fixture
def conn(tmp_path):
    """A fresh on-disk DB per test (on-disk so WAL + reconnects behave real)."""
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()
