# 本地提交推送就绪度评估

评估日期：2026-07-24
比较基线：本地 `origin/main`（`4a111de`）到 `HEAD`（`e5b7d05`）
评估性质：只分析，不执行 fetch、push、rebase、commit 或其他 Git 历史操作

## 结论

**需要修改后推送。**

整体方向是正向的：配置与 LLM 接入收敛、领域逻辑下沉、repository 拆分、重复实现删除、事务所有权统一，
都明显改善了架构边界；slot/tags、跨 subject 去重和 importance 联动 TTL 也符合 HL-Mem 的长期设计。
但当前 32 个提交同时跨越多个版本、三次 schema migration、核心召回排序和后台数据治理，不能按普通小改动处理。

当前不建议直接 push 到 `main`。完成下述阻断项后，应推送到独立分支并通过 PR 审查与 CI 合入。

## 基线与规模

`git log origin/main..HEAD --oneline` 显示本地领先 32 个提交。`git diff --stat origin/main..HEAD` 的累计结果为：

- 123 个文件发生变化；
- +9,767 / -2,682，净增 7,085 行；
- 源码 +5,295 / -2,422，净增 2,873 行；
- 测试 +757 / -246，净增 511 行；
- 文档与仓库说明 +3,713 / -12，净增约 3,701 行；
- 测试函数由约 192 个增加到约 212 个，净增约 20 个。

净增代码中超过一半是评审/重构文档。源码的净增主要来自拆分后的稳定模块、slot registry、迁移和 worker，
并非纯粹的业务膨胀；但新增功能与数据迁移的体量仍显著高于新增测试体量。

注意：本次严格按用户指定的本地 `origin/main` 引用比较，没有执行 `git fetch`。因此结论只对当前本地记录的远程
基线成立，不能证明 GitHub 上的 `main` 在评估时仍停留于 `4a111de`。

## 验证证据

- `tests/unit/`：259 passed；
- `tests/`：284 passed，1 skipped；
- import 检查成功，运行时版本为 `0.10.0`；
- 单元测试覆盖率：78%；
- `git diff --check origin/main..HEAD` 未通过：历史评审文档中存在多处 trailing whitespace；
- 覆盖薄弱的高风险新增模块：
  - `workers/deduplicate.py`：28%；
  - `storage/migrations/backfill_claim_slots_v1.py`：46%；
  - `workers/backfill_expires_at.py`：41%；
  - `workers/worker.py`：57%；
- 覆盖率运行暴露出大量未关闭 SQLite connection 的 `ResourceWarning`，普通测试运行未将 warning 视为失败。

现有测试能够支撑“回归基本稳定”，但尚不足以支撑“数据迁移和自动治理路径已达到线上安全标准”。

## 逐提交评估

