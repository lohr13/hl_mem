# v1.1.4 中文评测回归独立诊断

日期：2026-09-04

范围：只读调查；未修改 `src/`、`tests/` 或评测代码

比较对象：`v1.1.3`、`v1.1.4`，以及用户提供的 v0.26.0 MemDaily 基线

## 1. 结论摘要

### 1.1 最终判断

1. **E2E 的真实语义回归与 `e29275e` 引入的 extraction budget 变更包高度相关，但现有证据不能证明 4 个丢失语义是 `cap_extraction_claims()` 实际截掉的。** 该提交同时改变了提示词的数量目标和粒度、把 schema 上限从 30 改为 16、加入确定性 cap，并删除了 count-overflow split / soft-split / delta-repair。现有报告只保存 cap 后的 `extracted_claims`，没有 cap 前 raw count 或持久化的 `overflow_truncated` 事件。因此，证据支持的是“**预算提示词驱动的少提取/合并，加上失去补提取路径**”这一提交级因果包；把根因进一步收窄成“硬 cap 函数实际触发并截掉这 4 条”属于过度归因。

2. **E2E 的净 -10pp 可拆成：4 个真实的 answer-bearing 语义缺失（毛损失 -10pp）+ 1 个确定性 rubric 假阴性（毛损失 -2.5pp）+ 1 个新提示词带来的真实恢复（+2.5pp）。** 即 5 个“对→错”和 1 个“错→对”，净少 4/40。4 个真实语义缺失均表现为检索包中没有足够信息让 reader 作答；但 Hermes 所称“4 条 R@5 正常”不成立，实际只有 2 条 R@5=1，另 2 条 R@5=0。

3. **MemDaily 180 的 `noisy`、`post_processing`、`conditional` 掉分不是同一 extraction-budget 根因。** 三类 90 个 case 的 180 个 gold event 全部至少关联一个 stored claim；31 个错题的 answer-bearing 原始事实也都在库中。31 个错题全部进入 `我 → person:user` 的强制实体作用域，返回项全是“用户将（要）参加……”plan claim，活动地点/规模/时间等 claim 在 FTS/dense 读取时已被 pre-RRF SQL 作用域排除。所谓“plan claim 霸榜”是表象：它们不是在排序中打败 gold，而是过滤后几乎成了仅存候选。

4. **`extraction_coverage=1.0` 没报警是指标定义所致。** 它只检查每个 gold event 是否存在任意一条 `evidence_links`，不校验 claim 数量、语义原子或是否包含问题所需关系。`2ce...` 从 61 降到 26、某 gold event 从 10 条降到 4 条，仍然是 1/1 covered。

5. **12/16 没有足以支撑中文全链路质量的配套实验。** 依据是 post-admission“每 Event claim 数”的历史分布和 5 个 GLM 合成 smoke；smoke 中模型从未超过 16，且没有执行计划中的 LongMemEval。机械单测验证了“如何截”，没有验证“截后仍可答”。更严重的是，同版 Qwen 全 40 candidate 已经得到 0.85、`gate.passed=false`，实际验收信号在发布前已经出现。

### 1.2 与 Hermes 初步结论的差异

| Hermes 说法 | 独立核查 |
| --- | --- |
| 根因就是 deterministic hard cap | **证据不足以收窄到 hard cap 实际触发。** 高概率根因是 `e29275e` 整体，包括 12/16 提示词、粗粒度化和取消补提取路径。 |
| 5 个翻转中 4 个“R@5 正常 → 信息不足” | 4 个信息不足属实；其中只有 2 个 R@5=1，另 2 个 R@5=0。且 R@5 只看 event provenance，不代表 answer-bearing 语义存在。 |
| PerLTQA 4 persona 187→136 | 新版 136 可复算；旧版保留的 3 个完整 DB 已经合计 187，第四个旧 DB 当前为 0 且 manifest 缺失。**旧总数 187 无法从现存产物复现为“4 persona 总数”**，所以 -27% 不应作为精确证据。关键 case 61→26 可直接复现。 |
| MemDaily 掉分与提取量下降同源 | **反驳。** 目标三类全部 gold event 已提取，错题需要的事实也在库中；主因是 `entity_constraint_mode=enforce` 的第一人称硬过滤。 |
| noisy 是 plan 排序霸榜 | **机制不准确。** plan claim 是唯一/少数通过实体 SQL 作用域的候选，gold 活动属性在 RRF 前已被过滤。 |
| b5bc95d 与 budget 直接冲突 | 存在“软完整性要求 vs 硬容量”的执行冲突，但不是代码级互斥；而 3 个典型缺失分别是关系、发现意义、方法作用，并不都属于 b5 的“个人观点/感受/行为原因”范围。 |

