# HL-Mem 项目交接状态

> 最后更新：2026-08-29

## 当前状态

- **分支**：`eval/glm-effort-low-20case`
- **版本**：v0.33.0
- **阶段**：v0.33.0 发版准备；等待用户验收后 push/tag
- **发布状态**：本地 commit，未 push、未打 tag、未部署
- **服务**：FastAPI 默认监听 8200；非敏感配置只从工作目录 `hl_mem.toml` 读取
- **存储**：SQLite WAL + FTS5 + 向量 BLOB；默认 `sqlite_scan`，可选 `sqlite_vec`
- **Schema**：56 migrations（SQL 001–056），只允许向前迁移
- **密钥**：`LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`

## v0.33.0 发版默认

| 能力 | 默认 | 决议依据 |
|---|---|---|
| 冲突自动化 | `conflict.auto_mode="l0_only"` | E1 两轮 SEALED；只保留 sealed 集 37/37、危险反向 0 的 L0 |
| Plan fulfillment | `plan.fulfillment_mode="enforce"` | E5 A 臂 143 场景满分，错误关闭 0 |
| 价格 target | `price.target_mode="enforce"` | E6 B 臂 precision/series accuracy 1.0，coverage 0.90，跨 target supersede 0 |
| 版本状态关链 | `state.latest_wins_mode="observe"` | ADR-0004 两份独立冻结集：800/800 exact、危险误关链 0；显式切 enforce 前保持只观察 |
| 提取触顶软拆分 | `extraction.soft_split_enabled=false` | 实验装备已随版提供，轻量模型代际终验未触发；保留懒触发能力但不启用 |
| 提取残余修复 | `extraction.delta_repair_enabled=false` | 仅 soft split 启用且首次二分后的子块仍触顶时懒触发；密度/gold coverage 扩展门未全过 |
| LLM 输出上限 | `llm.max_tokens` 未设置 | 保险丝仅供部署显式选择；默认保持 provider 既有上限 |
| Thinking 方言 | `llm.thinking_control="auto"` | 仅 llama.cpp 等端点需要显式切 `chat_template_kwargs` |
| Zhipu 推理强度 | `llm.reasoning_effort` 未设置 | 仅显式配置时透传；20 案 `low` 终验 P50 21.7 秒 |
| Dedup apply | `dedup.audit_only=true` | E2 SEALED_v2，不批量应用历史 equivalent |
| Lesson signal | `extraction.lesson_signal_mode="observe"` | E3 SEALED_v2，旧 notability prompt 保持 |
| 查询实体约束 | `recall.entity_constraint_mode="observe"` | E4 行为过但证据全为 synthetic，production-shaped coverage 不足 |
| Hermes 人工冲突提醒 | `hermes.manual_conflict_notice=true` | 只读 health；同 session 首次或计数变化提示一次 |

生产没有常驻 LLM 判官依赖。`[maintenance_judge]` 为纯可选配置；默认 `l0_only` 不调用它。若用户要启用
L2，须先用随包 E1 回放装备在自己的冻结语料上自验并显式改配置。

## 已交付能力

- migration 055–056 为每次 Claim UPDATE/DELETE 增加数据库边界审计，记录 changed fields 与调用来源；空库按
  migration → 审计上下文 trigger 的顺序初始化，避免首次启动找不到 `claims` 表。
- reclassify 跳过证据完备的 `report-version` 确定性探针；legacy slot backfill 注册同一组 SQLite 函数。
- compact 提取改为 coverage-first 的 12–30 条高密度协议，schema `maxItems=30`；Zhipu 可显式透传
  `reasoning_effort`，通用 provider 支持可选 `max_tokens` 保险丝与 llama.cpp thinking 方言。
- 软拆分和 delta repair 仅作为默认关闭的 opt-in 能力随版提供；A/B runner 可用 `--respect-llm-config` 忠实复用
  TOML provider/model/base URL。
- migration 050–054 增加治理动作账本、conflict policy version、typed canonical entity/alias/relation/Claim links、
  plan outcomes 与 slot-aware cross-subject dedup metadata；001–049 未改。
- typed entity 保持 person/agent/device/environment/instrument/project/topic 类型隔离；无 proof、跨类型同名和多 active
  alias 均 fail-closed，Claim 继续兼容 legacy subject/entity JSON。
- plan fulfillment 以严格坐标匹配 complete/cancel/replace/partial，只关闭 valid time；数量用 Decimal 守恒，
  结果/关系/governance action 同事务写入。
- 价格序列以 `(axis, canonical_target_entity_id, snapshot_date)` 定位；qualified code 与唯一 typed alias 可 enforce，
  target/date/币种/单位不完整继续 `uncertain`。
- `config.version` latest-wins 只接受可信 `status_report_v1` currentness proof；默认 observe，灰区并存且不建人工队列。
  `hl-mem report-version` 从包版本构造确定性事件与 Claim，不调用 LLM；`state.latest_wins_mode="off"` 可停止新建议和动作。
