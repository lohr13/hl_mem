# HL-Mem v0.19 Integration and Polish Proposal

> 状态：Proposed；目标版本：v0.19；日期：2026-07-31
>
> 源码基线：v0.18.0（`pyproject.toml:3`）；数据库基线：既有 migration `001`–`034` 不可变
>
> 核心原则：评测先行；Canonical Claim 与 Search/Display Text 两层分离；所有必做集成项在 v0.19 一次完成，但按依赖顺序实施。

## 1. Executive Summary

v0.19 不缩小范围。它仍要完成搜索投影、发布门禁、Context Packet、Hermes adapter、feedback receipt、备份恢复、JSONL 导入、幂等写入、跨平台哈希、启动脚本和 namespace 正确性收口。变化只在执行顺序：先把评测尺子校准并冻结，再实现候选方案；只有 A/B 证明 `answerable` 胜出，才迁移生产默认；Context Packet 与 feedback 在生产检索语义稳定后接入；最后补齐运维和 namespace。

当前问题全景如下：

1. 评测中的 `recall_at_5` 实际是“top-5 至少命中一条”的 Hit@5；baseline 仍未初始化，现有 gate 只检查不退化，不能证明 `answerable` 胜出。
2. Claim 的事实数据与搜索/展示文本边界不清。FTS 和 dense 已基于 `index_text`，但 reranker、REST 输出及部分嵌套 Claim 文本仍直接消费原始 `value`。
3. `answerable` 已有雏形，但只做 `subject + label + str(value)`，未稳定纳入 qualifier；现有 A/B 又只比较 dense cosine/rank，不能支撑发布。
4. Recall 尚无冻结的 Agent 消费协议。Hermes 预取过早压平成字符串，exposure 在预算裁剪前创建，缓存还可能复用旧 receipt。
5. backup/restore、JSONL import、显式保存幂等、CRLF hash、启动脚本和 namespace 仍有明确的正确性或可恢复性缺口。

### 1.1 两层架构

v0.19 采用两层，而不是三层：

| 层 | 权威职责 | v0.19 约束 |
|---|---|---|
| Canonical Claim | `subject + predicate/canonical_slot + value + qualifiers`，以及 fact hash、冲突、双时间、状态、证据、scope、importance/TTL 等领域与生命周期约束 | 新写入继续使用当前已工作的原子 Claim：`value` 是字符串。除审定的 2 条定点修复外，不迁移、不批量改写 `value_json` |
| Search/Display Text | 从 Canonical Claim 确定性生成 `index_text`，供 FTS、dense embedding、reranker、REST/MCP/Hermes 的 Claim `text` 统一消费 | 投影不参与 fact hash、冲突键或双时间计算；投影变化不得制造新 Claim 或改变证据链 |

Canonical Claim 与投影分离的目的，是不再拿原始 JSON 存储表示直接做 embedding、reranker 或展示；它不意味着 v0.19 要解决任意 JSON 类型系统。

### 1.2 三条 Release Rule

> **Release Rule 1 — `answerable` 不预设胜出。** 它是待验证候选，不是既定结论。设计、实现和报告均不得把“完成实现”等同于“可以切默认”。

> **Release Rule 2 — gate 不通过就保留 `legacy` 默认。** 失败报告照常归档，Phase 3/4 仍继续完成；只是生产 `index.text_mode` 不切换，且不执行全库 `answerable` backfill。

> **Release Rule 3 — 四类记忆、多视图、Mental Model、Policy 必须通过各自独立 RFC 和量化证据重新进入路线图。** 现有代码骨架或概念吸引力都不构成 v0.19 实施授权。

### 1.3 阶段顺序与工作量

| 阶段 | 依赖 | 交付结果 | 直接工作量 |
|---|---|---|---:|
| Phase 0: Evaluation Foundation | 无 | 正确指标、冻结查询集/快照/baseline、版本化 gate | 2–3 人日 |
| Phase 1: Projection Unification | Phase 0 契约 | 改善 `answerable`、统一 `index_text` 消费、保持 `legacy` 默认 | 8–10 人日 |
| Phase 2: Migration & Gate | Phase 1 完成 | legacy vs answerable A/B、发布决定；胜出时停机迁移与切默认 | 3–4 人日 |
| Phase 3: Context Packet & Feedback | Phase 2 已锁定生产默认 | 简化 packet、Hermes adapter、每次注入的新 receipt、显式反馈闭环 | 8–10 人日 |
| Phase 4: Operations & Hardening | Phase 3 契约稳定 | backup/restore、import、幂等、CRLF、启动、namespace 收口 | 5–7 人日 |

总工作量采用两个可审计场景，而不是把相互重叠的阶段上下限机械相加：

| 场景 | 五阶段直接工作 | 跨阶段集成、真实 provider A/B、停机演练与发布观察 | 总计 |
|---|---:|---:|---:|
| 乐观 | 2 + 8 + 3 + 8 + 5 = 26 | 4 | 30 人日 |
| 保守 | 3 + 10 + 4 + 10 + 7 = 34 | 3；各阶段上限已包含较多联调/文档，避免重复计数 | 37 人日 |

因此 v0.19 的承诺总工作量为 **30–37 人日，37 人日是硬上限**。若实际证据要求突破上限，必须重新审查计划，不能把额外工作静默塞入“集成预留”。真实 embedding/reranker 调用费用和等待时间单独记录，不折算开发人日。

Phase 2 有两个合法出口：

- Phase 1 的同期 legacy control 先通过相对 v0.18.0 baseline 的兼容/非退化 gate；随后 `answerable` 通过胜出 gate：停写、备份、完成两条定点修复、全量 backfill、切默认、校验。
- Phase 1 的同期 legacy control 通过兼容 gate，但 `answerable` 未通过胜出 gate：同样先停写、备份并完成两条定点修复，随后保留 `legacy` 默认并归档差异报告；Phase 3 在稳定的 legacy 投影上继续。

若同期 legacy control 本身未通过兼容 gate，说明 Phase 1 的统一消费面造成了不可接受退化；Phase 2 必须返回修复，Phase 3 不得开始。因此 Phase 3 依赖的是“核心检索兼容 gate 已通过，且生产投影选择已经关闭”，而不是预设 `answerable` 必须获胜。

---

## 2. Phase 0: Evaluation Foundation（2–3 人日）

### 2.1 修正 Hit@5 / Recall@5 指标语义

#### 问题描述

仓库至少有三处把“是否命中任一相关项”命名为 Recall@5。单 gold case 时二者数值相同，多 gold case 时会高估覆盖率，并直接污染 baseline 与发布 gate。`recall_at_1` 也存在同类命名问题；两个 runner 对 `low_confidence` 是否等于 no-answer 的定义也不一致。

#### 源码证据

- `tests/eval/eval_runner.py:104-122`：第 119 行用 `bool(relevant ∩ top5)` 计算 `recall_at_5`，实际是 Hit@5；第 118 行的 `recall_at_1` 实际是 Hit@1。
- `tests/eval/metrics.py:85-120`：`recall_at_5` 同样只判断是否至少命中一条。
- `evaluation/run_recall_regression.py:21-29,45-50`：第三处重复了相同误名。
- `src/hl_mem/evaluation/metrics.py:32-38`：另一套评测已按“命中相关项数 / gold 数”计算真正 Recall@k，证明仓库内语义分裂。
- `tests/eval/eval_runner.py:123-127` 仅把 `no_evidence` 视为 predicted no-answer；`evaluation/run_recall_regression.py:28-30` 又把 `low_confidence` 一并计入。

#### 修复方案

冻结以下指标语义，并在所有 release-facing runner 中复用同一实现：

```text
hit_at_k       = 1 if unique(relevant) ∩ unique(top_k) 非空 else 0
recall_at_k    = |unique(relevant) ∩ unique(top_k)| / |unique(relevant)|
precision_at_3 = |unique(relevant) ∩ unique(top_3)| / 3
MRR            = 第一个 relevant 的 reciprocal rank
```

规则：

1. `hit_at_1`、`hit_at_5` 与 `recall_at_1`、`recall_at_5` 同时报告，不再复用字段名。
2. 重复返回的同一 Claim ID 只计一次 relevant hit，但仍作为排序质量问题单独报告。
3. no-answer 只指 `answerability == "no_evidence"`；`low_confidence` 单列为诊断率，不悄悄并入 no-answer。
4. 指标 schema 升版；旧 baseline 不做字段级兼容或自动转换，必须在新语义下重建。
5. `nDCG@5`、Hit@5、各通道 rank 和 cosine 保留为诊断指标；发布主指标为 Recall@5、MRR、P@3、no-answer precision/recall。

#### 改动文件列表

- `tests/eval/metrics.py`
- `tests/eval/eval_runner.py`
- `tests/eval/gate_check.py`
- `tests/eval/test_metrics.py`
- `tests/eval/test_recall_v2_gate.py`
- `evaluation/run_recall_regression.py`
- 必要时将公共纯函数收口到 `src/hl_mem/evaluation/metrics.py`

#### 工作量估算

0.75–1 人日。

#### 验收标准

- multi-gold fixture 为 `[a,b]`、top-5 只命中 `a` 时，`hit_at_5 == 1.0`、`recall_at_5 == 0.5`。
- top-5 重复返回 `a` 不会把 Recall@5 算成 1.0。
- 三个 runner 对同一 fixture 输出相同 Hit@k、Recall@k 和 no-answer 语义。
- 新报告不再把 Hit@1/5 标成 Recall@1/5；旧 schema/baseline 被明确拒绝。

