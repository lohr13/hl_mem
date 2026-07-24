"""受控 canonical attribute 映射、校验与确定性推断。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SlotDefinition:
    """描述一个兼容 canonical attribute 及其 operational slot 元数据。"""

    name: str
    predicate: str
    description: str
    participates_in_conflict: bool = False
    ttl_class: str = "none"
    required_qualifiers: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    is_operational: bool = False
    is_fallback: bool = False


def _slot(
    name: str,
    predicate: str,
    description: str,
    *,
    participates_in_conflict: bool = False,
    ttl_class: str = "none",
    required_qualifiers: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    examples: tuple[str, ...] = (),
    is_operational: bool = False,
    is_fallback: bool = False,
) -> SlotDefinition:
    """用紧凑声明构造不可变 slot 定义。"""
    return SlotDefinition(
        name=name,
        predicate=predicate,
        description=description,
        participates_in_conflict=participates_in_conflict,
        ttl_class=ttl_class,
        required_qualifiers=required_qualifiers,
        aliases=aliases,
        examples=examples,
        is_operational=is_operational,
        is_fallback=is_fallback,
    )


_SLOT_DEFINITIONS = (
    _slot(
        "preference.ui_theme",
        "偏好",
        "UI 主题偏好",
        participates_in_conflict=True,
        aliases=("theme", "主题"),
        examples=("深色模式",),
        is_operational=True,
    ),
    _slot(
        "preference.response_style",
        "偏好",
        "回复风格偏好",
        participates_in_conflict=True,
        aliases=("style",),
        examples=("简洁",),
        is_operational=True,
    ),
    _slot("preference.workflow", "偏好", "工作流偏好"),
    _slot("preference.architecture", "偏好", "架构偏好"),
    _slot(
        "preference.tool_choice",
        "偏好",
        "工具选择偏好",
        required_qualifiers=("task",),
        examples=("Codex CLI 修改代码",),
        is_operational=True,
    ),
    _slot("preference.other", "偏好", "其他偏好", is_fallback=True),
    _slot(
        "choice.tool",
        "使用",
        "使用的工具",
        required_qualifiers=("role",),
        examples=("Hermes Agent",),
        is_operational=True,
        is_fallback=True,
    ),
    _slot(
        "choice.database",
        "使用",
        "使用的数据库",
        required_qualifiers=("project",),
        examples=("PostgreSQL",),
        is_operational=True,
    ),
    _slot("choice.os", "使用", "使用的操作系统"),
    _slot(
        "choice.model",
        "使用",
        "使用的模型",
        participates_in_conflict=True,
        required_qualifiers=("task",),
        examples=("qwen3.7-plus",),
        is_operational=True,
    ),
    _slot("choice.api", "使用", "使用的 API"),
    _slot("choice.framework", "使用", "使用的框架"),
    _slot(
        "choice.provider",
        "使用",
        "使用的服务商",
        required_qualifiers=("service",),
        examples=("百炼",),
        is_operational=True,
    ),
    _slot("choice.protocol", "使用", "使用的协议"),
    _slot(
        "choice.memory_system",
        "使用",
        "使用的记忆系统",
        required_qualifiers=("project",),
        examples=("hl_mem",),
        is_operational=True,
    ),
    _slot(
        "state.service_health",
        "状态",
        "服务健康状态",
        participates_in_conflict=True,
        ttl_class="short",
        required_qualifiers=("service",),
        examples=("running",),
        is_operational=True,
    ),
    _slot("state.process", "状态", "进程状态"),
    _slot("state.deployment", "状态", "部署状态"),
    _slot("state.test_suite", "状态", "测试套件状态"),
    _slot("state.connectivity", "状态", "连接状态"),
    _slot("state.job", "状态", "任务状态"),
    _slot("state.other", "状态", "其他状态", is_fallback=True),
    _slot("identity.name", "身份", "用户名称", aliases=("name",), examples=("本地小马",), is_operational=True),
    _slot("identity.role", "身份", "用户角色"),
    _slot("identity.contact", "身份", "联系方式"),
    _slot("identity.account", "身份", "账号"),
    _slot("identity.other", "身份", "其他身份信息", is_fallback=True),
    _slot(
        "config.port",
        "配置",
        "服务端口",
        participates_in_conflict=True,
        required_qualifiers=("service",),
        aliases=("port",),
        examples=("8200",),
        is_operational=True,
    ),
    _slot(
        "config.path",
        "配置",
        "文件路径",
        required_qualifiers=("purpose",),
        aliases=("path",),
        examples=("D:/workspace/hl_agent/hl_mem",),
        is_operational=True,
    ),
    _slot(
        "config.env",
        "配置",
        "环境变量",
        required_qualifiers=("key",),
        aliases=("env",),
        examples=("HL_MEM_PORT=8200",),
        is_operational=True,
    ),
    _slot(
        "config.network",
        "配置",
        "网络配置",
        required_qualifiers=("target",),
        aliases=("network",),
        examples=("VLESS proxy on 10808",),
        is_operational=True,
    ),
    _slot("config.routing", "配置", "路由配置"),
    _slot("config.provider", "配置", "服务商配置"),
    _slot("config.model", "配置", "模型配置", participates_in_conflict=True),
    _slot("config.timeout", "配置", "超时配置"),
    _slot("config.schedule", "配置", "调度配置"),
    _slot("config.hardware", "配置", "硬件配置"),
    _slot("config.other", "配置", "其他配置", is_fallback=True),
    _slot("plan.goal", "计划", "计划目标"),
    _slot(
        "plan.deadline",
        "计划",
        "截止日期",
        required_qualifiers=("plan",),
        aliases=("deadline",),
        examples=("Phase 17 完成时间",),
        is_operational=True,
    ),
    _slot("plan.decision", "计划", "计划决策"),
    _slot("plan.migration", "计划", "迁移计划"),
    _slot("plan.evaluation", "计划", "评测计划"),
    _slot("plan.other", "计划", "其他计划", is_fallback=True),
    _slot("fact.capability", "事实", "能力事实"),
    _slot("fact.implementation", "事实", "实现事实"),
    _slot("fact.issue", "事实", "问题事实"),
    _slot("fact.cause", "事实", "原因事实"),
    _slot("fact.resolution", "事实", "解决方案事实"),
    _slot("fact.constraint", "事实", "约束事实"),
    _slot("fact.project_membership", "事实", "项目成员事实"),
    _slot("fact.tool_choice", "事实", "工具选择事实"),
    _slot("fact.other", "事实", "其他事实", is_fallback=True),
    _slot("memory.explicit", "explicit_memory", "显式长期记忆", is_fallback=True),
    _slot("custom.unknown", "", "未知自定义属性", is_fallback=True),
)

SLOT_REGISTRY: dict[str, SlotDefinition] = {definition.name: definition for definition in _SLOT_DEFINITIONS}
OPERATIONAL_SLOT_NAMES = tuple(definition.name for definition in _SLOT_DEFINITIONS if definition.is_operational)

ALLOWED_TOPIC_TAGS = frozenset(
    {
        "fact",
        "preference",
        "config",
        "state",
        "identity",
        "plan",
        "choice",
        "memory",
        "implementation",
        "issue",
        "cause",
        "resolution",
        "constraint",
        "capability",
        "membership",
        "tool_choice",
        "behavior",
        "architecture",
        "decision",
        "requirement",
        "bugfix",
        "dependency",
        "version",
        "migration",
        "evaluation",
        "workflow",
        "test",
        "deployment",
        "process",
        "job",
        "connectivity",
        "hardware",
        "timeout",
        "schedule",
        "routing",
        "protocol",
        "framework",
        "api",
        "os",
        "role",
        "contact",
        "account",
        "goal",
        "other",
    }
)


PREDICATE_NORMALIZE = {
    "prefers": "偏好",
    "preference": "偏好",
    "偏好": "偏好",
    "喜欢": "偏好",
    "uses": "使用",
    "use": "使用",
    "使用": "使用",
    "用": "使用",
    "status": "状态",
    "service_status": "状态",
    "状态": "状态",
    "identity": "身份",
    "身份": "身份",
    "config": "配置",
    "配置": "配置",
    "plan": "计划",
    "计划": "计划",
    "fact": "事实",
    "事实": "事实",
    "explicit_memory": "explicit_memory",
}

PREDICATE_ATTRIBUTE_MAP: dict[str, tuple[tuple[str, ...], str]] = {
    predicate: (
        tuple(definition.name for definition in _SLOT_DEFINITIONS if definition.predicate == predicate),
        next(
            definition.name
            for definition in _SLOT_DEFINITIONS
            if definition.predicate == predicate and definition.is_fallback
        ),
    )
    for predicate in ("偏好", "使用", "状态", "身份", "配置", "计划", "事实", "explicit_memory")
}

ATTRIBUTE_ALLOWLIST = frozenset(SLOT_REGISTRY)

ATTRIBUTE_ALIASES = {
    "preference.tool": "preference.tool_choice",
    "choice.tool_choice": "choice.tool",
    "fact.tool": "fact.tool_choice",
}

MUTUALLY_EXCLUSIVE_SLOTS = frozenset(
    definition.name for definition in _SLOT_DEFINITIONS if definition.participates_in_conflict
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
        (("进程", "运行中"), "state.process"),
        (("部署",), "state.deployment"),
        (("passed", "failed", "pytest", "测试通过", "测试数", "测试"), "state.test_suite"),
        (("超时", "不可达", "连接"), "state.connectivity"),
        (("任务", "job"), "state.job"),
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
                "环境变量",
                "http_proxy",
                "https_proxy",
                "no_proxy",
                "api_key",
                "llm_model",
                "embedding_model",
                "reranker_model",
                "env",
            ),
            "config.env",
        ),
        (
            (
                "base_url",
                "endpoint",
                "hostname",
                "localhost",
                "127.0.0.1",
                "ipv4",
                "host",
                "域名",
                "代理",
                "网络",
                "network",
            ),
            "config.network",
        ),
        (("路径", "目录", "文件", "path", ".py", ".toml", ".json", ".db"), "config.path"),
        (("端口", "port", "listen", "监听"), "config.port"),
        (("模型名", "model="), "config.model"),
        (("百炼", "dashscope", "智谱", "zhipu", "openai", "anthropic", "provider", "供应商"), "config.provider"),
        (("路由", "直连"), "config.routing"),
        (("timeout", "超时"), "config.timeout"),
        (("cron", "定时", "schedule"), "config.schedule"),
        (("gpu", "显卡", "硬件"), "config.hardware"),
    ),
    "计划": (
        (("截止", "deadline", "之前"), "plan.deadline"),
        (("决定", "选择", "不切换"), "plan.decision"),
        (("迁移",), "plan.migration"),
        (("评测", "evaluation"), "plan.evaluation"),
        (("计划", "打算", "目标"), "plan.goal"),
    ),
    "事实": (
        (("当前采用", "当前使用", "选择了", "codex"), "fact.tool_choice"),
        (("支持", "具备", "能力"), "fact.capability"),
        (("已实现", "实现了", "新增", "接入", "修复实现"), "fact.implementation"),
        (("缺陷", "问题", "bug"), "fact.issue"),
        (("因为", "原因"), "fact.cause"),
        (("已修复", "解决"), "fact.resolution"),
        (("只允许", "必须", "约束"), "fact.constraint"),
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
    "事实": ((re.compile(r"(?:已实现|新增|接入|支持|修复实现)"), "fact.implementation"),),
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


def validate_canonical_slot(slot: str | None) -> str | None:
    """仅接受 operational slot；开放事实统一返回 None。"""
    if not slot:
        return None
    normalized = normalize_canonical_attribute(slot)
    definition = SLOT_REGISTRY.get(normalized)
    return normalized if definition is not None and definition.is_operational else None


def validate_slot_instance(slot: str | None, qualifiers: dict[str, Any] | None) -> str | None:
    """校验 operational slot 及其实例必需 qualifier，失败时降级为空 slot。"""
    normalized = validate_canonical_slot(slot)
    if normalized is None:
        return None
    values = qualifiers if isinstance(qualifiers, dict) else {}
    for key in SLOT_REGISTRY[normalized].required_qualifiers:
        value = values.get(key)
        if value is None:
            return None
        if isinstance(value, str) and not unicodedata.normalize("NFKC", value).strip():
            return None
    return normalized


def normalize_topic_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    """规范化、去重并过滤存储、统计与分类标签。"""
    if not tags:
        return []
    normalized = (unicodedata.normalize("NFKC", str(tag)).strip().casefold().replace("-", "_") for tag in tags)
    return list(dict.fromkeys(tag for tag in normalized if tag in ALLOWED_TOPIC_TAGS))


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
