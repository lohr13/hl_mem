#!/usr/bin/env python
"""Generate docs/configuration.md from the Settings configuration metadata."""

from __future__ import annotations

import sys
import types
from dataclasses import Field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hl_mem import __version__  # noqa: E402
from hl_mem.config.models import iter_config_fields  # noqa: E402
from hl_mem.config_loader import load_settings  # noqa: E402
from hl_mem.settings import Settings  # noqa: E402

CONSTRAINTS = {
    "database.pool_size": ">= 1",
    "database.busy_timeout_seconds": ">= 1",
    "conflict.l1_min_confidence_delta": "`0.10`、`0.15`、`0.20`",
    "conflict.l1_min_time_delta_seconds": "`0`、`300`、`3600`",
    "entity.aliases_path": "非空字符串；可省略",
    "index.backfill_batch_size": ">= 1",
    "index.backfill_max_attempts": ">= 1",
    "index.text_version": "非空字符串",
    "embedding.text_type": "`document`、`query`；可省略",
    "relation.expansion_max_depth": ">= 1",
    "relation.discovery_pool_limit": ">= 1",
    "relation.discovery_max_proposals": ">= 1",
    "state.latest_wins_slots": "代码白名单内的 slot；当前仅 `config.version`",
    "recall.default_limit": "1 - 100",
    "recall.vector_scan_limit": ">= 1",
    "recall.packed_context_token_budget": ">= 1",
    "recall.candidate_floor": ">= 1",
    "recall.dedup_threshold": "0.0 - 1.0；0 关闭折叠",
    "recall.dedup_candidate_limit": ">= 1",
    "recall.resurrection_candidate_limit": ">= 1",
    "recall.resurrection_min_term_coverage": "0.0 - 1.0（不含 0）",
    "recall.relevance_reranker_floor": "0.0 - 1.0",
    "recall.relevance_dense_floor": "0.0 - 1.0",
    "recall.relevance_relative_drop": "0.0 - 1.0",
    "recall.relevance_intents": ("非空数组；元素为 current_state、preference、historical、tool、procedure"),
    "recall.preference_recency_boost": "0.0 - 1.0",
    "recall.tag_boost_weight": "0.0 - 1.0",
    "recall.query_expansion_max": "0 - 2",
    "recall.query_expansion_candidate_floor": "> 0",
    "recall.query_expansion_token_ceiling": "> 0",
    "recall.query_expansion_timeout_seconds": "> 0",
    "recall.query_expansion_total_timeout_seconds": "> 0",
    "recall.query_expansion_max_concurrency": "> 0",
    "recall.query_context_max_events": "> 0",
    "recall.query_context_token_budget": "> 0",
    "recall.procedure_llm_threshold": "0.0 - 1.0",
    "recall.procedure_router_timeout_seconds": "> 0",
    "recall.procedure_candidate_limit": "> 0",
    "recall.procedure_recent_outcome_window": "> 0",
    "recall.procedure_outcome_half_life_days": "> 0",
    "recall.side_effect_max_attempts": ">= 1",
    "recall.side_effect_backoff_seconds": ">= 0",
    "recall.vector_batch_size": ">= 1",
    "recall.feedback_min_samples": ">= 1",
    "recall.echo_session_window_seconds": "60 - 14400",
    "recall.echo_pending_similarity_threshold": "0.0 - 1.0",
    "recall.echo_pending_max_seconds": ">= 60",
    "recall.expansion_circuit_failure_threshold": ">= 1",
    "recall.expansion_circuit_open_seconds": "> 0",
    "hermes.timeout": ">= 1",
    "hermes.home": "非空字符串；可省略",
    "hermes.circuit_failure_threshold": ">= 1",
    "hermes.circuit_open_seconds": "> 0",
    "hermes.on_demand_recall_timeout_seconds": "> 0",
    "hermes.prefetch_cache_ttl_seconds": "> 0",
    "usage.price_book_path": "非空本地路径；可省略",
    "llm.model": "非空字符串",
    "llm.max_tokens": "正整数；输出上限保险丝，截断可能导致 JSON 不完整（`finish=length`），结构化提取将“快速失败”并由上层重试/降级",
    "llm.reasoning_effort": "`low`、`high`、`max`；可省略",
    "llm.timeout": "> 0",
    "llm.max_attempts": ">= 1",
    "llm.schema_retries": ">= 0",
    "image_describer.timeout_seconds": "> 0",
    "image_describer.max_bytes": ">= 1",
    "image_describer.max_parts": ">= 1",
    "extraction.chunk_target_chars": ">= 1",
    "extraction.chunk_overlap_turns": ">= 0",
    "extraction.max_split_depth": ">= 0",
    "extraction.soft_split_enabled": "已弃用 no-op；仍接受 `true`、`false`",
    "extraction.delta_repair_enabled": "已弃用 no-op；仍接受 `true`、`false`",
    "extraction.batch_max_events": "1 - 32",
    "extraction.batch_max_wait_seconds": ">= 0",
    "dedup.threshold": "0.0 - 1.0",
    "dedup.auto_merge_min_confidence": "dedup.threshold - 1.0",
    "dedup.scan_limit": ">= 1",
    "dedup.cron": "HH:MM（00:00 - 23:59）",
    "dedup.max_pending_pairs": ">= 1",
    "retention.temporal_ttl_days_low": ">= 1",
    "retention.temporal_ttl_days_normal": ">= 1",
    "retention.temporal_ttl_days_high": ">= 1",
    "retention.importance_low_threshold": "见字段联动约束",
    "retention.importance_high_threshold": "见字段联动约束",
    "retention.importance_write_floor": "见字段联动约束",
    "retention.slot_short_ttl_seconds": ">= 1",
    "retention.ttl_backfill_batch_size": ">= 1",
    "retention.ttl_backfill_grace_hours": ">= 0",
    "retention.temporal_cleanup_age_days": ">= 1",
    "retention.temporal_cleanup_expiry_days": ">= 1",
    "retention.decay_temporal_days": ">= 1；不得大于 archive_temporal_days",
    "retention.archive_temporal_days": ">= 1",
    "retention.decay_permanent_days": ">= 1；不得大于 archive_permanent_days",
    "retention.archive_permanent_days": ">= 1",
    "retention.access_bonus_every": ">= 1",
    "retention.access_bonus_days": ">= 0",
    "retention.access_bonus_cap_days": ">= 0",
    "retention.decay_rollout_grace_days": ">= 1",
    "retention.decay_min_confidence": "0.0 - 1.0",
    "retention.feedback_bonus_every": "> 0",
    "retention.feedback_bonus_days": ">= 0",
    "retention.feedback_bonus_cap_days": ">= 0",
    "retention.operational_batch_size": ">= 1",
    "retention.expired_claim_retention_days": ">= 1",
    "retention.expired_cleanup_batch_size": ">= 1",
    "retention.job_succeeded_days": ">= 1",
    "retention.job_dead_days": ">= 1",
    "retention.llm_span_days": ">= 1",
    "retention.dedup_pair_days": ">= 1",
    "retention.feedback_uninjected_days": ">= 1",
    "retention.feedback_unlabeled_days": ">= 1",
    "server.max_request_body": ">= 0",
    "decay.temporal_half_life_days": ">= 1",
    "decay.permanent_half_life_days": ">= 1",
    "decay.identity_half_life_days": ">= 1",
    "decay.halflife_archive_threshold": "0.0 - 1.0（不含端点）",
    "decay.halflife_archive_grace_days": ">= 1",
    "worker.policy_induction_lookback_days": ">= 1",
    "worker.policy_induction_min_episodes": ">= 1",
    "worker.job_lease_minutes": ">= 1",
    "worker.conflict_maintenance_max_cases": "1 - 1000",
    "worker.conflict_maintenance_budget_ms": "50 - 10000",
    "worker.conflict_failure_backoff_seconds": "1 - 86400",
    "worker.conflict_writer_yield_ms": "0 - 1000",
    "worker.conflict_auto_resolve_max_candidates": "2 - 10000",
}