### 2.2 冻结 baseline、查询集和发布 gate

#### 问题描述

当前 80 条 recall_v2 查询已具备良好 slice，但 gold 仍依赖本地 snapshot 动态绑定；baseline 文件没有 snapshot hash 和指标，状态是 `uninitialized`。现有 gate 只能阻止退化，没有“候选必须证明赢”的条件，也没有冻结真实 embedding/reranker 与完整 recall 配置。它还只检查 `forbidden_ids`，而数据集主要声明的是 forbidden status/time 规则；若不物化或执行这些规则，`forbidden hits = 0` 只是空门禁。CI 当前只跑 quality-smoke，不跑 recall gate。

#### 源码证据

- `tests/eval/datasets/recall_v2.manifest.json:4-21`：固定集有 80 case、6 个 slice，其中 25 条 no-answer/hard-negative。
- `tests/eval/dataset.py:158-213`：55 条可回答 case 在 snapshot 上按 canonical 字段动态绑定 gold；snapshot 不冻结时 gold 会漂移。
- `tests/eval/baselines/baseline_v1.json:2-18`：`status` 为 `uninitialized`，`snapshot_sha256` 与指标为 `null`，且字段集与 gate 不完整。
- `tests/eval/gate_check.py:21-56`：已检查 artifact hash、case/slice、forbidden hit、HTTP success 和非退化，但没有 win condition、P@3 或配置/model hash。
- `tests/eval/eval_runner.py:99-131`：只按 `forbidden_ids` 计算 forbidden hit，未执行 dataset 中的 `forbidden_statuses`/时间约束。
- `tests/eval/eval_runner.py:163-224`：已通过临时数据库走完整 REST/RecallService，可作为端到端 runner 基础。
- `scripts/ab_test_index_text.py:20,85-123`：只比较三种模式的 dense cosine/target rank，不是发布评测。
- `.github/workflows/test.yml:55-61`：只执行 quality-smoke，recall gate 尚未进入 CI。

#### 修复方案

Phase 0 先创建不可漂移的评测契约：

1. 冻结 `recall_v2.jsonl` 的内容摘要、case 数、slice 分布和 immutable dataset manifest。
2. 先在 snapshot 副本上应用用户审定的同一份 2-row repair manifest，再冻结该“v0.19 目标 canonical 状态”的 snapshot；生产库仍不修改。一次性解析并保存 bound gold artifact，同时记录 snapshot 文件 hash、canonical-data digest、repair manifest hash 和 schema migration 列表，不提交敏感数据库。
3. bound artifact 除 relevant/equivalent IDs 外，还要物化适用的 forbidden IDs；权威 runner 同时执行 forbidden status、双时间和已知时间约束，并加入 stale/superseded 负向 fixture。
4. 将 generated snapshot manifest 与 dataset manifest 分开，避免 `build_snapshot.py` 覆盖数据集清单。
5. 冻结共同 release 配置：embedding provider/model/dim、reranker mode/model、FTS tokenizer、候选数、RRF/relevance 参数和 top-k。实验 manifest 单独声明 `experiment.variable=index_text_mode`、`control=legacy`、`candidate=answerable` 及各自 version；artifact gate 要求除这一声明变量外逐项相同，而不是错误要求两臂 mode 相同。fake/off 仅用于确定性单测，不得作为生产切换证据。
6. 用改造前 v0.18.0 legacy 代码和已修复的冻结 snapshot 生成 `ready` baseline。baseline 更新必须是显式、可审查操作，候选运行不能自动覆盖它。
7. 冻结两级 gate：
   - compatibility gate：Phase 1 同期 legacy control 相对 v0.18.0 baseline 必须通过 hard gate 和 non-regression，不要求 win；
   - selection gate：只有 compatibility gate 通过后，answerable 才能相对同期 legacy control 接受 non-regression + win 判定。
8. 在 Phase 0 固定 LF/CRLF 无关的 `sha256-utf8-lf-v1` 内容摘要定义；Phase 4 将该定义落到 quality-smoke baseline。
9. 把 recall gate 加入 CI；无本地 release snapshot 时跑契约/fixture gate，正式发布使用受控环境中的真实 provider gate。

v0.19 的 gate 定义冻结为：

| 类型 | 条件 |
|---|---|
| Artifact hard gate | dataset、bound gold/forbidden、repair manifest、snapshot/canonical digest、schema、model metadata 和除声明实验变量外的配置必须逐项匹配；两臂 mode/version 必须分别等于 manifest 的 control/candidate |
| Execution hard gate | HTTP success = 100%；forbidden hits = 0；无混合 projection/embedding 版本 |
| Compatibility gate | Phase 1 同期 legacy control 相对 v0.18.0 legacy baseline：Recall@5、MRR、P@3、no-answer precision、no-answer recall 任一不得低超过 0.01 |
| Candidate non-regression | answerable 相对同期 legacy control：五个主指标任一不得低超过 0.01 |
| Slice non-regression | 任一批准的关键 slice 不得低超过 0.05；historical、preference、no-answer 必须单列 |
| Win condition | answerable 的 Recall@5 或 MRR 至少一项提升达到 0.02，且同一指标的 paired delta bootstrap 95% CI 下界严格大于 0 |
| Cost guard | P95 不超过 `max(legacy × 1.10, legacy + 20ms)`；provider 调用和 backfill 成本完整报告 |

阈值写入版本化 gate 文件；A/B 开始后不得为迁就结果而修改。同期 legacy control 未通过 compatibility gate 时，先修复 Phase 1，不进行候选选择。answerable 只有同时满足 hard gate、candidate/slice non-regression 和 win condition，才算“胜出”。

#### 改动文件列表

- `tests/eval/datasets/recall_v2.jsonl`
- `tests/eval/datasets/recall_v2.manifest.json`
- `tests/eval/fixtures/build_snapshot.py`
- `tests/eval/eval_runner.py`
- `tests/eval/gate_check.py`
- `tests/eval/baselines/baseline_v1.json`
- 新增 bound-gold / snapshot manifest / `gate_v019.json`
- `tests/eval/README.md`
- `.github/workflows/test.yml`

#### 工作量估算

1.25–2 人日。

#### 验收标准

- baseline 为 `ready`，包含指标 schema、全部 artifact/config/model/repair hash、case/slice 分布和五个主指标。
- 相同 dataset、bound gold、snapshot 和配置可复现同一报告；任一 artifact/config 漂移都会在算分前失败。
- 至少一个 stale/superseded fixture 能触发非空 forbidden hit；runner 实际执行 forbidden status/双时间规则。
- Phase 1 legacy control 退化时 compatibility gate 会阻断 selection gate 和 Phase 3。
- candidate 只“不退化”但没有达到 win condition 时，gate 明确失败，不能切默认。
- A/B manifest 允许且只允许 `index_text_mode` 这一实验变量不同。
- CI 覆盖指标契约和 gate；正式发布报告来自批准的真实 provider 配置，不来自 fake embedding。
- baseline 更新与 candidate 运行是两个命令/权限动作，候选不能自我批准。

### 2.3 Phase 0 出口

Phase 0 只有一个出口：团队已经拥有正确且不可漂移的尺子。此时没有实现 `answerable` 改造、没有修改生产数据、没有改变默认值。

---

## 3. Phase 1: Projection Unification（8–10 人日）

### 3.1 冻结两层契约并改善 `answerable`

#### 问题描述

现有 `answerable` 只把 value 做 `str()` 后拼接 subject 与 slot description/predicate，没有纳入 required qualifier，也没有明确处理历史 supersede envelope。与此同时，Repository 能 JSON 编码任意 Python 值，容易被误读为“系统已经承诺通用 typed-value 平台”；实际上新写入 schema 一直要求字符串。

#### 源码证据

- `src/hl_mem/ingest/extractors.py:16-31`：`ExtractedClaim.value: str`。
- `src/hl_mem/ingest/schemas.py:81-100`：LLM 提取 schema 同样要求非空字符串 value。
- `src/hl_mem/domain/claims/claim.py:9-38`：已有四种 mode；`answerable` 在第 16、22–26 行只做 `subject + label + str(value)`。
- `src/hl_mem/domain/claims/attributes.py:11-24,599-611`：slot 已有 description 和 required qualifiers；现有校验只确认 qualifier 是否存在。
- `src/hl_mem/storage/claims.py:52-66`：Repository 对 value 使用通用 JSON 编码，但这是存储实现能力，不是已冻结的 object/array 写入契约。
- `src/hl_mem/storage/claims.py:482-505`：历史 supersede 会写内部 `_type=superseded_value` envelope；这是生命周期兼容结构，不是开放的 Claim value 类型。

#### 修复方案

1. 明确 v0.19 的 Canonical Claim 写入契约：`value` 继续是原子字符串；`ExtractedClaim`、Pydantic schema、prompt 和显式保存均不放宽。
2. 不新增通用 value validator，不给 slot 增加 value kind、object schema 或递归 JSON 规则。
3. `answerable` 投影只解决文本质量：
   - 必须包含 subject；
   - 注册 slot 使用现有可读 description，未知 slot 使用规范化 predicate；
   - value 按原子字符串处理并规范空白；
   - required qualifier 按 registry 顺序以稳定、可读短语进入文本；
   - 不拼 topic tags、内部 slot 名或裸 `value_json`；
   - 同一输入与 `index_text_version` 必须产生逐字节相同的 UTF-8 文本，不调用 LLM。
