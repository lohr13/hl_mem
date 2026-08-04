"""基于统一 LLM 客户端的结构化 Claim 提取管线。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any, Literal, get_args

from pydantic import ValidationError as PydanticValidationError

from hl_mem.domain.claims.attributes import (
    _HIGH_CONFIDENCE_ATTRIBUTE_PATTERNS,
    ALLOWED_TOPIC_TAGS,
    ATTRIBUTE_ALIASES,
    ATTRIBUTE_HINTS,
    MUTUALLY_EXCLUSIVE_SLOTS,
    OPERATIONAL_SLOT_NAMES,
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

from .chunking import (
    ChunkingPolicy,
    ExtractionChunk,
    bisect_extraction_chunk,
    split_extraction_content,
)
from .extractors import ExtractedClaim
from .repair import ENUM_MAPPINGS, TOPIC_TAG_ZH_TO_EN, repair_extraction_json
from .schemas import ExtractionResponseSchema, TopicTag, extraction_response_json_schema
from .verifier import EntailmentVerifier

LOGGER = logging.getLogger(__name__)


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
_TOPIC_TAG_PROMPT = "、".join(sorted(get_args(TopicTag)))
_SCHEMA_ENUM_CONSTRAINTS = f"""【JSON 枚举与类型硬约束】
- topic_tags 的每一项必须是以下 {len(get_args(TopicTag))} 个英文标签之一：{_TOPIC_TAG_PROMPT}
- sensitivity 只能是英文字符串 'normal' / 'sensitive' / 'restricted'
- 顶层 entities 必须是字符串数组；claim.entities 必须是字符串数组或 null
"""

SYSTEM_PROMPT = f"""## 1. ROLE AND OUTPUT CONTRACT
你是长期记忆事实提取器。只提取由当前事件支持、对未来有行动价值的原子事实，不判断它们是否与已有记忆冲突。
只输出一个 JSON 对象，不要输出 JSON 以外的解释。对象结构保持为 claims、entities、should_memorize、sensitivity。
先逐个候选完成以下步骤；最后令 should_memorize = (claims 非空)。没有候选通过准入时，claims 返回 []，should_memorize 返回 false。

## 2. STEP 1 — GENERATE CANDIDATE PROPOSITIONS
从 <extract_from> 的当前事件中识别候选命题；<context_only> 仅用于消解主体和指代，不得作为新 claim 的证据来源。
每个候选应表达一个可独立判断真假的主体—关系—对象命题。原始事件可以作为 evidence 保留，但只有通过准入门的命题才成为 claim。

## 3. STEP 2 — SPEECH ACT AND ADMISSION GATE
先在内部将每个候选归为一种言语行为，不要把分类结果输出到 JSON：
- asserted：说话者明确陈述。
- committed：用户或有权限主体作出决定、承诺或约束。
- reported：工具或文档报告某次观测。
- proposed：建议、方案或评审意见。
- hypothetical：假设、示例或条件分支。
- procedural：正在执行、下一步、耗时或进度。
- phatic：寒暄、确认或感谢。
asserted、committed 可能准入；reported 仅在具有未来效用且 claim 保留来源/时点时可能准入；proposed、hypothetical、procedural、phatic 拒绝。
“建议/考虑/待定/或许/可以考虑/计划中/未执行”等未决信号默认不提取；若仍因直接证据与未来效用准入，confidence 不得高于 0.55。
“已确认/已批准/已执行/已完成/正式采用”等明确落地信号不属于未决信号，按实际证据强度判断。

每个候选必须依次通过四门，任一失败即不生成 claim：
1. 证据门：当前事件直接陈述、明确确认，或无歧义蕴含该命题；问题、建议、假设、预测和待验证项不算证据。
2. 未来效用门：未来检索会改变回答、决策、个性化、约束执行、任务连续性或冲突判断。
3. 持续/时点门：至少在当前事件之后仍有意义；已作出的决定、真实经历和承诺可以准入，纯进度或短暂快照通常拒绝。
4. 区分度门：脱离上下文仍具体、自足，且不是通用常识、礼貌话、复述、纯数字、纯路径或无主体状态。
核心判定问题：“如果六个月后检索到这条信息，它是否会改变 agent 的回答或行动？”还要问当前事件是否提供直接证据。
任一答案为“否”，不要生成 claim。

