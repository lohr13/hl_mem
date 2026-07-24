"""v006 canonical attribute 与 conflict key 算法的不可变自包含快照。"""

from __future__ import annotations

import hashlib
import json
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

MUTUALLY_EXCLUSIVE_SLOTS = frozenset(
    {
        "preference.ui_theme",
        "preference.response_style",
        "choice.model",
        "config.port",
        "config.model",
        "state.service_health",
    }
)

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
        (
            ("gpt-", "glm-", "qwen", "claude", "gemini", "deepseek", "llama", "mistral", "model", "模型"),
            "choice.model",
        ),
        (("api", "sdk", "接口", "openai-compatible"), "choice.api"),
        (("fastapi", "pytorch", "django", "flask", "pytest", "uvicorn", "框架"), "choice.framework"),
        (("百炼", "dashscope", "智谱", "zhipu", "openai", "anthropic", "provider", "供应商"), "choice.provider"),
        (("http", "https", "grpc", "websocket", "sse", "mcp", "协议"), "choice.protocol"),
        (("hl_mem", "memos", "memory system", "记忆系统"), "choice.memory_system"),
    ),
    "状态": (
        (("挂了", "健康", "正常", "ok"), "state.service_health"),
        (("进程", "运行中"), "state.process"), (("部署",), "state.deployment"),
        (("passed", "failed", "pytest", "测试通过", "测试数", "测试"), "state.test_suite"),
        (("超时", "不可达", "连接"), "state.connectivity"), (("任务", "job"), "state.job"),
    ),
    "身份": (
        (("姓名", "名字", "昵称"), "identity.name"),
        (("角色", "开发者", "工程师"), "identity.role"),
        (("邮箱", "电话", "email"), "identity.contact"),
        (("账号", "用户名", "account"), "identity.account"),
    ),
    "配置": (
        (
            (
                "环境变量", "http_proxy", "https_proxy", "no_proxy", "api_key",
                "llm_model", "embedding_model", "reranker_model", "env",
            ),
            "config.env",
        ),
        (
            (
                "base_url", "endpoint", "hostname", "localhost", "127.0.0.1",
                "ipv4", "host", "域名", "代理", "网络", "network",
            ),
            "config.network",
        ),
        (("路径", "目录", "文件", "path", ".py", ".toml", ".json", ".db"), "config.path"),
        (("端口", "port", "listen", "监听"), "config.port"),
        (("模型名", "model="), "config.model"),
        (("百炼", "dashscope", "智谱", "zhipu", "openai", "anthropic", "provider", "供应商"), "config.provider"),
        (("路由", "直连"), "config.routing"),
        (("timeout", "超时"), "config.timeout"), (("cron", "定时", "schedule"), "config.schedule"),
        (("gpu", "显卡", "硬件"), "config.hardware"),
    ),
    "计划": (
        (("截止", "deadline", "之前"), "plan.deadline"), (("决定", "选择", "不切换"), "plan.decision"),
        (("迁移",), "plan.migration"), (("评测", "evaluation"), "plan.evaluation"),
        (("计划", "打算", "目标"), "plan.goal"),
    ),
    "事实": (
        (("当前采用", "当前使用", "选择了", "codex"), "fact.tool_choice"),
        (("支持", "具备", "能力"), "fact.capability"),
        (("已实现", "实现了", "新增", "接入", "修复实现"), "fact.implementation"),
        (("缺陷", "问题", "bug"), "fact.issue"), (("因为", "原因"), "fact.cause"),
        (("已修复", "解决"), "fact.resolution"), (("只允许", "必须", "约束"), "fact.constraint"),
        (("项目", "成员"), "fact.project_membership"),
    ),
}

_HIGH_CONFIDENCE_ATTRIBUTE_PATTERNS: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    "使用": (
        (
            re.compile(r"(?i)(?:gpt-|glm-|qwen|claude|gemini|deepseek|llama|mistral|embedding|rerank)"),
            "choice.model",
        ),
        (
            re.compile(r"(?i)(?:百炼|dashscope|智谱|zhipu|openai|anthropic|\bprovider\b|供应商)"),
            "choice.provider",
        ),
        (re.compile(r"(?i)(?:openai-compatible|\b(?:http|https|grpc|websocket|sse|mcp)\b|协议)"), "choice.protocol"),
    ),
    "配置": (
        (
            re.compile(
                r"(?i)(?:\b[A-Z][A-Z0-9_]*(?:_URL|_HOST|_PORT)\b|"
                r"\b(?:HTTP_PROXY|HTTPS_PROXY|NO_PROXY|API_KEY|LLM_MODEL|EMBEDDING_MODEL|RERANKER_MODEL)\b)"
            ),
            "config.env",
        ),
        (
            re.compile(
                r"(?i)(?:https?://|(?:\b(?:\d{1,3}\.){3}\d{1,3}\b)|"
                r"\blocalhost\b|\b(?:host|hostname|endpoint|base_url)\b)"
            ),
            "config.network",
        ),
        (
            re.compile(
                r"(?i)(?:\b[A-Z]:[\\/]|\\\\[^\\\s]+\\[^\\\s]+|"
                r"(?:^|[\s'\"=])(?:\.{1,2}[\\/]|src[\\/])[\w./\\-]+|"
                r"[\w./\\-]+\.(?:py|toml|json|db)\b)"
            ),
            "config.path",
        ),
        (
            re.compile(
                r"(?i)(?:端口|\bport\b|\blisten(?:ing)?\b|监听)\D{0,12}"
                r"(?:[1-9]\d{0,3}|[1-5]\d{4}|6[0-4]\d{3}|65[0-4]\d{2}|655[0-2]\d|6553[0-5])\b"
            ),
            "config.port",
        ),
        (re.compile(r"(?i)(?:\b(?:LLM_MODEL|EMBEDDING_MODEL|RERANKER_MODEL)\b|模型名|\bmodel\s*=)"), "config.model"),
        (
            re.compile(r"(?i)(?:百炼|dashscope|智谱|zhipu|openai|anthropic|\bprovider\b|供应商)"),
            "config.provider",
        ),
    ),
    "状态": (
        (re.compile(r"(?i)(?:\bpassed\b|\bfailed\b|pytest|测试通过|测试数)"), "state.test_suite"),
        (re.compile(r"(?i)(?:部署|deployed|上线|发布)"), "state.deployment"),
    ),
    "事实": (
        (re.compile(r"(?:已实现|新增|接入|支持|修复实现)"), "fact.implementation"),
    ),
}