4. 对既有 `_type=superseded_value` 只做明确的内部兼容解包，historical statement 使用 `old_value`；不泛化为 object/array renderer。
5. `legacy/value_only/natural/answerable` 继续保留用于诊断和回滚；Phase 1 的代码默认与配置默认仍为 `legacy`。
6. 生产写入和 Repository fallback 必须显式接收已选择的 mode，消除不同入口各自依赖函数默认值的问题，但这一阶段传入的仍是 legacy。

#### 改动文件列表

- `src/hl_mem/domain/claims/claim.py`
- 可选新增 `src/hl_mem/domain/claims/projection.py`，由 `claim.py` 保留兼容 facade
- `src/hl_mem/application/ingest.py`
- `src/hl_mem/storage/claims.py`
- `src/hl_mem/evaluation/runner.py`
- `scripts/reextract_claims.py`
- `scripts/run_quality_smoke.py`
- `tests/unit/test_answerable_index.py`
- 新增/扩展 projection fixture 与 repository fallback 测试

#### 工作量估算

3–4 人日。

#### 验收标准

- 新提取、显式保存、import 后提取和 Repository fallback 的 Canonical value 仍是字符串。
- `answerable` 对注册 slot、未知 predicate、required qualifier 和 superseded historical wrapper 均有稳定 fixture。
- 同一 Claim 重跑投影结果逐字节一致；投影不调用 LLM，不写回 `value_json`。
- `fact_hash`、conflict key、双时间、status、evidence、scope、importance/TTL 不因生成投影而改变。
- `Settings.index_text_mode`、示例配置和所有生产入口在 Phase 1 结束时仍为 `legacy`。

### 3.2 FTS、dense、reranker 与 API 统一消费 `index_text`

#### 问题描述

当前系统已经持久化 `index_text`，FTS trigger 和 dense 写入也基本采用它；但 reranker 与 API 又重新使用原始 value，导致同一 Claim 在候选生成、重排和 Agent 展示阶段代表成不同文本。replacement 与 conflict rival 也存在同类旁路。

#### 源码证据

- `src/hl_mem/storage/migrations/034_fix_claims_fts_column_name.sql:6-28`：当前最终 FTS trigger 只同步 `index_text`。
- `src/hl_mem/storage/claims.py:553-594`：FTS 查询通过 `claims_fts` join Claim。
- `src/hl_mem/application/ingest.py:73-75,526-574`：写入先生成 `index_text`，dense embedding 再消费该文本。
- `src/hl_mem/storage/claims.py:293-329`：dense 查询消费已存储的 embedding BLOB。
- `src/hl_mem/recall/staged_pipeline.py:122-123,596-619`：reranker 仍重新拼 `subject + predicate + value`。
- `src/hl_mem/application/recall.py:647-698`：顶层 Claim `text` 仍来自解码 value。
- `src/hl_mem/application/recall.py:807-820`：replacement `text` 仍来自 value。
- `src/hl_mem/storage/claims.py:402-418`：disputed rivals 只查询/返回 raw value。
- `src/hl_mem/api/schemas.py:67-95`：`ClaimOutput.text` 仍允许 `Any`。
- `src/hl_mem/mcp/server.py:145-165` 与 `src/hl_mem/adapters/hermes/prefetch.py:41-48`：MCP/Hermes 复用 RecallService 文本，因此应用层修复会自然向外收口。

#### 修复方案

1. Claim 的唯一公开文本面定义为持久化 `index_text`。
2. reranker documents 直接读取候选的 `index_text`，禁止再拼 subject/predicate/value。
3. `_assemble_results()` 的顶层 Claim `text`、replacement `text`、conflict rival `text` 全部来自各自 Claim 的 `index_text`。
4. `ClaimOutput.text` 收紧为 `str`；结构化 canonical value 不再冒充文本字段。
5. REST、MCP 和 Hermes 不再各自生成 Claim 文本；它们只消费应用服务已提供的 `text`。
6. observation、episode、trace、policy 继续使用各自正文，不被错误强制套用 Claim `index_text`。
7. SearchTrace 可记录全局 `index_text_mode/version`，但不得记录敏感 raw value 或 qualifier 正文。

#### 改动文件列表

- `src/hl_mem/recall/staged_pipeline.py`
- `src/hl_mem/application/recall.py`
- `src/hl_mem/storage/claims.py`
- `src/hl_mem/protocols.py`
- `src/hl_mem/api/schemas.py`
- `src/hl_mem/mcp/server.py`（schema/快照回归）
- `src/hl_mem/adapters/hermes/prefetch.py`（只做兼容回归，结构化重构在 Phase 3）
- `tests/integration/test_conflict_pipeline.py`
- API/MCP/Hermes 文本一致性测试

#### 工作量估算

3–3.5 人日。

#### 验收标准

- 用一个“raw value 与 `index_text` 故意不同”的 sentinel Claim，逐项断言 FTS 文档、dense 输入、reranker document、REST/MCP/Hermes 顶层 `text`、replacement 和 conflict rival 全部等于 `index_text`。
- 仓库生产路径不再存在 Claim 展示用的 `subject + predicate + value` 拼接。
- `ClaimOutput.text` 始终为字符串；API 不输出 dict/list/Python repr 作为 Claim text。
- 关闭 reranker、开启 reranker、historical recall 和 disputed explain 均使用同一文本面。
- legacy 模式的既有可见文本只发生“统一消费面”所需的批准变化，不在本阶段切换投影模式。

### 3.3 增强现有 A/B 与 `backfill-index-text`，但不迁移生产

#### 问题描述

现有 A/B 不是端到端评测；现有 backfill 基础较好，但只覆盖 active Claim，缺少显式 `--mode`、历史可见状态覆盖和发布前完整性报告。Phase 1 必须把工具准备好，同时严格保持生产库不变。

#### 源码证据

- `scripts/ab_test_index_text.py:20,85-123`：不包含 `answerable`，只报告 dense cosine 与 target rank。
- `src/hl_mem/cli.py:176-180,198-213`：已有 `backfill-index-text`，但 mode 只能来自 Settings。
- `src/hl_mem/workers/backfill_index_text.py:70-183`：已有 dry-run、batch、provider retry、`BEGIN IMMEDIATE` 和源字段/旧文本 CAS；只更新 `index_text`、embedding、model、dim。
- `src/hl_mem/workers/backfill_index_text.py:95-100`：只选择 `status='active'`。
- `src/hl_mem/storage/claims.py:280-285,308-313,565-580`：historical recall 还会读取 superseded/expired。
- `tests/unit/test_answerable_index.py:82-209`：已有零写、幂等、模型/维度重嵌入、CAS 和 retry 测试基础。

#### 修复方案

1. 保留并增强现有 `backfill-index-text`，不新建同义命令：
   - 增加显式 `--mode legacy|answerable`，不依赖待切换的默认配置；
   - 增加状态范围，至少覆盖所有可能被 current/historical recall 读取的 active/superseded/expired；
   - dry-run 输出 would-update、skip、failed、cursor、text hash、provider items/requests、model/dim；
   - apply 保持 batch、CAS、有限 retry 和每批事务；
   - `failed > 0`、覆盖不完整或校验失败时命令非零退出。
2. 增加投影/embedding 完整性检查：stored `index_text` 等于 projector；embedding blob 长度为 `4 × dim`；model/dim 一致；FTS integrity 和 row coverage 正常。
3. 生成 canonical digest，证明 backfill 前后 `value_json`、qualifiers、fact hash、冲突键、双时间、status、evidence 和生命周期字段未变。
4. 将 A/B 脚本改为调用 Phase 0 runner：从同一冻结 snapshot 创建 legacy 与 answerable 两个隔离副本，分别重建投影/embedding，再走完整 RecallService。唯一变量必须是 projection mode。
5. 对 semantic dedup、consolidation 和 relation discovery 记录 embedding 分布/候选变化的 shadow 报告；阈值未获证据前不自动改默认。
6. Phase 1 只交付代码、dry-run 和隔离副本报告能力；禁止对生产库 apply，禁止切默认。

#### 改动文件列表

- `scripts/ab_test_index_text.py`
- `src/hl_mem/workers/backfill_index_text.py`
- `src/hl_mem/cli.py`
- `tests/unit/test_answerable_index.py`
- `tests/unit/test_recall_diagnostic_scripts.py`
- `tests/eval/eval_runner.py`
- `tests/eval/gate_check.py`
- dedup/consolidation/relation shadow 报告测试或 fixture

#### 工作量估算

2–2.5 人日。

#### 验收标准

- `backfill-index-text --dry-run --mode answerable` 零写入、可断点续跑、重复执行结果稳定。
- 状态覆盖与 Recall intent 一致；不会留下 active/superseded/expired 混合投影。
- apply fixture 只改变允许的四类派生字段，canonical digest 前后相同。
- A/B 两边使用同一 snapshot、gold、真实 embedding/reranker 配置和完整召回链路。
- Phase 1 结束时生产数据库、生产默认和 baseline 均未被候选实现自动修改。

### 3.4 Phase 1 出口

