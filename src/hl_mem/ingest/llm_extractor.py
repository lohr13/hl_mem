"""基于统一 LLM 客户端的结构化 Claim 提取管线。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from hl_mem.domain.action_coordinates import project_action_qualifiers
from hl_mem.domain.claims.attributes import (
    _HIGH_CONFIDENCE_ATTRIBUTE_PATTERNS,
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
    validate_slot_instance,
)
from hl_mem.domain.claims.query_tags import extract_query_tags
from hl_mem.domain.entity import (
    _ENVIRONMENT_VARIABLE_PATTERN,
    _FILE_SUBJECT_PATTERN,
    _PASCAL_CASE_SUBJECT_PATTERN,
    DEFAULT_ENTITY_ALIASES,
    normalize_entity_alias,
)
from hl_mem.llm.client import LLMClient
from hl_mem.llm.types import (
    StructuredOutputMode,
)
from hl_mem.observability.audit import current_audit

from . import lesson_signals
from .admission import (
    ALNUM_SECRET_RE,
    LOW_VALUE_HEALTH_STATES,
    NUMERIC_OR_VERSION_RE,
    RECOVERY_CODE_RE,
    SECRET_ASSIGNMENT_RE,
    SECRET_FIELD_NAME_RE,
    SK_TOKEN_RE,
    AdmissionDecision,
    MemoryCandidate,
    admission_rules_fingerprint,
    admit_claim,
    low_value_reason,
    secret_reason,
)
from .chunking import (
    ChunkingPolicy,
    ExtractionChunk,
)
from .extraction.model_coordinates import (
    MODEL_TASK_SOURCE_MARKERS,
    project_model_coordinates,
)
from .extraction.orchestrator import (
    ExtractionOrchestrator,
    ExtractionOrchestratorConfig,
    ExtractionOrchestratorHooks,
)
from .extraction.parsing import (
    count_repairs,
    is_claim_count_overflow,
    looks_like_truncated_json,
    parse_json_response,
    parse_legacy_defaults,
    schema_error_details,
    schema_error_paths,
    schema_retry_instruction,
    uses_compact_schema,
)
from .extraction.postprocessing import claim_from_payload, merge_chunk_claims
from .extraction.prompts import (
    ENGLISH_SYSTEM_PROMPT,
    LEGACY_ENGLISH_SYSTEM_PROMPT,
    LEGACY_SYSTEM_PROMPT,
    SOURCE_BOUNDED_RAO_ENGLISH_SYSTEM_PROMPT,
    SOURCE_BOUNDED_RAO_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from .extraction.repair import ENUM_MAPPINGS, TOPIC_TAG_ZH_TO_EN
from .extraction.run_state import ExtractionRunState
from .extraction.schema import (
    CompactExtractionResponseSchema,
    ExtractionResponseSchema,
    temporal_gate_extraction_response_json_schema,
)
from .extraction.verification import VerificationCoordinator
from .extractors import ExtractedClaim
from .relative_time import infer_occurrence, relative_time_rules_fingerprint
from .verifier import EntailmentVerifier

LOGGER = logging.getLogger(__name__)

__all__ = [
    "ENGLISH_SYSTEM_PROMPT",
    "LEGACY_ENGLISH_SYSTEM_PROMPT",
    "LEGACY_SYSTEM_PROMPT",
    "SOURCE_BOUNDED_RAO_ENGLISH_SYSTEM_PROMPT",
    "SOURCE_BOUNDED_RAO_SYSTEM_PROMPT",
    "SYSTEM_PROMPT",
]

ALIASES = {"pg": "PostgreSQL", "postgres": "PostgreSQL", "postgresql": "PostgreSQL"}
_HAN_CHARACTER_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
_ZH_FUNCTION_SIGNAL_RE = re.compile(r"我|我们|你|您|他|她|它|的|了|在|是|用|要|会|把|给|这|那|昨天|明天")
_EN_FUNCTION_SIGNAL_RE = re.compile(
    r"(?i)\b(?:i|we|you|he|she|it|the|a|an|to|of|in|on|at|for|with|my|our|your|this|that|yesterday|tomorrow)\b"
)
LANGUAGE_ROUTER_VERSION = "language-router-v1"
CLAIM_COUNT_OVERFLOW_POLICY_VERSION = "claim-count-auto-split-v1"
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
    "preference": ("preference", "preference.other", "permanent", "stable"),
    "architecture": ("fact", "fact.architecture", "permanent", "stable"),
    "identity": ("identity", "identity.other", "permanent", "stable"),
    "config": ("config", "config.other", "permanent", "stable"),
    "fact": ("fact", "fact.other", "permanent", "stable"),
    "plan": ("plan", "plan.other", "temporal", "stable"),
    "choice": ("uses", "choice.tool", "permanent", "stable"),
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
_MODEL_TASK_SOURCE_MARKERS = MODEL_TASK_SOURCE_MARKERS
_TECH_ENTITY_RE = re.compile(
    r"(?ix)(?<![A-Za-z0-9_])(?:"
    r"PostgreSQL|SQLite|MySQL|Redis|FastAPI|Uvicorn|PyTorch|Django|Flask|"
    r"OpenAI|Anthropic|DashScope|Qwen(?:[\w.-]+)?|GPT(?:[\w.-]+)?|"
    r"Claude(?:[\w.-]+)?|Gemini(?:[\w.-]+)?|DeepSeek(?:[\w.-]+)?"
    r")(?![A-Za-z0-9_])"
)
_BACKTICK_ENTITY_RE = re.compile(r"`([^`\r\n]{2,100})`")
_LEGACY_PREDICATE_KIND = {
    "偏好": "preference",
    "身份": "identity",
    "配置": "config",
    "计划": "plan",
    "使用": "choice",
}


def _regex_fingerprint(pattern: re.Pattern[str]) -> dict[str, Any]:
    return {"pattern": pattern.pattern, "flags": pattern.flags}


def detect_extraction_language(text: str) -> Literal["zh", "en"]:
    """按主要自然语言信号为单个提取分块选择中文或英文。"""
    normalized = unicodedata.normalize("NFKC", str(text))
    han_count = len(_HAN_CHARACTER_RE.findall(normalized))
    latin_word_count = len(_LATIN_WORD_RE.findall(normalized))
    zh_signal_count = len(_ZH_FUNCTION_SIGNAL_RE.findall(normalized))
    en_signal_count = len(_EN_FUNCTION_SIGNAL_RE.findall(normalized))
    if zh_signal_count != en_signal_count:
        return "zh" if zh_signal_count > en_signal_count else "en"
    if latin_word_count > han_count:
        return "en"
    return "zh"


def _normalize_compact_subject(subject: str) -> str:
    """只规范第一人称和已知别名，保留命名主体的原文形式。"""
    return normalize_entity_alias(subject)


def _postprocess_rules_fingerprint(
    language_router_version: str = LANGUAGE_ROUTER_VERSION,
) -> dict[str, Any]:
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
        "lesson_signals": lesson_signals.lesson_signal_rules_fingerprint(),
        "relative_time": relative_time_rules_fingerprint(),
        "english_system_prompt": ENGLISH_SYSTEM_PROMPT,
        "language_router_version": language_router_version,
        "admission": admission_rules_fingerprint(),
        "claim_count_overflow_policy": CLAIM_COUNT_OVERFLOW_POLICY_VERSION,
        "relation_metadata_projection": "disabled-after-v028-e3-gate",
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
            "compact_model_task_source_markers": _MODEL_TASK_SOURCE_MARKERS,
            "compact_tech_entity": _regex_fingerprint(_TECH_ENTITY_RE),
            "compact_backtick_entity": _regex_fingerprint(_BACKTICK_ENTITY_RE),
            "language_han": _regex_fingerprint(_HAN_CHARACTER_RE),
            "language_latin_word": _regex_fingerprint(_LATIN_WORD_RE),
            "language_zh_function": _regex_fingerprint(_ZH_FUNCTION_SIGNAL_RE),
            "language_en_function": _regex_fingerprint(_EN_FUNCTION_SIGNAL_RE),
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
    language_router_version: str = LANGUAGE_ROUTER_VERSION,
) -> str:
    """计算 prompt、响应 schema 与后处理规则的稳定提取配置指纹。"""
    payload = {
        "system_prompt": system_prompt,
        "response_schema": (
            temporal_gate_extraction_response_json_schema() if response_schema is None else response_schema
        ),
        "postprocess_rules": (
            _postprocess_rules_fingerprint(language_router_version) if postprocess_rules is None else postprocess_rules
        ),
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


@dataclass(frozen=True)
class ExtractionModes:
    verification_mode: Literal["off", "audit", "enforce"] = "off"
    lesson_signal_mode: Literal["off", "observe", "enforce"] = "observe"
    verification_claim_threshold: int = 5
    verification_empty_text_threshold: int = 1_000


def _resolve_extraction_modes(modes: ExtractionModes | None, legacy_modes: dict[str, Any]) -> ExtractionModes:
    if modes is not None and legacy_modes:
        raise TypeError("modes cannot be combined with legacy extraction mode keywords")
    return modes or ExtractionModes(**legacy_modes)


class LLMExtractor:
    """通过统一 LLMClient 执行结构化事实提取。"""

    prompt_hash = PROMPT_HASH
    extractor_version = LLM_EXTRACTOR_VERSION
    language_router_version = LANGUAGE_ROUTER_VERSION
    relation_metadata_projection_enabled = False

    def __init__(
        self,
        llm_client: LLMClient,
        chunking_policy: ChunkingPolicy,
        *,
        schema_retries: int = 2,
        structured_mode: StructuredOutputMode = StructuredOutputMode.JSON_SCHEMA,
        soft_split_enabled: bool = False,
        delta_repair_enabled: bool = False,
        verifier: EntailmentVerifier | None = None,
        modes: ExtractionModes | None = None,
        **legacy_modes: Any,
    ) -> None:
        self.llm_client = llm_client
        self.model = llm_client.model
        provider_name = getattr(llm_client, "provider_name", None)
        self.provider_name: str = provider_name if isinstance(provider_name, str) and provider_name else "unknown"
        self.schema_retries = schema_retries
        if self.schema_retries < 0:
            raise ValueError("schema_retries must be non-negative")
        self.structured_mode = structured_mode
        self.soft_split_enabled = soft_split_enabled
        self.delta_repair_enabled = delta_repair_enabled
        self.chunking_policy = chunking_policy
        modes = _resolve_extraction_modes(modes, legacy_modes)
        if modes.verification_mode not in {"off", "audit", "enforce"}:
            raise ValueError("verification_mode must be 'off', 'audit', or 'enforce'")
        if modes.verification_claim_threshold < 0 or modes.verification_empty_text_threshold < 0:
            raise ValueError("verification thresholds must be non-negative")
        self.verifier = verifier
        self.verification_mode = modes.verification_mode
        self.lesson_signal_mode = lesson_signals.validate_lesson_signal_mode(modes.lesson_signal_mode)
        self.verification_claim_threshold = modes.verification_claim_threshold
        self.verification_empty_text_threshold = modes.verification_empty_text_threshold
        self._run_state = ExtractionRunState()
        self._verification = VerificationCoordinator(
            verifier=self.verifier,
            mode=self.verification_mode,
            claim_threshold=self.verification_claim_threshold,
            empty_text_threshold=self.verification_empty_text_threshold,
            audit_getter=current_audit,
        )
        self._orchestrator = ExtractionOrchestrator(
            client=self.llm_client,
            provider_name=self.provider_name,
            model=self.model,
            config=ExtractionOrchestratorConfig(
                chunking_policy=self.chunking_policy,
                schema_retries=self.schema_retries,
                structured_mode=self.structured_mode,
                soft_split_enabled=self.soft_split_enabled,
                delta_repair_enabled=self.delta_repair_enabled,
            ),
            hooks=ExtractionOrchestratorHooks(
                bind_run_state=self._bind_run_state,
                project_claims=self._project_extraction_result,
                verify_claims=self._verify_extracted_claims,
                postprocess_claims=_postprocess_extracted_claims,
                system_prompt_for_language=self._system_prompt_for_language,
                response_json_schema=self._response_json_schema,
                language_detector=detect_extraction_language,
                legacy_claim_defaults=_LEGACY_CLAIM_DEFAULTS,
                kind_values=set(_KIND_MAP),
                notability_values=set(_NOTABILITY_IMPORTANCE),
                extractor_hash=self.prompt_hash,
            ),
            verification_enabled=self.verifier is not None and self.verification_mode != "off",
        )

    def _bind_run_state(self, state: ExtractionRunState) -> None:
        self._run_state = state

    @property
    def last_usage_tokens(self) -> int:
        return self._run_state.total_tokens

    @last_usage_tokens.setter
    def last_usage_tokens(self, value: int) -> None:
        self._run_state.total_tokens = value

    @property
    def last_input_tokens(self) -> int:
        return self._run_state.input_tokens

    @last_input_tokens.setter
    def last_input_tokens(self, value: int) -> None:
        self._run_state.input_tokens = value

    @property
    def last_output_tokens(self) -> int:
        return self._run_state.output_tokens

    @last_output_tokens.setter
    def last_output_tokens(self, value: int) -> None:
        self._run_state.output_tokens = value

    @property
    def _schema_retry_count(self) -> int:
        return self._run_state.schema_retry_count

    @_schema_retry_count.setter
    def _schema_retry_count(self, value: int) -> None:
        self._run_state.schema_retry_count = value

    @property
    def _repair_count(self) -> int:
        return self._run_state.repair_count

    @_repair_count.setter
    def _repair_count(self, value: int) -> None:
        self._run_state.repair_count = value

    @property
    def _llm_call_count(self) -> int:
        return self._run_state.llm_call_count

    @_llm_call_count.setter
    def _llm_call_count(self, value: int) -> None:
        self._run_state.llm_call_count = value

    @property
    def _memorize_decisions(self) -> list[tuple[bool, str]]:
        return self._run_state.memorize_decisions

    @property
    def _last_schema_errors(self) -> list[dict[str, Any]]:
        return self._run_state.schema_errors

    @property
    def _secret_rejections(self) -> dict[str, int]:
        return self._run_state.secret_rejections

    @property
    def _relation_metadata_counts(self) -> dict[str, int]:
        return self._run_state.relation_metadata_counts

    @property
    def last_relation_metadata(self) -> dict[str, int]:
        """返回最近一次 extract 的 RAO 来源边界判定计数。"""
        return dict(self._relation_metadata_counts)

    @property
    def last_llm_call_count(self) -> int:
        """返回最近一次 extract 含 schema retry/verifier 的实际 LLM 调用数。"""
        return self._llm_call_count

    def extract(self, content: dict[str, Any] | str, context: dict[str, Any] | None = None) -> list[ExtractedClaim]:
        """同步分块提取事实，并在输出截断或 claim 数超限时递归二分恢复。"""
        return list(self._orchestrator.extract(content, context).claims)

    def _verify_extracted_claims(
        self,
        claims: list[ExtractedClaim],
        source_text: str,
    ) -> list[ExtractedClaim]:
        """按 rollout 策略执行 audit-only 验证，并始终原样返回 claims。"""
        return self._verification.verify(claims, source_text, self._run_state)

    def _emit_verification_failure(self, error: Exception, claim_count: int) -> None:
        """记录安全、截断后的 fail-open verifier 错误。"""
        self._verification.emit_failure(error, claim_count)

    def _record_verifier_usage(self) -> None:
        """把额外审计调用计入提取器预算与诊断指标。"""
        self._verification.record_usage(self._run_state)

    @staticmethod
    def _infer_compact_qualifiers(
        attribute: str,
        subject: str,
        value: str,
        evidence_quote: str,
    ) -> dict[str, str]:
        """Only infer required qualifiers that are explicit in value and evidence."""
        definition = SLOT_REGISTRY.get(attribute)
        if definition is None or not definition.required_qualifiers:
            return {}
        normalized_value = unicodedata.normalize("NFC", value).casefold()
        normalized_evidence = unicodedata.normalize("NFC", evidence_quote).casefold()

        def source_bounded(candidate: str | None) -> str | None:
            if not candidate:
                return None
            normalized = unicodedata.normalize("NFC", candidate).strip()
            if not normalized:
                return None
            needle = normalized.casefold()
            return normalized if needle in normalized_value and needle in normalized_evidence else None

        qualifiers: dict[str, str] = {}
        for key in definition.required_qualifiers:
            if key == "key":
                match = _ENV_KEY_RE.search(value)
                candidate = source_bounded(match.group(0) if match is not None else None)
            elif key == "plan":
                candidate = source_bounded(value[:200])
            elif key == "task" and attribute == "choice.model":
                candidate = None
            else:
                candidate = source_bounded(subject)
            if candidate is not None:
                qualifiers[key] = candidate
        return qualifiers

    @staticmethod
    def _infer_compact_occurrence(
        text: str,
        occurred_at: str | None,
        claim_value: str | None = None,
    ) -> tuple[str | None, str | None]:
        """从绝对/相对日期与对话时间推断 claim 的发生区间。"""
        return infer_occurrence(text, occurred_at, claim_value=claim_value)

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
        """把 LLM 的 compact 候选准入并映射为现有完整 claim schema。"""
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
        lesson_signal, enforce_lesson_signal = lesson_signals.evaluate_lesson_signal(
            candidate.value, candidate.evidence_quote, self.lesson_signal_mode
        )
        candidate = replace(candidate, notability="high") if enforce_lesson_signal else candidate
        decision = admit_claim(candidate, source_text)
        episodic = decision.memory_layer == "episodic"
        current_audit().emit(
            "extract",
            "admission_checked",
            "accepted" if decision.accepted else "rejected",
            detail={
                "reason": decision.reason,
                "kind": candidate.kind,
                "notability": candidate.notability,
                "memory_layer": decision.memory_layer,
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
        if episodic:
            scope = "temporal"
            volatility = "ephemeral"
        predicate = normalize_predicate(predicate)
        subject = _normalize_compact_subject(candidate.subject)
        inferred_attribute = infer_canonical_attribute(predicate, subject, candidate.value, {})
        fallback_attribute = PREDICATE_ATTRIBUTE_MAP[predicate][1]
        if inferred_attribute not in {"custom.unknown", fallback_attribute}:
            canonical_attribute = inferred_attribute
        model_coordinate = project_model_coordinates(
            canonical_attribute,
            subject,
            candidate.value,
            candidate.evidence_quote,
        )
        subject = model_coordinate.subject
        qualifiers: dict[str, Any] = self._infer_compact_qualifiers(
            canonical_attribute,
            subject,
            candidate.value,
            candidate.evidence_quote,
        )
        if model_coordinate.task is not None:
            qualifiers["task"] = model_coordinate.task
        if model_coordinate.state_change:
            qualifiers["state_change"] = True
        qualifiers.update({"lesson_signal": lesson_signal} if enforce_lesson_signal else {})
        qualifiers = project_action_qualifiers(candidate.value, qualifiers, is_plan=candidate.kind == "plan")
        canonical_slot = validate_slot_instance(canonical_attribute, qualifiers)
        relation_qualifiers: dict[str, Any] = {}
        relation_reason = "not_provided"
        if self.relation_metadata_projection_enabled:
            relation_qualifiers, relation_reason = self._project_relation_metadata(
                subject=subject,
                value=candidate.value,
                evidence_quote=candidate.evidence_quote,
                action=raw.get("action"),
                object_=raw.get("object"),
            )
        if relation_reason != "not_provided":
            self._relation_metadata_counts[relation_reason] = self._relation_metadata_counts.get(relation_reason, 0) + 1
        if relation_reason not in {"accepted", "not_provided"}:
            current_audit().emit(
                "extract",
                "relation_metadata_checked",
                "discarded",
                detail={"reason": relation_reason},
            )
        qualifiers.update(relation_qualifiers)
        occurred_start, occurred_end = self._infer_compact_occurrence(
            candidate.evidence_quote,
            occurred_at,
            candidate.value,
        )
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
            "memory_layer": decision.memory_layer,
            "assertion_kind": raw.get("assertion_kind", "unknown"),
            "source_event_indices": raw["source_event_indices"],
        }

    @staticmethod
    def _project_relation_metadata(
        *,
        subject: str,
        value: str,
        evidence_quote: str,
        action: Any,
        object_: Any,
    ) -> tuple[dict[str, str], str]:
        """只投影可同时由公开 value 与证据逐字证明的完整 RAO。"""
        normalized_action = unicodedata.normalize("NFC", str(action or "")).strip()
        normalized_object = unicodedata.normalize("NFC", str(object_ or "")).strip()
        if not normalized_action and not normalized_object:
            return {}, "not_provided"
        if not normalized_action or not normalized_object:
            return {}, "partial_relation_metadata"
        normalized_value = unicodedata.normalize("NFC", value)
        normalized_evidence = unicodedata.normalize("NFC", evidence_quote)
        checks = (
            (normalized_action, normalized_evidence, "action_not_in_evidence_quote"),
            (normalized_action, normalized_value, "action_not_in_value"),
            (normalized_object, normalized_evidence, "object_not_in_evidence_quote"),
            (normalized_object, normalized_value, "object_not_in_value"),
        )
        for needle, haystack, reason in checks:
            if needle not in haystack:
                return {}, reason
        return {
            "role": unicodedata.normalize("NFC", subject).strip(),
            "action": normalized_action,
            "object": normalized_object,
        }, "accepted"

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

    def _record_admission(self, candidate: MemoryCandidate, source_text: str) -> AdmissionDecision:
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
                "memory_layer": decision.memory_layer,
            },
        )
        if not decision.accepted and decision.reason in {
            "recovery_code",
            "secret_assignment",
            "sk_token",
            "mixed_alnum_token",
        }:
            self._secret_rejections[decision.reason] = self._secret_rejections.get(decision.reason, 0) + 1
        return decision

    def _project_extraction_result(
        self,
        result: CompactExtractionResponseSchema | ExtractionResponseSchema,
        chunk: ExtractionChunk,
        event_context: dict[str, Any],
        occurred_at: str,
    ) -> list[ExtractedClaim]:
        """Project validated provider output through HL-Mem admission policy."""
        compact_response = isinstance(result, CompactExtractionResponseSchema)
        parsed: list[ExtractedClaim] = []
        source_kind = str(event_context.get("source_kind") or event_context.get("category") or "")
        if re.search(r"(?i)(?:\[quoted message\]|quoted report|历史报告|引用消息)", chunk.text):
            source_kind = "quoted_report"
        for item in result.claims:
            raw_claim = item.model_dump()
            source_mapping = self._source_mapping(
                raw_claim,
                event_context,
                indices_supplied="source_event_indices" in item.model_fields_set,
                fallback_text=chunk.text,
                fallback_occurred_at=occurred_at,
            )
            if source_mapping is None:
                continue
            source_text, source_occurred_at, source_actor_type = source_mapping
            if compact_response:
                postprocessed = self._postprocess_claim(raw_claim, source_text, source_occurred_at)
                if postprocessed is None:
                    continue
                raw_claim = postprocessed
            else:
                secret_reason = _secret_reason(raw_claim)
                if secret_reason is not None:
                    self._secret_rejections[secret_reason] = self._secret_rejections.get(secret_reason, 0) + 1
                    continue
                legacy_candidate = self._legacy_admission_candidate(raw_claim)
                if legacy_candidate is None:
                    continue
                decision = self._record_admission(legacy_candidate, source_text)
                if not decision.accepted:
                    continue
                raw_claim.setdefault("reason", decision.reason)
                raw_claim["memory_layer"] = decision.memory_layer
                if decision.memory_layer == "episodic":
                    raw_claim["scope"] = "temporal"
                    raw_claim["volatility"] = "ephemeral"
            secret_reason = _secret_reason(raw_claim)
            if secret_reason is not None:
                self._secret_rejections[secret_reason] = self._secret_rejections.get(secret_reason, 0) + 1
                continue
            claim = self._claim(raw_claim, preserve_subject=compact_response)
            normalized_scope, reason_code = normalize_scope(
                claim.scope,
                claim.predicate,
                claim.canonical_slot,
                claim.subject,
                claim.value,
                claim.qualifiers,
                canonical_attribute=claim.canonical_attribute,
                actor_type=source_actor_type,
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
        return [claim for claim in parsed if not _is_low_value_claim(claim)]

    @staticmethod
    def _source_mapping(
        raw_claim: dict[str, Any],
        event_context: dict[str, Any],
        *,
        indices_supplied: bool,
        fallback_text: str,
        fallback_occurred_at: str,
    ) -> tuple[str, str, str] | None:
        """校验批内来源索引，并返回仅由声明来源组成的准入文本与元数据。"""
        source_events = event_context.get("_source_events")
        if not isinstance(source_events, list) or not source_events:
            raw_claim["source_event_indices"] = list(raw_claim.get("source_event_indices") or [0])
            return (
                fallback_text,
                fallback_occurred_at,
                str(event_context.get("actor_type") or event_context.get("actor") or ""),
            )
        if len(source_events) > 1 and not indices_supplied:
            return None
        raw_indices = raw_claim.get("source_event_indices") or [0]
        if not isinstance(raw_indices, list):
            return None
        indices: list[int] = []
        for value in raw_indices:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= len(source_events):
                return None
            if value not in indices:
                indices.append(value)
        if not indices:
            return None
        selected = [source_events[index] for index in indices]
        if not all(isinstance(item, dict) for item in selected):
            return None
        raw_claim["source_event_indices"] = indices
        primary = selected[0]
        return (
            "\n".join(LLMExtractor._source_event_text(item) for item in selected),
            str(primary.get("occurred_at") or fallback_occurred_at),
            str(primary.get("actor_type") or primary.get("speaker") or ""),
        )

    @staticmethod
    def _source_event_text(event: dict[str, Any]) -> str:
        content = event.get("content", {})
        if isinstance(content, dict):
            text = content.get("text")
            return str(text) if text is not None else json.dumps(content, ensure_ascii=False, sort_keys=True)
        return str(content)

    @staticmethod
    def _count_repairs(original: Any, repaired: Any) -> int:
        return count_repairs(original, repaired)

    @staticmethod
    def _looks_like_truncated_json(content: str | dict[str, Any]) -> bool:
        return looks_like_truncated_json(content)

    @staticmethod
    def _merge_chunk_claims(chunks: list[list[ExtractedClaim]]) -> list[ExtractedClaim]:
        return merge_chunk_claims(chunks)

    @staticmethod
    def _uses_compact_schema(payload: dict[str, Any]) -> bool:
        return uses_compact_schema(payload)

    @staticmethod
    def _parse_legacy_defaults(payload: dict[str, Any]) -> dict[str, Any]:
        return parse_legacy_defaults(payload, _LEGACY_CLAIM_DEFAULTS)

    @staticmethod
    def _schema_error_paths(error: Exception) -> list[str]:
        return schema_error_paths(error)

    @staticmethod
    def _is_claim_count_overflow(error: BaseException) -> bool:
        return is_claim_count_overflow(error)

    @staticmethod
    def _schema_error_details(error: Exception, payload: Any) -> list[dict[str, Any]]:
        return schema_error_details(
            error,
            payload,
            kind_values=set(_KIND_MAP),
            notability_values=set(_NOTABILITY_IMPORTANCE),
        )

    @staticmethod
    def _schema_retry_instruction(
        previous_output: Any,
        schema_errors: list[dict[str, Any]],
        language: Literal["zh", "en"] = "zh",
    ) -> str:
        return schema_retry_instruction(previous_output, schema_errors, language)

    @staticmethod
    def _parse_json(raw: Any) -> dict[str, Any]:
        return parse_json_response(raw)

    @staticmethod
    def _claim(item: dict[str, Any], *, preserve_subject: bool = False) -> ExtractedClaim:
        return claim_from_payload(item, preserve_subject=preserve_subject, aliases=ALIASES)

    def _system_prompt_for_language(self, language: Literal["zh", "en"]) -> str:
        prompt = ENGLISH_SYSTEM_PROMPT if language == "en" else SYSTEM_PROMPT
        return lesson_signals.lesson_notability_prompt(prompt, language, self.lesson_signal_mode)

    def _response_json_schema(self) -> dict[str, Any]:
        """返回当前产品 compact 响应 schema；评测子类可冻结旧契约。"""
        return temporal_gate_extraction_response_json_schema()
