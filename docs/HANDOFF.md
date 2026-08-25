# HL-Mem 项目交接状态

> 最后更新：2026-08-25

## 当前状态

- **分支**：`batch5-v0310-release`
- **版本**：v0.31.1
- **阶段**：发版层 5.1–5.3 已完成；等待 Hermes 终验
- **发布状态**：无 push、无 tag、无 PyPI 发布；v0.30.0 撤回轮保持未发布记录
- **服务**：FastAPI 默认监听 8200；非敏感配置只从工作目录 `hl_mem.toml` 读取
- **存储**：SQLite WAL + FTS5 + 向量 BLOB；默认 `sqlite_scan`，可选 `sqlite_vec`
- **Schema**：54 migrations（SQL 001–054），只允许向前迁移
- **密钥**：`LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`

## v0.31.1 发版默认

| 能力 | 默认 | 决议依据 |
|---|---|---|
| 冲突自动化 | `conflict.auto_mode="l0_only"` | E1 两轮 SEALED；只保留 sealed 集 37/37、危险反向 0 的 L0 |
| Plan fulfillment | `plan.fulfillment_mode="enforce"` | E5 A 臂 143 场景满分，错误关闭 0 |
| 价格 target | `price.target_mode="enforce"` | E6 B 臂 precision/series accuracy 1.0，coverage 0.90，跨 target supersede 0 |
| Dedup apply | `dedup.audit_only=true` | E2 SEALED_v2，不批量应用历史 equivalent |
| Lesson signal | `extraction.lesson_signal_mode="observe"` | E3 SEALED_v2，旧 notability prompt 保持 |
| 查询实体约束 | `recall.entity_constraint_mode="observe"` | E4 行为过但证据全为 synthetic，production-shaped coverage 不足 |
| Hermes 人工冲突提醒 | `hermes.manual_conflict_notice=true` | 只读 health；同 session 首次或计数变化提示一次 |

生产没有常驻 LLM 判官依赖。`[maintenance_judge]` 为纯可选配置；默认 `l0_only` 不调用它。若用户要启用
L2，须先用随包 E1 回放装备在自己的冻结语料上自验并显式改配置。

## 已交付能力

- migration 050–054 增加治理动作账本、conflict policy version、typed canonical entity/alias/relation/Claim links、
  plan outcomes 与 slot-aware cross-subject dedup metadata；001–049 未改。
- typed entity 保持 person/agent/device/environment/instrument/project/topic 类型隔离；无 proof、跨类型同名和多 active
  alias 均 fail-closed，Claim 继续兼容 legacy subject/entity JSON。
- plan fulfillment 以严格坐标匹配 complete/cancel/replace/partial，只关闭 valid time；数量用 Decimal 守恒，
  结果/关系/governance action 同事务写入。
- 价格序列以 `(axis, canonical_target_entity_id, snapshot_date)` 定位；qualified code 与唯一 typed alias 可 enforce，
  target/date/币种/单位不完整继续 `uncertain`。
- conflict、dedup、plan 共享输入 fingerprint、短事务 CAS、governance ledger 和有条件 rollback，但不共享领域决策枚举。
- `l0_only` 运行时只调用 L0；L1 不进入维护路径，未命中 L0 的案稳定转 `manual_required` 且不建 L2 job。即使
  jobs 表残留旧 L2 job，handler 也会在构造 judge 前返回 skipped。
- query entity filter 只运行 observe shadow，不增加通道、boost 或 weight；lesson signal 只记录 qualifier/audit，不改变
  importance/scope。
- `/healthz` 提供 residual `manual_required` 计数与年龄，Hermes plugin 2.1.0 提供 session 级 no-spam 提示；
  daemon contract 1、plugin contract major 2、Context Packet 1.1 均未改变。

## 发版证据

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
- schema 为 additive forward-only。旧二进制不了解新治理语义，升级后不得恢复旧二进制写库；恢复应使用升级前
  主库 + tombstone sidecar 的一致备份。

## 下一步

Windows 全套预检已通过：Black、isort、Ruff、mypy、import boundary、complexity ratchet、quality smoke、docs、
OpenAPI 和 MCP snapshot 均为绿。全量 pytest 原始运行在修复非伪影回归后只剩 4 个 worktree `.venv` 伪影；
精确排除这 4 个 node id 后结果为 **2206 passed、1 skipped、4 deselected、110 subtests passed**。

任务书预告 3 个 runtime/launcher 伪影；本机另安装了 Git Bash，因此同一缺失 worktree `.venv` 根因的 Git Bash
launcher 分支也被执行，实际为 4 个。四项均明确指向缺失的 `.venv/pyvenv.cfg` 或
`.venv/Scripts/python.exe`，没有业务测试被豁免。

下一步仅由 Hermes 审阅本分支与外部发版证据；验收后再统一 push/tag。本 worktree 不 push、不打 tag、不部署。

## 当前规范

- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [REST API](api.md)
- [Capability matrix](capability-matrix.md)
- [Compatibility policy](compatibility.md)
- [Changelog](CHANGELOG.md)
- [Historical archive](archive/)