## 2. `e29275e` 与 v1.1.3 的实际差异

### 2.1 版本边界

- `e29275e38df2c9856d96c8e17d6ffc69b1869afe`：`fix: bound extraction claims without retry storms`，2026-09-02。
- `git merge-base --is-ancestor e29275e v1.1.3` 返回非零；对 `v1.1.4` 返回 0。即该变更不在 v1.1.3，存在于 v1.1.4。
- 后续 `b0649168ff011c34b1d50235266f4ab694ec25aa` 又微调了缺失/非法 notability 的排序行为。

### 2.2 hard cap 的精确行为

当前 v1.1.4：

- `ORDINARY_CLAIM_TARGET=12`、`MAX_CLAIMS_PER_CHUNK=16`：`src/hl_mem/ingest/extraction/schema.py:14-15`。
- compact schema 的 `claims.max_length` 直接引用 16：`src/hl_mem/ingest/extraction/schema.py:147-152`。
- cap 在 JSON parse 和 deterministic repair 后、Pydantic validation 前运行：`src/hl_mem/ingest/extraction/orchestrator.py:252-265`。
- 只有 `claims` 是 list 且 `len(claims)>limit` 才截；`<=16` 完全不变，非 list 留给后续 validation，非正 limit 抛错：`src/hl_mem/ingest/extraction/parsing.py:47-61`。
- 排序键为：recognized raw `notability`（high=3、medium=2、low=1）降序，再按有限数值 `confidence` 降序，最后按原始 index 升序稳定打破平局：`src/hl_mem/ingest/extraction/parsing.py:38-44,62-67`。
- `e29275e` 提交当时，缺失/非法 notability 会回退到 raw `importance`；`b064916` 改为优先级 0，只再看 confidence。对应回归测试在 `tests/unit/test_extraction_chunking.py:118-140`。
- **没有任何语义豁免**：归因、关系、原因、作用、数字、gold source unit 都不参与排序；超过 16 个 high 时也只按 confidence/index 留前 16。
- 截断时会调用 `current_audit().emit("extract", "claim_budget", "overflow_truncated", ...)`，记录 generated/retained/dropped 和 chunk 坐标：`src/hl_mem/ingest/extraction/orchestrator.py:267-279`。但评测 runner 未绑定 extraction audit logger；默认是 `NullAuditLogger`（`src/hl_mem/observability/audit.py:295-299`）。提供的 a2f 新 DB 和 noisy-0 DB 的 `audit_log` 中 `phase='extract' AND action='claim_budget'` 均为 0 条，所以产物无法回答 hard cap 是否触发。

### 2.3 提示词和恢复路径也是同一提交的一部分

`e29275e` 不只是加一个末端 cap：

- v1.1.3 prompt 要求“Coverage first”，称密集输入常有 12–30 条，并以 30 为上限；历史位置为 `v1.1.3:src/hl_mem/ingest/extraction/prompts.py:215-218`。
- v1.1.4 改为 context-rich/coarser memory，并写明“通常不超过 12、最多 16，先按 notability 再按 confidence 排序”：`src/hl_mem/ingest/extraction/prompts.py:111-114`。
- 同一 prompt 又说 low 不是丢弃、只要有证据就必须进入 claims：`src/hl_mem/ingest/extraction/prompts.py:78-82`。当可答信息超过 16 个时，这与 hard maximum 是不可同时满足的契约。
- v1.1.3 schema 是 `max_length=30`：`v1.1.3:src/hl_mem/ingest/extraction/schema.py:149`；不存在 `ORDINARY_CLAIM_TARGET`、`MAX_CLAIMS_PER_CHUNK` 或 `cap_extraction_claims()`。
- v1.1.3 对 exact-30 saturation 有 soft split / delta repair，对 schema count overflow 有递归 bisect；历史位置为 `v1.1.3:src/hl_mem/ingest/extraction/orchestrator.py:175-218,266-351,373-416,494`。`e29275e` 删除这些 count-driven coverage calls，只保留真正 output-token truncation 的 bisect。

