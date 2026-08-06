"""基于统一 LLM 客户端的结构化 Claim 提取管线。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import ValidationError as PydanticValidationError

from hl_mem.domain.claims.attributes import (
    _HIGH_CONFIDENCE_ATTRIBUTE_PATTERNS,
    ALLOWED_TOPIC_TAGS,
    ATTRIBUTE_ALIASES,
    ATTRIBUTE_HINTS,
    MUTUALLY_EXCLUSIVE_SLOTS,
    PREDICATE_ATTRIBUTE_MAP,
    PREDICATE_NORMALIZE,
    SLOT_REGISTRY,
    infer_canonical_attribute,
    normalize_canonical_attribute,
    normalize_predicate,
    normalize_topic_tags,
    predicate_for_canonical_attribute,
    reconcile_canonical_attribute,
    validate_slot_instance,
)
from hl_mem.domain.claims.query_tags import extract_query_tags
from hl_mem.domain.entity import (
    _ENVIRONMENT_VARIABLE_PATTERN,
    _FILE_SUBJECT_PATTERN,
    _PASCAL_CASE_SUBJECT_PATTERN,
    DEFAULT_ENTITY_ALIASES,
    invalid_subject_reason,
    isolated_subject_id,
    normalize_entity_id,
)
from hl_mem.errors import LLMOutputTruncatedError, LLMSchemaValidationError
from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import (
    LLMMessage,
    LLMRequest,
    StructuredOutputMode,
    StructuredOutputSpec,
)
from hl_mem.observability.audit import current_audit

from .admission import (
    ALNUM_SECRET_RE,
    LOW_VALUE_HEALTH_STATES,
    NUMERIC_OR_VERSION_RE,
    RECOVERY_CODE_RE,
    SECRET_ASSIGNMENT_RE,
    SECRET_FIELD_NAME_RE,
    SK_TOKEN_RE,
    MemoryCandidate,
    admission_rules_fingerprint,
    admit_claim,
    low_value_reason,
    secret_reason,
)
from .chunking import (
    ChunkingPolicy,
    ExtractionChunk,
    bisect_extraction_chunk,
    split_extraction_content,
)
from .extractors import ExtractedClaim
from .repair import ENUM_MAPPINGS, TOPIC_TAG_ZH_TO_EN, repair_extraction_json
from .schemas import (
    CompactExtractionResponseSchema,
    ExtractionResponseSchema,
    extraction_response_json_schema,
)
from .verifier import EntailmentVerifier

LOGGER = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是记忆事实提取器。从对话中提取对未来有价值的原子事实。

只输出严格 JSON，不要输出解释、Markdown 或额外字段：
{
  "claims": [
    {
      "subject": "主体名称",
      "value": "原子事实描述",
      "kind": "preference|architecture|identity|config|fact|plan|choice",
      "confidence": 0.0,
      "notability": "high|medium|low",
      "evidence_quote": "原文中支持这条 claim 的片段"
    }
  ],
  "should_memorize": true
}

输入边界：
- 只从 <extract_from> 提取事实。
- <context_only> 仅用于消解主体和代词，不得作为事实证据。
- 没有可提取事实时返回 {"claims":[],"should_memorize":false}。
- should_memorize 必须等于 claims 是否非空。

原子事实规则：
- 每条 claim 只表达一个原子事实；一句话有多个事实时拆开，避免漏项。
- value 必须脱离上下文仍可理解，包含必要的主体、关系、对象和单位。
- subject 用标准名称（hl_mem、Hermes、用户、Codex 等），不用代词。
- 保留用户原始语言：中文原文输出中文，英文原文输出英文。
- 不判断与已有记忆是否冲突，只判断当前原文是否支持。

kind 分类：
- preference：用户偏好/习惯/工作方式。
- architecture：已执行的架构决策、系统结构、组件关系。
- identity：用户名、硬件、角色等身份信息。
- config：端口、路径、模型名、API 地址等技术配置。
- fact：其他稳定的客观事实。
- plan：已确认的计划和截止日期。
- choice：已生效的数据库、模型、工具或 provider 技术选型。

notability 分级：
- high：核心身份、永久偏好、关键架构决策。
- medium：重要配置、项目特征、一般事实。
- low：边缘信息、临时状态、低频引用 → 跳过不提取。
- 不要把 low 候选放入 claims。

confidence：
- 1.0：原文直接、明确陈述，主体和对象无歧义。
- 0.8：结合上下文消解代词或省略后，只有一种合理解释。
- 0.6：原文中的转述或弱推断；不能定位证据时不要输出。

evidence_quote：
- 必须逐字或近似摘自 <extract_from>，并能在原文中定位。
- 引用足以支持本条 claim 的最短片段，不要引用 <context_only>。

跳过：
- 服务健康快照、CI 测试数量、版本号查询结果、过程进度、纯问候、未确认建议。
- running/stopped/ok、测试通过数、环境变量已清空、正在重启等操作快照。
- assistant 对用户原话的简单复述、示例、假设和通用常识。
- 密钥、令牌、密码、恢复码等敏感凭据。

限制：
- max 10 claims per chunk。
- claims 中每项必须且只能包含上述 6 个字段。
- kind、notability 和 confidence 必须满足上述枚举与范围。"""