| 提交 | 质量与价值 | 主要风险 | 评级 |
|---|---|---|---|
| `89a4205` | 增加 v0.4.3 架构评审和实施依据，追溯性好 | +1,660 行纯文档，部分 trailing whitespace；对运行时无风险 | 低 |
| `dbe4e30` | 收敛 Settings 与 LLM provider，减少配置双轨，架构收益高 | 11 文件、接口面广；测试在后一提交补齐，提交自身不完全自证 | 中 |
| `be2f1a2` | 适配 5 组配置/LLM 测试，补足前一重构 | 多为测试改写而非新增行为覆盖；与前一提交强耦合 | 低 |
| `b92867b` | production 禁止 fake extractor，修复 reranker 测试环境，属于必要安全护栏 | 变更小且有测试，风险可控 | 低 |
| `17deba4` | 写入逻辑下沉 `domain/claims`，建立迁移快照，分层方向正确 | 27 文件、净增 434 行；保留兼容层并移动公共入口，导入与行为回归面大 | 高 |
| `c3c0d3a` | 删除过时 monkeypatch 测试，清理合理 | 仅测试清理，无实质线上风险 | 低 |
| `49f4b07` | 拆分 ingest/recall/worker 三个 god function，可读性和可测性改善 | 净增 325 行，拆分同时扩大内部状态传递；该提交未新增专门测试 | 中高 |
| `2f23f41` | repository 按职责拆分，稳定内部类型与协议，显著改善存储边界 | 19 文件、+1,328/-1,071；大规模机械迁移可能遗漏事务/解码语义 | 高 |
| `f684d08` | 修正 JSON 自动解码后的测试契约 | 只调整 3 处断言，风险低 | 低 |
| `334e826` | Hermes provider 拆分、schema 默认值修正、常量集中，方向良好 | 同时包含多类变更且“prep multi-hop”增加前置复杂度；提交职责偏宽 | 中高 |
| `34d11d2` | 记录 dead code/bloat 审查和 Phase 15 任务，便于追溯 | +1,289 行文档，不影响运行时，但增加仓库噪声 | 低 |
| `7b66ab6` | 增加架构质量审查，提供后续收敛依据 | 纯文档，风险低 | 低 |
| `7576915` | 汇总第三轮质量/Hermes 反馈 | 纯文档，风险低 | 低 |
| `41f99c2` | 删除 vector/RRF/retry/stage 重复实现，净减 81 行，偿还技术债明显 | 新增 542 行 staged pipeline，核心召回路径迁移，排序回归风险高 | 高 |
| `20bbcdf` | 适配 staged pipeline API 的测试 | 主要是兼容性调整，新增边界覆盖有限 | 低 |
| `ee82ccc` | 统一事务所有权、删除 monkeypatch 与 JSON 双轨，净减 113 行，收益明确 | 事务边界属于数据一致性关键路径；提交自身未带专项并发/回滚测试 | 高 |
| `f1ee3ea` | 修正上一提交的 transaction ownership 调用点和值字段访问 | 出现紧随重构的修复说明前一提交并非独立稳定；最终状态有测试支撑 | 中 |
| `5bf5a04` | 移除 `_queue_event` 后的测试适配 | 变更很小，风险低 | 低 |
| `d80bb14` | 收敛 Hermes 契约、修复 circuit breaker、删除 7 个过期兼容模块，净减 139 行 | 28 文件且改变同步/异步契约；调用方兼容风险较高 | 高 |
| `6fada35` | 广泛适配 provider 同步 hook 契约，覆盖 22 个测试文件 | 主要是测试迁移；没有真实 Hermes 网络集成验证 | 中 |
| `8a14361` | 修正 `__version__` 为 0.7.0 | 必要但显示此前版本管理曾漂移 | 低 |
| `1248073` | 引入 operational slots、migration 016 和回填工具，符合 slot+tags 架构 | 数据分类、冲突键和迁移同时变化；当时缺少对应专项测试 | 高 |
| `072c987` | 将 claim 读取链路切换到 canonical slots，完成行为迁移 | 影响 ingest、recall、worker、TTL、冲突和 dedup，回归面很大 | 高 |
| `05c8b8c` | 适配 canonical slot 的冲突/去重/召回/TTL 测试 | 以既有测试改写为主，缺少回填和真实旧库升级验证 | 中 |
| `a98eb72` | 增加跨 subject 语义去重、审计表和 worker，默认 audit-only 是正确护栏 | 自动合并属于不可逆语义风险；当前 worker 覆盖率仅 28%，候选成本和并发安全需重点验证 | 高 |
| `a81b9e7` | importance 联动 TTL、纯函数策略和回填 worker，长期方向正确 | 会改变数据生命周期；回填 worker 覆盖率 41%，旧数据批量过期风险高 | 高 |
| `cff72bb` | 适配 TTL matrix 测试 | 仅 1 个测试文件的小调整，无法覆盖新增回填/并发路径 | 中 |
| `7b8ab6c` | 更新 v0.9.0 README、CHANGELOG、版本元数据 | 随后又升到 0.9.1/0.10.0，当前文档再次漂移 | 低 |
| `a6fcd96` | 针对 v0.9.0 审查集中修复 confidence/CAS/UTC/qualifier/API 等问题，并新增 243 行专项测试 | 26 文件的大型“修复包”；说明此前多个 P0/P1，仍需迁移级验证 | 高但必要 |
| `c4e0966` | 适配 conflict_key v3、slot 校验、UTC retention 测试 | 仍以测试适配为主，未覆盖全部高风险 worker | 低 |
| `f02e30a` | topic tag soft boost + 独立 FTS channel、trace 和 188 行新测试，设计与可观测性较完整 | 默认开启 soft boost，未经离线检索评测就改变线上排序；新增 migration 018；版本升至 0.10.0 但文档未同步 | 高 |
| `e5b7d05` | 修正 FTS trigger 导致 `total_changes` 断言变化 | 只修测试期望；也表明 migration 018 会改变连接级变更计数语义 | 低 |

## 按评估维度汇总

### 代码量与复杂度

整体净增 7,085 行看似很大，但约 3,701 行是文档，源码净增 2,873 行。repository、Hermes provider 和旧兼容层的
拆分/删除降低了局部复杂度；另一方面，slot registry、三个迁移、两个回填工具、dedup worker 和第三召回通道增加了
系统状态空间。结论是：**结构复杂度下降，业务与运维复杂度上升**。这是合理演进，但需要更强的迁移和运行验证。

