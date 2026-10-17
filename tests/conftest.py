"""pytest 共享 fixture。"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """脱敏出院小结样例目录。"""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def sample_discharge_path() -> Path:
    """默认脱敏样例（用于最小切片验证）。后续 task 会添加完整 fixtures。"""
    return FIXTURES_DIR / "discharge_01.txt"


@pytest.fixture
def tmp_audit_dir(tmp_path: Path) -> Path:
    """测试用临时审计目录。"""
    d = tmp_path / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def tmp_billing_db(tmp_path: Path) -> Path:
    """测试用临时 SQLite 路径。"""
    return tmp_path / "billing_fields.db"


@pytest.fixture
def tmp_export_dir(tmp_path: Path) -> Path:
    """测试用临时导出目录。"""
    d = tmp_path / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d