`answerable` 已可评测，所有 Claim 消费者已统一到 `index_text`，迁移工具已准备好；但生产仍是 legacy。此状态本身可发布到测试环境，不代表同期 legacy control 已通过兼容 gate，更不代表获得切默认授权。

---

## 4. Phase 2: Migration & Gate（3–4 人日）

### 4.1 执行 legacy vs answerable 单变量 A/B

#### 问题描述

如果先切换、回填并接入整套 Agent 协议，最后才发现 `answerable` 没有稳定收益，返工面会覆盖检索、缓存和反馈。必须先在 Phase 0 冻结的框架内给出可复核结论。

#### 源码证据

- Phase 0 的 `baseline_v1.json` 当前尚未 ready，说明旧流程没有可用发布基线。
- `scripts/ab_test_index_text.py` 当前只做 dense 诊断，不能覆盖 FTS/Tag/RRF/reranker/relevance/API。
- `tests/eval/eval_runner.py:163-224` 已有复制 snapshot 并通过 REST/RecallService 运行的基础。

#### 修复方案

1. 从 Phase 0 已应用 2-row repair manifest 的同一冻结 snapshot 生成两个隔离副本；两个 arm 的 canonical 数据完全一致。
2. 用 Phase 1 代码生成同期 legacy control，重建 legacy `index_text + embedding`；另一个副本重建 answerable `index_text + embedding`。
3. 两边使用完全相同的真实 embedding/reranker、查询集、bound gold/forbidden、候选参数和硬件环境；实验 manifest 只允许 projection mode/version 不同。
4. 先跑 compatibility gate：同期 legacy control 相对 Phase 0 的 v0.18.0 legacy baseline 必须满足 hard gate 和 overall/slice non-regression。失败时停止选择，回到 Phase 1 修复统一消费面；不得进入 Phase 3。
5. compatibility gate 通过后，再跑 selection gate：answerable 相对同期 legacy control 必须同时满足 hard gate、candidate/slice non-regression、win condition 和 cost guard。
6. 两级比较都运行完整 FTS + Dense + Tag + RRF + reranker + relevance + REST。
7. 输出总体、slice、逐查询 rank delta、answerability、winning channel、P95、provider cost，以及 dedup/consolidation/relation shadow drift。
8. 只调用 Phase 0 冻结 gate，不在 Phase 2 修改阈值、repair manifest 或 baseline。

#### 改动文件列表

- `scripts/ab_test_index_text.py`
- `tests/eval/eval_runner.py`
- `tests/eval/gate_check.py`
- `tests/eval/baselines/baseline_v1.json`（只保存已批准 legacy baseline）
- `evaluation/results/` 或约定的非敏感报告目录

#### 工作量估算

1.5–2 人日。

#### 验收标准

- 两个同期 arm 的 canonical/repair digest 相同，报告能证明唯一实验变量是 projection mode/version。
- artifact/config/model hash 任一非声明差异都会使比较失败。
- compatibility gate 先证明 Phase 1 legacy 不退化；失败时 selection gate 和 Phase 3 均被阻断。
- selection gate 结果包含所有 hard gate、non-regression、win condition 和成本条件的逐项结论。
- answerable 未达 win condition 时结论必须是“保留 legacy”，不能写成“基本通过”或人工绕过。
- 报告归档后 baseline 不被自动更新。

### 4.2 Gate 后的定点修复、停机迁移和默认切换

#### 问题描述

生产库不能在服务持续写入时混用两种投影/embedding。另有审查确认的 2 条 double-encoded 数据需要修正，但这不应演变成全库 value migration 或通用 JSON 平台。

#### 源码证据

- `src/hl_mem/workers/backfill_index_text.py:146-182` 已以事务和 CAS 更新派生字段，可作为唯一 backfill 实现。
- `src/hl_mem/storage/backup.py:20-42` 已有 SQLite online backup 与 SHA-256 manifest，可在 Phase 4 CLI 完成前供维护 runbook 调用。
- `src/hl_mem/ingest/extractors.py:16-31` 与 `src/hl_mem/ingest/schemas.py:81-100` 明确新写入 value 是字符串。
- `src/hl_mem/storage/claims.py:482-505`：历史 supersede 会写内部生命周期 envelope；这不能被泛化为开放 object/array 写入契约。

#### 修复方案

两条数据修复属于用户批准的范围前提，并已通过同一 manifest 进入冻结评测 snapshot；生产 apply 仍须严格定点：

1. 使用审定清单中的恰好 2 个 Claim ID、旧 `value_json` SHA-256 和期望新字符串；proposal 不臆造 ID 或数据类型。
2. 修复脚本默认 dry-run，apply 使用 ID + before-hash CAS；任一行不匹配即全批回滚。
3. 若 canonical value 实际变化，在同一事务内按当前规则重算该 Claim 的 fact hash；先检查 hash collision。conflict key 仅在其输入确实受影响时重算。
4. 不改变 status、双时间、evidence、scope、importance/TTL 或关系；生成 before/after 审计报告。
5. 这 2 条之外的 `value_json` 一律不写。

只有 compatibility gate 通过并完成 selection decision 后，才进入共同维护前置：

1. 停止 API、Worker 和所有写入者。
2. 使用现有 SQLite backup API 创建一致备份和 manifest，并恢复到 scratch DB 验证 checksum、`PRAGMA integrity_check` 与核心计数。
3. 在停写事务中执行两条定点修复；其 repair manifest/hash 必须与评测 snapshot 使用的版本一致。
4. 校验清单 CAS、目标值/fact hash 和生产 canonical before/after digest；注意生产库整体 digest 不要求等于冻结评测 snapshot。

完成共同前置后，按 selection gate 分支执行：

**若 answerable 胜出**

1. 运行增强后的 `backfill-index-text --mode answerable`，覆盖 active/superseded/expired。
2. 全部校验通过后，才把 Settings、示例配置和部署配置默认切为 `answerable`，再启动服务。
3. 启动后跑 health、production smoke 和抽样 historical recall；这是 post-migration validation，不冒充依赖冻结 snapshot hash 的 A/B release gate。

**若 answerable 未胜出**

1. 保持 `legacy` 默认，不执行全库 answerable backfill。
2. 仅对共同前置中修复的两行按 legacy 重建 `index_text + embedding`。
3. 归档失败报告，`answerable` 保留为显式实验模式。

#### 改动文件列表

- `scripts/repair_v019_double_encoded_values.py`（一次性、清单驱动、默认 dry-run）
- `src/hl_mem/workers/backfill_index_text.py`
- `src/hl_mem/cli.py`
- `src/hl_mem/settings.py`
- `config.example.toml`
- `docs/configuration.md`
- `tests/unit/test_answerable_index.py`
- 定点修复脚本测试与维护 runbook

#### 工作量估算

1–1.5 人日。

#### 验收标准

- 修复报告显示恰好 2 条目标记录被检查；apply 最多修改这 2 条 canonical value，清单外写入数为 0。
- 两个分支在修改任何生产 canonical 数据前都已停写，且备份 manifest、scratch restore、integrity check 和核心计数均通过。
- 生产使用的 2-row repair manifest 与冻结评测 snapshot 完全相同；CAS 不匹配会中止而不是临时改清单。
- answerable 胜出时，所有可召回状态的 stored `index_text`、embedding、FTS 和配置默认完全一致。
- answerable 未胜出时，`Settings.index_text_mode` 与部署配置仍为 `legacy`，不存在全库 answerable 写入。
- 任何 failed/CAS miss/coverage mismatch 都中止切换；回滚使用已验证备份并恢复 legacy 配置。

### 4.3 发布后校验与 Phase 2 出口

#### 问题描述

一次成功命令不等于迁移成功；必须证明数据库、运行配置和对外文本面处于单一版本，并为 Phase 3 提供稳定前提。

#### 源码证据

- backfill 的 `version` 当前只出现在 summary，没有持久化强约束。
- current 与 historical recall 读取的状态集合不同，单看 active smoke 不足以发现混合投影。

#### 修复方案

1. 保存 migration report、canonical digest、FTS integrity、embedding model/dim/长度分布和最终默认配置快照。
2. 分别执行 current、historical、preference、no-answer 查询抽样。
3. 观察一个批准窗口内的 P95、answerability 分布、reranker fallback 和 side-effect failure。
4. 将选中的生产投影（legacy 或 answerable）写入 v0.19 release manifest。
5. Phase 3 只消费该稳定 `index_text`，不再讨论或重新选择投影。

#### 改动文件列表

- 发布 runbook
- release manifest / 非敏感评测报告
- `src/hl_mem/doctor.py`（如需增加投影一致性检查）
- doctor / historical smoke 测试

#### 工作量估算

0.5 人日。

#### 验收标准

- release manifest 明确记录最终 mode、index version、embedding model/dim 和 gate report hash。
- current/historical 均无混合投影或 embedding。
- 生产默认一经 Phase 2 锁定，Phase 3/4 不再隐式改变。
- answerable 失败不阻断 v0.19 其余范围；legacy 稳定状态同样满足 Phase 3 入口。

---

## 5. Phase 3: Context Packet & Feedback（8–10 人日）

### 5.1 冻结简化 Context Packet v1

#### 问题描述

当前 `RecallOutput` 暴露内部 results/observations/policies 和 packed context，没有冻结的 Agent 协议。旧 proposal 的 packet 包含 packet_id、query 对象、四个 section、rank、score、budget 和 feedback 子对象，远超首版实际需要，并把已延期的四类产品记忆提前固化进协议。