因此应区分两个机制：

1. **模型侧预算效应**：模型看到 12/16 和“合并同主题”，直接少输出或把多条关系压成宽泛 claim。此时 hard cap 不会触发。
2. **应用侧截断效应**：模型仍输出 >16，程序按 generic notability/confidence/index 截断。现有报告没有 raw count，无法区分 1 和 2。

## 3. 12/16 是怎么定的，证据够不够

### 3.1 设计依据

设计文档给出的历史库统计是 4,678 个有 claim 的 Events，post-admission claim 数分布 P50=3、P90=10、P95=13、P99=19（`docs/superpowers/specs/2026-09-02-extraction-claim-budget-design.md:27-29`）。模拟截断为：

| limit | 受影响 Events | 比例 | 丢 high | 丢 medium | 丢 low |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 12 | 272 | 5.81% | 48 | 443 | 943 |
| 16 | 126 | 2.69% | 21 | 226 | 365 |
| 20 | 13 | 0.28% | 7 | 146 | 121 |

来源：`docs/superpowers/specs/2026-09-02-extraction-claim-budget-design.md:31-40`。文档自己的结论是 12 太紧，不应作为 failure boundary，16 作为 buffer；并明确这些数字只是 directional、不是 benchmark。外部实现也没有 universal per-input fact limit：同文档 `:42-46`。

这份标定还有两个单位不一致：

- 统计对象是 **post-admission、per Event** 的已存 claim；实际 cap 对象是 **pre-validation、per Chunk** 的 raw claim。一个 chunk 可包含多个 Event，且 admission/merge 会在 cap 后继续减少数量。
- 模拟以历史 persisted importance/confidence 排序；运行时以模型 raw notability/confidence 排序。二者不能直接当作同一风险分布。

因此 2.69% 不是“实际 chunk 只会有 2.69% 被截”的可靠估计；而模拟本身已预告 limit=16 会丢 21 条 high 和 226 条 medium。

### 3.2 配套实验实际覆盖了什么

- 计划要求 5 个合成 smoke，并在可用时补一个小 LongMemEval slice：`docs/superpowers/specs/2026-09-02-extraction-claim-budget-design.md:156-172`。
- 实际只跑了 Zhipu `glm-5.3-flash`、low reasoning、5 个合成 case；dense case 只生成/保留 10/10：`docs/research/2026-09-02-extraction-claim-budget-smoke.md:18-25`。
- 文档明确：模型没有超过 16，17/30 截断的验收证据仅来自 deterministic tests；该 smoke 不是一般 benchmark 或跨对话语义质量证明：同文档 `:32-39`。
- 没有 LongMemEval：同文档 `:41-43`。
- 单测验证的是 schema `maxItems=16`、排序、17/30 一次调用截到 16、非法字段重试和 audit：`tests/unit/test_ingest_schemas.py:123`、`tests/unit/test_extraction_chunking.py:76-150,335-423`；prompt 测试只断言固定文案存在：`tests/unit/test_extraction_prompt_quality.py:23-32`。

### 3.3 发布前已有反证

`var/eval/v114/candidate/full40/qwen37/run1/report.json` 已记录：

- `metrics.overall.qa_accuracy=0.85`（34/40）、R@5=0.9375；
- `gate.passed=false`；
- `gate.failures` 同时包含 overall 0.85 < 0.90、PerLTQA 0.8214 < 0.85、MemDaily noisy R@5 0.25 < 0.5。

而同版设计规定 worst fresh run 至少 36/40，且要按 extraction/retrieval/QA 分层归因：`docs/superpowers/specs/2026-09-03-extraction-quality-plan-ttl-design.md:219-237`。这说明 12/16 不仅缺少正向质量证明，candidate 全量评测还已给出失败信号。

结论：**12/16 是面向成本、retry storm 和记忆粗粒度的工程启发式，不是由 answer-quality 曲线选出的质量最优点。**

## 4. E2E -10pp 逐案归因

### 4.1 比较口径