TABLE_NOTES = {
    "database": [
        "",
        "`database.path` 的相对值以配置文件 symlink 的真实目标目录为基准，而不是进程当前目录。建议使用",
        '`path = "var/hl_mem.db"` 保持跨平台可移植；Windows drive/UNC 绝对路径只允许在 Windows，POSIX',
        "绝对路径只允许在 POSIX，异平台绝对路径会在启动前 fail-fast。",
    ],
    "embedding": [
        "",
        "`embedding.text_type` 仅在 native API 模式下发送；默认不设置，compatible 模式不使用该参数。显式启用、修改或取消",
        "该角色后，应以同一最终配置重建存量 Claim 向量，避免查询与文档向量混用不同表示约定。sparse/instruct 变体仅用于",
        "显式 benchmark 配置，生产默认关闭。",
    ],
    "llm": [
        "",
        '`llm.thinking_control = "auto"` 保持 provider 现有请求格式：DashScope 发送顶层 `enable_thinking`，',
        'Zhipu 与通用 OpenAI-compatible provider 不发送思考控制字段。仅当 `llm.provider = "openai_compatible"` 且',
        '`llm.thinking_control = "chat_template_kwargs"` 时，客户端发送嵌套的',
        "`chat_template_kwargs = {enable_thinking = ...}`，直接使用 `json_object` 结构化输出，并仅剥离 JSON 前的空",
        "`<think>...</think>` 块。该兼容模式面向 llama.cpp 等本地 OpenAI-compatible 端点。",
        "`llm.reasoning_effort` 仅在显式配置时作为顶层字段发送给 Zhipu；默认未设置，不改变其他 provider 请求体。",
    ],
    "extraction": [
        "",
        "Worker 只合并同一 namespace/session 的 `message` Event；窗口满 `batch_max_events` 时立即提取，否则最多等待",
        "`batch_max_wait_seconds`。显式记忆、无 session 事件和非 message 事件不等待。Hermes 的 `sync_turn` 会原子写入",
        "user/assistant 一对 Event，通常在该上限内与后续相邻 turn 合并；Claim 仍分别链接实际来源 Event。",
        "默认值偏向降低提取调用成本：增大批量上限或等待时间有利于合并更多相邻 Event、摊薄 LLM 调用成本，",
        "但会增加低流量 session 的提取延迟；需要低延迟时可调小这两个值。",
        "从 v1.1.3 起，`extraction.soft_split_enabled` 与 `extraction.delta_repair_enabled` 仅作为兼容配置继续接受，",
        "已弃用且不再触发额外模型调用。普通提取以不超过 12 条上下文完整 Claim 为目标；合法响应超过每 chunk",
        "16 条时，程序按 notability、confidence 和原始顺序确定性保留前 16 条并记录 overflow 审计。只有 provider",
        "实际输出截断（例如 `finish_reason=length`）仍会按 `extraction.max_split_depth` 对输入二分恢复。",
    ],
    "index": [
        "",
        "`natural` 生成 `subject：value`，不把内部 predicate、slot 或 topic tags 混入 FTS/embedding 文本。已有数据库不会在启动时自动重算 embedding；先运行 `hlmem backfill-index-text --mode natural --dry-run` 查看影响，再显式运行不带 `--dry-run` 的同一命令完成可续跑回填。",
    ],
    "plan": [
        "",
        "`enforce` 只允许唯一逻辑计划组的严格坐标匹配；complete/cancel/replace 只关闭 `valid_to`，partial 使用 Decimal",
        "累计。任何坐标缺失、多组匹配、超量或单位变化都不关闭计划。",
    ],
    "price": [
        "",
        "`enforce` 只接受已存在 typed instrument 的 exchange-qualified code 或唯一显式 alias；解析器不能创建 canonical ID。",
        "缺 target 时价格序列继续返回 `uncertain:price_target_missing`。",
    ],
    "provenance": [
        "",
        "`enforce` applies deterministic source/session admission. `observe` records the",
        "same decisions without changing extraction or Claim semantics. Neither mode",
        "adds model calls or attempts fact verification.",
        "",
        "Event 只新增 `origin_class` 和 `session_kind` 两个来源字段。旧客户端未提供时，两者均保存为 `unknown` 并保持",
        "1.0 行为。当前受控值如下：",
        "",
        "| 场景 | `origin_class` | `session_kind` | `enforce` 行为 |",
        "|---|---|---|---|",
        "| 交互用户原话 | `direct_user` | `interactive` | 正常提取与写入 |",
        "| Agent 自身输出 | `agent` | `interactive` | 正常提取与写入 |",
        "| 外部 Tool 原文或转述 | `external` / `external_derived` | `interactive` | 可保存；Claim 降为 low authority，并保留观察时间和证据 |",
        "| cron 自动会话 | `system` | `cron` | 可保存为低权威时效观察，不自动成为永久规则 |",
        "| heartbeat / subagent | `system` | 对应 session | Event 保留；在模型调用前阻止自动 Claim 提取 |",
        "| 旧宿主或未知来源 | `unknown` | `unknown` | 完全保持旧行为 |",
        "",
        "Hermes 只从结构化 `platform` / `agent_context` 映射 interactive、cron、heartbeat 或 subagent；没有该元数据时为",
        "`unknown`，不会根据消息正文猜测 session 类型。",
        "",
        "用户显式要求记忆外部信息只保护保留意图，不会把来源升级为已验证事实。系统不使用 LLM 猜测来源，也不执行",
        "事实核查或历史回填。只读命令 `hl-mem explain claim <claim-id> [--json]` 显示当前 Claim 状态、直接 Evidence、",
        "安全来源提示和当前治理解释；它不输出 Claim 正文、工具结果、URL 凭据/路径/查询或配置密钥，也不重建已经过期的",
        "历史准入审计。",
    ],
    "reranker": [
        "",
        "Reranker 的具体型号通过 `reranker.model` 配置；API 密钥由 `RERANKER_API_KEY` 提供。升级时以当前 `Settings` 或部署",
        "TOML 为准，活文档不固定具体型号。",
    ],
    "state": [
        "",
        "`[state]` 控制白名单状态 slot 的确定性 latest-wins 关链；当前仅支持 `config.version`。默认 `observe` 只记录",
        '建议，不改变 claim 或 conflict case；设置 `latest_wins_mode = "off"` 可完全关闭建议和动作。`enforce` 只执行',
        "已通过冻结门禁的确定性动作，灰区仍保持并存。",
    ],
    "usage": [
        "",
        "`usage.price_book_path` 是可选的宿主本地 JSON 价格表，schema 见",
        "[`usage-pricing.schema.json`](usage-pricing.schema.json)。相对路径以 `hl_mem.toml` 所在目录解析；",
        "价格表只支持 CNY 整数 microunits、精确 capability/model/provider 匹配，不支持远程 include、正则或表达式。",
        "未配置或没有匹配规则时成本保持 unknown；启用有限 `daily_cost_limit_microunits` 时 unknown reserve 会 fail-closed。",
    ],
    "worker": [
        "",
        "Worker 在任务执行期间按 lease 时长的三分之一周期续租全部同窗口 job；进度回调也会续租。若 token ownership",
        "在终态写入前丢失，本次执行返回 `lease_lost`，不会把更新 0 行误报为成功。",
        "冲突维护从持久 dirty queue 按 case 数与毫秒预算有界处理；失败按 case 退避，稳定人工案不会重复扫描或写入。",
    ],
    "retention": [
        "",
        "运维历史清理默认开启，并按表独立事务和 `operational_batch_size` 分批执行。pending/running Job、pending 去重候选、",
        "已标注 feedback 永不由该清理器删除；未注入 feedback 使用较短窗口。关闭 `operational_cleanup_enabled` 会同时跳过",
        "运维表和 audit 的定期清理。",
    ],
}