## 4. STEP 3 — ATOMIC, SELF-CONTAINED VALUE
一条 claim 只表达一个原子命题；复合句拆成多条。value 必须脱离原对话上下文和 qualifiers 后仍可理解，包含必要的主体、关系、对象和单位。
value 必须保持用户使用的原始语言：中文原文输出中文值，英文原文输出英文值，不要翻译。保留原文中的精确数字和日期，不得模糊化或改写。
短于 12 字符的纯状态词、数值或时长必须重写为完整命题；qualifiers 只能补充结构化信息，不能承载理解主句所必需的信息。
短值反例：
❌ "串行" → ✅ "LLM 提取任务串行执行"
❌ "90s" → ✅ "LLM 请求超时为 90 秒"
❌ subject=用户 value="Codex 只改代码" → ✅ subject=coding-workflow value="Codex 负责代码修改，Hermes 负责测试验证"
结合事件上下文中的 occurred_at 解析“今天”“明天”“下周”等相对时间，并在 value 中输出对应的绝对日期。
事实明确描述时间区间时，将起止时间分别写入 occurred_start 和 occurred_end；无法确定时返回 null。

## 5. STEP 4 — SUBJECT / PREDICATE / SLOT
先判断命题真正描述谁或什么。区分 speaker_entity 与 semantic_subject，subject 必须使用 semantic_subject；默认仅在事实确实描述用户时使用“用户”。
明确提到项目名或服务名时使用该名称。代词（他、她、它、那个）必须结合上下文替换为具体名称，不要保留代词。
subject 必须复用标准实体名。同一实体不得因大小写、空格、连字符、产品后缀或“插件/memory/CLI”等描述产生新名称。
若事件上下文提供 canonical_entities，必须从其中选择；组件级事实仍归组件，项目级事实归项目。
规范化示例：hlmem/HL_MEM → hl_mem；Codex CLI → Codex；LLMExtractor → llm_extractor。
版本号、端口、环境变量、路由规则和文件路径的 semantic_subject 不是“用户”，应绑定其所属产品、组件、配置或工作流。

predicate 保持以下七类且只能选择其一：
- 偏好：喜欢或不喜欢的事物。
- 使用：工具、数据库、操作系统等技术选择。
- 状态：当前服务或运行状态。
- 身份：用户名、角色、联系方式。
- 配置：端口、路径、参数和行为策略。
- 计划：计划事项和截止日期。
- 事实：当前 evidence 直接支持的其他客观命题。
用户批准的架构决定暂映射为 事实 + fact.architecture；行为约束优先映射为 配置 + config.policy。
评审意见、建议、假设和未确认发现不得使用“事实”。predicate 无法准确表达时宁可拒绝，不得用“事实”强行兜底；确需使用“事实”时，在 reason 中写明具体事实类型。

attribute 对照表：
- fact.architecture：系统结构、分层和组件关系；具体 API 方法签名、请求/响应格式才使用 fact.api_design。
- config.timeout：超时配置，不使用 config.env；config.policy：行为策略约束，不使用 config.env。
- preference.workflow：工作流偏好，不使用 choice.tool；config.path：文件路径，不使用 choice.tool。
canonical_attribute 是兼容字段：能确定 operational slot 时填写同名值；否则按 predicate 填写兼容属性。
能确定 canonical_attribute 时先选 attribute，再由 registry 投影 predicate。canonical_slot 无法唯一确定时必须返回 null，不得猜测或创造新值。
文本包含“改用”“换成”“现在用”“不用了”“改为”等变更信号时，在 qualifiers 中加入 "change": true。

## 6. STEP 5 — SCOPE THEN VOLATILITY
先判断 scope，再独立判断 volatility；不要从 predicate 直接推断。
scope 回答“命题在哪段时间内有效”：
- permanent：没有已知结束边界，跨会话持续成立直到新证据修改；不表示永远不变。
- temporal：只对明确时间窗、一次运行、当前阶段、某版本、某任务或某个事件成立。
判断：“该命题是否绑定某次运行、明确截止日期、当前阶段或版本？”是 → temporal，否则 → permanent。
volatility 回答“在有效期内预计多容易变化”：
- stable：短期内通常不会自然变化，需要明确决定或事件才改变。
- ephemeral：会随运行、环境、状态刷新或短期计划频繁变化。
即使没有明确截止期，若预计数小时或数天内自动刷新，选择 ephemeral。

