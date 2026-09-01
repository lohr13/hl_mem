"""Shared value objects for read-only diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CheckStatus(StrEnum):
    """诊断检查状态。"""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


_CHECK_CODES = {
    "Python 版本": "python",
    "配置结构": "config_schema",
    "生产就绪": "runtime_readiness",
    "数据库文件": "database",
    "Migration 数量": "migrations",
    "claims_fts rebuild": "fts_rebuild",
    "恢复集": "recovery",
    "密钥配置": "secrets",
    "Embedding API": "embedding",
    "LLM API": "llm",
    "Reranker API": "reranker",
    "Provider 插件": "provider_plugins",
    "Provider 信任": "provider_trust",
    "Provider 用量账本": "usage_ledger",
    "Provider 价格表": "usage_price_book",
    "服务端口": "server_port",
    "Daemon 兼容性": "daemon_contract",
    "Hermes 插件": "hermes_files",
    "Hermes 插件兼容性": "hermes_contract",
    "Context Packet wire": "context_packet",
}


@dataclass(frozen=True)
class CheckResult:
    """单项诊断结果。"""

    status: CheckStatus
    name: str
    detail: str
    code: str = ""

    def __post_init__(self) -> None:
        if not self.code:
            object.__setattr__(self, "code", _CHECK_CODES.get(self.name, "diagnostic"))

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "status": self.status.value,
            "name": self.name,
            "detail": self.detail,
        }