TABLE_NOTES["retention"].extend(
    [
        "",
        "Expired Claims are eligible only after `expired_claim_retention_days`, with no downstream evidence consumer "
        "and no open conflict. Maintenance defaults to `observe`; `on` processes one bounded batch through the "
        "tombstone-backed `DeletionService`. Offline-copy apply requires the exact dry-run `--expected-count`.",
        "",
        "Pending dedup pairs below the current `dedup.threshold` can be reported read-only and terminally classified "
        "with `dedup drain-below-floor --apply --expected-count <exact-count>`; the drain never changes Claims.",
    ]
)


def render_type(annotation: Any) -> str:
    """Return the TOML-facing type name for one Settings annotation."""
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Literal:
        return render_type(type(arguments[0]))
    if origin is tuple:
        item_type = render_type(arguments[0]) if arguments else "值"
        return f"{item_type} 数组"
    if origin in {types.UnionType, Union}:
        non_none = [item for item in arguments if item is not type(None)]
        if len(non_none) == 1:
            return render_type(non_none[0])
        return " / ".join(render_type(item) for item in non_none)
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return "字符串"
    return {
        str: "字符串",
        int: "整数",
        float: "数值",
        bool: "布尔值",
    }.get(annotation, str(annotation))


def render_default(value: Any) -> str:
    """Render a Settings default as its TOML equivalent."""
    if value is None:
        return "未设置"
    if isinstance(value, StrEnum):
        value = value.value
    if isinstance(value, str):
        return f'`"{value}"`'
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    if isinstance(value, tuple):
        items = ", ".join(f'"{item}"' for item in value)
        return f"`[{items}]`"
    return f"`{value}`"