#### 源码证据

- `src/hl_mem/api/schemas.py:50-62`：`RecallInput` 只有 `context_mode=packed`，没有 packet response format。
- `src/hl_mem/api/schemas.py:111-121`：`RecallOutput` 仍是 legacy 字段集合。
- `src/hl_mem/application/recall.py:481-503,538-561`：现有 `context_items` 包装不是稳定 packet。
- `src/hl_mem/application/recall.py:505-531`：已有 `supported|low_confidence|no_evidence` answerability。
- `src/hl_mem/application/recall.py:647-698` 与 `src/hl_mem/storage/evidence.py:30-45`：已具备有序 item、id、text 和 evidence 基础。

#### 修复方案

Context Packet 是应用层 DTO，不是数据库模型。v1 schema 只包含以下字段：

```json
{
  "schema_major": 1,
  "schema_minor": 0,
  "query_id": "...",
  "answerability": "supported",
  "feedback_state": "available",
  "items": [
    {
      "type": "claim",
      "id": "...",
      "text": "...",
      "evidence": [],
      "feedback_id": "..."
    }
  ],
  "used_tokens_estimate": 320,
  "truncated": false
}
```

冻结规则：

1. 顶层严格为 8 个字段；item 严格为 5 个字段。不加入 `packet_id`、四个 section、显式 rank、score、memory class、budget 对象或 diagnostics。
2. `items` 数组顺序就是最终 rank；exposure rank 等于数组下标 + 1。
3. `answerability` 复用 `supported|low_confidence|no_evidence`。
4. `feedback_state` 只允许 `available|degraded`。exposure/receipt 持久化失败时 packet 和 recall 仍成功，不能返回 503。
5. 每个 `feedback_id` 始终是本次注入新生成的非空字符串。`available` 表示对应的新 exposure 已持久化；`degraded` 表示持久性尚未确认，consumer 不得提交或使用这些 ID，provider 只做有界内部重试并记录指标。
6. Claim item 的 `text` 只取 Phase 2 锁定的 `index_text`；assembler 不读取 `value_json`。
7. evidence 继续使用现有可追溯引用；query 原文、SearchTrace 和内部 feature 不进入 packet。
8. `schema_major` 不认识时 consumer 必须降级，不猜字段；兼容增加使用 `schema_minor`。
9. `RecallOutput` additive 增加可选 `context_packet`；`response_format=legacy|context_packet|both` 首版默认保持 legacy 兼容，Hermes 显式请求 packet。

#### 改动文件列表

- 新增 `src/hl_mem/application/context_packet.py`
- `src/hl_mem/application/recall.py`
- `src/hl_mem/api/schemas.py`
- `src/hl_mem/api/server.py`
- `src/hl_mem/mcp/server.py`
- `docs/api.md`
- `docs/mcp-tools.json`
- 新增 `tests/unit/test_context_packet.py`
- API/MCP schema snapshot 测试

#### 工作量估算

2–2.5 人日。

#### 验收标准

- schema snapshot 精确匹配 8 个顶层字段和 5 个 item 字段；没有 sections、rank 或四类 taxonomy。
- `items` 顺序逐项等于最终 packing 顺序，`used_tokens_estimate` 与 `truncated` 可复现。
- 每个 Claim `text` 等于对应 `index_text`，assembler/REST/MCP 不解码 `value_json`。
- legacy response 默认行为兼容；请求 packet/both 时才增加新协议。
- schema 校验要求 `feedback_id` 始终为非空字符串；exposure 持久化失败仍返回有效 packet，且 `feedback_state == "degraded"`。

### 5.2 将 exposure 移到注入物化，并把 `used_by_model` 改为 `injected`

#### 问题描述

现有普通 recall 在预算裁剪前给所有返回组创建 exposure；写入失败时仍可能把未落库 ID 留在 item 上。数据库字段 `used_by_model` 既没有被更新，也表达了系统无法证明的语义。缓存复用旧 receipt 会把多次 Agent 注入错误合并为一次 exposure。

#### 源码证据

- `src/hl_mem/application/recall.py:573-607`：先写 `feedback_id` 到 item，再 best-effort 批量落库。
- `src/hl_mem/application/recall.py:480-499`：普通路径先建 exposure，后做 token packing；预算淘汰项也可能有 exposure。
- `src/hl_mem/application/recall.py:608-616`：失败只记日志，无法形成 per-call `feedback_state`。
- `src/hl_mem/storage/migrations/008_experience.sql:45-55`：字段名为 `used_by_model`。
- `src/hl_mem/storage/experience.py:239-292`：已有单条/批量 exposure insert；全仓没有把 `used_by_model` 更新为 1 的正式路径。
- `src/hl_mem/storage/experience.py:294-362` 与 `src/hl_mem/storage/usefulness.py:115-142`：已有幂等 feedback 和 usefulness 重建基础，可直接复用。
- `tests/unit/test_comprehensive_fixes.py:163-173`：已冻结“feedback 写失败不得使 recall 失败”的行为。

#### 修复方案

1. 将流程拆成：

```text
cacheable RetrievalBundle（保留原始 query_id，无 feedback_id）
    → 最终预算裁剪与排序
    → 为本次注入生成一组新 feedback_id
    → 原子尝试为最终 items 批量持久化 exposure
    → 按结果组装 available/degraded Context Packet
    → 渲染并交付
    → 在 delivery 边界标记或排队重试 injected
```

2. `query_id` 标识本次检索及其 SearchTrace，保留在 RetrievalBundle 中；缓存复用同一检索结果时沿用该 `query_id`。每次注入只新建一组 `feedback_id`，绝不能复用上次 receipt。
3. 只为最终 packet items 建 exposure；不为候选池或预算淘汰项建。
4. exposure batch 写入全部成功才标 `feedback_state=available`；事务失败则回滚整批，仍携带本次新生成的非空 ID 返回同一文本 packet + `degraded`，consumer 不得提交这些未确认 ID。
5. 新增下一顺序 migration，将 `retrieval_feedback.used_by_model` 改名为 `injected`；不修改 migration 008。
6. `injected=1` 只表示 adapter 已把包含该 item 的渲染结果交给 Agent host/model 输入边界，不表示模型阅读、采纳或引用了它。
7. 保留现有显式 feedback 幂等语义，并增加 batch outcome/receipt 应用服务。只有明确 user feedback、host task outcome 或可靠 Episode 终态才写 helpful/outcome。
8. 未知 `feedback_id` 明确失败；同 payload 重放不重复累计 usefulness；“没有报错”或“session 正常结束”不自动推断 helpful。
9. `feedback_state` 只反映 exposure/receipt 是否持久化；packet 组装后发生的 `injected` 标记失败不能回写该字段，只进入有界重试、health counter 和日志。

#### 改动文件列表

- `src/hl_mem/application/context_packet.py`
- `src/hl_mem/application/recall.py`
- `src/hl_mem/storage/migrations/035_retrieval_feedback_injected.sql`（编号以落地时下一顺序为准）
- `src/hl_mem/storage/experience.py`
- `src/hl_mem/experience/service.py`
- `src/hl_mem/api/schemas.py`
- `src/hl_mem/api/server.py`
- `tests/unit/test_experience_api.py`
- `tests/unit/test_comprehensive_fixes.py`
- 新增 `tests/integration/test_context_packet_feedback.py`
- migration upgrade 测试

#### 工作量估算

2–2.5 人日。

#### 验收标准

- packet 中 exposure.rank 与 item 数组位置一致；被预算淘汰的候选无 exposure。
- 同一 RetrievalBundle 连续注入两次时 `query_id` 保持不变，item id/text/evidence 可相同，但两次的 `feedback_id` 全部不同；两次均 available 时数据库存在两组 exposure。
- 未被读取的预取不创建 exposure；已经物化但交付失败时 exposure 保持 `injected=0`；只有跨过 delivery 边界的注入才标 1。
- exposure 持久化失败不阻断 recall/Agent，packet 标 degraded；`injected` 标记失败同样不阻断交付，但不改变既有 `feedback_state`，进入内部重试且 health counter 可观测。
- 明确 feedback/outcome 可更新 usefulness；相同重放不增加聚合；无明确信号时 helpful/outcome 保持 `NULL`。
- 新代码、API 和文档不再声称系统能证明“模型使用了记忆”。

### 5.3 Hermes structured prefetch、renderer 与 delivery receipt

#### 问题描述

Hermes 当前在后台线程中把 `/v1/recall` 的 results 立即 join 成字符串，只按 `(session_id, query_hash)` 缓存。limit、intent、as_of 等参数被丢弃；同一字符串可以被反复返回，既丢结构也会复用旧 receipt。单一全局预取线程还可能在一个请求运行时静默丢弃其他 key。

#### 源码证据

- `src/hl_mem/adapters/hermes/prefetch.py:16-21`：`PrefetchEntry.value` 只有字符串。
- `src/hl_mem/adapters/hermes/prefetch.py:41-56`：预取后立即 join `results[].text`。
- `src/hl_mem/adapters/hermes/prefetch.py:58-62`：仅有一个全局线程，运行中不接受另一预取。
- `src/hl_mem/adapters/hermes/prefetch.py:64-74,90-93`：同一缓存字符串可重复读取，key 只有 session/query hash。
- `src/hl_mem/adapters/hermes/provider.py:133-145`：`limit/intent/as_of` 被显式丢弃。
- `src/hl_mem/adapters/hermes/provider.py:205-210`：queue/get 没有 materialization 或 feedback delivery。

