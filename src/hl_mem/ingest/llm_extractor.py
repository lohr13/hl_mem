from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import replace
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from hl_mem.domain.claims.attributes import (
    ALLOWED_TOPIC_TAGS,
    MUTUALLY_EXCLUSIVE_SLOTS,
    OPERATIONAL_SLOT_NAMES,
    SLOT_REGISTRY,
    infer_canonical_attribute,
    normalize_predicate,
    normalize_topic_tags,
    reconcile_canonical_attribute,
    validate_canonical_slot,
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

from .chunking import (
    ChunkingPolicy,
    ExtractionChunk,
    bisect_extraction_chunk,
    split_extraction_content,
)
from .extractors import ExtractedClaim
from .schemas import ExtractionResponseSchema, extraction_response_json_schema


def _operational_slot_prompt() -> str:
    """从 registry 渲染允许 LLM 选择的 operational slot。"""
    lines: list[str] = []
    for name in OPERATIONAL_SLOT_NAMES:
        definition = SLOT_REGISTRY[name]
        qualifiers = "、".join(definition.required_qualifiers) or "无"
        examples = "；".join(definition.examples) or "无"
        lines.append(f"- {name}：{definition.description}；必需 qualifiers：{qualifiers}；示例：{examples}")
    return "\n".join(lines)


_OPERATIONAL_SLOT_PROMPT = _operational_slot_prompt()
_TOPIC_TAG_PROMPT = "、".join(sorted(ALLOWED_TOPIC_TAGS))

SYSTEM_PROMPT = f"""你是长期记忆事实提取器。只提取用户值得长期记住的原子事实；忽略闲聊、寒暄和临时信息。只提取事实，不判断是否与已有记忆冲突。输出一个 JSON 对象，包含 claims、entities、should_memorize、sensitivity。每个 claim 包含 subject、predicate、canonical_attribute、canonical_slot、topic_tags、value、qualifiers、confidence、volatility、reason。volatility 只能是 ephemeral（实时状态或临时数据）或 stable（偏好、配置和事实）。
value 必须保持用户使用的原始语言：中文原文输出中文值，英文原文输出英文值，不要翻译。保留原文中的精确数字和日期，不得模糊化或改写。
结合事件上下文中的 occurred_at 解析“今天”“明天”“下周”等相对时间，并在事实中输出对应的绝对日期。
predicate 只能是以下标准值之一：偏好（喜欢或不喜欢的事物）、使用（工具、数据库、操作系统等技术选择）、状态（当前服务或运行状态）、身份（用户名、角色、联系方式）、配置（端口、路径、参数）、计划（计划事项、截止日期）、事实（其他客观事实）。
canonical_attribute 是兼容字段：对能确定 operational slot 的事实填写同名值；否则按 predicate 填写兼容属性，系统会保持旧逻辑校验。
canonical_slot 只表示参与业务规则的 operational slot，只能从以下 15 个值选择；无法唯一确定时必须返回 null，不得创造新值：
{_OPERATIONAL_SLOT_PROMPT}
topic_tags 是用于检索的多值标签，只能从以下集合选择，可返回空数组：
{_TOPIC_TAG_PROMPT}
subject 默认为“用户”；明确提到项目名或服务名时使用该名称。代词（他、她、它、那个）必须结合上下文替换为具体名称；不要在事实中保留代词。
subject 必须复用标准实体名。同一实体不得因大小写、空格、连字符、产品后缀或“插件/memory/CLI”等描述产生新名称。若事件上下文提供 canonical_entities，必须从其中选择；组件级事实仍归组件，项目级事实归项目。示例：hlmem/HL_MEM → hl_mem；Codex CLI → Codex；LLMExtractor → llm_extractor。
文本包含“改用”“换成”“现在用”“不用了”“改为”等变更信号时，在 qualifiers 中加入 \"change\": true。
跳过以下低价值信息，不要提取为 claim：
- 服务健康状态报告（如 healthz 返回值、服务状态 ok/running/stopped、版本号查询结果）
- 工具自身的实现细节（如 git commit hash、文件行数、测试数量、迁移编号、数据库审计日志条数）
- 脱离上下文的纯数字、纯版本号、纯路径（value 少于 5 个字符或仅为数字和点号的组合时不提取）
- 临时调试输出、中间步骤状态报告（如"正在处理..."、"已启动 Codex"）
- 已被覆盖的旧配置值（如 superseded 的 provider 变更历史）
如果 should_memorize 为 false 或所有 claim 都属于上述类型，返回空 claims 列表。
不要输出 JSON 以外的解释。"""

SYSTEM_PROMPT += """
Every claim must also include scope and importance. Scope is independent from volatility.
scope must be temporal (useful for a bounded real-world period, such as a trip next week,
a current project deadline, or a temporary service state) or permanent (a durable preference,
identity, convention, configuration, or explicit long-term memory). Volatility describes only
change rate, not retention. importance must be a number from 0.0 to 1.0: 0.0-0.3 incidental,
0.4-0.6 useful, 0.7-0.9 an important preference, commitment, or constraint, and 1.0 an explicit
must-remember instruction. Do not infer importance merely from emotional wording.

scope 表示事实的有效期，不表示变化频率：
- temporal：有截止期、仅描述当前/本次/某版本/某次运行，或未来会被新状态替换；
- permanent：身份、稳定偏好、长期约束、设计原则，以及不依赖某次运行或版本的系统能力。
判断问题：一年后且脱离本次会话，这条事实仍应作为当前事实成立吗？是 → permanent；否 → temporal。
正反例：
“当前测试 180 passed” → temporal；“项目使用 pytest” → permanent。
“已部署 v0.3.0” → temporal；“系统支持在线备份” → permanent。
“本次修复了 FTS5 查询” → temporal；“FTS5 查询会转义用户 token” → permanent。
“端口固定为 8200” → permanent；“服务现在监听 8200” → temporal。
"""

ALIASES = {"pg": "PostgreSQL", "postgres": "PostgreSQL", "postgresql": "PostgreSQL"}
LOW_VALUE_HEALTH_STATES = frozenset({"ok", "running", "stopped", "健康", "正常"})
NUMERIC_OR_VERSION_RE = re.compile(r"[0-9.]+")
_TEMPORAL_SCOPE_RE = re.compile(
    r"(?i)(?:"
    r"\bdeadline\b|截止|临时|本次|这次|当前运行|本轮|某次运行|"
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


def normalize_scope(
    llm_scope: str,
    predicate: str,
    canonical_attribute: str,
    subject: str,
    value: Any,
    qualifiers: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """根据高置信语义规则规范 scope，并返回可审计的原因码。"""
    scope = llm_scope if llm_scope in {"temporal", "permanent"} else "permanent"
    normalized_predicate = normalize_predicate(predicate)
    text = unicodedata.normalize("NFKC", f"{subject} {value} {qualifiers or {}}")

    if _TEMPORAL_SCOPE_RE.search(text):
        return "temporal", "explicit_temporal_signal"
    if normalized_predicate in {"身份", "偏好", "explicit_memory"}:
        return "permanent", "durable_predicate"
    if _PERMANENT_SCOPE_RE.search(text):
        return "permanent", "explicit_permanent_signal"
    if canonical_attribute.startswith("state."):
        return "temporal", "state_default"
    if canonical_attribute.startswith("plan."):
        return "temporal", "plan_default"
    return scope, "llm_preserved"


def _is_low_value_claim(claim: ExtractedClaim) -> bool:
    """判断 LLM 提取结果是否属于应在输出边界丢弃的低价值 claim。"""
    value = unicodedata.normalize("NFKC", str(claim.value)).strip()
    if not value:
        return True
    if NUMERIC_OR_VERSION_RE.fullmatch(value) and claim.canonical_attribute not in MUTUALLY_EXCLUSIVE_SLOTS:
        return True
    return claim.canonical_attribute == "state.service_health" and value.casefold() in LOW_VALUE_HEALTH_STATES


class LLMExtractor:
    """通过统一 LLMClient 执行结构化事实提取。"""

    def __init__(
        self,
        llm_client: LLMClient,
        chunking_policy: ChunkingPolicy,
        *,
        schema_retries: int = 2,
        structured_mode: StructuredOutputMode = StructuredOutputMode.JSON_SCHEMA,
    ) -> None:
        self.llm_client = llm_client
        self.model = llm_client.model
        self.schema_retries = schema_retries
        if self.schema_retries < 0:
            raise ValueError("schema_retries must be non-negative")
        self.structured_mode = structured_mode
        self.chunking_policy = chunking_policy
        self.last_usage_tokens = 0

    def extract(self, content: dict[str, Any] | str, context: dict[str, Any] | None = None) -> list[ExtractedClaim]:
        """同步分块提取事实，并在输出截断时递归二分恢复。"""
        self.last_usage_tokens = 0
        event_context = context or {}
        chunks = split_extraction_content(content, self.chunking_policy)
        chunk_claims = [self._extract_chunk_with_auto_split(chunk, event_context, depth=0) for chunk in chunks]
        return self._merge_chunk_claims(chunk_claims)

    def _extract_chunk_with_auto_split(
        self,
        chunk: ExtractionChunk,
        event_context: dict[str, Any],
        depth: int,
    ) -> list[ExtractedClaim]:
        """提取单块；仅输出截断时按策略递归二分。"""
        try:
            return self._extract_one_chunk(chunk, event_context)
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

    def _extract_one_chunk(
        self,
        chunk: ExtractionChunk,
        event_context: dict[str, Any],
    ) -> list[ExtractedClaim]:
        """请求并严格校验一个内容分块，schema 失败时执行内容级重试。"""
        context = json.dumps(event_context, ensure_ascii=False)
        occurred_at = str(event_context.get("occurred_at", "未知"))
        result = self._request_chunk(chunk, context, occurred_at)
        if not result.should_memorize:
            return []
        parsed: list[ExtractedClaim] = []
        for item in result.claims:
            claim = self._claim(item.model_dump())
            normalized_scope, reason_code = normalize_scope(
                claim.scope,
                claim.predicate,
                claim.canonical_attribute,
                claim.subject,
                claim.value,
                claim.qualifiers,
            )
            current_audit().emit(
                "extract",
                "scope_normalized",
                "changed" if normalized_scope != claim.scope else "preserved",
                detail={
                    "llm_scope": claim.scope,
                    "normalized_scope": normalized_scope,
                    "reason_code": reason_code,
                    "canonical_attribute": claim.canonical_attribute,
                },
            )
            parsed.append(replace(claim, scope=normalized_scope))
        return [claim for claim in parsed if not _is_low_value_claim(claim)]

    def _request_chunk(
        self,
        chunk: ExtractionChunk,
        context: str,
        occurred_at: str,
    ) -> ExtractionResponseSchema:
        """请求并严格校验一个内容分块，schema 失败时执行内容级重试。"""
        schema_errors: list[str] = []
        for attempt in range(self.schema_retries + 1):
            retry_instruction = ""
            if schema_errors:
                retry_instruction = "\n上一次输出不符合 schema。只修正这些错误路径/类型：" + ", ".join(schema_errors)
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
            self.last_usage_tokens += response.usage_total_tokens
            if response.finish_reason in {"length", "max_tokens"}:
                raise LLMOutputTruncatedError(
                    f"LLM output truncated: provider={self.llm_client.provider.name}, model={self.model}"
                )
            try:
                raw = self._parse_json(response.content)
                compatible = self._parse_legacy_defaults(raw)
                return ExtractionResponseSchema.model_validate(compatible)
            except (PydanticValidationError, ValueError) as error:
                if self._looks_like_truncated_json(response.content):
                    raise LLMOutputTruncatedError(
                        f"LLM output appears truncated: provider={self.llm_client.provider.name}, model={self.model}"
                    ) from error
                schema_errors = self._schema_error_paths(error)
                if attempt == self.schema_retries:
                    raise LLMSchemaValidationError(
                        "LLM response does not contain valid JSON or match schema: "
                        f"provider={self.llm_client.provider.name}, model={self.model}, "
                        f"chunk_length={len(chunk.text)}, errors={schema_errors}"
                    ) from error
        raise RuntimeError("unreachable")

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
                    unicodedata.normalize("NFKC", claim.canonical_attribute).strip().casefold(),
                    unicodedata.normalize("NFKC", str(claim.value)).strip().casefold(),
                    unicodedata.normalize(
                        "NFKC",
                        json.dumps(claim.qualifiers, ensure_ascii=False, sort_keys=True, default=str),
                    ),
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(claim)
        return merged

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
            defaults: dict[str, Any] = {
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
        subject = str(item.get("subject", "用户"))
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
            canonical_slot=validate_canonical_slot(item.get("canonical_slot")),
            topic_tags=normalize_topic_tags(item.get("topic_tags")),
        )