def render_choices(annotation: Any) -> str | None:
    """Extract enum choices directly from the Settings annotation."""
    origin = get_origin(annotation)
    if origin is Literal:
        return "、".join(f"`{value}`" for value in get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return "、".join(f"`{item.value}`" for item in annotation)
    return None


def render_allowed(settings_field: Field[Any], annotation: Any) -> str:
    """Describe allowed values, adding Settings.validate ranges where present."""
    key_path = str(settings_field.metadata["toml"])
    production_choices = {
        "embedding.mode": "生产仅 `real`；`fake` 仅供测试",
        "extraction.mode": "生产为 `real` 或 `llm`；`fake` 仅供测试",
        "reranker.mode": "生产为 `off`、`on` 或 `real`；`fake` 仅供测试",
    }
    if key_path in production_choices:
        return production_choices[key_path]
    if key_path in CONSTRAINTS:
        return CONSTRAINTS[key_path]
    choices = render_choices(annotation)
    if choices is not None:
        return choices
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    optional = origin in {types.UnionType, Union} and type(None) in arguments
    if annotation is bool:
        return "`true`、`false`"
    if optional:
        return f"{render_type(annotation)}；可省略"
    return f"任意{render_type(annotation)}"


def generate() -> str:
    """Build the complete reference from Settings metadata."""
    annotations = get_type_hints(Settings)
    toml_fields = sorted(
        (item for item in iter_config_fields() if "toml" in item.metadata),
        key=lambda item: str(item.metadata["toml"]),
    )
    secret_fields = sorted(
        (item for item in iter_config_fields() if "secret_env" in item.metadata),
        key=lambda item: str(item.metadata["secret_env"]),
    )

    lines = [
        "# HL-Mem 配置参考",
        "",
        f"HL-Mem {__version__} 使用带 `schema_version = 1` 的 TOML 保存非敏感配置，并用 `.env` 或同名进程环境变量保存五个密钥。",
        "`Settings` 是唯一 schema；下表由 `Settings` 字段 metadata 自动生成。未写入 TOML 的字段使用代码默认值。",
        "模型型号不在活文档中固化：LLM、Embedding、Reranker 和图片描述器的 API 密钥通过 `.env` 配置，provider/model 等非敏感选项通过 TOML 配置。",
        "",
        f"v{__version__} 的 assertion/source/session 治理共用 `[provenance].mode`；存量 `unknown` 保持旧行为，不改变",
        "supersede、召回或注入。",
        "",
        "## 后台自动化默认值",
        "",
        "后台维护按副作用分成两类。确定性维护默认开启且不调用模型；语义任务必须分别显式开启，不能由 API key 的存在",
        "隐式启用。",
        "",
        "| 工作 | 默认值 | 模型调用 | 行为边界 |",
        "|---|---:|---:|---|",
        "| TTL、过期、衰减、归档、清理与 stale 传播 | 开 | 否 | 有界批处理；单项失败回滚后继续其他维护项 |",
        "| Observation 构建 | 开 | 否 | 只构建有证据的 Observation；不自动生成 Mental Model |",
        "| near-copy review | `dedup.enabled=true` | 否 | 只更新 dedup 审计 pair，不合并 Claim |",
        "| 确定性 L0 冲突处理 | 开 | 否 | 只执行受控规则；灰区进入人工案卷 |",
        "| LLM dedup | `dedup.llm_enabled=false` | 是 | 开启后仍受统一用量预算与审计约束 |",
        "| LLM 冲突归并 | `worker.semantic_conflict_consolidation_enabled=false` | 是 | 只记录 `audit_only:<kind>`，不改变 Claim 或冲突案卷 |",
        "| Policy 归纳发布 | `worker.policy_induction_enabled=false` | 否 | 关闭原因是自动发布派生状态，而非模型费用 |",
        "| LLM 重分类 | `worker.reclassify_enabled=false` | 是 | 禁用时入队和执行两处均拒绝 |",
        '| 关系发现 | `relation.discovery_mode="off"` | 是 | 仅支持 `off|audit`；`audit` 只写 Proposal |',
        '| Resurrection | `recall.resurrection_mode="off"` | 否 | 禁用时旧 pending 待办在 handler 前被废弃 |',
        "",
        "升级到本版本时，Migration 058 会把旧版本遗留的 pending 语义 Job 标记为 `dead`，并把 pending Resurrection",
        "待办标记为 `abandoned`；运行中的 Job 和其他待办不被迁移器改写。Migration 059 为正式关系边增加来源：旧边为",
        "`legacy`，新边只能由受控写入口标记为 `deterministic`、`manual` 或 `approved_proposal`。",
        "",
        "## 合并版发版决议",
        "",
        '- `conflict.auto_mode="l0_only"`：生产仅执行确定性 L0；灰区案进入 `manual_required`。紧急停用可设为 `off`。',
        '- `plan.fulfillment_mode="enforce"`：E5 A 臂严格确定性规则通过，错误关闭为 0；回滚开关为 `observe` 或 `off`。',
        '- `price.target_mode="enforce"`：E6 B 臂 exact code / typed alias 通过；缺 target、跨市场歧义、币种或单位不一致仍',
        "  fail-closed。回滚开关为 `observe` 或 `off`。",
        "- `dedup.audit_only=true`：E2 v2 的 0.98/0.99 臂均未达到发布精度门禁；再次翻转前仍需 auto precision 100%、",
        "  Wilson 95% 下界至少 96%、结构违规为 0 且 recall 无显著退化。",
        '- `extraction.lesson_signal_mode="observe"`：E3 v2 封存，继续使用旧 notability prompt；新 prompt 只有在目标信号、',
        "  high precision、诱饵误报、误 permanent 和一般提取覆盖全部过线后才能替换。",
        '  **此开关的作用（供用户自行判断是否开启）**：设为 `"enforce"` 后，提取器会识别"用户纠正 / 可复用防错规则 /',
        "  曾导致高成本失败的教训 / 『以后必须/禁止』类持久指令”四类信号并提高其 importance——效果是这类记忆在召回",
        '  排序和 TTL 中获得更高权重（"记教训比记事实值钱"场景）。离线评测：目标信号 recall 95%、high precision 100%、',
        "  诱饵误报 0、误转 permanent 0（合成语料）；未过的门禁仅为判分模型双序一致性，属评测装备限制而非信号质量缺陷。",
        '  该改动只影响新提取 claim 的重要性打分，非破坏性、可随时改回 `"observe"` 回滚。如果你的 agent 使用场景中',
        "  纠错/防错类记忆的价值密度高（运维、交易纪律、协作规范等），建议开启。",
        '- `recall.entity_constraint_mode="enforce"`：仅当查询中的全部 active mention 唯一解析到同一 typed canonical entity，',
        "  且 Claim link coverage 完整时，才在 FTS/Dense 候选截断前限定实体范围；历史 alias、歧义、多实体、链接不完整或",
        "  存储异常均回退宽召回。可显式设为 `observe` 做 shadow 对照，或设为 `off` 完全关闭解析。",
        "- `hermes.manual_conflict_notice=true`：只提示 residual `manual_required_count`，同一会话首次或计数变化时显示；",
        "  可设为 `false` 立即关闭。",
        "",
        "## 加载规则",
        "",
        "- 通用 CLI/server 默认读取当前工作目录的 `hl_mem.toml`；Hermes 插件固定读取 `<HERMES_HOME>/hl_mem.toml`，不依赖宿主进程 CWD。文件缺失、TOML 语法错误、未知表、未知键或类型错误都会阻止启动。",
        "- 通用 CLI/server 的 `.env` 默认相对当前工作目录读取；Hermes 插件固定读取 `<HERMES_HOME>/.env`。`.env` 可以缺失，进程环境中的同名密钥覆盖它。",
        "- 相对 `database.path` 以配置文件 symlink 的真实目标目录为基准；异平台绝对路径会阻止启动，不会被当成相对路径创建影子数据库。",
        "- 除五个密钥外，环境变量不参与配置；所有 `HL_MEM_*` 变量均被忽略。",
        "- TOML 使用原生类型；仅允许数组转换为 tuple、字符串转换为枚举。密钥不得写入 TOML。",
        "- 可从 [`config.example.toml`](../config.example.toml) 复制常用配置；该示例显式启用真实能力，推荐值不等于代码默认值。",
        "",
        "```bash",
        "hl-mem init",
        "uv run hl-mem doctor",
        "uv run python start_server.py",
        "```",
        "",
        "## 启动入口",
        "",
        "- Windows 使用 `start_production.bat`，Git Bash/POSIX 使用 `./start_hl_mem.sh`。两个脚本都从脚本自身位置定位仓库根目录，并调用 `start_server.py`，因此可以从任意当前目录启动。",
        "- 脚本使用仓库内的虚拟环境；Shell 入口兼容 `.venv/bin/python` 和 `.venv/Scripts/python.exe`。",
        "- 启动脚本不保存第二份运行配置，也不选择 provider/model，且不再设置旧版 `HL_MEM_*` 覆盖。除五个密钥外，所有有效配置都只来自仓库根目录的 `hl_mem.toml`；loader 会忽略继承到进程中的 `HL_MEM_*`。",
        "- 直接执行 `uv run python start_server.py` 时，`hl_mem.toml` 和 `.env` 仍相对进程当前目录解析。",
        "",
        "## 部署边界",
        "",
        "HL-Mem 面向受信任环境中的本地单租户部署。API 的 `namespace` 只是用于召回、Episode、Policy 和维护任务的",
        "相关性/profile 软标签，不是认证、授权、加密或侧信道安全边界；`tenant_id` 仅作为已弃用的兼容别名。备份与",
        "恢复始终覆盖整个 SQLite 数据库，不提供按 namespace 导出、RBAC、按租户密钥、计费或 SaaS 多租户隔离。",
        "",
        "## 备份与恢复",
        "",
        "```bash",
        "hl-mem backup var/backup.db --db var/hl_mem.db",
        "hl-mem restore var/backup.db --manifest var/backup.db.manifest.json \\",
        "  --db var/hl_mem.db --confirm-overwrite",
        "```",
        "",
        "`backup` 会在首次运行时创建并绑定 `<source>.tombstones.db`，输出的 manifest v2 记录 ledger ID 与 schema",
        "version；JSON 结果同时包含 `ledger_id` 和 `ledger_schema_version`。ledger 不嵌入主库 backup，必须与 backup",
        "分别受保护保存。恢复到目标库前，应把权威 ledger 放在 `<target>.tombstones.db`；缺失、ID/版本错配或无 ledger",
        "identity 的 v1 manifest 均 fail-closed，不能静默恢复。",
        "",
        "`restore` 会先校验 manifest、大小、哈希以及 backup/manifest/ledger 三方身份，再在目标同目录的临时数据库",
        "重放全部 tombstone 并执行 `PRAGMA integrity_check`，成功后才原子替换目标；任何失败都保留原目标。结果额外",
        "报告 `tombstones_replayed`、`claims_removed` 和 `events_removed`。目标已存在时必须提供",
        "`--confirm-overwrite`，且 source、backup、manifest、target 不得解析为同一路径。校验与恢复会拒绝 backup、",
        "target 或 ledger 旁残留的 `-wal`、`-shm`、`-journal` sidecar，防止未纳入验证的页面影响结果。执行 restore",
        "前必须停止 API、Worker 及其他数据库/ledger 使用者，成功后再重启服务。",
        "",
        "## 从 v0.36.1 配置迁移到 schema v1",
        "",
        "先停止 API、Worker 和全部写入者。用显式 `--db` 创建恢复集；该命令不加载旧配置：",
        "",
        "```bash",
        "hl-mem backup var/pre-v1.db --db var/hl_mem.db",
        "hl-mem config migrate --config hl_mem.toml",
        "hl-mem config migrate --config hl_mem.toml --apply \\",
        "  --backup var/pre-v1.db --manifest var/pre-v1.db.manifest.json",
        "hl-mem doctor --config hl_mem.toml --backup var/pre-v1.db \\",
        "  --manifest var/pre-v1.db.manifest.json",
        "```",
        "",
        "第一次 `config migrate` 只输出确定性变更计划，不写文件。`--apply` 会先验证 backup、manifest 与当前数据库",
        "的 tombstone ledger 身份，再把旧配置逐字节保存为 `hl_mem.toml.v0.bak`，最后原子替换配置。已有备份、",
        "来源在规划后变化、未知键、Fake 模型模式或恢复集错配都会 fail-closed。配置 schema 不支持 downgrade。",
        "数据库 migration 仍只向前；回滚必须停止写入者，恢复升级前数据库、权威 tombstone ledger、旧配置与旧二进制，",
        "不能让旧二进制打开已升级数据库。",
        "",
        "## JSONL Event 归档",
        "",
        "```bash",
        "hl-mem export var/events.jsonl --db var/hl_mem.db",
        "hl-mem import var/events.jsonl --db var/restored.db",
        "```",
        "",
        "默认 import 会在同一批次事务中为每个新 Event 创建 `extract_event` job，幂等键为",
        "`extract:<event_id>`，使 Worker 能从归档重建 Claims。重复导入会跳过已有 Event/job，不增加记录。JSON",
        "报告包含 `processed`、`events_created`、`events_skipped`、`jobs_queued`、`failed_batch` 和",
        "`claims_not_rebuilt`；非法记录会回滚当前批次并报告 batch/line。",
        "",
        "若 Event 已由旧版 importer 或 `--skip-extraction-jobs` 导入，但稳定 extraction job 缺失，随后执行普通 import",
        "会验证 Event payload 并补建 job；同 ID 不同 payload 会明确失败，不会被当作重复记录静默跳过。",
        "Event 的 `metadata_json` 属于归档与幂等冲突判定的一部分；turn locator 等 metadata 会在导入/导出后原样保留。",
        "",
        "`--skip-extraction-jobs` 只用于不希望重建 Claims 的取证恢复。该模式仅导入 Events、不会排队提取，并明确",
        "输出 `claims_not_rebuilt=true`。",
        "",
        "## 密钥",
        "",
        "| 环境变量 | Settings 字段 | 需要提供的条件 |",
        "|---|---|---|",
    ]
    secret_requirements = {
        "LLM_API_KEY": "生产 extraction，以及启用的 query expansion 或 relation discovery",
        "QUERY_EXPANSION_API_KEY": "可选；配置 recall.query_expansion_provider/base_url 时必填",
        "EMBEDDING_API_KEY": "embedding.mode = real",
        "RERANKER_API_KEY": "reranker.mode = on 或 real",
        "IMAGE_API_KEY": "image_describer.mode = on",
    }
    for item in secret_fields:
        name = str(item.metadata["secret_env"])
        lines.append(f"| `{name}` | `{item.name}` | {secret_requirements[name]} |")

    lines.extend(
        [
            "",
            "空值和常见占位值（如 `xxx`、`changeme`、`<key>`）不能用于已启用的真实组件；图片密钥不回退到 LLM 密钥。",
            "",
            "## TOML 键",
            "",
            "“允许值”来自字段注解及 `Settings.validate()`；标为“任意”的字段当前只做 TOML 原生类型校验。",
        ]
    )

    current_table = ""
    for item in toml_fields:
        key_path = str(item.metadata["toml"])
        table = key_path.split(".", 1)[0]
        if table != current_table:
            if current_table:
                lines.extend(TABLE_NOTES.get(current_table, []))
            current_table = table
            lines.extend(
                [
                    "",
                    f"### `[{table}]`",
                    "",
                    "| TOML 键 | 类型 | 默认值 | 允许值 | Settings 字段 |",
                    "|---|---|---|---|---|",
                ]
            )
        annotation = annotations[item.name]
        lines.append(
            f"| `{key_path}` | {render_type(annotation)} | {render_default(item.default)} | "
            f"{render_allowed(item, annotation)} | `{item.name}` |"
        )

    lines.extend(TABLE_NOTES.get(current_table, []))

    lines.extend(
        [
            "",
            "## 字段联动",
            "",
            "- `retention.importance_write_floor <= retention.importance_low_threshold <= "
            "retention.importance_high_threshold`，且三者都在 `0.0 - 1.0`。",
            "- `retention.decay_temporal_days <= retention.archive_temporal_days`；"
            "`retention.decay_permanent_days <= retention.archive_permanent_days`。",
            "- `dedup.auto_merge_min_confidence` 不得低于 `dedup.threshold`。",
            '- `recall.resurrection_mode = "auto"` 只在主召回低 answerability 或空结果时执行有界 archived-only FTS；'
            "候选仍须通过当前有效时间、全部来源引用完整性、冲突竞争者和高词项覆盖门禁；重嵌入服务失败时保留原召回结果。",
            '- `decay.model = "activation_halflife"` 是默认值，使用 '
            "`activation = base * 2^(-inactive_days / half_life)` 且不改 confidence；"
            '`"legacy_linear"` 保留旧版 confidence 线性衰减，`"confidence_halflife"` 仅保留为实验对照臂。',
            "- activation 半衰期按 temporal/permanent/identity 分档为 45/90/365 天；"
            "新建与迁移存量的 `activation_base/activation` 均从 1.0 开始，命中只刷新 `last_accessed_at`；"
            "activation 低于阈值并持续超过宽限期后才归档；"
            "`activation_halflife` 排序臂用 activation 承接原 confidence 的 `0.075` 冻结权重。",
            '- `image_describer.mode = "on"` 时，base URL 必须使用 HTTPS，模型名不能为空；'
            "若同时允许 `file:` URI，`file_allow_roots` 不能为空。",
            "- `hermes.enabled = true` 时，`hermes.url` 不能为空。",
            "",
            "权威实现见 [`src/hl_mem/settings.py`](../src/hl_mem/settings.py) 和 "
            "[`src/hl_mem/config_loader.py`](../src/hl_mem/config_loader.py)。",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    load_settings(
        ROOT / "config.example.toml",
        ROOT / ".env.example",
        environ={
            "LLM_API_KEY": "sk-reference-llm",
            "EMBEDDING_API_KEY": "sk-reference-embedding",
            "RERANKER_API_KEY": "sk-reference-reranker",
            "IMAGE_API_KEY": "sk-reference-image",
        },
    )
    (ROOT / "docs" / "configuration.md").write_text(generate(), encoding="utf-8", newline="\n")
