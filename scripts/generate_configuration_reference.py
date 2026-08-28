#!/usr/bin/env python
"""Generate docs/configuration.md from the Settings configuration metadata."""

from __future__ import annotations

import sys
import types
from dataclasses import Field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hl_mem import __version__  # noqa: E402
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
    "relation.auto_apply_confidence": "0.0 - 1.0",
    "relation.conflict_confidence": "0.0 - 1.0",
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
    "recall.tag_channel_weight": "0.0 - 1.0",
    "recall.tag_candidate_limit": ">= 1",
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
    "llm.model": "非空字符串",
    "llm.max_tokens": "正整数；输出上限保险丝，截断可能导致 JSON 不完整（`finish=length`），结构化提取将“快速失败”并由上层重试/降级",
    "llm.timeout": "> 0",
    "llm.max_attempts": ">= 1",
    "llm.schema_retries": ">= 0",
    "maintenance_judge.base_url": "loopback OpenAI-compatible `/v1` 端点",
    "maintenance_judge.model": "端点提供的模型名",
    "maintenance_judge.prompt_version": "非空版本标识",
    "maintenance_judge.tokenizer_identity": "非空 tokenizer 标识",
    "maintenance_judge.timeout_seconds": "> 0",
    "image_describer.timeout_seconds": "> 0",
    "image_describer.max_bytes": ">= 1",
    "image_describer.max_parts": ">= 1",
    "extraction.chunk_target_chars": ">= 1",
    "extraction.chunk_overlap_turns": ">= 0",
    "extraction.max_split_depth": ">= 0",
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
    "extraction": [
        "",
        "Worker 只合并同一 namespace/session 的 `message` Event；窗口满 `batch_max_events` 时立即提取，否则最多等待",
        "`batch_max_wait_seconds`。显式记忆、无 session 事件和非 message 事件不等待。Hermes 的 `sync_turn` 会原子写入",
        "user/assistant 一对 Event，通常在该上限内与后续相邻 turn 合并；Claim 仍分别链接实际来源 Event。",
        "默认值偏向降低提取调用成本：增大批量上限或等待时间有利于合并更多相邻 Event、摊薄 LLM 调用成本，",
        "但会增加低流量 session 的提取延迟；需要低延迟时可调小这两个值。",
    ],
    "index": [
        "",
        "`natural` 生成 `subject：value`，不把内部 predicate、slot 或 topic tags 混入 FTS/embedding 文本。已有数据库不会在启动时自动重算 embedding；先运行 `hlmem backfill-index-text --mode natural --dry-run` 查看影响，再显式运行不带 `--dry-run` 的同一命令完成可续跑回填。",
    ],
    "maintenance_judge": [
        "",
        "`[maintenance_judge]` 是可选的冲突 backlog 判官配置，不属于摄取 LLM。生产发布配置使用",
        '`conflict.auto_mode = "l0_only"`，不配置本段时没有常驻 AI 旁听或生产 LLM 依赖；模糊案保留为',
        "`manual_required`，可由 Hermes 的一次性提示暴露。",
        "",
        "端点可以承载任意 OpenAI-compatible 判官和更强模型；非本机服务应先通过 loopback-only 网关暴露，避免维护",
        "进程直接依赖公网地址。需要定时裁决 backlog 时，先用随包 E1 装备对冻结 70 案自验：",
        "",
        "```powershell",
        ".venv/Scripts/python.exe scripts/run_v030_experiments.py e1 `",
        "  --manifest-dir ~/hl_mem_docs/evaluations/v030/manifests `",
        "  --output-dir ~/hl_mem_docs/evaluations/v030/e1-user-replay `",
        "  --e1-replay-overlay ~/hl_mem_docs/evaluations/v030/manifests/e1_replay_overlay_v2.json",
        "```",
        "",
        "只有用户自己的 replay 达到预注册门禁且逐案报告可审计后，才由用户自行把 L2 调度切到 enforce；配置本段本身",
        '不会启用 L1 或 L2。L1 保持禁用，紧急回滚直接恢复 `conflict.auto_mode = "l0_only"`。',
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
        (item for item in fields(Settings) if "toml" in item.metadata),
        key=lambda item: str(item.metadata["toml"]),
    )
    secret_fields = sorted(
        (item for item in fields(Settings) if "secret_env" in item.metadata),
        key=lambda item: str(item.metadata["secret_env"]),
    )

    lines = [
        "# HL-Mem 配置参考",
        "",
        f"HL-Mem {__version__} 使用单个 TOML 文件保存非敏感配置，并用 `.env` 或同名进程环境变量保存四个密钥。",
        "`Settings` 是唯一 schema；下表由 `Settings` 字段 metadata 自动生成。未写入 TOML 的字段使用代码默认值。",
        "模型型号不在活文档中固化：LLM、Embedding、Reranker 和图片描述器的 API 密钥通过 `.env` 配置，provider/model 等非敏感选项通过 TOML 配置。",
        "",
        f"v{__version__} 的受限 assertion 门控没有配置键；存量 `unknown` 只可观测，不改变 supersede、召回或注入行为。",
        "",
        "## 合并版发版决议",
        "",
        '- `conflict.auto_mode="l0_only"`：E1 两轮均封存；生产仅执行确定性 L0，L1 禁用，L2 只保留为用户配置',
        "  `[maintenance_judge]` 后自行回放验证的可选能力。紧急停用可设为 `off`，只观察可设为 `observe`。",
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
        '- `recall.entity_constraint_mode="observe"`：E4 行为门禁通过，但冻结查询 100% 为 snapshot-derived synthetic，证据',
        "  等级不足；取得满足预注册真实查询占比的语料前不启用 hard filter。",
        "- `hermes.manual_conflict_notice=true`：只提示 residual `manual_required_count`，同一会话首次或计数变化时显示；",
        "  可设为 `false` 立即关闭。",
        "",
        "## 加载规则",
        "",
        "- 通用 CLI/server 默认读取当前工作目录的 `hl_mem.toml`；Hermes 插件固定读取 `<HERMES_HOME>/hl_mem.toml`，不依赖宿主进程 CWD。文件缺失、TOML 语法错误、未知表、未知键或类型错误都会阻止启动。",
        "- 通用 CLI/server 的 `.env` 默认相对当前工作目录读取；Hermes 插件固定读取 `<HERMES_HOME>/.env`。`.env` 可以缺失，进程环境中的同名密钥覆盖它。",
        "- 相对 `database.path` 以配置文件 symlink 的真实目标目录为基准；异平台绝对路径会阻止启动，不会被当成相对路径创建影子数据库。",
        "- 除四个密钥外，环境变量不参与配置；所有 `HL_MEM_*` 变量均被忽略。",
        "- TOML 使用原生类型；仅允许数组转换为 tuple、字符串转换为枚举。密钥不得写入 TOML。",
        "- 可从 [`config.example.toml`](../config.example.toml) 复制常用配置；该示例显式启用真实能力，推荐值不等于代码默认值。",
        "",
        "```bash",
        "cp config.example.toml hl_mem.toml",
        "cp .env.example .env",
        "uv run hl-mem doctor",
        "uv run python start_server.py",
        "```",
        "",
        "## 启动入口",
        "",
        "- Windows 使用 `start_production.bat`，Git Bash/POSIX 使用 `./start_hl_mem.sh`。两个脚本都从脚本自身位置定位仓库根目录，并调用 `start_server.py`，因此可以从任意当前目录启动。",
        "- 脚本使用仓库内的虚拟环境；Shell 入口兼容 `.venv/bin/python` 和 `.venv/Scripts/python.exe`。",
        "- 启动脚本不保存第二份运行配置，也不选择 provider/model，且不再设置旧版 `HL_MEM_*` 覆盖。除四个密钥外，所有有效配置都只来自仓库根目录的 `hl_mem.toml`；loader 会忽略继承到进程中的 `HL_MEM_*`。",
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
        "## 升级到 v0.28.9",
        "",
        "先停止 API、Worker 和全部写入者，并保留主库的离线副本。v0.28.9 首次打开主库时会继续执行 migration",
        "045/046：前者建立单案多候选、generation/revision、dirty queue 与版本化裁决约束；后者增加运维清理索引，并将",
        "历史上低于摄入 floor 的 pending dedup pair 一次性标记为已审查。migration 不执行存量冲突裁决。",
        "",
        "迁移成功后立即运行一次 `hlmem backup`。若该库此前没有 tombstone sidecar，backup 会创建并绑定",
        "`<database>.tombstones.db`，再生成 manifest v2；从此应把主库 backup、manifest 和 ledger 作为同一恢复集合保存。",
        "旧 manifest v1 可留作升级前取证副本，但 v0.28.9 restore 会拒绝它，因为它无法证明删除历史。确认新的 v2",
        "backup 可校验后，再恢复 API/Worker 服务。",
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
        "LLM_API_KEY": "extraction 非 fake、query expansion 非 off 或 relation discovery 非 off",
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
    (ROOT / "docs" / "configuration.md").write_text(generate(), encoding="utf-8")