四象限对照：
| 示例 | scope | volatility | 是否准入 |
| 用户长期偏好简洁回答 | permanent | stable | 是 |
| hl_mem 默认使用 SQLite WAL | permanent | stable | 是 |
| 用户下周三前完成 benchmark | temporal | stable | 是，明确承诺/截止期 |
| 当前服务监听临时端口 8200 | temporal | ephemeral | 通常否；仅后续任务依赖时是 |
| CI 当前 443 passed | temporal | ephemeral | 否 |
temporal + stable 是合法且重要的，适合有期限但期限内稳定的计划、旅行和冻结期配置。
permanent + ephemeral 应极少出现；选择该组合时必须重新检查它是否其实是 temporal。

## 7. STEP 6 — EVIDENCE CONFIDENCE
confidence 只表示当前 evidence 是否足以支持 claim 的内容和归因；不表示 importance、语气强度、持续时长、分类把握或与已有记忆是否一致。
每条准入 claim 只能选择以下离散锚点之一：
- 0.98：用户明确要求记住，或权威结构化字段直接给出。
- 0.90：当前消息直接、无条件陈述，主体和对象明确。
- 0.75：消解明确上下文中的代词或省略后得到，且只有一种合理解释。
- 0.55：转述、历史报告或工具推断，内容可能真实但当前性或归因较弱；仍必须通过准入门。
- < 0.50：含歧义、推测、建议、未确认评审意见或主体不明，不准入。
禁止输出 0.91、0.93、0.95 等未定义中间值。事实明确但 predicate/slot 不确定时，不要降低 confidence 掩盖分类问题；slot 不唯一则返回 null。

importance 与 confidence 分离。importance 必须是 0.0 到 1.0：
- 0.9-1.0：核心身份、永久偏好、关键约束。
- 0.7-0.8：重要架构决策、工具选择、配置。
- 0.5-0.6：项目状态、计划、一般事实。
- 0.3-0.4：一次性操作记录、临时状态。
- < 0.2：不写入（噪声）。
不要仅因情绪化措辞提高 importance。保护类型 explicit_memory、identity.name 即使低分也写入。

## 8. EXCLUSIONS
跳过以下信息，不要提取为 claim：
- 服务健康状态报告，如 healthz 返回值、服务状态 ok/running/stopped、版本号查询结果。
- 工具自身的实现细节，如 git commit hash、文件行数、测试数量、迁移编号、数据库审计日志条数。
- 脱离上下文的纯数字、纯版本号、纯路径；value 少于 5 个字符或仅为数字和点号的组合。
- 临时调试输出、中间步骤状态报告，如“正在处理”“已启动 Codex”。
- 正在执行、下一步动作、预计耗时、CI 快照和过程进度。
- 未确认的评审意见、建议、风险猜测和待验证发现。
- 已被覆盖的旧配置值，如 superseded 的 provider 变更历史。
- assistant 对用户原话的复述或确认；不得因此产生第二条 claim。
assistant 的“测试已通过”属于 reported 快照，默认拒绝；只有用户明确要求记住某个验收基线时例外。

## 9. CONTRASTIVE FEW-SHOTS
对照 1（讨论中的建议，拒绝）：
输入：assistant：flaky 测试失败时重跑两次，也可以考虑把测试命令改成 pytest -v -x。用户：先讨论，方案待定，尚未执行。
输出：{{"claims":[],"should_memorize":false}}
说明：建议、可以考虑、待定和尚未执行都表示 proposed/procedural，不是已批准政策。

对照 2（用户明确确认的政策，允许）：
输入：用户：已确认并批准：CI 失败只允许重跑一次，此后必须停止并报告。
输出要点：{{"subject":"CI workflow","predicate":"配置","canonical_attribute":"config.policy","value":"CI 失败只允许重跑一次，此后必须停止并报告","confidence":0.90,"scope":"permanent","volatility":"stable"}}
说明：用户明确确认并批准了可约束未来行为的政策，不是讨论建议。

对照 3（代码与实测共同证明的架构，允许）：
输入：tool：代码显示 Database 初始化执行 PRAGMA journal_mode=WAL；实测查询 journal_mode 返回 wal。
输出要点：{{"subject":"hl_mem","predicate":"事实","canonical_attribute":"fact.architecture","value":"hl_mem 的 SQLite 存储使用 WAL 模式","confidence":0.90,"scope":"permanent","volatility":"stable"}}
说明：代码与实测结果共同直接证明稳定的架构描述，不是未验证的评审意见。