#### 修复方案

1. PrefetchCache 缓存本地可独立渲染的 receipt-free RetrievalBundle，至少保留原始 `query_id`、按候选结构化的 `text/evidence` 和 packing 所需元数据；不只缓存服务端 handle，也不缓存 Context Packet、渲染字符串或 feedback ID。
2. cache key 覆盖所有影响结果的参数：session、query hash、limit、intent、as_of、known_as_of、namespace、token budget 和选定 projection/config version。
3. 预取按 key 去重并显式记录 pending/completed/expired，不因另一个 key 正在运行而静默丢弃。
4. 纯预取只生成 RetrievalBundle，不创建 exposure；`prefetch()/prefetched()` 真正请求注入文本时，才在最终 packing 后调用 packet materializer，为本次注入创建新 receipt。
5. 新增专用 renderer，只按 `items` 数组顺序渲染 `text`；不重排、不读取 `value_json`，也不把 `feedback_id` 输出到提示词。
6. Provider 内部维护有界 `DeliveryReceipt(session/turn/query_id/feedback_ids)`，只用于 injection 标记和后续明确 outcome，不加入外部 packet schema。
7. 非空 renderer 输出成功交给 Hermes 的 Agent host/model 输入边界后才标 `injected`；标记失败进入有限重试/flush 和 health metric，不改变已组装 packet 的 `feedback_state`，也不能阻断 Agent 或伪造成功。
8. 只有 retrieval 本身失败、没有可用 RetrievalBundle，或遇到未知 schema major 时，才 fail-open 为空/legacy 兼容上下文并记降级指标。若已有有效缓存 bundle，即使 exposure 持久化或 `injected` 标记失败，也必须继续渲染其文本：前者反馈降级，后者仅内部重试。

#### 改动文件列表

- `src/hl_mem/adapters/hermes/prefetch.py`
- `src/hl_mem/adapters/hermes/provider.py`
- 新增 `src/hl_mem/adapters/hermes/renderer.py`
- `src/hl_mem/adapters/hermes/http_client.py`
- 必要时 `src/hl_mem/adapters/hermes/plugin/plugin.yaml`
- `tests/unit/test_provider.py`
- 新增 `tests/unit/test_hermes_renderer.py`
- Hermes 端到端 delivery/feedback 测试

#### 工作量估算

2.5–3 人日。

#### 验收标准

- prefetch 缓存中没有 feedback ID；缓存命中后每次 delivery 都得到新 receipt。
- 缓存 bundle 自带原始 `query_id` 和本地结构化 text/evidence；daemon 后续不可用时仍可完成 packing 与渲染。
- limit/intent/as_of/namespace/budget 不再被丢弃，任一不同都不会串缓存。
- renderer 输出顺序与 packet items 完全一致，不包含 feedback ID、SearchTrace 或 raw value。
- 未读取的预取不创建 exposure；已经物化但未交付的 exposure 保持 `injected=0`，实际交付后才标记。
- 同时预取不同 session/query 不静默丢任务。
- 没有 bundle 的 retrieval/daemon 失败才允许空/legacy 降级；已有缓存 bundle 时 receipt/injection 失败仍保留其渲染文本，Agent 主任务继续，且相应状态可观测。

### 5.4 Phase 3 兼容与端到端闭环

#### 问题描述

Packet、receipt 和 Hermes 分别通过单测仍不足以证明一次真实 turn 能闭环，也不能证明 legacy REST/MCP 调用方未被破坏。

#### 源码证据

- `tests/unit/test_experience_api.py:95-126` 与 `tests/integration/test_p0p1_integration.py:27-115` 已覆盖部分 exposure → feedback → usefulness，可扩展。
- 当前没有 `test_context_packet.py`、Hermes renderer 测试或 packet-feedback 集成测试。

#### 修复方案

1. 建立一次完整 turn fixture：召回/缓存 → 最终 packing → packet materialization → Hermes render → delivery → `injected` → 明确 outcome → usefulness。
2. 覆盖 available/degraded、缓存命中两次、未知 schema、unknown feedback ID、重复 outcome 和 session end flush。
3. 保留 legacy REST/MCP fixture；packet 是 additive 能力。
4. `/healthz` 暴露 packet materialization、feedback persistence、injection delivery 的失败计数，不包含敏感正文。

#### 改动文件列表

- `tests/integration/test_context_packet_feedback.py`
- `tests/unit/test_context_packet.py`
- `tests/unit/test_provider.py`
- `tests/unit/test_hermes_renderer.py`
- `tests/unit/test_experience_api.py`
- `src/hl_mem/api/server.py`
- `docs/api.md`

#### 工作量估算

1.5–2 人日。

#### 验收标准

- 可用 `query_id + feedback_id` 追踪一次 delivery 和显式 outcome。
- 缓存命中两次产生两组 receipt，不重复累计 usefulness。
- degraded 路径保持 recall/Agent 成功，且没有伪 durable feedback。
- legacy REST/MCP schema snapshot 通过。
- Context Packet 不引入四类记忆、Mental Model 或 Policy 新语义。

---

## 6. Phase 4: Operations & Hardening（5–7 人日）

### 6.1 backup / restore CLI

#### 问题描述

底层已有 SQLite online backup 和 checksum manifest，但 CLI 没有入口；restore 校验后直接写目标，缺少覆盖确认、同目录临时文件、integrity check 和最终原子替换。

#### 源码证据

- `src/hl_mem/storage/backup.py:20-42`：已有 online backup 与 manifest。
- `src/hl_mem/storage/backup.py:45-56`：restore 直接写 target。
- `src/hl_mem/cli.py:145-183,198-257`：没有 backup/restore 子命令。
- `tests/unit/test_production_boundaries.py:28-39`：只覆盖 checksum/tamper。

#### 修复方案

新增：

```text
hl-mem backup <backup.db> [--db <source.db>]
hl-mem restore <backup.db> --manifest <manifest.json> [--db <target.db>] --confirm-overwrite
```

backup 输出 machine-readable JSON。restore 先校验 path、manifest、size/hash，再恢复到目标同目录临时文件，执行 `PRAGMA integrity_check`，最后原子 replace；目标存在时必须显式确认。拒绝 source/backup/target 解析到同一路径，文档要求停服务后 restore。失败不改原目标。

#### 改动文件列表

- `src/hl_mem/storage/backup.py`
- `src/hl_mem/cli.py`
- `docs/configuration.md`
- 新增 `tests/unit/test_backup_cli.py`

#### 工作量估算

1.25–1.75 人日。

#### 验收标准

- 损坏 manifest/backup 被拒绝；无确认不覆盖。
- 任一失败保留原目标；成功后 `integrity_check == ok`，event/claim 核心计数一致。
- CLI 输出包含 backup、manifest、size、sha256、integrity status。
- 相同路径和运行中 restore 风险有明确拒绝/文档提示。

### 6.2 JSONL import 重建 extraction job

#### 问题描述

JSONL import 只恢复 event，不创建 extraction job；逐行 commit 时中途错误还会留下部分导入。因此“导入成功”不能保证 Claims 可重建。

#### 源码证据

- `src/hl_mem/cli.py:26-42`：export 只导出 events。
- `src/hl_mem/cli.py:45-68`：import 逐行 `insert_event(..., commit=True)`，不建 job。
- `src/hl_mem/application/ingest.py:134-167`：正常入口在同一事务写 event + job。
- `src/hl_mem/application/ingest.py:204-215`：提取 job 使用稳定键 `extract:<event_id>`。
- `tests/unit/test_management_surfaces.py:25-44`：现有 round-trip 只断言 event。

#### 修复方案

1. 每个新 event 与对应 `extract_event` job 在同一事务/批次写入。
2. job 使用 `extract:<event_id>`，重复 import 不增加 event 或 job。
3. 非法记录回滚当前批次；报告 processed、events_created、events_skipped、jobs_queued、failed_batch。
4. 提供显式 `--skip-extraction-jobs` 仅用于取证恢复，并输出 `claims_not_rebuilt=true`。

#### 改动文件列表

- `src/hl_mem/cli.py`
- `src/hl_mem/storage/jobs.py`（如需批量入口）
- `docs/configuration.md`
- `tests/unit/test_management_surfaces.py`
- 新增 `tests/unit/test_jsonl_import.py`

#### 工作量估算

0.75–1 人日。

#### 验收标准

- 空库导入 N 个新 event 后恰有 N 个唯一 extract job。
- 重复导入 event/job 均不增长。
- 任一 event/job pair 不出现半写入；错误报告可定位 batch/line。
- Worker 能从导入 archive 重建预期 Claims。

### 6.3 REST / MCP / Hermes 显式保存幂等键

#### 问题描述

通用 event ingest 已支持幂等键，但显式记忆保存固定写 `None`；REST/MCP schema 无 key，MCP 还固定返回 `created=true`。重试会制造重复 event/job。

#### 源码证据

- `src/hl_mem/api/schemas.py:124-131`：`MemoryInput` 无幂等键。
- `src/hl_mem/api/server.py:368-401`：`/v1/memories` 不接受 header/key。
- `src/hl_mem/application/ingest.py:169-202`：`save_explicit_memory()` 固定 `idempotency_key=None`。
- `src/hl_mem/mcp/server.py:45-56,132-143`：schema 无 key，响应固定 `created=true`。
- `src/hl_mem/application/ingest.py:134-167` 与 `src/hl_mem/storage/migrations/001_initial.sql:1-4`：已有事务幂等基础和唯一约束。
- `src/hl_mem/adapters/hermes/provider.py:189-193`：Hermes 显式写未带 key。