ALIASES = {"pg": "PostgreSQL", "postgres": "PostgreSQL", "postgresql": "PostgreSQL"}
_UNSETTLED_SIGNAL_RE = re.compile(r"可以考虑|建议|考虑|待定|或许|计划中|未执行")
_SETTLED_SIGNAL_RE = re.compile(
    r"已经确认|已确认|已经批准|已批准|已经执行|已执行|已经实施|已实施|"
    r"已经完成|已完成|已经决定|已决定|正式采用|已经采纳|已采纳|已验证|已上线"
)
_TEMPORAL_SCOPE_RE = re.compile(
    r"(?i)(?:"
    r"\bdeadline\b|截止|临时|本次|这次|当前运行|本轮|某次运行|需要重启|重启后生效|"
    r"\b(?:passed|failed)\b|测试(?:数量|数|通过|失败|结果)|构建(?:结果|成功|失败)|"
    r"版本(?:查询|结果)|\bversion\s+(?:query|result)\b|评分|得分|行数|"
    r"\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?\s*(?:至|到|~)\s*"
    r"\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?|"
    r"(?:从|自).{0,20}(?:到|至|截至).{0,20}(?:日|号|年|月)"
    r")"
)
_PERMANENT_SCOPE_RE = re.compile(
    r"(?i)(?:长期|永久|始终|固定(?:配置|为)|设计原则|长期约束|必须记住|记住这个|explicit memory)"
)
_HEALTH_CHECK_RE = re.compile(
    r"(?i)(?:\bhealthz\b|\bhealth\s*check\b|健康(?:检查|状态)|" r"\b(?:ok|healthy|unhealthy|success|successful)\b)"
)
_RUNTIME_CONFIGURATION_RE = re.compile(
    r"(?i)(?:\b(?:HTTP_PROXY|HTTPS_PROXY|NO_PROXY)\b|"
    r"\b(?:proxy|代理)(?:配置|环境变量|端口)?\b|"
    r"\b(?:codex|claude|gemini|qwen|glm|python|uv)(?:\.exe)?\s+(?:CLI\s+)?(?:路径|path)\b|"
    r"(?:本次|这次|本轮|当前运行).{0,24}(?:模型|model)|"
    r"(?:监听|运行于|bound to).{0,12}(?:端口|port))"
)
_TOOL_SNAPSHOT_RE = re.compile(
    r"(?i)(?:\bv?\d+\.\d+(?:\.\d+){0,2}\b|"
    r"\b\d+\s+(?:passed|failed|skipped|tests?)\b|"
    r"\b(?:passed|failed|skipped)\s*[:=]?\s*\d+\b|"
    r"测试(?:数量|总数|通过|失败|结果)|运行结果|执行结果|"
    r"\b(?:running|stopped|exited|completed)\b|进程(?:状态|已启动|已停止)|"
    r"审查(?:问题|缺陷|发现)|review (?:issue|finding))"
)
_QUOTED_REPORT_RE = re.compile(r"(?i)(?:quoted|historical|history|report|snapshot|引用|历史|报告|快照)")
_DURABLE_SCOPE_ATTRIBUTES = frozenset(
    {
        *(name for name in SLOT_REGISTRY if name.startswith(("identity.", "preference.", "config."))),
        "memory.explicit",
    }
)
_UNSETTLED_CONFIDENCE_CEILING = 0.55
_LEGACY_CLAIM_DEFAULTS: dict[str, Any] = {
    "subject": "用户",
    "canonical_attribute": "fact.other",
    "canonical_slot": None,
    "topic_tags": [],
    "qualifiers": {},
    "confidence": 0.5,
    "volatility": "stable",
    "reason": "",
    "scope": "permanent",
    "importance": 0.5,
}
_KIND_MAP: dict[str, tuple[str, str, str, str]] = {
    "preference": ("偏好", "preference.other", "permanent", "stable"),
    "architecture": ("事实", "fact.architecture", "permanent", "stable"),
    "identity": ("身份", "identity.other", "permanent", "stable"),
    "config": ("配置", "config.other", "permanent", "stable"),
    "fact": ("事实", "fact.other", "permanent", "stable"),
    "plan": ("计划", "plan.other", "temporal", "stable"),
    "choice": ("使用", "choice.tool", "permanent", "stable"),
}
_KIND_TOPIC_TAG = {
    "preference": "preference",
    "architecture": "architecture",
    "identity": "identity",
    "config": "config",
    "fact": "fact",
    "plan": "plan",
    "choice": "choice",
}
_NOTABILITY_IMPORTANCE = {"high": 0.9, "medium": 0.6, "low": 0.3}
_ENV_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_ABSOLUTE_DATE_RE = re.compile(
    r"(?P<year>\d{4})(?:-|/|年)(?P<month>\d{1,2})(?:-|/|月)(?P<day>\d{1,2})日?"
    r"(?:[ T](?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?"
)
_TECH_ENTITY_RE = re.compile(
    r"(?ix)(?<![A-Za-z0-9_])(?:"
    r"PostgreSQL|SQLite|MySQL|Redis|FastAPI|Uvicorn|PyTorch|Django|Flask|"
    r"OpenAI|Anthropic|DashScope|Qwen(?:[\w.-]+)?|GPT(?:[\w.-]+)?|"
    r"Claude(?:[\w.-]+)?|Gemini(?:[\w.-]+)?|DeepSeek(?:[\w.-]+)?"
    r")(?![A-Za-z0-9_])"
)
_BACKTICK_ENTITY_RE = re.compile(r"`([^`\r\n]{2,100})`")
_RELATIVE_DATE_OFFSETS = {"今天": 0, "明天": 1, "后天": 2, "下周": 7}
_LEGACY_PREDICATE_KIND = {
    "偏好": "preference",
    "身份": "identity",
    "配置": "config",
    "计划": "plan",
    "使用": "choice",
}


def _regex_fingerprint(pattern: re.Pattern[str]) -> dict[str, Any]:
    return {"pattern": pattern.pattern, "flags": pattern.flags}