两个报告均为 `scorer_version="deterministic-rubric-v2"`，models 都是 extractor/QA `qwen3.7-plus`、embedding `qwen3.7-text-embedding`、reranker `qwen3-rerank`：

- v1.1.3：`var/eval/chinese_e2e_report.json::{scorer_version,run.models,metrics.overall}`，accuracy 0.925、R@5 0.9625。
- v1.1.4：`var/eval/v114_e2e40_20260904/report.json::{scorer_version,run.models,metrics.overall}`，accuracy 0.825、R@5 0.9125。

`git diff v1.1.3..v1.1.4 -- tests/eval/chinese_e2e.py evaluation/tools/run_memdaily_benchmark.py` 中，E2E 只有 gate 对 `None` 的处理变化；MemDaily 只有 QA transport 注入/API-key override，没有修改 reader prompt 或判分逻辑。故“reader prompt 变化/判分代码变化”不能解释版本差异。

### 4.2 五个“对→错”

报告字段路径均为 `cases[case_id=...].{question,ingest,retrieval,retrieved,qa,gold_extraction_units,covered_extraction_units}`。

| case | v1.1.3 → v1.1.4 | 证据链 | 归因 |
| --- | --- | --- | --- |
| `perltqa:0709ec234e33:events:4a3607094e6c` | 正确“电影制作技巧、艺术表达能力、职业规划和人生发展” → “信息不足”；R@5 1→1、MRR 1→1 | 旧 top-5 有“常常向张丽请教”“张丽帮助提高电影制作技巧和艺术表达能力”“指导职业规划和人生发展”；新 top-5 只剩导师/指导关系及徐佳自己的电影兴趣，无被请教内容 | **真实语义缺失。** gold event 仍有其他 claim，所以 provenance R@5 和 extraction coverage 均为 1。 |
| `perltqa:0709ec234e33:dialogues:7336d023b16e` | 正确“对电影故事情节着迷” → 被判错的“因对电影《那些年我们一起追的女孩》的故事情节着迷”；R@5/MRR 均为 1 | 新 top-1 就是完整原因 claim，reader 答案语义正确。rubric 只接受连续子串如“对电影故事情节着迷”；片名插入使 substring 不匹配 | **确定性 scorer 假阴性，不是提取/检索回归。** scorer 实现在 `tests/eval/chinese_e2e.py:714-756`。 |
| `perltqa:2ceebb337754:social_relationship:dc0a4055bb49` | “杨晓” → “信息不足”；R@5 1→0、MRR 1→0.125 | 旧 top-1“杨晓与许慧经常合作完成项目”；新库仍有“杨晓是一起工作的年轻研究员/关系是同事”，但没有“经常合作完成项目”，新 top-5 全是王明/亚马逊内容 | **真实关系 claim 缺失，随后检索 miss。** |
| `perltqa:2ceebb337754:events:2825172d6952` | “媒体传播对社会观念的影响” → “信息不足”；R@5 1→0、MRR 1→0.1 | 旧 top-1 为“许慧的发现关于媒体传播对社会观念的影响具有重要意义”；新库无该意义 claim，top-5 是癌症染色体/亚马逊内容 | **真实意义 claim 缺失，随后检索 miss。** |
| `perltqa:2ceebb337754:events:5e2fae77f0b5` | 正确作用 → “信息不足”；R@5 1→1、MRR 1→0.5 | 新库/检索仍有“王明提出媒体内容分析新方法”，但没有“可更全面了解媒体塑造公众意识形态的作用” | **真实作用语义缺失。** R@5=1 仅因同一 gold event 的“提出方法”claim 命中，不能回答“有什么作用”。 |

R@5 的实现只查 top-k claim 的 `evidence_event_ids` 是否与 gold event 相交：`tests/eval/chinese_e2e.py:693-710`。它不判断 claim 是否包含问题需要的谓词/宾语，所以“R@5 正常”不能反驳提取层缺失。

### 4.3 一个“错→对”与净变化

`perltqa:0709ec234e33:dialogues:77cdfec7b17e` 从旧版“信息不足”变为新版正确“电影”；新版 top-1 新增“陈刚对电影拍摄有很多见解”。这与 `b5bc95d` 强调显式归因语义/关系保留的方向一致，是一个真实 +2.5pp 恢复。

