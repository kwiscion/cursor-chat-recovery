from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def minimal_db() -> Path:
    return FIXTURES_DIR / "globalStorage_minimal.vscdb"


@pytest.fixture
def table_db() -> Path:
    return FIXTURES_DIR / "globalStorage_table_mode.vscdb"


@pytest.fixture
def drift_db() -> Path:
    return FIXTURES_DIR / "globalStorage_schema_drift.vscdb"


@pytest.fixture
def corrupt_db() -> Path:
    return FIXTURES_DIR / "globalStorage_corrupt.vscdb"


@pytest.fixture
def workspace_storage_dir() -> Path:
    return FIXTURES_DIR / "workspaceStorage"