def _high_confidence_attribute(predicate: str, text: str) -> str | None:
    """按从精确到宽泛的命名模式推断高置信 canonical attribute。"""
    for pattern, attribute in _HIGH_CONFIDENCE_ATTRIBUTE_PATTERNS.get(predicate, ()):
        if pattern.search(text):
            return attribute
    return None


def normalize_predicate(predicate: str) -> str:
    """把标准及历史 predicate 归一化为映射表键。"""
    normalized = unicodedata.normalize("NFKC", str(predicate)).strip()
    return PREDICATE_NORMALIZE.get(normalized.casefold(), normalized)


def normalize_canonical_attribute(attribute: str) -> str:
    """把 LLM 属性字符串归一化并应用受控别名。"""
    normalized = unicodedata.normalize("NFKC", str(attribute)).strip().casefold().replace("-", "_")
    normalized = re.sub(r"\s+", "", normalized)
    return ATTRIBUTE_ALIASES.get(normalized, normalized)


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
    precise = _high_confidence_attribute(normalized_predicate, text)
    if precise is not None:
        return precise
    for hints, attribute in ATTRIBUTE_HINTS.get(normalized_predicate, ()):
        if any(hint in text for hint in hints):
            return attribute
    return mapping[1]


def reconcile_canonical_attribute(
    predicate: str,
    llm_attribute: str | None,
    inferred_attribute: str,
    subject: str,
    value: Any,
    qualifiers: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """仅在 LLM 回退或高置信规则命中时协调 canonical attribute。"""
    normalized_predicate = normalize_predicate(predicate)
    mapping = PREDICATE_ATTRIBUTE_MAP.get(normalized_predicate)
    validated = validate_canonical_attribute(normalized_predicate, llm_attribute)
    if mapping is None:
        return validated, "unknown_predicate"

    allowed, fallback = mapping
    text = unicodedata.normalize("NFKC", f"{subject} {value} {qualifiers or {}}").casefold()
    precise = _high_confidence_attribute(normalized_predicate, text)
    if precise is not None and precise in allowed:
        return precise, "high_confidence_rule"

    normalized_inferred = validate_canonical_attribute(normalized_predicate, inferred_attribute)
    if validated in {fallback, "custom.unknown"} and normalized_inferred in allowed:
        return normalized_inferred, "fallback_reconciled"
    return validated, "llm_preserved"


def canonical_conflict_slot(attribute: str) -> str:
    """返回经校验的 canonical conflict slot，不跨属性合并。"""
    normalized = normalize_canonical_attribute(attribute)
    return normalized if normalized in ATTRIBUTE_ALLOWLIST else "custom.unknown"


def is_mutually_exclusive_attribute(attribute: str | None) -> bool:
    """判断 canonical attribute 是否可参与确定性冲突检测。"""
    if not attribute:
        return False
    return canonical_conflict_slot(attribute) in MUTUALLY_EXCLUSIVE_SLOTS


EXCLUSIVE_QUALIFIERS = {"scope", "context", "environment", "project", "channel"}


def compute_conflict_key(
    namespace: str,
    subject: str,
    canonical_attribute: str,
    qualifiers: dict[str, Any] | None,
    *,
    version: int = 2,
) -> str:
    """按 v006 冻结规则计算 canonical attribute v2 冲突键。"""
    if version != 2:
        raise ValueError("compute_conflict_key only supports version 2")
    canonical_namespace = unicodedata.normalize("NFKC", namespace).strip().casefold()
    canonical_subject = re.sub(r"\s+", "", unicodedata.normalize("NFKC", subject)).casefold()
    exclusive = {
        key: _canonicalize_json(value)
        for key, value in (qualifiers or {}).items()
        if key in EXCLUSIVE_QUALIFIERS
    }
    slot = canonical_conflict_slot(normalize_canonical_attribute(canonical_attribute))
    raw = json.dumps(
        ["v2", canonical_namespace, canonical_subject, slot, exclusive],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_legacy_conflict_key(
    namespace: str,
    subject: str,
    predicate: str,
    qualifiers: dict[str, Any] | None,
) -> str:
    """按 v006 冻结规则复现 v1 冲突键算法。"""
    canonical_subject = re.sub(r"\s+", "", subject).casefold()
    exclusive = {key: value for key, value in (qualifiers or {}).items() if key in EXCLUSIVE_QUALIFIERS}
    raw = json.dumps(
        [namespace.casefold(), canonical_subject, predicate.casefold(), exclusive],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _canonicalize_json(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value).strip().casefold()
    if isinstance(value, dict):
        return {str(key): _canonicalize_json(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonicalize_json(item) for item in value]
    return value