所以计数为：

- 4 个真实 answer-bearing 语义缺失：-10pp；
- 1 个 rubric 假阴性：-2.5pp；
- 1 个真实恢复：+2.5pp；
- 净值：-10pp。

### 4.4 claim 数和调用证据

对 `perltqa:2ceebb337754` 的旧/新 cache DB 直接查询：

- active claims：61 → 26；
- 8 个 event 的 claim 数：旧 `[18,3,13,2,10,9,3,3]`，新 `[6,2,6,2,4,3,1,2]`（按 evidence_id 排序）；
- 含两个 events 问题的 `aa0e402...`：10 → 4；含合作关系的 `485df691...`：3 → 2；
- `llm_call_spans`：12 个成功 extract / 9,056 output tokens → 10 个 / 4,093 tokens。

旧库中三个后来丢失的 claim 都是 persisted `importance=0.6, confidence=1.0`：

- “杨晓与许慧经常合作完成项目”；
- “许慧的发现关于媒体传播对社会观念的影响具有重要意义”；
- “王明的新方法可以更全面地了解媒体在塑造公众意识形态方面的作用”。

这证明输出收缩与 answer-bearing 语义丢失同时发生，也符合 `e29275e` 的行为目标；但它仍不能证明某一次 raw response 超过 16 并由 cap 删除了恰好这些条目。报告的 `ingest` 只有 cap 后 `extracted_claims/stored_claims`；case DB 没有 raw response，评测 audit 也未记录 cap 事件。

同理，“旧 Qwen 每 chunk 实际产出 30–60 条”无法由当前提供的 E2E 产物验证。a2f 旧库只有 cap 后/merge 后 61 条和 12 次成功调用，没有每个 root chunk 的 raw count；不能把 persona 总量或最终 DB 总量当成单 chunk raw output。

## 5. MemDaily 180：独立的实体作用域故障

### 5.1 报告层结果

`var/eval/v114_memdaily180_20260904/report.json::metrics.by_type`：

| qtype | accuracy | R@5 | 错题数 |
| --- | ---: | ---: | ---: |
| `conditional` | 0.8667 | 0.7167 | 4/30 |
| `post_processing` | 0.7667 | 0.8500 | 7/30 |
| `noisy` | 0.3333 | 0.2667 | 20/30 |

三类合计 31 个错误，正好是本节分析的全部失败；全报告另外还有 3 个 aggregative 错误。

### 5.2 逐类型 DB 核查

对报告每个 case 的 `ingest.cache_manifest` 替换为同名 `.db`，用下列条件核查 gold event：

```sql
SELECT DISTINCT evidence_id
FROM evidence_links
WHERE derived_type='claim'
  AND evidence_type='event'
  AND evidence_id IN (...gold_event_ids...);
```

再对同一 DB 执行 `plan_query_entity(connection, question, namespace, "enforce")`。结果：

| qtype | gold event 被任意 claim 覆盖 | entity scope | wide scope | scope 与正确性 |
| --- | ---: | ---: | ---: | --- |
| conditional | 60/60；30/30 case 完整 | 16 | 14 | entity：12 对/4 错；wide：14 对/0 错 |
| post_processing | 60/60；30/30 case 完整 | 7 | 23 | entity：0 对/7 错；wide：23 对/0 错 |
| noisy | 60/60；30/30 case 完整 | 30 | 0 | entity：10 对/20 错 |

内容核查还显示：

- conditional 的 4 个错题，正确答案文本都存在于 gold-linked stored claim；
- noisy 的 20 个错题，正确答案文本都存在于 gold-linked stored claim；
- post_processing 的 7 个错题，所需原始时间/地点事实全部存在。gold answer 是相对时间转绝对日期或城市转描述等后处理结果，所以不会逐字出现在原始 claim，但其输入事实完整；
- 31 个错题合计 40 个 retrieved items，全部是“用户将（要）参加……”plan claim；其中 36 个报告为 `entities=["user"]`，4 个 `entities=[]`，但后者仍以 `subject_canonical_entity_id=person:user` 通过 SQL scope。conditional 错题分别只返回 1 或 4 条，post_processing 全部只返回 1 条，noisy 19 题只返回 1 条、1 题返回 4 条。

这排除了“提取量下降导致这些题没有事实”的解释。

