"""离线评测的 pytest 选项与共享夹具。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hl_mem.api.server import create_app
from hl_mem.storage.database import Database


def pytest_addoption(parser: pytest.Parser) -> None:
    """注册快照和报告路径选项。"""
    group = parser.getgroup("hl-mem-eval")
    group.addoption("--eval-db", action="store", default=None, help="只读评测源 SQLite 快照")
    group.addoption("--eval-report", action="store", default=None, help="评测 JSON 报告输出路径")


@pytest.fixture
def eval_report_path(pytestconfig: pytest.Config, tmp_path: Path) -> Path:
    """返回显式配置或临时的评测报告路径。"""
    configured = pytestconfig.getoption("--eval-report")
    return Path(configured) if configured else tmp_path / "recall-v2-report.json"


@pytest.fixture
def eval_database_path(pytestconfig: pytest.Config, tmp_path: Path) -> Path:
    """复制用户快照，确保评测执行不会改写源文件。"""
    configured = pytestconfig.getoption("--eval-db")
    target = tmp_path / "eval.db"
    if configured:
        source = Path(configured).resolve()
        if not source.is_file():
            pytest.fail(f"--eval-db 不存在: {source}")
        shutil.copy2(source, target)
    else:
        database = Database(target)
        database.open()
        database.close()
    return target


@pytest.fixture
def eval_client(eval_database_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """创建不访问外部 API 的确定性 FastAPI 客户端。"""
    monkeypatch.setenv("HL_MEM_EMBEDDER", "fake")
    monkeypatch.setenv("HL_MEM_RERANKER", "off")
    with TestClient(create_app(eval_database_path)) as client:
        yield client
