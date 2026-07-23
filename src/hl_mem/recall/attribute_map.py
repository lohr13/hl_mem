"""受控 canonical attribute 映射、校验与确定性推断。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


PREDICATE_NORMALIZE = {
    "prefers": "偏好", "preference": "偏好", "偏好": "偏好", "喜欢": "偏好",
    "uses": "使用", "use": "使用", "使用": "使用", "用": "使用",
    "status": "状态", "service_status": "状态", "状态": "状态",
    "identity": "身份", "身份": "身份", "config": "配置", "配置": "配置",
    "plan": "计划", "计划": "计划", "fact": "事实", "事实": "事实",
    "explicit_memory": "explicit_memory",
}

PREDICATE_ATTRIBUTE_MAP: dict[str, tuple[tuple[str, ...], str]] = {
    "偏好": ((
        "preference.ui_theme", "preference.response_style", "preference.workflow",
        "preference.architecture", "preference.tool_choice", "preference.other",
    ), "preference.other"),
    "使用": ((
        "choice.tool", "choice.database", "choice.os", "choice.model", "choice.api",
        "choice.framework", "choice.provider", "choice.protocol", "choice.memory_system",
    ), "choice.tool"),
    "状态": ((
        "state.service_health", "state.process", "state.deployment", "state.test_suite",
        "state.connectivity", "state.job", "state.other",
    ), "state.other"),
    "身份": ((
        "identity.name", "identity.role", "identity.contact", "identity.account", "identity.other",
    ), "identity.other"),
    "配置": ((
        "config.port", "config.path", "config.env", "config.network", "config.routing",
        "config.provider", "config.model", "config.timeout", "config.schedule",
        "config.hardware", "config.other",
    ), "config.other"),
    "计划": ((
        "plan.goal", "plan.deadline", "plan.decision", "plan.migration",
        "plan.evaluation", "plan.other",
    ), "plan.other"),
    "事实": ((
        "fact.capability", "fact.implementation", "fact.issue", "fact.cause",
        "fact.resolution", "fact.constraint", "fact.project_membership",
        "fact.tool_choice", "fact.other",
    ), "fact.other"),
    "explicit_memory": (("memory.explicit",), "memory.explicit"),
}

ATTRIBUTE_ALLOWLIST = frozenset(
    attribute
    for attributes, _fallback in PREDICATE_ATTRIBUTE_MAP.values()
    for attribute in attributes
) | {"custom.unknown"}

ATTRIBUTE_ALIASES = {
    "preference.tool": "preference.tool_choice",
    "choice.tool_choice": "choice.tool",
    "fact.tool": "fact.tool_choice",
}

CONFLICT_SLOT_ALIASES = {
    "preference.tool_choice": "tool_choice",
    "choice.tool": "tool_choice",
    "fact.tool_choice": "tool_choice",
    "choice.database": "database_choice",
    "config.network": "config.port",
}

ATTRIBUTE_HINTS: dict[str, tuple[tuple[tuple[str, ...], str], ...]] = {
    "偏好": (
        (("深色", "浅色", "主题", "theme"), "preference.ui_theme"),
        (("详细", "简洁", "回复", "response"), "preference.response_style"),
        (("工作流", "流程", "workflow"), "preference.workflow"),
        (("本地优先", "架构", "architecture"), "preference.architecture"),
        (("工具", "codex", "v2rayn"), "preference.tool_choice"),
    ),
    "使用": (
        (("sqlite", "postgresql", "postgres", "mysql", "数据库"), "choice.database"),
        (("windows", "linux", "macos", "操作系统"), "choice.os"),
        (("qwen", "gpt", "模型"), "choice.model"),
        (("api", "接口"), "choice.api"), (("fastapi", "pytorch", "框架"), "choice.framework"),
        (("dashscope", "百炼", "provider"), "choice.provider"),
        (("http", "grpc", "协议"), "choice.protocol"),
        (("hl_mem", "memos", "memory system", "记忆系统"), "choice.memory_system"),
    ),
    "状态": (
        (("挂了", "健康", "正常", "ok"), "state.service_health"),
        (("进程", "运行中"), "state.process"), (("部署",), "state.deployment"),
        (("测试", "tests", "pytest"), "state.test_suite"),
        (("超时", "不可达", "连接"), "state.connectivity"), (("任务", "job"), "state.job"),
    ),
    "身份": (
        (("姓名", "名字", "昵称"), "identity.name"),
        (("角色", "开发者", "工程师"), "identity.role"),
        (("邮箱", "电话", "email"), "identity.contact"),
        (("账号", "用户名", "account"), "identity.account"),
    ),
    "配置": (
        (("端口", "port"), "config.port"), (("路径", "目录", "path", "\\", "/"), "config.path"),
        (("环境变量", "no_proxy", "env"), "config.env"),
        (("代理", "网络", "network"), "config.network"), (("路由", "直连"), "config.routing"),
        (("provider", "供应商"), "config.provider"), (("模型", "model"), "config.model"),
        (("timeout", "超时", "秒"), "config.timeout"), (("cron", "定时", "schedule"), "config.schedule"),
        (("gpu", "显卡", "硬件"), "config.hardware"),
    ),
    "计划": (
        (("截止", "deadline", "之前"), "plan.deadline"), (("决定", "选择", "不切换"), "plan.decision"),
        (("迁移",), "plan.migration"), (("评测", "evaluation"), "plan.evaluation"),
        (("计划", "打算", "目标"), "plan.goal"),
    ),
    "事实": (
        (("当前采用", "当前使用", "选择了", "codex"), "fact.tool_choice"),
        (("支持", "具备", "能力"), "fact.capability"), (("已实现", "实现了"), "fact.implementation"),
        (("缺陷", "问题", "bug"), "fact.issue"), (("因为", "原因"), "fact.cause"),
        (("已修复", "解决"), "fact.resolution"), (("只允许", "必须", "约束"), "fact.constraint"),
        (("项目", "成员"), "fact.project_membership"),
    ),
}


def normalize_predicate(predicate: str) -> str:
    """把标准及历史 predicate 归一化为映射表键。"""
    normalized = unicodedata.normalize("NFKC", str(predicate)).strip()
    return PREDICATE_NORMALIZE.get(normalized.casefold(), normalized)


def normalize_canonical_attribute(attribute: str) -> str:
    """把 LLM 属性字符串归一化并应用受控别名。"""
    normalized = unicodedata.normalize("NFKC", str(attribute)).strip().casefold().replace("-", "_")
    normalized = re.sub(r"\s+", "", normalized)
    return ATTRIBUTE_ALIASES.get(normalized, normalized)


def is_non_exclusive_attribute(attribute: str | None) -> bool:
    """判断 canonical attribute 是否为不能据此推断冲突的共享兜底槽。"""
    if not attribute:
        return False
    normalized = normalize_canonical_attribute(attribute)
    return normalized.endswith(".other") or normalized in {"memory.explicit", "custom.unknown"}


def validate_canonical_attribute(predicate: str, attribute: str | None) -> str:
    """校验属性是否属于 predicate 的允许集合，否则确定性回退。"""
    normalized_predicate = normalize_predicate(predicate)
    mapping = PREDICATE_ATTRIBUTE_MAP.get(normalized_predicate)
    if mapping is None:
        return "custom.unknown"
    allowed, fallback = mapping
    if not attribute:
        return fallback
    normalized_attribute = normalize_canonical_attribute(attribute or "")
    if normalized_attribute not in ATTRIBUTE_ALLOWLIST:
        return "custom.unknown"
    return normalized_attribute if normalized_attribute in allowed else fallback


def infer_canonical_attribute(
    predicate: str,
    subject: str,
    value: Any,
    qualifiers: dict[str, Any] | None = None,
) -> str:
    """根据历史 claim 内容确定性推断 canonical attribute。"""
    normalized_predicate = normalize_predicate(predicate)
    mapping = PREDICATE_ATTRIBUTE_MAP.get(normalized_predicate)
    if mapping is None:
        return "custom.unknown"
    text = unicodedata.normalize("NFKC", f"{subject} {value} {qualifiers or {}}").casefold()
    for hints, attribute in ATTRIBUTE_HINTS.get(normalized_predicate, ()):
        if any(hint in text for hint in hints):
            return attribute
    return mapping[1]


def canonical_conflict_slot(attribute: str) -> str:
    """将细粒度 canonical attribute 归并为互斥 conflict slot。"""
    normalized = normalize_canonical_attribute(attribute)
    if normalized not in ATTRIBUTE_ALLOWLIST:
        normalized = "custom.unknown"
    return CONFLICT_SLOT_ALIASES.get(normalized, normalized)