### 5.3 机制链

1. 当前默认 `recall.entity_constraint_mode="enforce"`：`src/hl_mem/config/models.py:285`、`docs/configuration.md:389`；本次 `hl_mem.toml` 的 `[recall]` 没有覆盖该键。
2. `resolve_query_entity()` 对 query 中全部 active alias 做 substring span 匹配；唯一实体且链接完整时给出 high confidence/filter ID：`src/hl_mem/recall/entity_query.py:145-240`。
3. `plan_query_entity()` 在 enforce 下把该 ID 变成 `scope_mode="entity"`，并从搜索 query 中删去 alias：`src/hl_mem/recall/entity_query.py:243-265`。
4. RecallService 把 scope mode/ID 传入 claim pipeline：`src/hl_mem/application/recall.py:419-448`。
5. FTS 和 dense 都直接以该 entity ID 读取：`src/hl_mem/recall/candidate_channels.py:46-84`。
6. storage SQL 只允许 subject canonical ID、target canonical ID 或 `claim_entity_links` 命中该实体的 claim：`src/hl_mem/storage/claim_search.py:36-50`。

MemDaily 中“我”被解析为 `person:user`。参加活动的 plan claim 关联 user；活动自己的地点/规模/时间 claim 没有关联 user。因此 property claim 不是低分，而是在 RRF 前不可见。

典型 `memdaily:noisy:events:0`：

- 问题真正问“规模是三千人的活动，地点是什么？”，噪声中另有“差点找不到我的朋友”；
- DB 有“金融科技精英论坛的地点是北京”和“规模是三千人”，也有“用户将要参加金融科技精英论坛”；
- `plan_query_entity` 因“我”进入 entity scope；报告 `cases[case_id=memdaily:noisy:events:0].retrieved` 最终只剩 plan claim，R@5=0，reader 只能失败。

`6b0acd54bf447b1a528b586a5e536e616735268a`（`feat: execute exact-entity recall plans`，2026-09-01）引入了 exact-entity pre-RRF 执行；`git tag --contains 6b0acd54` 从 v1.1.0 起包含 v1.1.0–v1.1.4。故它可以解释相对 v0.26.0 基线的当前失败形态，却不是 v1.1.3→v1.1.4 的 extraction-budget 变更。由于缺少 v0.26.0 的逐 case report/DB，不能把历史 -16pp 的每一分都严格归给该 commit；可以严格说的是：**v1.1.4 当前三类 31 个失败均呈现这一实体硬过滤机制。**

## 6. b5bc95d 与 budget 是否冲突

### 6.1 有执行层冲突，但没有语义优先级实现

`b5bc95d47566def3de7420f832d83593703025ef` 只改 prompt 和 prompt contract tests。当前中文规则要求保留显式归因的观点、信念、理解、感受、行为原因和实践原则：`src/hl_mem/ingest/extraction/prompts.py:46`；相应测试只断言这段固定文本出现一次：`tests/unit/test_extraction_prompt_quality.py:64-78`。

它没有：

- 增加 protected/semantic-priority schema 字段；
- 提升这类 claim 的 notability；
- 为关系、作用、原因或归因内容预留 quota；
- 改动 `cap_extraction_claims()` 排序键；
- 在 cap 后做 coverage repair。

所以 prompt 一边说“必须保留”，另一边要求 <=16，并让模型自己给 notability。容量冲突发生时，hard cap 完全不知道哪条是 b5 想保护的个人语义。这是**政策意图与执行机制不闭环**，而不是两个函数直接互斥。

此外，把三个 a2f 缺失都称为“b5 归因型”并不准确：合作完成项目是关系，发现的意义和方法的作用是客观关系/功能；它们不都属于“个人观点/感受/行为原因”。b5 只能部分覆盖这一更广的 answer-bearing relation 类别。

### 6.2 smoke 不能证明冲突已解决

`var/eval/v114/candidate/smoke/qwen3.7-plus-candidate.json` 的 `summary` 是 7/8、target coverage 0.9091。`cases[id=attributed_viewpoint_and_speaker]` 生成/保留 3/3、未触发 cap，却被标记 missing target 1。产出的三条分别保留了自由观点、边界、反思/成长；fixture 要求“边界”和“反思/成长”出现在同一 claim（`tests/eval/fixtures/extraction_quality_smoke_v1.json:13-16`），因此这更像 scorer 粒度不匹配，而不是语义真的丢失。