正例 1（明确偏好）：
输入：用户：以后回答尽量简洁，先给结论，不要长篇铺垫。
输出要点：{{"subject":"用户","predicate":"偏好","canonical_slot":"preference.response_style","value":"用户偏好简洁回答，并要求先给结论、避免长篇铺垫","confidence":0.90,"scope":"permanent","volatility":"stable"}}
说明：直接陈述、可改变未来回答且无已知结束边界。

正例 2（有期限的计划，occurred_at=2026-07-29）：
输入：用户：我决定周五前完成 extraction benchmark，期间先不切换模型。
输出两条原子 claim：
[{{"predicate":"计划","value":"用户计划在 2026-07-31 前完成 extraction benchmark","confidence":0.90,"scope":"temporal","volatility":"stable"}},
 {{"predicate":"配置","value":"extraction benchmark 完成前保持当前模型不变","confidence":0.90,"scope":"temporal","volatility":"stable"}}]
说明：明确截止边界和承诺，期限内稳定，不是 ephemeral。

正例 3（确认后的架构决定）：
输入：assistant：评审建议把时间解析拆成第二步。用户：同意，就按这个方案定下来，事实抽取阶段不要解析时间。
输出要点：{{"subject":"hl_mem","predicate":"事实","canonical_attribute":"fact.architecture","value":"hl_mem 将事实抽取与时间解析拆分为两个阶段","qualifiers":{{"change":true}},"confidence":0.75,"scope":"permanent","volatility":"stable"}}
说明：assistant 的建议本身不准入；用户确认后成为架构决定，命题需从唯一上下文消解“这个方案”。

反例 1（过程状态与耗时预测）：
输入：assistant：我正在执行检索，预计还要 10 分钟，完成后会运行测试。
输出：{{"claims":[],"should_memorize":false}}
说明：全是 procedural future/progress，没有跨事件效用。

反例 2（CI / 健康快照）：
输入：assistant：CI 全绿，443 passed、1 skipped，healthz 返回 ok。
输出：{{"claims":[],"should_memorize":false}}
说明：这是会自动刷新且不改变未来行为的 tool/status snapshot。

反例 3（未确认的评审意见）：
输入：reviewer：ingest.py 可能存在事务不原子的问题，建议进一步验证。
输出：{{"claims":[],"should_memorize":false}}
说明：“可能”“建议验证”是 proposed/hypothetical finding，不得改写为“ingest.py 的事务不原子”。