#### 修复方案

1. REST body、`Idempotency-Key` header 和 MCP `memory_save` 增加最长 200 字符的可选 key；REST header 优先。
2. `save_explicit_memory()` 委托统一 `ingest_event()` 事务，返回 `{id, created}`。
3. 同 key + 同规范化 payload 返回原 ID、`created=false`；同 key + 不同 payload 返回 409/MCP 明确错误，不能静默复用。
4. Hermes 以 host key/target/content hash 生成稳定重试键。
5. 无 key 保持每次新建的现有语义。

#### 改动文件列表

- `src/hl_mem/api/schemas.py`
- `src/hl_mem/api/server.py`
- `src/hl_mem/application/ingest.py`
- `src/hl_mem/storage/events.py`
- `src/hl_mem/mcp/server.py`
- `src/hl_mem/adapters/hermes/provider.py`
- `docs/api-schema.json`
- `docs/mcp-tools.json`
- REST/MCP/Hermes 幂等测试

#### 工作量估算

0.75–1 人日。

#### 验收标准

- REST body/header、MCP 和 Hermes 重试只产生 1 个 event + 1 个 job，返回同一 ID。
- header 与 body 同时存在时 header 优先。
- key 相同而 payload 不同明确失败；无 key 仍可创建多条。
- response 不再固定谎报 `created=true`。

### 6.4 quality-smoke CRLF 哈希

#### 问题描述

quality-smoke 对原始 bytes 哈希，LF/CRLF/裸 CR 差异会让同一逻辑数据集失配；baseline 又未声明 hash algorithm。

#### 源码证据

- `scripts/run_quality_smoke.py:45-49`：解析本身与换行风格无关。
- `scripts/run_quality_smoke.py:273-275`：hash 使用 `read_bytes()`。
- `scripts/run_quality_smoke.py:312-327,355-360`：baseline 只有 dataset hash，没有 algorithm/version。

#### 修复方案

按 Phase 0 冻结的 `sha256-utf8-lf-v1`：以 UTF-8 解码，将 CRLF 和裸 CR 规范为 LF，再计算 SHA-256。baseline schema 升版并写入 `hash_algorithm`；未知算法或旧新 schema 不静默比较。只更新 hash 元数据，不借机修改查询集、指标值或 tolerance。

#### 改动文件列表

- `scripts/run_quality_smoke.py`
- `evaluation/baselines/smoke_v2_baseline.json`
- quality-smoke hash 单元测试

#### 工作量估算

0.25–0.5 人日。

#### 验收标准

- 同一 UTF-8 JSONL 的 LF、CRLF、裸 CR 得到相同 hash 和 gate。
- 字符或记录变化仍改变 hash。
- baseline 明确记录 algorithm/version，v1/v2 不静默混用。

### 6.5 启动脚本与配置来源一致性

#### 问题描述

Windows 脚本硬编码本机路径并启动不存在的模块级 ASGI app，没有 Worker；Shell 脚本绑定旧版本、硬编码 Windows venv 和失效环境变量。两者都绕开了当前 TOML + secret environment 的统一配置入口。

#### 源码证据

- `start_production.bat:3-7`：硬编码 `D:\workspace\...`、设置旧 `HL_MEM_*`，调用 `hl_mem.api.server:app`。
- `src/hl_mem/api/server.py:72-431`：只有 `create_app()`，没有模块级 `app`。
- `start_server.py:13-22`：当前唯一同时启动统一配置、Worker 和 FastAPI 的入口。
- `start_v017.sh:2-7`：旧版本名、硬编码路径/Windows Python、注入失效配置。
- `src/hl_mem/config_loader.py:186-239`：非 secret 配置来自 TOML，process env 只覆盖 secret。

#### 修复方案

1. `start_production.bat` 使用 `%~dp0` 找根目录，检查 venv/TOML 后调用 `.venv\Scripts\python.exe start_server.py`，透传退出码。
2. `git mv start_v017.sh start_hl_mem.sh`；用 `BASH_SOURCE[0]` 定位目录，兼容 `.venv/bin/python` 与 `.venv/Scripts/python.exe`。
3. 两个脚本不再复制非 secret 运行配置；配置来自 TOML，密钥来自 `.env`/进程环境。
4. 启动脚本不更换 provider/model，也不覆盖 `hl_mem.toml`；部署选择继续由现有配置文件负责。

#### 改动文件列表

- `start_production.bat`
- `start_v017.sh` → `start_hl_mem.sh`
- `start_server.py`（仅在需要参数/退出处理时）
- `docs/configuration.md`
- 引用旧脚本名的活文档
- 启动/config smoke 测试

#### 工作量估算

0.5–0.75 人日。

#### 验收标准

- 从任意 cwd 启动均能定位项目，API 与 Worker 同时启动。
- 缺 venv/TOML 时快速非零退出；脚本无绝对工作区路径和失效配置覆盖。
- Windows/Git Bash/POSIX venv 路径均有 smoke。
- 同一 `hl_mem.toml` 通过脚本和直接运行 `start_server.py` 得到相同有效配置；脚本不擅自更换 provider/model。

### 6.6 namespace 收口：单租户软标签

#### 问题描述

Recall 已按 namespace 过滤，但 Episode 创建/列表、Policy induction、retention 和显式保存仍有遗漏或固定 `default`。这会跨相关性分区聚合，同时现有 `tenant_id` 命名又容易被误解为安全多租户。

#### 源码证据

- `src/hl_mem/api/schemas.py:12-18,50-64` 与 `src/hl_mem/application/ingest.py:445-447`：已明确 tenant/namespace 只是软标签。
- `src/hl_mem/storage/migrations/010_experience_constraints.sql:3-13`：Episode 表已有 namespace 字段。
- `src/hl_mem/storage/experience.py:99-120,159-175`：Episode create 不写 namespace，list 不过滤。
- `src/hl_mem/api/schemas.py:134-139` 与 `src/hl_mem/api/server.py:242-249`：Episode API 不接 namespace。
- `src/hl_mem/workers/induce_policies.py:27-33,59-63`：跨 namespace 扫描 Episode，并查/写 `default` Policy。
- `src/hl_mem/workers/worker.py:192-193`：retention 只 purge `default`。
- `src/hl_mem/application/ingest.py:184-193`：显式 memory 固定 `tenant_id=default`。

#### 修复方案

1. 对外统一术语 `namespace`：同一受信任、本地单租户部署内的相关性/profile 软分区；不是 authentication、authorization、encryption 或 side-channel 边界。
2. `tenant_id` 保留为兼容 alias 并标 deprecated；新 API/应用服务使用 namespace，二者冲突时明确失败。
3. memory save、Episode create/list、Context Packet、Hermes、Policy induction 和维护 job 显式传递 namespace。
4. Policy induction 按 namespace 分桶，supporting Episode 必须同 namespace；后台任务不得把非 default 数据归入 default。
5. Hermes namespace 只来自受信配置/host 参数，不允许消息正文覆盖。
6. backup/restore 仍是整库操作；不得宣称已提供 SaaS 多租户隔离、RBAC、按租户密钥或计费。

#### 改动文件列表

- `src/hl_mem/api/schemas.py`
- `src/hl_mem/api/server.py`
- `src/hl_mem/application/ingest.py`
- `src/hl_mem/application/recall.py`
- `src/hl_mem/storage/experience.py`
- `src/hl_mem/workers/induce_policies.py`
- `src/hl_mem/workers/worker.py`
- Phase 3 Context Packet/Hermes 文件
- `docs/architecture.md`
- `docs/api.md`
- `docs/configuration.md`
- API/MCP schema 与双 namespace 负向测试

#### 工作量估算

1.5–2 人日。

#### 验收标准

- A/B 两个 namespace 的 recall、packet、Episode、Policy 和维护结果不交叉聚合。
- 所有新对象保留调用方 namespace；后台任务不再静默落入 default。
- `tenant_id` 兼容旧请求，但新代码不把它当授权信息。
- 活文档明确“单租户、软标签、非安全边界”，不宣传多租户支持。

### 6.7 Phase 4 出口

六项运维工作共同满足 5–7 人日预算，原因是共享 CLI、配置、API snapshot 和集成测试。v0.19 完成时，备份可恢复、event archive 可重建 Claim、重试不会重复写、跨平台 baseline 稳定、启动入口唯一、namespace 相关性过滤一致。

---

## 7. 后续 RFC 清单

以下主题不在 v0.19 实施。每一项必须单独提交 RFC，不能捆绑回流，也不能仅凭已有表/骨架进入路线图。

### 7.1 RFC：四类产品记忆

#### 问题描述

Profile、Entity/Project Facts、Episodic、Procedural 是潜在产品语义，但当前外部契约仍按存储来源表达。直接在 v0.19 固化四类 section、配额和新表，会在没有效果证据时扩大协议和路由复杂度。

#### 源码证据

- `src/hl_mem/protocols.py:14`：当前类型是 claim/observation/policy/episode/trace。
- `src/hl_mem/api/schemas.py:65-114`：API 也按 Claim 与 Experience 存储类型暴露，没有四类 projector。