它说明两件事：b5 可以在低 claim 数下保留归因内容；同时该 smoke 没有覆盖“归因内容与 >16 候选竞争”这一真正的预算冲突。

## 7. `extraction_coverage` 为什么没有报警

实现非常直接：

- `covered_gold_events()` 只查 `evidence_links` 是否存在该 `evidence_id`，docstring 也是“produced at least one stored claim evidence link”：`tests/eval/chinese_e2e.py:834-847`。
- 聚合器只计算 unique covered event IDs / unique gold event IDs：`tests/eval/chinese_e2e.py:872-884`。
- 合同测试明确要求“看全部 stored evidence，不只 retrieved claims”，且一个 claim 链接足以覆盖一个 event：`tests/eval/test_chinese_e2e_contract.py:245-256`。

因此用户提出的说法成立，并可进一步精确化：`gold_extraction_units` 不是“应提取语义单元”，而是 gold **event/source unit ID**。它检测的是零提取，不检测欠提取。

例子：

- `cases[case_id=perltqa:2ceebb337754:events:5e2fae77f0b5]` 的 `gold_extraction_units` 和 `covered_extraction_units` 都是同一个 `aa0e402...` event；该 event 新版仍有 4 条 claim，所以 coverage=1，即使“新方法的作用”和“发现的意义”都不在库中。
- a2f 的 8 个 event 新版每个仍有 1–6 条 claim，所以整体 coverage 仍为 100%，尽管总 claim 61→26。

## 8. 修复方向建议（不实施）

以下将 E2E 提取问题和 MemDaily 检索问题分开，避免用一个开关同时治疗两个根因。

| 优先级/方向 | 复杂度 | 性价比 | 合理性与约束 |
| --- | --- | --- | --- |
| **P0：先补可归因 A/B 与 cap 遥测**。同一模型、同一输入、同一 chunking，分别运行旧 prompt、12/16 prompt、放宽 prompt；报告每 chunk generated/retained/dropped、post-admission 数和 answer-bearing miss | 低到中 | 极高；能防止误修 hard cap 而真正问题是模型侧少输出 | 当前产物无法区分 prompt under-production 与 hard truncation。评测必须绑定 audit logger或直接把安全计数写进 case report；不保存 claim 文本也可满足隐私约束。 |
| **P0：MemDaily 第一人称查询 fail-wide 或 scoped+wide union**。pronoun-only alias 不作为 hard entity scope，或同时保留 wide channel | 中 | 极高；直接覆盖当前三类 31 个失败的共同机制 | “我参加的活动的地点”是二跳关系查询，不是只搜 user 属性。最安全的短期策略是 pronoun-only/残余关系词场景退化为 wide；union 需控制候选预算和重复。 |
| **P1：放宽固定档位**。先试 ordinary 20 / hard 30，或至少 hard 20；保留“count overflow 不重试”以避免 retry storm | 低 | 高，最快验证 E2E 语义恢复 | 设计自己的模拟显示 hard 20 只影响 0.28% Events，远低于 16 的 2.69%；但应以 per-chunk raw A/B 重新标定。仅提高 hard cap 若模型已被 prompt 压到 <=12，收益有限，因此 ordinary target 和粒度文案必须一起实验。 |
| **P1：动态预算**。按 source-unit 数、输入 token/字符数、枚举/实体/关系密度设 ordinary target，并保留全局 hard safety ceiling | 中 | 中高；更符合短消息与密集 persona 的差异 | 单纯长度是弱代理，必须同时考虑多 Event 和 independently-answerable relations。优点是短 MemDaily 不增成本，长 PerLTQA 获得容量；仍需安全上限和 cap rate 监控。 |
| **P1：语义/关系保留 quota**。给显式关系、原因、作用、归因观点、数字约束、每 source unit 的首个 claim保留最低配额 | 中到高 | 中；能针对当前丢失类型，但容易形成不断增长的例外表 | prompt-only“豁免”不可靠；若要硬保证，需要可验证 priority signal 或 deterministic source coverage。比只保护“归因型”更合理的是保护 answer-bearing relation/attribute，并避免把身份/爱好天然置于所有一般事实之上。 |
| **P2：按 source unit 做 completeness-aware second pass**。cap 后若某 Event 无覆盖或高密度 Event 的关系/属性明显收缩，做有界补提取；或先分 chunk 再 cap | 高 | 中；质量上限最高，成本也最高 | v1.1.3 的无限/递归重试不可直接恢复。可限制为一次、只处理未覆盖/高密度 source units，并在 merge 后施加总预算，兼顾成本和完整性。 |
| **P1：升级 extraction coverage 指标**。除 event coverage 外，增加 claim-count retention、gold semantic atom/required relation coverage、cap rate | 中 | 高；会让同类回归在发布门禁中显性化 | event-level coverage 应保留用于检测零提取，但不得继续作为完整性代理。至少报告 per-event claim-count delta 和问题目标谓词是否存在。 |
| **P2：修复 E2E deterministic rubric 的插入词假阴性** | 低 | 中；修复本次 1/40 的假掉分 | 可把 required concept 拆为顺序无关的“故事情节”+“着迷”，或允许实体插入；仍保持 deterministic，避免引入另一个 LLM judge。 |
| **P2：实体关系桥接**。将 `user --参加--> 活动` 与活动属性形成可扩展关系，先解析活动再检索其属性 | 高 | 长期高 | 这是比 fail-wide 更精确的长期方案，但需要可靠活动实体、关系边和二跳预算；不适合作为本次快速回归修复。 |