### 测试覆盖

测试全绿且新增约 20 个测试函数，`test_v091_fixes.py`、`test_tag_boost.py`、`test_query_tags.py` 对后期修复有实质价值。
不足之处是大量提交以“先实现、后 adapt tests”的形式出现，且最危险的数据治理 worker 覆盖最低。测试增量与新增功能
并不对称，当前 78% 总覆盖率掩盖了 dedup/backfill 的局部缺口。

### 技术债

已偿还的债包括：配置双轨、LLM provider 双轨、重复 RRF/vector/retry、repository 巨型文件、Hermes 双契约、
事务所有权含混、JSON 双轨和过期兼容层。

新增或尚未还清的债包括：

- worker/迁移专项覆盖不足；
- SQLite connection `ResourceWarning`；
- `0.10.0` 代码版本与 `v0.9.1` 项目说明、CHANGELOG 状态漂移；
- 16 个未跟踪的 Phase 16–18 设计/评审文档未进入提交序列；
- 历史文档 trailing whitespace；
- 缺少 topic tag 对 Recall@K/MRR/NDCG 的离线验收证据。

### 架构一致性

大部分改动与项目哲学一致：领域纯函数下沉、应用层持有事务、存储按职责拆分、迁移不可变、功能开关和 audit-only
默认值都体现了低侵入和证据链思路。最明显的不一致是 `tag_boost_enabled=True`：设计材料要求先 D、再评测、再 B，
而当前默认已经改变召回排序，却没有看到离线评测产物。独立 tag channel 默认关闭是正确的。

### 线上风险

最高风险集中于：

1. migration 016–018 和两类 Python 回填对既有数据库的升级、幂等、回滚和并发行为；
2. cross-subject dedup 在未来关闭 audit-only 后可能错误 supersede 长期记忆；
3. TTL 回填和 worker 可能批量改变 claim 生命周期；
4. staged pipeline 和默认 tag boost 改变召回排序；
5. Hermes 同步契约和 circuit breaker 改动缺少真实集成环境验证。

默认 audit-only、tag channel 默认关闭和全套测试通过降低了立即故障概率，但不能消除数据质量与排序回归风险。

## 推送前必须修改

1. 将 `HL_MEM_TAG_BOOST_ENABLED` 默认值改为关闭，或补充可复现的离线评测报告，证明开启后 Recall@K/MRR/NDCG
   不退化；在有证据前不应默认改变线上排序。
2. 为以下路径补充直接测试：dedup worker 的 scan/judge/CAS/apply，slot 回填的 dry-run/apply/重复运行/并发 CAS，
   expires_at 回填的 scope/recorded_from CAS、过期边界与恢复。
3. 使用生产副本或合成旧库执行 015→018 升级演练，记录 migration 行数、FTS 一致性、回填 dry-run、耗时和回滚方案。
4. 处理测试暴露的 SQLite connection `ResourceWarning`，至少定位并证明不会在 worker/服务常驻进程中累积。
5. 统一版本与文档：`pyproject.toml`/`__version__` 已是 0.10.0，但 AGENTS/README/CHANGELOG 仍以 v0.9.1 或
   v0.9.0 为主；增加明确的 v0.10.0 变更记录。
6. 决定 16 个未跟踪设计/评审文档哪些属于本次发布证据并纳入后续提交，哪些应明确排除；当前不能让实现与关键审查
   依据只存在于本地工作区。
7. 清理 `git diff --check` 报告的 trailing whitespace，使基础 Git 质量检查通过。
8. 推送前执行一次 `git fetch` 后重新运行本报告的 log/diff/test 检查，确认远程基线没有变化。

## 建议的推送策略

修复完成后采用 **独立分支 + PR**，不直接 push `main`。

建议把评审重点分成四组：架构收敛、slot/迁移、dedup/TTL 数据治理、topic-tags recall。32 个现有提交不必为了美观
强行重写历史，但 PR 描述应按这四组解释行为变化、迁移顺序、feature flag 默认值和回滚方式。合入前至少要求：

- Windows/Python 3.11 的全套测试与 coverage；
- 015→018 旧库升级演练；
- dedup 保持 audit-only；
- tag channel 与 tag boost 在无离线证据时均默认关闭；
- 一名熟悉存储迁移/事务的 reviewer 和一名熟悉 recall 排序的 reviewer 分别批准。

最终判断：**这些改动总体让仓库变得更好，但当前提交集尚未达到可直接推送主分支的风险水平。**