#### 修复方案（后续 RFC）

独立 RFC 必须给出定义、互斥分类规则、标注集、跨类型 quota、错误分类成本，以及相对扁平 packet 的 Agent task uplift。

#### 改动文件列表

v0.19：无。未来 RFC 自行列出 projector、recall routing、packet version 和评测文件。

#### 工作量估算

不计入 v0.19；RFC 评审前不承诺实现人日。

#### 验收标准（重新进入路线图）

具备 per-type labeled set、分类 precision/recall、跨类型 quota A/B、token cost 和 Agent task success uplift；否则保持扁平 items。

### 7.2 RFC：Multi-view Embedding

#### 问题描述

statement/question/aliases 多视图可能改善不同问法，也可能引入 view-count bias、额外存储/扫描和难以归因的调参。v0.19 先验证单一选定投影。

#### 源码证据

- `src/hl_mem/storage/migrations/031_claim_index_text.sql:1-33`：每个 Claim 只有一个 `index_text`。
- `src/hl_mem/storage/migrations/001_initial.sql:46-49`：每个 Claim 只有一套 dense embedding。
- `src/hl_mem/domain/claims/claim.py:9-38`：mode 是互斥生成一个文本，不是并存视图。

#### 修复方案（后续 RFC）

独立 RFC 需设计派生存储、Claim 内聚合、防重复投票、observe/on 开关和回填/回滚；不得把三个 view 当三个独立 RRF channel。

#### 改动文件列表

v0.19：无。未来 RFC 自行列出新 migration、view repository、pipeline 和评测工具。

#### 工作量估算

不计入 v0.19；先由 RFC 估算。

#### 验收标准（重新进入路线图）

在 answerable/最终单视图 baseline 上做单变量 A/B，证明 Recall/MRR/P@3 或关键 slice 有批准增益，且 P95、存储与 provider 成本在预算内。

### 7.3 RFC：Mental Model 与 Session Summary

#### 问题描述

当前派生记忆维护器接受这些 kind，但自动扫描只构建 Observation；没有 grounded builder、稳定更新水位或 hallucination 评测。

#### 源码证据

- `src/hl_mem/workers/mental_models.py:17-79`：rebuild 接受多个 kind/body。
- `src/hl_mem/workers/mental_models.py:100-138`：自动扫描只创建 Observation。
- `src/hl_mem/recall/observation.py:12-29`：正文只是拼 Claim value。

#### 修复方案（后续 RFC）

Mental Model/Session Summary RFC 必须定义 evidence grounding、逐句引用、stale 传播、watermark 幂等、token/cost 和 unsupported statement 处理。

#### 改动文件列表

v0.19：无。未来 RFC 自行列出 builder、evidence、worker、recall 和 eval 文件。

#### 工作量估算

不计入 v0.19；先由独立 RFC 估算。

#### 验收标准（重新进入路线图）

grounding/evidence coverage、stale accuracy、unsupported statement rate、token/provider cost 和真实任务收益达到预先批准阈值。

### 7.4 RFC：Policy induction 增强

#### 问题描述

Policy 已有 candidate/active 与 evidence 基础，但 induction 仍按成功 Episode 的原始 action 前缀粗聚类，容易误拆或误并。

#### 源码证据

- `src/hl_mem/workers/induce_policies.py:27-48`：仅成功 Episode + 前三个原始 action。
- `src/hl_mem/workers/induce_policies.py:57-63`：直接生成 trigger/steps。
- `src/hl_mem/storage/experience.py:364-408`：已有 evidence 与 candidate/active 基础。
- `src/hl_mem/application/recall.py:456-473`：Policy 主要在 Tool/Procedure 路径整合。

#### 修复方案（后续 RFC）

独立 RFC 必须设计 action 规范化、正反例、独立 session support、发布 precision、revision/retirement 和任务结果反馈，不能在 v0.19 顺手增强。

#### 改动文件列表

v0.19：无。未来 RFC 自行列出 induction、storage、procedure recall 和离线评测文件。

#### 工作量估算

不计入 v0.19；先由独立 RFC 估算。

#### 验收标准（重新进入路线图）

报告 cluster precision/recall、false activation、独立 session support、失败反例覆盖、task success uplift 和 helpful rate；无量化结果不得自动发布 active Policy。

### 7.5 RFC：typed JSON object/array

#### 问题描述

Repository 的通用 JSON 编码能力不等于产品已经需要 object/array Claim。现在建设通用 typed-value 平台会引入类型、校验、hash、冲突、投影、API 和迁移契约，而当前 active 写入仍是字符串。

#### 源码证据

- `src/hl_mem/ingest/extractors.py:16-31` 与 `src/hl_mem/ingest/schemas.py:81-100`：正式新写入 value 是字符串。
- `src/hl_mem/storage/claims.py:52-66`：存储层通用 encode 是实现细节。
- `src/hl_mem/storage/claims.py:482-505`：现有 object 是内部 supersede envelope。

#### 修复方案（后续 RFC）

只有出现真实、活跃、无法用原子 Claim + qualifiers 表达的 object/array 用例，才提交独立 RFC。RFC 必须覆盖 canonical equality、fact hash、conflict、projection、API compatibility、数据迁移和回滚。

#### 改动文件列表

v0.19：无；不新增 value type registry 或通用 validator。

#### 工作量估算

不计入 v0.19；真实用例和 RFC 通过前不估算。

#### 验收标准（重新进入路线图）

至少一个批准的 active use case、代表性 fixture、兼容/迁移计划和量化收益；仅有“未来可能需要”不构成入口。

---

## 8. 不做项清单

| 不做项 | 原因 |
|---|---|
| 预设 `answerable` 为生产默认 | 违反 Release Rule 1；必须先通过冻结 gate |
| gate 失败后仍切默认或全库 backfill | 违反 Release Rule 2；失败时保留 legacy |
| 建设通用 typed-value 平台 | 当前正式写入是字符串，没有 active 用例支撑复杂度 |
| 批量迁移、改写或 LLM 重写既有 `value_json` | 除审定 2 条定点修复外，canonical 数据保持不变 |
| 在 v0.19 支持任意 object/array Claim | 另开 typed JSON RFC |
| 任何通用 value 类型或结构校验框架 | 属于未来 typed JSON RFC，不进入本版 |
| 把 Canonical Claim、Search Projection、生命周期写成三层 | 生命周期是 Canonical Claim 的领域约束；本版只有两层 |
| 新建 `backfill-search-projections` | 复用并增强现有 `backfill-index-text` |
| 用绝对 cosine 决定发布 | projection/model 改变会改变分布；发布以端到端 gate 为准 |
| 在 Context Packet 增加四个 section、显式 rank、packet_id 或 memory class | 最终 schema 已冻结为扁平 items，数组顺序就是 rank |
| 缓存或复用旧 feedback receipt | 每次 Agent delivery 必须创建新 exposure |
| 用 `used_by_model` 或声称模型实际使用了记忆 | 系统只能证明已注入/已交付，因此改为 `injected` |
| feedback/exposure 失败导致 recall 或 Agent 失败 | 必须返回 `feedback_state=degraded` 并继续 |
| 从“无异常”自动推断 helpful | 只接受明确用户/host/可靠 Episode outcome |
| 四类记忆、Multi-view、Mental Model、Policy 在 v0.19 实施 | 必须各自通过独立 RFC 和量化证据 |
| 为四类产品记忆新增四张表 | 尚无分类与任务收益证据 |
| 修改既有 migration 001–034 | 历史 migration 不可变；需要列改名时只新增下一顺序 migration |
| 把 namespace 宣称为多租户安全边界 | 本版只做单租户相关性软标签 |
| 引入 RBAC、配额、计费、按租户密钥 | 属于独立安全/平台项目 |
| 更换 embedding 模型、引入 ANN/向量数据库 | 本轮变量是 projection 与消费契约，不能混入基础设施替换 |
| PostgreSQL、分布式 Worker、远程托管存储 | 与本地优先和当前规模无关 |
| 全面重调 FTS/Dense/RRF/reranker 权重 | 只做由投影变化触发且有标注证据的必要校准 |
| 管理 UI 或可视化策略编辑器 | CLI、JSON 报告、API 与审计足以验收 v0.19 |

## 完成定义

v0.19 完成不是“字段已经加上”，而是：

- Hit@5/Recall@5 语义正确，baseline、查询集、生产配置与 gate 已冻结；
- `answerable` 经过 legacy 对照评测，生产默认严格服从 gate 结果；
- Canonical Claim 继续使用字符串 value，除 2 条定点修复外 `value_json` 不迁移；
- FTS、dense、reranker、REST/MCP/Hermes 的 Claim 文本统一来自 `index_text`；
- 迁移可停机、可验证、可回滚，current/historical 不混合投影；
- Context Packet 使用已确认的扁平 schema，数组顺序就是 rank；
- 每次 Agent 注入创建新 receipt，缓存不复用 feedback ID；exposure 失败只降级 feedback，`injected` 标记失败只进入内部重试；
- Hermes 能结构化预取、确定性渲染并记录 `injected`，不声称模型真实使用；
- backup/restore、JSONL import、幂等、CRLF、启动脚本和 namespace 均通过故障与负向测试；
- 四类记忆、多视图、Mental Model、Policy、typed JSON object/array 保持在独立 RFC 队列中。