推荐实施顺序是：**先让 cap 是否触发可观测；并行修 MemDaily pronoun-only hard scope；然后用固定 E2E A/B 选择 20/30 或动态预算，而不是直接为“归因型”打一个不可验证的 prompt 补丁。**

## 9. 证据索引与复现说明

### Git / source

- `git show e29275e`
- `git show b064916`
- `git show b5bc95d`
- `git diff v1.1.3..v1.1.4 -- tests/eval/chinese_e2e.py evaluation/tools/run_memdaily_benchmark.py`
- `git tag --contains 6b0acd54`

### 报告字段

- E2E：`var/eval/chinese_e2e_report.json::{run,metrics.overall,cases}`
- E2E v1.1.4：`var/eval/v114_e2e40_20260904/report.json::{run,metrics.overall,cases}`
- v1.1.4 candidate gate：`var/eval/v114/candidate/full40/qwen37/run1/report.json::{metrics.overall,gate}`
- Qwen extraction smoke：`var/eval/v114/candidate/smoke/qwen3.7-plus-candidate.json::{summary,cases[id=attributed_viewpoint_and_speaker]}`
- MemDaily：`var/eval/v114_memdaily180_20260904/report.json::{run,metrics.by_type,cases}`
- PerLTQA 纯检索：`var/eval/v114_perltqa378_20260904/report.json::metrics.overall`（R@5 0.9683、MRR 0.8284），支持“全局检索引擎未普遍退化”。

### DB 核查

- E2E 关键旧/新 DB：
  - `var/eval/chinese_e2e_cache/perltqa/a2f3347cadf5c19ae82c5b1c.db`
  - `var/eval/v114_e2e40_20260904/cache/perltqa/a2f3347cadf5c19ae82c5b1c.db`
- MemDaily case DB：报告中 `cases[*].ingest.cache_manifest` 同名 `.db`，位于 `var/benchmark_memdaily/`。
- 本调查只执行 SELECT；未修改评测 DB、源码或测试。

## 10. 诊断置信度

- E2E 4 个真实语义缺失：**高**。
- `e29275e` 整体导致 E2E 提取收缩：**高**（时间边界、行为方向、claim/token/call 同步收缩、逐案语义均一致）。
- `cap_extraction_claims()` 在这 4 个 case 中实际触发并删除目标 claim：**未知/未证实**（缺 raw count 与 cap audit）。
- E2E 1 个 scorer 假阴性：**高**。
- MemDaily 三类主因是第一人称实体硬过滤：**高**（90/90 DB、31/31 错题、代码路径和候选实体一致）。
- 该实体过滤解释 v0.26.0→v1.1.4 全部 -16pp：**中**（当前失败机制明确，但缺旧版逐 case artifact，不能做严格 counterfactual）。