def _postprocess_rules_fingerprint() -> dict[str, Any]:
    """返回会改变 LLM 原始输出的稳定规则常量。"""
    return {
        "aliases": ALIASES,
        "attribute_aliases": ATTRIBUTE_ALIASES,
        "attribute_hints": ATTRIBUTE_HINTS,
        "attribute_high_confidence_patterns": {
            predicate: [
                {"pattern": _regex_fingerprint(pattern), "attribute": attribute} for pattern, attribute in patterns
            ]
            for predicate, patterns in _HIGH_CONFIDENCE_ATTRIBUTE_PATTERNS.items()
        },
        "default_entity_aliases": DEFAULT_ENTITY_ALIASES,
        "low_value_health_states": sorted(LOW_VALUE_HEALTH_STATES),
        "mutually_exclusive_slots": sorted(MUTUALLY_EXCLUSIVE_SLOTS),
        "predicate_attribute_map": PREDICATE_ATTRIBUTE_MAP,
        "predicate_normalize": PREDICATE_NORMALIZE,
        "slot_registry": {name: asdict(definition) for name, definition in SLOT_REGISTRY.items()},
        "durable_scope_attributes": sorted(_DURABLE_SCOPE_ATTRIBUTES),
        "legacy_claim_defaults": _LEGACY_CLAIM_DEFAULTS,
        "compact_kind_map": _KIND_MAP,
        "compact_kind_topic_tag": _KIND_TOPIC_TAG,
        "notability_importance": _NOTABILITY_IMPORTANCE,
        "relative_date_offsets": _RELATIVE_DATE_OFFSETS,
        "admission": admission_rules_fingerprint(),
        "unsettled_confidence_ceiling": _UNSETTLED_CONFIDENCE_CEILING,
        "repair_enum_mappings": ENUM_MAPPINGS,
        "repair_topic_tag_mappings": TOPIC_TAG_ZH_TO_EN,
        "patterns": {
            "numeric_or_version": _regex_fingerprint(NUMERIC_OR_VERSION_RE),
            "recovery_code": _regex_fingerprint(RECOVERY_CODE_RE),
            "secret_assignment": _regex_fingerprint(SECRET_ASSIGNMENT_RE),
            "secret_field_name": _regex_fingerprint(SECRET_FIELD_NAME_RE),
            "sk_token": _regex_fingerprint(SK_TOKEN_RE),
            "mixed_alnum_secret": _regex_fingerprint(ALNUM_SECRET_RE),
            "unsettled_signal": _regex_fingerprint(_UNSETTLED_SIGNAL_RE),
            "settled_signal": _regex_fingerprint(_SETTLED_SIGNAL_RE),
            "temporal_scope": _regex_fingerprint(_TEMPORAL_SCOPE_RE),
            "permanent_scope": _regex_fingerprint(_PERMANENT_SCOPE_RE),
            "health_check": _regex_fingerprint(_HEALTH_CHECK_RE),
            "runtime_configuration": _regex_fingerprint(_RUNTIME_CONFIGURATION_RE),
            "tool_snapshot": _regex_fingerprint(_TOOL_SNAPSHOT_RE),
            "quoted_report": _regex_fingerprint(_QUOTED_REPORT_RE),
            "compact_env_key": _regex_fingerprint(_ENV_KEY_RE),
            "compact_absolute_date": _regex_fingerprint(_ABSOLUTE_DATE_RE),
            "compact_tech_entity": _regex_fingerprint(_TECH_ENTITY_RE),
            "compact_backtick_entity": _regex_fingerprint(_BACKTICK_ENTITY_RE),
            "subject_environment_variable": _regex_fingerprint(_ENVIRONMENT_VARIABLE_PATTERN),
            "subject_filename": _regex_fingerprint(_FILE_SUBJECT_PATTERN),
            "subject_pascal_case": _regex_fingerprint(_PASCAL_CASE_SUBJECT_PATTERN),
        },
    }