- conflict、dedup、plan 共享输入 fingerprint、短事务 CAS、governance ledger 和有条件 rollback，但不共享领域决策枚举。
- `l0_only` 运行时只调用 L0；L1 不进入维护路径，未命中 L0 的案稳定转 `manual_required` 且不建 L2 job。即使
  jobs 表残留旧 L2 job，handler 也会在构造 judge 前返回 skipped。
- query entity filter 只运行 observe shadow，不增加通道、boost 或 weight；lesson signal 只记录 qualifier/audit，不改变
  importance/scope。
- `/healthz` 提供 residual `manual_required` 计数与年龄，Hermes plugin 2.1.0 提供 session 级 no-spam 提示；
  daemon contract 1、plugin contract major 2、Context Packet 1.1 均未改变。

## 发版证据

- v0.33.0 Zhipu coding `reasoning_effort=low` 20 案终验：P50 21.7 秒、P95 42.0 秒、15/20 案至少 12 条、
  抽审虚构 Claim 0；密度与 gold coverage 扩展门未全过，因此软拆分/delta repair 保持默认关闭。聚合证据位于
  `var/eval/prompt_density_ab_20260829/`。
- 总决议：`C:/Users/Administrator/hl_mem_docs/evaluations/v030/release-decision.json`，SHA256
  `2ab4a42fa98293a7bd80cbe171383d45cc0ae72c5a25001af80a651a4526cd97`。
- Cross-feature replay：typed alias → price target → plan closure → dedup audit → recall observe → Hermes notice；
  最终报告位于 `.../batch5/cross_feature/report.json`，SHA256
  `028d0fcf7377d4840278fc293173dd955ede0a4ef6b5cd186e311bc6048307a5`。
- Replay 在 migration 054 克隆库上通过：plan applied 1、dedup applied 0、rolled_back delta 0、跨领域 rollback 0、
  foreign-key error 0；源 snapshot hash 前后不变。
- 首次无效 fixture 使用 topic-only slot，typed resolver 正确拒绝其作为 subject；诊断保存在外部
  `cross_feature/diagnostics/attempt-1-invalid-topic-slot/`，未删除也未进入 Git。

实验语料、逐案报告、生产形状快照与 replay 数据都留在 `~/hl_mem_docs/evaluations/v030/`，不得加入 Git。

## 回滚边界

- 设置 `conflict.auto_mode="observe"|"off"`、`plan.fulfillment_mode="observe"|"off"`、
  `price.target_mode="observe"|"off"` 可停止新 mutation；已有动作必须由 governance action 在 after fingerprint
  仍匹配时回滚，不能直接清空新列或覆盖后续 outcome。
- `dedup.audit_only=true`、`extraction.lesson_signal_mode="observe"`、
  `recall.entity_constraint_mode="observe"` 已是发布保守值；Hermes 提示可单独设
  `hermes.manual_conflict_notice=false`。
- 新增提取实验回滚到保守值时设置 `extraction.soft_split_enabled=false`、
  `extraction.delta_repair_enabled=false`、`llm.thinking_control="auto"`；删除 `llm.max_tokens` 与
  `llm.reasoning_effort` 可恢复 provider 自身默认。
- schema 为 additive forward-only。旧二进制不了解新治理语义，升级后不得恢复旧二进制写库；恢复应使用升级前
  主库 + tombstone sidecar 的一致备份。

## 下一步

Windows 发版预检已通过：Black 检查 584 个仓库文件、isort 检查 593 个 tracked Python 路径、Ruff、mypy
（233 个 source files）、docs consistency、OpenAPI 与 MCP snapshot 均为绿。全量 unit suite 使用移除
`PYTHONPATH` 的 `.venv/Scripts/python.exe -m pytest tests/unit/ -q --tb=short`，结果为
**2369 passed、1 skipped、110 subtests passed**，无 deselect 或业务测试豁免。

本机现有未跟踪 `Temp/`、被忽略的 `.worktrees/` 与 ignored/untracked `var/` probe 脚本会被全目录格式工具看见；
发版门禁按 Git tracked scope 复验，未修改、删除或纳入这些本机文件。isort 仅机械修正 3 个 tracked 文件的
import/空行，其中包含两份随本批提交的 `var/eval/prompt_density_ab_20260829/` 终验脚本。

下一步由用户验收本分支与发版证据；验收后手动 push/tag，再进入正常三机部署。本 worktree 不 push、不打 tag、
不修改部署机 `.env`/`hl_mem.toml`。

## 当前规范

- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [REST API](api.md)
- [Capability matrix](capability-matrix.md)
- [Compatibility policy](compatibility.md)
- [Changelog](CHANGELOG.md)
- [Historical archive](archive/)