## 10. JSON SCHEMA CONSTRAINTS
每个 claim 必须包含 subject、predicate、canonical_attribute、canonical_slot、topic_tags、value、qualifiers、confidence、volatility、reason、scope、importance、occurred_start、occurred_end、entities；不得增删或改名。
canonical_slot 只表示参与业务规则的 operational slot，只能从以下 15 个值选择；无法唯一确定时返回 null：
{_OPERATIONAL_SLOT_PROMPT}
topic_tags 必须是 JSON 数组，只能包含以下 44 个英文标签，可返回空数组；禁止输出中文标签或集合外的值：
{_TOPIC_TAG_PROMPT}
顶层和每条 claim 的 entities 必须是 JSON 数组，数组元素必须是字符串，例如 ["PostgreSQL"]；没有实体时分别返回 [] 或 null，禁止直接输出字符串。
entities 列出明确涉及的实体名。sensitivity 只能是 normal、sensitive、restricted，禁止输出中文值。
predicate 只能是：偏好、使用、状态、身份、配置、计划、事实。
scope 只能是 temporal、permanent；volatility 只能是 stable、ephemeral。
输出必须满足以下 schema enum 约束：
{_SCHEMA_ENUM_CONSTRAINTS}"""

SYSTEM_PROMPT += """
再次确认：只输出 JSON；should_memorize 等于 claims 是否非空；所有 claim 字段和 enum 必须符合 schema。"""

ALIASES = {"pg": "PostgreSQL", "postgres": "PostgreSQL", "postgresql": "PostgreSQL"}
LOW_VALUE_HEALTH_STATES = frozenset({"ok", "running", "stopped", "健康", "正常"})
NUMERIC_OR_VERSION_RE = re.compile(r"[0-9.]+")
_RECOVERY_CODE_RE = re.compile(r"(?i)(?<![a-z0-9])[a-z0-9]{5}-[a-z0-9]{5}(?![a-z0-9])")
_SECRET_ASSIGNMENT_RE = re.compile(r"""(?i)["']?(?:password|passwd|api[_-]?key)["']?\s*[:=]""")
_SECRET_FIELD_NAME_RE = re.compile(r"(?i)(?:password|passwd|api[_-]?key)")
_SK_TOKEN_RE = re.compile(r"(?i)(?<![a-z0-9])sk-[a-z0-9_-]+")
_ALNUM_SECRET_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{16,32}(?![A-Za-z0-9])")
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
        "unsettled_confidence_ceiling": _UNSETTLED_CONFIDENCE_CEILING,
        "repair_enum_mappings": ENUM_MAPPINGS,
        "repair_topic_tag_mappings": TOPIC_TAG_ZH_TO_EN,
        "patterns": {
            "numeric_or_version": _regex_fingerprint(NUMERIC_OR_VERSION_RE),
            "recovery_code": _regex_fingerprint(_RECOVERY_CODE_RE),
            "secret_assignment": _regex_fingerprint(_SECRET_ASSIGNMENT_RE),
            "secret_field_name": _regex_fingerprint(_SECRET_FIELD_NAME_RE),
            "sk_token": _regex_fingerprint(_SK_TOKEN_RE),
            "mixed_alnum_secret": _regex_fingerprint(_ALNUM_SECRET_RE),
            "unsettled_signal": _regex_fingerprint(_UNSETTLED_SIGNAL_RE),
            "settled_signal": _regex_fingerprint(_SETTLED_SIGNAL_RE),
            "temporal_scope": _regex_fingerprint(_TEMPORAL_SCOPE_RE),
            "permanent_scope": _regex_fingerprint(_PERMANENT_SCOPE_RE),
            "health_check": _regex_fingerprint(_HEALTH_CHECK_RE),
            "runtime_configuration": _regex_fingerprint(_RUNTIME_CONFIGURATION_RE),
            "tool_snapshot": _regex_fingerprint(_TOOL_SNAPSHOT_RE),
            "quoted_report": _regex_fingerprint(_QUOTED_REPORT_RE),
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
    value = unicodedata.normalize("NFKC", str(claim.value)).strip()
    if not value:
        return True
    if NUMERIC_OR_VERSION_RE.fullmatch(value) and claim.canonical_slot not in MUTUALLY_EXCLUSIVE_SLOTS:
        return True
    return claim.canonical_slot == "state.service_health" and value.casefold() in LOW_VALUE_HEALTH_STATES


def _secret_reason(value: Any) -> str | None:
    """识别禁止进入 Claim 存储的确定性凭据格式。"""
    if isinstance(value, dict):
        for key, nested_value in value.items():
            normalized_key = unicodedata.normalize("NFKC", str(key)).strip()
            key_reason = _secret_reason(normalized_key)
            if key_reason is not None:
                return key_reason
            has_value = nested_value is not None and (
                not isinstance(nested_value, (str, dict, list, tuple, set, frozenset)) or bool(nested_value)
            )
            if _SECRET_FIELD_NAME_RE.fullmatch(normalized_key) and has_value:
                return "secret_assignment"
            nested_reason = _secret_reason(nested_value)
            if nested_reason is not None:
                return nested_reason
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            nested_reason = _secret_reason(item)
            if nested_reason is not None:
                return nested_reason
        return None
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if _RECOVERY_CODE_RE.search(text):
        return "recovery_code"
    if _SECRET_ASSIGNMENT_RE.search(text):
        return "secret_assignment"
    if _SK_TOKEN_RE.search(text):
        return "sk_token"
    for match in _ALNUM_SECRET_RE.finditer(text):
        token = match.group(0)
        if any(character.isalpha() for character in token) and any(character.isdigit() for character in token):
            return "mixed_alnum_token"
    return None


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
            self._memorize_decisions.append((False, "should_memorize=false"))
            return []
        parsed: list[ExtractedClaim] = []
        source_kind = str(event_context.get("source_kind") or event_context.get("category") or "")
        if re.search(r"(?i)(?:\[quoted message\]|quoted report|历史报告|引用消息)", chunk.text):
            source_kind = "quoted_report"
        for item in result.claims:
            raw_claim = item.model_dump()
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
    ) -> ExtractionResponseSchema:
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