def compute_prompt_hash(
    system_prompt: str = SYSTEM_PROMPT,
    *,
    response_schema: dict[str, Any] | None = None,
    postprocess_rules: dict[str, Any] | None = None,
) -> str:
    """计算 prompt、响应 schema 与后处理规则的稳定提取配置指纹。"""
    payload = {
        "system_prompt": system_prompt,
        "response_schema": extraction_response_json_schema() if response_schema is None else response_schema,
        "postprocess_rules": _postprocess_rules_fingerprint() if postprocess_rules is None else postprocess_rules,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


PROMPT_HASH = compute_prompt_hash()
LLM_EXTRACTOR_VERSION = f"llm-v2+{PROMPT_HASH}"


def normalize_scope(
    llm_scope: str,
    predicate: str,
    canonical_slot: str | None,
    subject: str,
    value: Any,
    qualifiers: dict[str, Any] | None = None,
    *,
    canonical_attribute: str | None = None,
    actor_type: str | None = None,
    event_type: str | None = None,
    source_kind: str | None = None,
) -> tuple[str, str]:
    """根据高置信语义规则规范 scope，并返回可审计的原因码。"""
    scope = llm_scope if llm_scope in {"temporal", "permanent"} else "permanent"
    normalized_predicate = normalize_predicate(predicate)
    text = unicodedata.normalize("NFKC", f"{subject} {value} {qualifiers or {}}")
    source = unicodedata.normalize("NFKC", f"{actor_type or ''} {event_type or ''} {source_kind or ''}").casefold()

    if scope != "permanent":
        return scope, "llm_preserved"
    slot_definition = SLOT_REGISTRY.get(normalize_canonical_attribute(canonical_slot)) if canonical_slot else None
    if slot_definition is not None and slot_definition.ttl_class == "short":
        return "temporal", "slot_short_ttl"
    if canonical_attribute in _DURABLE_SCOPE_ATTRIBUTES and not _RUNTIME_CONFIGURATION_RE.search(text):
        return "permanent", "durable_attribute"
    if not canonical_slot and canonical_attribute:
        slot_definition = SLOT_REGISTRY.get(normalize_canonical_attribute(canonical_attribute))
    if slot_definition is not None and slot_definition.ttl_class == "short":
        return "temporal", "slot_short_ttl"
    if _HEALTH_CHECK_RE.search(text):
        return "temporal", "health_check"
    if _RUNTIME_CONFIGURATION_RE.search(text):
        return "temporal", "runtime_configuration"
    if _QUOTED_REPORT_RE.search(source):
        return "temporal", "quoted_report"
    if (
        actor_type == "tool"
        or event_type in {"tool_result", "status_report"}
        or source_kind
        in {
            "tool_result",
            "status_report",
        }
    ):
        return "temporal", "tool_snapshot"
    if _TOOL_SNAPSHOT_RE.search(text):
        return "temporal", "tool_snapshot"
    if _TEMPORAL_SCOPE_RE.search(text):
        return "temporal", "explicit_temporal_signal"
    if _PERMANENT_SCOPE_RE.search(text):
        return "permanent", "explicit_permanent_signal"
    if slot_definition is not None and slot_definition.ttl_class == "none":
        return "permanent", "slot_no_ttl"
    if normalized_predicate in {"身份", "偏好", "explicit_memory"}:
        return "permanent", "durable_predicate"
    return scope, "llm_preserved"


def _is_low_value_claim(claim: ExtractedClaim) -> bool:
    """判断 LLM 提取结果是否属于应在输出边界丢弃的低价值 claim。"""
    return low_value_reason(claim.value, claim.canonical_slot) is not None


def _secret_reason(value: Any) -> str | None:
    """识别禁止进入 Claim 存储的确定性凭据格式。"""
    return secret_reason(value)


def _postprocess_extracted_claims(claims: list[ExtractedClaim]) -> list[ExtractedClaim]:
    """在 LLM 输出与持久化之间校准未决表述。"""
    retained: list[ExtractedClaim] = []
    lowered_signals: set[str] = set()
    lowered_count = 0
    for claim in claims:
        value = unicodedata.normalize("NFKC", str(claim.value)).strip()
        unsettled = _UNSETTLED_SIGNAL_RE.search(value)
        if unsettled is not None and _SETTLED_SIGNAL_RE.search(value) is None:
            confidence = min(claim.confidence, _UNSETTLED_CONFIDENCE_CEILING)
            if confidence != claim.confidence:
                lowered_count += 1
                lowered_signals.add(unsettled.group(0))
                claim = replace(claim, confidence=confidence)
        retained.append(claim)
    if lowered_count:
        current_audit().emit(
            "extract",
            "confidence_calibrated",
            "lowered",
            detail={
                "count": lowered_count,
                "ceiling": _UNSETTLED_CONFIDENCE_CEILING,
                "signals": sorted(lowered_signals),
            },
        )
    return retained


class LLMExtractor:
    """通过统一 LLMClient 执行结构化事实提取。"""

    prompt_hash = PROMPT_HASH
    extractor_version = LLM_EXTRACTOR_VERSION

    def __init__(
        self,
        llm_client: LLMClient,
        chunking_policy: ChunkingPolicy,
        *,
        schema_retries: int = 2,
        structured_mode: StructuredOutputMode = StructuredOutputMode.JSON_SCHEMA,
        verifier: EntailmentVerifier | None = None,
        verification_mode: Literal["off", "audit", "enforce"] = "off",
        verification_claim_threshold: int = 5,
        verification_empty_text_threshold: int = 1_000,
    ) -> None:
        self.llm_client = llm_client
        self.model = llm_client.model
        self.schema_retries = schema_retries
        if self.schema_retries < 0:
            raise ValueError("schema_retries must be non-negative")
        self.structured_mode = structured_mode
        self.chunking_policy = chunking_policy
        if verification_mode not in {"off", "audit", "enforce"}:
            raise ValueError("verification_mode must be 'off', 'audit', or 'enforce'")
        if verification_claim_threshold < 0 or verification_empty_text_threshold < 0:
            raise ValueError("verification thresholds must be non-negative")
        self.verifier = verifier
        self.verification_mode = verification_mode
        self.verification_claim_threshold = verification_claim_threshold
        self.verification_empty_text_threshold = verification_empty_text_threshold
        self.last_usage_tokens = 0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self._schema_retry_count = 0
        self._repair_count = 0
        self._llm_call_count = 0
        self._memorize_decisions: list[tuple[bool, str]] = []
        self._last_schema_errors: list[dict[str, Any]] = []
        self._secret_rejections: dict[str, int] = {}

    def extract(self, content: dict[str, Any] | str, context: dict[str, Any] | None = None) -> list[ExtractedClaim]:
        """同步分块提取事实，并在输出截断时递归二分恢复。"""
        self.last_usage_tokens = 0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        self._schema_retry_count = 0
        self._repair_count = 0
        self._llm_call_count = 0
        self._memorize_decisions = []
        self._last_schema_errors = []
        self._secret_rejections = {}
        event_context = context or {}
        chunks = split_extraction_content(content, self.chunking_policy)
        chunk_claims = [self._extract_chunk_with_auto_split(chunk, event_context, depth=0) for chunk in chunks]
        claims = self._merge_chunk_claims(chunk_claims)
        if self.verifier is None or self.verification_mode == "off":
            claims = _postprocess_extracted_claims(claims)
        if self._secret_rejections:
            current_audit().emit(
                "extract",
                "secret_rejected",
                "rejected",
                detail={
                    "count": sum(self._secret_rejections.values()),
                    "reason_counts": self._secret_rejections,
                    "extractor_hash": self.prompt_hash,
                },
            )
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug(
                "%s",
                json.dumps(
                    {
                        "event": "llm_extraction",
                        "actor": event_context.get("actor") or event_context.get("actor_type"),
                        "session_id": event_context.get("session_id"),
                        "content_length": self._content_length(content),
                        "should_memorize": bool(claims),
                        "reason": self._decision_reason(),
                        "claims_count": len(claims),
                        "schema_retry_count": self._schema_retry_count,
                        "repair_count": self._repair_count,
                        "llm_call_count": self._llm_call_count,
                        "input_tokens": self.last_input_tokens,
                        "output_tokens": self.last_output_tokens,
                        "total_tokens": self.last_usage_tokens,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        return claims

    def _extract_chunk_with_auto_split(
        self,
        chunk: ExtractionChunk,
        event_context: dict[str, Any],
        depth: int,
    ) -> list[ExtractedClaim]:
        """提取单块；仅输出截断时按策略递归二分。"""
        try:
            claims = self._extract_one_chunk(chunk, event_context)
            if self.verifier is None or self.verification_mode == "off":
                return claims
            return self._verify_extracted_claims(_postprocess_extracted_claims(claims), chunk.text)
        except LLMOutputTruncatedError as error:
            split = bisect_extraction_chunk(chunk)
            if depth >= self.chunking_policy.max_split_depth or split is None:
                raise LLMOutputTruncatedError(
                    "LLM output remains truncated after auto split: "
                    f"chunk={chunk.index}, start_unit={chunk.start_unit}, "
                    f"end_unit={chunk.end_unit}, depth={depth}"
                ) from error
            left, right = split
            return self._merge_chunk_claims(
                [
                    self._extract_chunk_with_auto_split(left, event_context, depth + 1),
                    self._extract_chunk_with_auto_split(right, event_context, depth + 1),
                ]
            )

    def _verify_extracted_claims(
        self,
        claims: list[ExtractedClaim],
        source_text: str,
    ) -> list[ExtractedClaim]:
        """按 rollout 策略执行 audit-only 验证，并始终原样返回 claims。"""
        if self.verifier is None or self.verification_mode == "off":
            return claims
        if not claims:
            if len(source_text) > self.verification_empty_text_threshold:
                current_audit().emit(
                    "extract",
                    "possible_under_extraction",
                    "observed",
                    detail={
                        "source_length": len(source_text),
                        "length_threshold": self.verification_empty_text_threshold,
                        "verification_mode": self.verification_mode,
                    },
                )
            return claims
        should_verify = self.verification_mode == "enforce" or len(claims) > self.verification_claim_threshold
        if not should_verify:
            return claims

        try:
            results = self.verifier.verify_batch(claims, source_text)
        except Exception as error:
            self._record_verifier_usage()
            self._emit_verification_failure(error, len(claims))
            return claims

        self._record_verifier_usage()
        try:
            if len(results) != len(claims):
                raise ValueError("verifier result count does not match claim count")
            for claim_index, (claim, result) in enumerate(zip(claims, results, strict=True)):
                current_audit().emit(
                    "extract",
                    "entailment_checked",
                    result.support_label,
                    detail={
                        "claim_index": claim_index,
                        "claim_subject": claim.subject[:100],
                        "claim_predicate": claim.predicate[:100],
                        "claim_value": claim.value[:100],
                        "rationale": result.rationale[:512],
                        "verification_mode": self.verification_mode,
                    },
                )
        except Exception as error:
            self._emit_verification_failure(error, len(claims))
        return claims

    def _emit_verification_failure(self, error: Exception, claim_count: int) -> None:
        """记录安全、截断后的 fail-open verifier 错误。"""
        current_audit().emit(
            "extract",
            "entailment_verification_failed",
            "error",
            detail={
                "error_class": type(error).__name__,
                "error": str(error).replace("\n", " ")[:256],
                "claim_count": claim_count,
                "verification_mode": self.verification_mode,
            },
        )

    def _record_verifier_usage(self) -> None:
        """把额外审计调用计入提取器预算与诊断指标。"""
        if self.verifier is None:
            return
        self.last_usage_tokens += int(getattr(self.verifier, "last_usage_tokens", 0))
        self.last_input_tokens += int(getattr(self.verifier, "last_input_tokens", 0))
        self.last_output_tokens += int(getattr(self.verifier, "last_output_tokens", 0))
        self._llm_call_count += int(getattr(self.verifier, "last_call_count", 0))

    @staticmethod
    def _infer_compact_qualifiers(attribute: str, subject: str, value: str) -> dict[str, str]:
        """为 operational slot 确定性补齐最小实例 qualifier。"""
        definition = SLOT_REGISTRY.get(attribute)
        if definition is None or not definition.required_qualifiers:
            return {}
        qualifiers: dict[str, str] = {}
        for key in definition.required_qualifiers:
            if key == "key":
                match = _ENV_KEY_RE.search(value)
                qualifiers[key] = match.group(0) if match is not None else subject
            elif key == "plan":
                qualifiers[key] = value[:200]
            else:
                qualifiers[key] = subject
        return qualifiers

    @staticmethod
    def _infer_compact_occurrence(text: str, occurred_at: str | None) -> tuple[str | None, str | None]:
        """从绝对/相对日期与对话时间推断 claim 的发生区间。"""
        base: datetime | None = None
        if occurred_at:
            try:
                base = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
                if base.tzinfo is None:
                    base = base.replace(tzinfo=timezone.utc)
            except ValueError:
                base = None
        tz = base.tzinfo if base is not None else timezone.utc
        moments: list[datetime] = []
        for match in _ABSOLUTE_DATE_RE.finditer(text):
            try:
                moment = datetime(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                    int(match.group("hour") or 0),
                    int(match.group("minute") or 0),
                    int(match.group("second") or 0),
                    tzinfo=tz,
                )
            except ValueError:
                continue
            if moment not in moments:
                moments.append(moment)
        if moments:
            return moments[0].isoformat(), moments[1].isoformat() if len(moments) > 1 else None
        if base is not None:
            for signal, days in _RELATIVE_DATE_OFFSETS.items():
                if signal in text:
                    moment = (base + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
                    return moment.isoformat(), None
        return None, None

    @staticmethod
    def _extract_compact_entities(subject: str, value: str) -> list[str]:
        """从 value 中提取受控技术名和显式标识，保留标准 subject。"""
        entities = [subject]
        candidates = [match.group(0) for match in _TECH_ENTITY_RE.finditer(value)]
        candidates.extend(match.group(1).strip() for match in _BACKTICK_ENTITY_RE.finditer(value))
        seen = {subject.casefold()}
        for candidate in candidates:
            canonical = ALIASES.get(candidate.casefold(), candidate)
            key = canonical.casefold()
            if key and key not in seen:
                seen.add(key)
                entities.append(canonical)
        return entities

    def _postprocess_claim(
        self,
        raw: dict[str, Any],
        source_text: str,
        occurred_at: str | None = None,
    ) -> dict[str, Any] | None:
        """把 LLM 的 6 字段候选准入并映射为现有完整 claim schema。"""
        try:
            candidate = MemoryCandidate(
                subject=str(raw["subject"]).strip(),
                value=str(raw["value"]).strip(),
                kind=str(raw["kind"]).strip().casefold(),
                confidence=float(raw["confidence"]),
                notability=str(raw["notability"]).strip().casefold(),
                evidence_quote=str(raw["evidence_quote"]).strip(),
            )
        except (KeyError, TypeError, ValueError):
            return None

        decision = admit_claim(candidate, source_text)
        current_audit().emit(
            "extract",
            "admission_checked",
            "accepted" if decision.accepted else "rejected",
            detail={
                "reason": decision.reason,
                "kind": candidate.kind,
                "notability": candidate.notability,
            },
        )
        if not decision.accepted:
            if decision.reason in {
                "recovery_code",
                "secret_assignment",
                "sk_token",
                "mixed_alnum_token",
            }:
                self._secret_rejections[decision.reason] = self._secret_rejections.get(decision.reason, 0) + 1
            return None

        predicate, canonical_attribute, scope, volatility = _KIND_MAP[candidate.kind]
        subject = normalize_entity_id(candidate.subject)
        inferred_attribute = infer_canonical_attribute(predicate, subject, candidate.value, {})
        fallback_attribute = PREDICATE_ATTRIBUTE_MAP[predicate][1]
        if inferred_attribute not in {"custom.unknown", fallback_attribute}:
            canonical_attribute = inferred_attribute
        qualifiers = self._infer_compact_qualifiers(canonical_attribute, subject, candidate.value)
        canonical_slot = validate_slot_instance(canonical_attribute, qualifiers)
        occurred_start, occurred_end = self._infer_compact_occurrence(candidate.evidence_quote, occurred_at)
        topic_tags = normalize_topic_tags(
            [
                _KIND_TOPIC_TAG[candidate.kind],
                *extract_query_tags(f"{subject} {candidate.value}"),
            ]
        )
        return {
            "subject": subject,
            "predicate": predicate,
            "canonical_attribute": canonical_attribute,
            "canonical_slot": canonical_slot,
            "topic_tags": topic_tags,
            "value": candidate.value,
            "qualifiers": qualifiers,
            "confidence": candidate.confidence,
            "volatility": volatility,
            "reason": decision.reason,
            "scope": scope,
            "importance": _NOTABILITY_IMPORTANCE[candidate.notability],
            "occurred_start": occurred_start,
            "occurred_end": occurred_end,
            "entities": self._extract_compact_entities(subject, candidate.value),
        }

    @staticmethod
    def _legacy_admission_candidate(raw: dict[str, Any]) -> MemoryCandidate | None:
        """把旧版完整 claim 投影为统一准入候选。"""
        try:
            attribute = str(raw.get("canonical_attribute") or "").strip().casefold()
            predicate = str(raw.get("predicate") or "").strip()
            if attribute == "fact.architecture":
                kind = "architecture"
            elif attribute.startswith("preference."):
                kind = "preference"
            elif attribute.startswith("identity."):
                kind = "identity"
            elif attribute.startswith("config."):
                kind = "config"
            elif attribute.startswith("plan."):
                kind = "plan"
            else:
                kind = _LEGACY_PREDICATE_KIND.get(predicate, "fact")
            importance = float(raw.get("importance", 0.5))
            notability = "high" if importance >= 0.8 else "low" if importance < 0.2 else "medium"
            value = str(raw["value"]).strip()
            return MemoryCandidate(
                subject=str(raw.get("subject") or "用户").strip(),
                value=value,
                kind=kind,
                confidence=float(raw.get("confidence", 0.5)),
                notability=notability,
                evidence_quote=value,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _record_admission(self, candidate: MemoryCandidate, source_text: str) -> bool:
        """执行并审计 compact/legacy 共用的准入策略。"""
        decision = admit_claim(candidate, source_text)
        current_audit().emit(
            "extract",
            "admission_checked",
            "accepted" if decision.accepted else "rejected",
            detail={
                "reason": decision.reason,
                "kind": candidate.kind,
                "notability": candidate.notability,
            },
        )
        if not decision.accepted and decision.reason in {
            "recovery_code",
            "secret_assignment",
            "sk_token",
            "mixed_alnum_token",
        }:
            self._secret_rejections[decision.reason] = self._secret_rejections.get(decision.reason, 0) + 1
        return decision.accepted

    def _extract_one_chunk(
        self,
        chunk: ExtractionChunk,
        event_context: dict[str, Any],
    ) -> list[ExtractedClaim]:
        """请求并严格校验一个内容分块，schema 失败时执行内容级重试。"""
        context = json.dumps(event_context, ensure_ascii=False)
        occurred_at = str(event_context.get("occurred_at", "未知"))
        result = self._request_chunk(chunk, context, occurred_at)
        if not result.should_memorize and not result.claims:
            self._memorize_decisions.append((False, "should_memorize=false"))
            return []
        if not result.should_memorize:
            current_audit().emit(
                "extract",
                "should_memorize_checked",
                "claims_override_should_memorize_false",
                detail={"claim_count": len(result.claims)},
            )
        parsed: list[ExtractedClaim] = []
        source_kind = str(event_context.get("source_kind") or event_context.get("category") or "")
        if re.search(r"(?i)(?:\[quoted message\]|quoted report|历史报告|引用消息)", chunk.text):
            source_kind = "quoted_report"
        compact_response = isinstance(result, CompactExtractionResponseSchema)
        for item in result.claims:
            raw_claim = item.model_dump()
            if compact_response:
                postprocessed = self._postprocess_claim(raw_claim, chunk.text, occurred_at)
                if postprocessed is None:
                    continue
                raw_claim = postprocessed
            else:
                secret_reason = _secret_reason(raw_claim)
                if secret_reason is not None:
                    self._secret_rejections[secret_reason] = self._secret_rejections.get(secret_reason, 0) + 1
                    continue
                legacy_candidate = self._legacy_admission_candidate(raw_claim)
                if legacy_candidate is None or not self._record_admission(legacy_candidate, chunk.text):
                    continue
            secret_reason = _secret_reason(raw_claim)
            if secret_reason is not None:
                self._secret_rejections[secret_reason] = self._secret_rejections.get(secret_reason, 0) + 1
                continue
            claim = self._claim(raw_claim)
            normalized_scope, reason_code = normalize_scope(
                claim.scope,
                claim.predicate,
                claim.canonical_slot,
                claim.subject,
                claim.value,
                claim.qualifiers,
                canonical_attribute=claim.canonical_attribute,
                actor_type=str(event_context.get("actor_type") or event_context.get("actor") or ""),
                event_type=str(event_context.get("event_type") or ""),
                source_kind=source_kind,
            )
            current_audit().emit(
                "extract",
                "scope_normalized",
                "changed" if normalized_scope != claim.scope else "preserved",
                detail={
                    "llm_scope": claim.scope,
                    "normalized_scope": normalized_scope,
                    "reason_code": reason_code,
                    "canonical_slot": claim.canonical_slot,
                },
            )
            parsed.append(replace(claim, scope=normalized_scope))
        retained = [claim for claim in parsed if not _is_low_value_claim(claim)]
        reasons = sorted({claim.reason for claim in retained if claim.reason})
        self._memorize_decisions.append(
            (
                bool(retained),
                "；".join(reasons) if retained else "postprocess_rejected",
            )
        )
        return retained

    def _request_chunk(
        self,
        chunk: ExtractionChunk,
        context: str,
        occurred_at: str,
    ) -> CompactExtractionResponseSchema | ExtractionResponseSchema:
        """请求并严格校验一个内容分块，schema 失败时执行内容级重试。"""
        schema_errors: list[dict[str, Any]] = []
        previous_output: Any = None
        for attempt in range(self.schema_retries + 1):
            if attempt:
                self._schema_retry_count += 1
            retry_instruction = ""
            if schema_errors:
                retry_instruction = self._schema_retry_instruction(previous_output, schema_errors)
            request = LLMRequest(
                messages=[
                    LLMMessage(role="system", content=SYSTEM_PROMPT),
                    LLMMessage(
                        role="user",
                        content=(
                            f"事件发生时间 occurred_at：{occurred_at}\n"
                            f"事件上下文：{context}\n"
                            "<context_only>\n"
                            f"{chunk.context_prefix}\n"
                            "</context_only>\n"
                            "context_only 仅用于消解主语，禁止从中提取 claim。\n"
                            "<extract_from>\n"
                            f"{chunk.text}\n"
                            "</extract_from>"
                            f"{retry_instruction}"
                        ),
                    ),
                ],
                structured_output=StructuredOutputSpec(
                    name="extraction_response",
                    schema=extraction_response_json_schema(),
                    preferred_mode=self.structured_mode,
                ),
            )
            response = self.llm_client.complete(request)
            self._llm_call_count += 1
            self.last_usage_tokens += response.usage_total_tokens
            self.last_input_tokens += response.input_tokens or 0
            self.last_output_tokens += response.output_tokens or 0
            if response.finish_reason in {"length", "max_tokens"}:
                raise LLMOutputTruncatedError(
                    f"LLM output truncated: provider={self.llm_client.provider.name}, model={self.model}"
                )
            previous_output_payload: Any = response.content
            try:
                raw = self._parse_json(response.content)
                previous_output_payload = raw
                repaired = repair_extraction_json(
                    raw,
                    provider=self.llm_client.provider.name,
                    model=self.model,
                )
                self._repair_count += self._count_repairs(raw, repaired)
                if self._uses_compact_schema(repaired):
                    return CompactExtractionResponseSchema.model_validate(repaired)
                compatible = self._parse_legacy_defaults(repaired)
                return ExtractionResponseSchema.model_validate(compatible)
            except (PydanticValidationError, ValueError) as error:
                if isinstance(error, PydanticValidationError):
                    self._last_schema_errors.extend(dict(item) for item in error.errors())
                if self._looks_like_truncated_json(response.content):
                    raise LLMOutputTruncatedError(
                        f"LLM output appears truncated: provider={self.llm_client.provider.name}, model={self.model}"
                    ) from error
                previous_output = previous_output_payload
                schema_errors = self._schema_error_details(error, previous_output)
                if attempt == self.schema_retries:
                    raise LLMSchemaValidationError(
                        "LLM response does not contain valid JSON or match schema: "
                        f"provider={self.llm_client.provider.name}, model={self.model}, "
                        f"chunk_length={len(chunk.text)}, errors={self._schema_error_paths(error)}"
                    ) from error
        raise RuntimeError("unreachable")

    @staticmethod
    def _content_length(content: dict[str, Any] | str) -> int:
        """返回实际待提取文本长度。"""
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            return len(content["text"])
        return len(content) if isinstance(content, str) else len(json.dumps(content, ensure_ascii=False))

    def _decision_reason(self) -> str:
        """合并分块判定原因并保持稳定顺序。"""
        reasons = list(dict.fromkeys(reason for _decision, reason in self._memorize_decisions if reason))
        return "；".join(reasons) or "no_chunks"

    @classmethod
    def _count_repairs(cls, original: Any, repaired: Any) -> int:
        """递归统计确定性修复改变的叶子字段数。"""
        if isinstance(original, dict) and isinstance(repaired, dict):
            return sum(
                cls._count_repairs(original.get(key), repaired.get(key)) for key in original.keys() | repaired.keys()
            )
        if isinstance(original, list) and isinstance(repaired, list):
            return sum(cls._count_repairs(left, right) for left, right in zip(original, repaired, strict=False)) + abs(
                len(original) - len(repaired)
            )
        return int(original != repaired)

    @staticmethod
    def _looks_like_truncated_json(content: str | dict[str, Any]) -> bool:
        """识别空响应或括号未闭合的明显 JSON 截断。"""
        if isinstance(content, dict):
            return False
        text = str(content).strip()
        if not text:
            return True
        return (text.startswith("{") and text.count("{") > text.count("}")) or (
            text.startswith("[") and text.count("[") > text.count("]")
        )

    @staticmethod
    def _merge_chunk_claims(chunks: list[list[ExtractedClaim]]) -> list[ExtractedClaim]:
        """按规范化事实字段稳定合并同一次分块提取的结果。"""
        merged: list[ExtractedClaim] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for claims in chunks:
            for claim in claims:
                key = (
                    unicodedata.normalize("NFKC", claim.subject).strip().casefold(),
                    unicodedata.normalize("NFKC", claim.predicate).strip().casefold(),
                    unicodedata.normalize("NFKC", claim.canonical_slot or "").strip().casefold(),
                    unicodedata.normalize("NFKC", str(claim.value)).strip().casefold(),
                    unicodedata.normalize(
                        "NFKC",
                        json.dumps(
                            claim.qualifiers,
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ),
                    ),
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(claim)
        return merged

    @staticmethod
    def _uses_compact_schema(payload: dict[str, Any]) -> bool:
        """区分当前 6 字段响应与需要兼容的旧响应。"""
        claims = payload.get("claims")
        if not isinstance(claims, list):
            return False
        if not claims:
            return set(payload).issubset({"claims", "should_memorize"})
        compact_markers = {"kind", "notability", "evidence_quote"}
        return any(isinstance(item, dict) and compact_markers.intersection(item) for item in claims)

    @staticmethod
    def _parse_legacy_defaults(payload: dict[str, Any]) -> dict[str, Any]:
        """仅对带有旧版核心字段签名的响应补齐后来新增的字段。"""
        compatible = dict(payload)
        claims = compatible.get("claims")
        if not isinstance(claims, list):
            return compatible
        normalized_claims: list[Any] = []
        for item in claims:
            if not isinstance(item, dict):
                normalized_claims.append(item)
                continue
            claim = dict(item)
            legacy_core = {"predicate", "value"}
            versioned_fields = {"canonical_attribute", "scope", "importance"}
            if not legacy_core.issubset(claim) or not versioned_fields.isdisjoint(claim):
                normalized_claims.append(claim)
                continue
            defaults = deepcopy(_LEGACY_CLAIM_DEFAULTS)
            missing = [key for key in defaults if key not in claim]
            for key in missing:
                claim[key] = defaults[key]
            if missing:
                current_audit().emit(
                    "extract",
                    "legacy_schema_defaults",
                    "applied",
                    detail={"fields": missing},
                )
            normalized_claims.append(claim)
        compatible["claims"] = normalized_claims
        return compatible

    @staticmethod
    def _schema_error_paths(error: Exception) -> list[str]:
        """提取可安全回传给模型的 schema 错误路径与类型。"""
        if isinstance(error, PydanticValidationError):
            return [f"{'.'.join(str(part) for part in item['loc'])}:{item['type']}" for item in error.errors()]
        return [f"response:{type(error).__name__}"]

    @staticmethod
    def _schema_error_details(error: Exception, payload: Any) -> list[dict[str, Any]]:
        """提取错误路径、非法值和该字段允许值，供 schema 重试使用。"""
        if not isinstance(error, PydanticValidationError):
            return [
                {
                    "path": "response",
                    "error_type": type(error).__name__,
                    "invalid_value": payload,
                    "allowed_values": ["valid JSON object matching the supplied schema"],
                }
            ]

        details: list[dict[str, Any]] = []
        for item in error.errors():
            path = ".".join(str(part) for part in item["loc"])
            if "topic_tags" in item["loc"]:
                allowed_values: list[str] = sorted(ALLOWED_TOPIC_TAGS)
            elif item["loc"] and item["loc"][-1] == "kind":
                allowed_values = sorted(_KIND_MAP)
            elif item["loc"] and item["loc"][-1] == "notability":
                allowed_values = sorted(_NOTABILITY_IMPORTANCE)
            elif item["loc"] and item["loc"][-1] == "sensitivity":
                allowed_values = ["normal", "sensitive", "restricted"]
            elif item["loc"] and item["loc"][-1] == "entities":
                allowed_values = ["JSON array of strings", "null (claim entities only)"]
            else:
                allowed_values = [str(item.get("ctx", {}).get("expected", "value matching the JSON schema"))]
            details.append(
                {
                    "path": path,
                    "error_type": item["type"],
                    "invalid_value": item.get("input"),
                    "allowed_values": allowed_values,
                }
            )
        return details

    @staticmethod
    def _schema_retry_instruction(previous_output: Any, schema_errors: list[dict[str, Any]]) -> str:
        """构建包含上次 JSON 和可操作错误详情的 schema 重试指令。"""
        return (
            "\n上一次输出不符合 schema。请基于上次输出生成完整 JSON，只修正下列错误。\n"
            "<previous_invalid_json>\n"
            f"{json.dumps(previous_output, ensure_ascii=False, default=str)}\n"
            "</previous_invalid_json>\n"
            "<schema_errors>\n"
            f"{json.dumps(schema_errors, ensure_ascii=False, default=str)}\n"
            "</schema_errors>"
        )

    @staticmethod
    def _parse_json(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        text = str(raw).strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise ValueError("LLM response does not contain valid JSON") from error
            value = json.loads(match.group())
        if not isinstance(value, dict):
            raise ValueError("LLM response must be a JSON object")
        return value

    @staticmethod
    def _claim(item: dict[str, Any]) -> ExtractedClaim:
        value = str(item.get("value", "")).strip()
        value = ALIASES.get(value.casefold(), value)
        predicate = str(item.get("predicate", "事实")).strip()
        predicate = normalize_predicate(predicate)
        original_subject = str(item.get("subject", "用户"))
        subject = normalize_entity_id(original_subject)
        entities = list(item.get("entities") or [])
        invalid_reason = invalid_subject_reason(original_subject)
        if invalid_reason is not None:
            replacement = next(
                (normalize_entity_id(entity) for entity in entities if invalid_subject_reason(entity) is None),
                None,
            )
            subject = replacement or isolated_subject_id(original_subject, predicate, value)
            if original_subject not in entities:
                entities.append(original_subject)
            current_audit().emit(
                "extract",
                "subject_guard",
                "replaced" if replacement else "isolated",
                detail={
                    "original_subject": original_subject,
                    "normalized_subject": normalize_entity_id(original_subject),
                    "replacement_subject": subject,
                    "reason_code": invalid_reason,
                    "isolation_reason": None if replacement else "invalid_subject_isolated",
                },
            )
        qualifiers = item.get("qualifiers") or {}
        inferred_attribute = infer_canonical_attribute(predicate, subject, value, qualifiers)
        canonical_attribute, _attribute_reason = reconcile_canonical_attribute(
            predicate=predicate,
            llm_attribute=str(item.get("canonical_attribute", "")),
            inferred_attribute=inferred_attribute,
            subject=subject,
            value=value,
            qualifiers=qualifiers,
        )
        projected_predicate = predicate_for_canonical_attribute(canonical_attribute, predicate)
        current_audit().emit(
            "extract",
            "predicate_normalized",
            "changed" if projected_predicate != predicate else "preserved",
            detail={
                "llm_predicate": predicate,
                "normalized_predicate": projected_predicate,
                "canonical_attribute": canonical_attribute,
                "reason_code": (
                    "canonical_attribute_projection" if projected_predicate != predicate else "llm_preserved"
                ),
            },
        )
        predicate = projected_predicate
        volatility = item.get("volatility", "stable")
        scope = item.get("scope", "permanent")
        scope = scope if scope in {"temporal", "permanent"} else "permanent"
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        try:
            importance = min(1.0, max(0.0, float(item.get("importance", 0.5))))
        except (TypeError, ValueError):
            importance = 0.5
        return ExtractedClaim(
            predicate=predicate,
            value=value,
            confidence=confidence,
            volatility=volatility if volatility in {"stable", "ephemeral"} else "stable",
            subject=subject,
            qualifiers=qualifiers,
            reason=str(item.get("reason", "")),
            scope=scope,
            importance=importance,
            canonical_attribute=canonical_attribute,
            canonical_slot=validate_slot_instance(item.get("canonical_slot"), qualifiers),
            topic_tags=normalize_topic_tags(item.get("topic_tags")),
            occurred_start=item.get("occurred_start"),
            occurred_end=item.get("occurred_end"),
            entities=entities or None,
        )
