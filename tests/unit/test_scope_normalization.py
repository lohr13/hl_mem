"""scope 确定性降级策略测试。"""

from __future__ import annotations

import pytest

from hl_mem.ingest.llm_extractor import normalize_scope


def _normalize(
    value: str,
    *,
    attribute: str = "fact.other",
    actor_type: str = "user",
    event_type: str = "message",
    source_kind: str = "",
) -> tuple[str, str]:
    """以 permanent 输入调用 scope 策略。"""
    return normalize_scope(
        "permanent",
        "事实",
        None,
        "hl_mem",
        value,
        canonical_attribute=attribute,
        actor_type=actor_type,
        event_type=event_type,
        source_kind=source_kind,
    )


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("GET /healthz 返回 ok", "health_check"),
        ("本次 pytest 共有 443 passed，1 skipped", "tool_snapshot"),
        ("当前进程状态 running", "tool_snapshot"),
        ("HTTP_PROXY=http://127.0.0.1:10808", "runtime_configuration"),
        ("本次执行使用 glm-5.2 模型", "runtime_configuration"),
        ("审查发现 3 个问题", "tool_snapshot"),
        ("Hermes 需要重启才能加载更新后的 adapter", "explicit_temporal_signal"),
    ],
)
def test_high_volatility_values_downgrade_permanent_scope(value: str, reason: str) -> None:
    assert _normalize(value) == ("temporal", reason)


def test_tool_and_quoted_reports_downgrade_permanent_scope() -> None:
    assert _normalize("命令输出了一条快照", actor_type="tool", event_type="tool_result") == (
        "temporal",
        "tool_snapshot",
    )
    assert _normalize("项目此前采用旧实现", source_kind="quoted_report") == ("temporal", "quoted_report")


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("identity.role", "用户是 AI/CV 研究者"),
        ("preference.workflow", "用户偏好先读代码再修改"),
        ("config.port", "hl_mem 固定端口为 8200"),
        ("config.path", "PostgreSQL 数据目录固定为 D:/postgresql/data"),
    ],
)
def test_durable_attributes_remain_permanent(attribute: str, value: str) -> None:
    assert (
        _normalize(
            value,
            attribute=attribute,
            actor_type="tool",
            event_type="tool_result",
        )[0]
        == "permanent"
    )


def test_runtime_proxy_is_not_protected_by_config_family() -> None:
    assert _normalize(
        "NO_PROXY=localhost,127.0.0.1",
        attribute="config.env",
        actor_type="tool",
    ) == ("temporal", "runtime_configuration")


def test_temporal_scope_is_never_upgraded() -> None:
    assert normalize_scope(
        "temporal",
        "身份",
        None,
        "用户",
        "用户是开发者",
        canonical_attribute="identity.role",
    ) == ("temporal", "llm_preserved")
