# 外部代码评审建议评估（建议 3–8）

评估日期：2026-07-24  
评估范围：`src/hl_mem/recall/staged_pipeline.py`、`src/hl_mem/storage/claims.py`、
`src/hl_mem/recall/ranking.py`、`src/hl_mem/application/recall.py`、`src/hl_mem/settings.py` 与 `tests/`

## 结论摘要

六条建议的方向大多合理，但紧迫性不能脱离当前部署形态。对只有 522 个 active claims、单用户、
单机 SQLite、solo 维护的 HL-Mem 来说，当前最高优先级不是替换向量后端，而是：

1. 冻结新的召回排序因子，用已有离线评测框架补齐排序指标并做 A/B；
2. 消除召回管线内部创建 `Settings()` 所形成的第二配置入口；
3. 在同一次低风险重构中收拢 `hybrid_claims()` 参数，并把动态 state 改成类型化上下文。

向量暴力扫描确实是长期规模上限，但在 522 条数据上不是现实瓶颈。现在应做基准和触发条件，不应仅按
claim 数量预先承诺 `sqlite-vec -> pgvector` 的固定迁移路线。

另有两项事实校正：

- `hybrid_claims()` 和 `_collect_candidates()` 当前各有 **20 个参数**，不是 21 个；即便如此，数量仍明显过多。
- `pytest --collect-only -q` 当前收集到 **285 tests**，不是背景中的 284。测试数变化不影响“数量不能证明召回质量”
  的核心判断。

## 证据概览

### 召回管线

`staged_pipeline.py` 共 542 行。`_collect_candidates()` 返回的初始字典有 28 个键；
`_filter_and_score()` 再加入 5 个键，`_rerank()` 初始化 5 个键并另加 `outcome`。整个生命周期实际涉及
**39 个不同 state 字段**。这些字段跨 `_collect_candidates -> _filter_and_score -> _expand_related ->
_rerank -> _finalize` 原地扩充和修改。

生产调用入口集中在 `RecallService.recall()`，但单元测试也直接调用 `hybrid_claims()`。应用服务已经持有
`self.settings` 并显式传入 7 个召回配置值；管线内部仍执行 `Settings()` 作为缺省回退。

### 向量检索

`ClaimRepository.search_claims_vector()` 调用 `list_embedded()`，后者从 SQLite 取出指定 namespace 和时间窗口内
所有带 embedding 的可见 claim。随后 Python 对每条记录调用 `cosine_similarity()`，全量排序后才切片到
`limit`。因此 `limit` 只限制返回数量，不限制扫描或排序成本。

### 实际排序链

当前链路应准确描述为：

1. FTS、dense，以及默认关闭的 tag channel 各自产生有序候选；
2. weighted RRF 把通道名次融合成 `semantic` 特征；
3. `memory_score()` 加权 semantic 0.65、recency 0.08、access frequency 0.07、confidence 0.075、
   importance 0.075、utility/helpful rate 0.05；
4. 可选地叠加 tag boost 和 preference recency boost；
5. 可选 reranker 以 0.80 reranker score + 0.20 非语义先验重新排序。

因此外部评审列出的“13 个排序因子”混合了候选通道、融合算法、特征和最终重排阶段。代码中没有独立
`scope` 排序因子，反而有清单遗漏的 `confidence`。不过其“不要继续无评测地叠加因素”的结论是正确的。

### 测试与评测资产

当前测试文件分布为：

| 目录 | `test_*.py` 文件数 | 实际性质 |
|---|---:|---|
| `tests/unit/` | 43 | 纯函数、repository、管线、API/组件边界等单元和轻量组件测试 |
| `tests/integration/` | 4 | extract、conflict、forget、端到端集成 |
| `tests/eval/` | 6 | 数据集 schema、指标、双时间、no-answer、API 合同 |
| `tests/` 根目录 | 1 | `test_e2e_real.py`，真实外部组件路径 |

项目已经有版本化的 50-case `recall_v2.jsonl`、快照构建器和离线 runner，并报告 Recall@5、micro recall、
top-1、no-answer precision/recall、stale/disputed hit rate、evidence correctness、temporal violation 和
mean latency。当前没有 MRR、nDCG@10、p50/p95，也没有清晰独立命名的 behavioral memory scenarios 层。

## 建议 3：用显式 dataclass 替换管线 state 字典

**合理性评分：4/5**

### 当前实际风险

风险真实存在，而且比评审描述略严重：不是单纯“38 个字段”，而是生命周期内 39 个不同键。字段的产生时点
依赖调用顺序，例如 `_expand_related()` 假定 `by_id`、`feature_by_id`、`pre_scores`、`ranked_claims` 已存在，
`_finalize()` 假定 `_rerank()` 已经初始化 `rerank_us`、`reranked`、`valid_reranked`、`rerank_scores`、
`ranked_result` 和 `outcome`。类型检查无法发现键名错误或缺失阶段。

不过这是内部同步流水线，只有固定的五阶段调用链，生产入口单一，现有测试覆盖了主要分支。以 522 claims 和
solo 维护来看，它不是运行时可靠性的紧急事故源，主要是后续继续增加 tag、relation、trace 或排序实验时的
维护成本与回归风险。

### 决策

**计划做，和建议 4、8 合并为一次召回边界重构；不建议单独立即插入一个“大而全”的可变 dataclass。**

### 具体方案

采用少量、按阶段输出划分的 `@dataclass`，避免把字典机械地改写成一个拥有 39 个可选字段的“上帝对象”：

- `RecallRequest`：`query`、`query_blob`、`limit`、`as_of`、`known_as_of`、`intent`、`namespace`、`now`；
- `RecallConfig`：候选、偏好、tag 与 relation 配置，全部为已解析的非 Optional 值；
- `RecallDependencies`：`repo`、`reranker`、`tracer`、`relation_connection`；
- `CandidateCollection`：统一时间快照、candidate limit、query tags、三个通道结果、阶段耗时和起始时间；
- `ScoredCandidates`：候选集合、features、pre-scores、tag boosts、pre-ranked claims；
- `RerankedCandidates`：rerank outcome、耗时、有效结果、分数和最终有序结果。

阶段函数应返回下一阶段对象，不用 `state.update()` 原地追加字段。claim 本身仍可暂时保留
`dict[str, Any]`，因为 repository/API 边界全项目都使用字典；本次不要顺带把整个 Claim 模型类型化。

迁移时先保留 `hybrid_claims()` 薄包装以兼容现有直接调用测试，再逐步让阶段测试构造明确的阶段对象。该方案能
让阶段依赖成为构造器要求，并使 typo 在类型检查或对象构造时暴露。

## 建议 4：拆分 `hybrid_claims()` 的过多参数

**合理性评分：4/5**

### 当前实际风险

实际是 20 个参数，其中前 10 个允许位置传参，后 10 个为 keyword-only。keyword-only 已降低错位风险，
而且生产调用点只有 `RecallService.recall()` 一个，所以当前误调用概率不高。主要问题是配置、依赖、请求数据
混在同一签名中，并完整复制到了 `_collect_candidates()`；每增加一个开关，需要同步修改两层签名、应用服务
和测试。

solo 项目的额外抽象也有成本。若三个对象只是把 20 个字段搬家、阶段内部仍使用动态 state，则收益有限。

### 决策

**计划做，与建议 3、8 同批实施。** 不需要为此单独安排紧急版本，但应在下一个召回功能或排序实验之前完成。

### 具体方案

评审者提出的三分法基本合适，但边界应按现有代码调整：

- `RecallRequest` 只含一次请求变化的数据；
- `RecallConfig` 是从应用层 `Settings` 和 `RelationExpansionConfig` 映射出的完整、冻结配置；
- `RecallDependencies` 含 repository、reranker、tracer、relation connection。

建议新核心入口为：

```python
def hybrid_claims(
    request: RecallRequest,
    dependencies: RecallDependencies,
    config: RecallConfig,
) -> list[dict[str, Any]]:
    ...
```

`RecallService.recall()` 负责构造三者。短期兼容层可保留旧函数签名，但不要长期维护两个公开入口；所有仓内测试
迁移后删除兼容层。`RecallDependencies` 不应变成全局 service locator，也不应包含 embedder，因为
`query_blob` 已在应用层生成。

## 建议 5：向量检索是规模上限瓶颈

**合理性评分：3/5**

### 当前实际风险

瓶颈判断正确：查询会把全部可见 embedding 从 SQLite 解码到 Python，逐条计算余弦并进行全量排序，时间复杂度
约为 O(N·D + N log N)，内存和 SQLite-to-Python 拷贝也随 N 增长。代码中的 100k × 2048 float32 约
819 MB 只计算原始向量体积，还没包括 row、dict、bytes 和排序列表开销。

但当前仅 522 active claims，约占 2048 维 float32 原始向量 4.3 MB；本地单用户并发很低。此时引入 native
SQLite 扩展或 PostgreSQL 会增加安装、备份、迁移、Windows 兼容和运维成本，现实风险低于替换后端的复杂度。

此外，“10k–100k 必须 sqlite-vec，>100k 必须 pgvector”的固定阶梯过于武断。阈值还取决于维度、可见
候选比例、查询频率、延迟目标、硬件、索引召回损失和扩展可部署性。pgvector 也与当前 local-first、SQLite
在线备份、单机零运维目标有明显架构代价。

### 决策

**计划做基准与升级触发条件；暂缓更换向量后端。**

### 具体方案

项目文档已经提出在 2,000 和 10,000 个 2048 维 claim 上记录 warm p50/p95，但仓内尚无可运行 benchmark。
应先实现可重复、非 CI gate 的基准：

- 数据规模：522 实际量级、2k、10k，必要时再加 50k；
- 分开记录 `list_embedded`/解码、cosine、排序以及端到端 recall；
- warm/cold 分开，至少报告 p50/p95、峰值 RSS、数据库大小；
- 固定 Windows/GPU/CPU、Python 版本、维度、namespace、可见率和 limit；
- 外部 reranker 关闭并单独计时，避免网络延迟污染本地向量结果。

升级触发条件应是“实测超出本项目 SLO 或预计一年内逼近上限”，而不是仅看 N。触发后先做技术 spike，
比较当前实现的 top-k heap 优化、sqlite-vec（或同类 SQLite 内嵌方案）的 Windows 分发/备份/重建/召回率；
只有出现多进程服务、远程部署或 SQLite 方案无法满足容量/SLO 时，才评估 pgvector。保持
`search_claims_vector()` repository seam，可避免提前改上层管线。

## 建议 6：Topic Tags 之前应先建立正式评测集

**合理性评分：5/5**

### 当前实际风险

“不要继续增加排序因子”在当前阶段非常合理。semantic 占 0.65，但 tag boost 最多直接加 0.05，偏好场景还可
叠加 `0.12 * recency`；可选 reranker 又用 80/20 重混合。多个启发式项的尺度并不完全同构，单元测试只能证明
某个构造样例会升降序，不能证明真实查询总体改善，也不能发现某类查询改善、另一类退化。

风险在当前 522 条个人数据上不是算力或稳定性，而是“看似相关但实际错误的记忆进入 Agent 上下文”。由于系统
服务于一个真实 Agent，top-k 顺序质量比数据规模更重要。

项目并非没有正式评测集：已有 50-case `recall_v2` 和 Recall@5/top-1 等指标。真正缺口是：

- Recall@5 当前是“top 5 是否命中至少一个”的二值宏平均，不足以评价多个 relevant claims 的排序；
- 没有 reciprocal rank/MRR 和 graded relevance/nDCG@10；
- 没有 tag boost 开/关、tag channel 开/关、reranker 开/关的固定控变量报告；
- 没有真实 embedding/reranker 配置下的版本基线结果。

### 决策

**现在就做：冻结新增排序因子，先补评测与基线。** 这不意味着立即删除现有 tag boost；独立 tag channel 已默认
关闭，应继续关闭直到有证据。默认开启的 soft tag boost 应做 A/B 后决定保留、调权或关闭。

### 具体方案

1. 给现有 case 增加可排序的 relevance judgment。只有二元标签时可先实现 MRR 和 binary nDCG@10；
   若要 graded nDCG，再明确 0/1/2 相关性，而不是从关键词匹配强行推断等级。
2. 固定同一 snapshot、query set、embedding、reranker 和随机条件，逐次只改变一个变量：
   baseline、tag boost、tag channel、reranker。
3. 至少报告 Recall@5、micro recall、MRR、nDCG@10、top-1、no-answer、时间有效性、stale hit 和 p50/p95；
   按 current/historical/preference/tag 类型切片。
4. 保存配置、数据集 hash、snapshot hash 和逐 query 结果，比较 win/tie/loss；不要只看总体均值。
5. 在评测证据前不再加入新的 boost 或权重。50 cases 可作为首个回归基线，但应逐步从真实失败查询扩充。

## 建议 7：“284 tests”不足以证明召回质量

**合理性评分：4/5**

### 当前实际风险

核心观点完全正确：测试数量主要证明代码行为和回归保护，不证明检索相关性。尤其大量参数化 unit tests 会让数字
增长，但不会增加真实查询分布覆盖。

不过建议对现状判断过时。项目已存在 unit、integration、真实 E2E 和 `tests/eval` 四类资产；50-case eval
已经覆盖召回、no-answer、时间有效性、证据和 stale 状态。问题不是“没有分层”，而是：

- `tests/unit/` 中混有 API/组件边界测试，分类口径不够纯；
- behavioral scenarios 只有 `tests/scenarios/chinese_test_cases.py` 数据和散落测试，没有独立执行/报告层；
- eval 的排序指标不足，且真实 embedding/reranker 结果不属于默认离线基线；
- 对外只报总 tests 数，会掩盖每层能证明什么、不能证明什么。

### 决策

**现在改报告口径；计划补齐 behavioral/retrieval 指标。** 不建议为了目录美观立即大规模搬迁现有测试文件，
那会制造低价值 churn。

### 具体方案

发布或评审报告分别列出：

- Unit/Component：确定性规则、纯函数、repository 和单组件契约；
- Integration/E2E：SQLite migration、应用服务事务、API/MCP/Hermes 跨层路径；
- Behavioral memory scenarios：摄入 -> 冲突/时间变化 -> 召回/遗忘的用户故事；
- Retrieval benchmark：固定 snapshot 和 gold queries 的质量、延迟与配置。

近期先通过 pytest markers 或独立命令形成四份结果，不强制移动所有文件。为 behavioral 层补少量高价值场景：
偏好变更、历史查询、冲突/替代、TTL 后当前与历史可见性、无答案、证据链。Retrieval benchmark 沿用现有
`tests/eval`，补 MRR/nDCG@10 和 p50/p95。报告应写成类似“285 collected；unit/integration 均通过；
50-case eval 的各指标为 X”，而不是只写“285 tests passed”。

## 建议 8：不要在 `_collect_candidates()` 内部创建 `Settings()`

**合理性评分：5/5**

### 当前实际风险

这是六条中最明确的设计缺陷。`Settings.from_env()` 才读取环境变量并执行完整校验；`Settings()` 只产生 dataclass
字段默认值。管线内部的：

```python
defaults = Settings()
```

因此不是“补全当前运行配置”，而是静默回退到源码默认值。生产路径目前由 `RecallService` 显式传入全部 7 个
相关字段，所以正常 API/Hermes 调用大体安全；风险集中在直接调用 `hybrid_claims()` 的测试、未来新调用点，
以及以后新增配置字段却忘记在应用层透传时。届时环境变量看似生效，管线实际可能使用默认值，且不会报错。

这也破坏了 `settings.py` 模块 docstring 所表达的“启动时解析一次”的配置快照原则，并让纯召回管线隐式依赖
进程配置类型。

### 决策

**现在就做，优先级高。** 若不准备立即完成建议 3/4 的完整重构，也应先做一个小改动消除隐式 `Settings()`；
完整对象化随后跟进。

### 具体方案

最终方案是由 `RecallService` 在入口把 `self.settings` 映射为完整、冻结的 `RecallConfig`，所有字段非 Optional，
管线只消费它，不 import `Settings`，也不读取环境变量。

过渡方案可先让 `hybrid_claims()` 必须接收一个完整 config，或为兼容直接单测提供显式
`RecallConfig.defaults_for_test()`。不要在 domain/recall 管线里调用 `Settings.from_env()`，那只是把隐式配置
从错误的默认值换成隐式环境读取，并会让测试受进程环境污染。

同时修正当前使用 `or` 的两个数值回退：

- `effective_floor = candidate_floor or defaults.recall_candidate_floor`
- `effective_tag_candidate_limit = tag_candidate_limit or defaults.tag_candidate_limit`

完整配置对象经过入口校验后无需回退；若保留 Optional 过渡期，也应按 `is None` 判断，避免把显式但非法的 0
静默替换为默认值。

## 汇总表

| 建议 | 合理性 | 当前场景实际风险 | 决策 | 优先级 |
|---|---:|---|---|---:|
| 3. state dataclass | 4/5 | 中：39 个动态字段造成阶段耦合和维护风险，但固定内部链路已有测试 | 计划做，与 4/8 合并 | P1 |
| 4. 参数对象化 | 4/5 | 中低：实际 20 参数、生产调用点单一，但配置扩展成本持续上升 | 计划做，与 3/8 合并 | P1 |
| 5. 向量后端升级 | 3/5 | 当前低、长期高：522 claims 下扫描可接受，10k+ 需实测 | 先基准和触发条件，后端暂缓 | P2 |
| 6. 先评测再加排序因子 | 5/5 | 高：真实 Agent 的 top-k 错序比数据规模更影响结果；现有指标不足 | 现在做，冻结新因子 | P0 |
| 7. 分层测试与报告 | 4/5 | 中：已有分层雏形和 50-case eval，但总数口径会误导 | 现在改报告，计划补指标/场景 | P0/P1 |
| 8. 入口解析完整配置 | 5/5 | 中高：生产现路径安全，但存在静默源码默认值和未来漏传风险 | 现在做 | P0 |

## 建议执行顺序

### P0：立即完成

1. **冻结排序功能增量**：不再加入新 boost/channel/weight。
2. **扩充现有 eval**：增加 MRR、binary nDCG@10、p50/p95，并输出 baseline；对 tag boost 做单变量 A/B。
3. **修正质量报告口径**：分别报告测试层与 retrieval 指标，不再用单一测试总数代替质量证明。
4. **移除管线内部 `Settings()`**：由应用入口提供完整配置快照。

### P1：下一个召回改动前

5. 合并实施建议 3 和 4：引入 Request/Dependencies/Config 以及明确阶段输出 dataclass，迁移现有单测，
   不扩散到全项目 Claim 类型重构。
6. 把 behavioral memory scenarios 形成可独立执行和报告的一层，优先覆盖真实失败模式。

### P2：容量工作，不阻塞当前版本

7. 建立 522/2k/10k 的可重复向量与端到端 recall benchmark，记录 p50/p95 和内存。
8. 只有实测 SLO 或容量趋势触发时才比较 sqlite-vec 等内嵌索引；pgvector 由部署形态和运维需求决定，
   不以 `>100k` 作为自动迁移规则。

## 总体判断

外部评审在“类型化管线边界、集中配置、评测优先、向量扫描终会触顶”四个方向上是合理的，但不能把所有建议
都当作当前故障处理。对于现阶段 HL-Mem，最值得立即投入的是评测闭环和配置单一来源；类型化重构紧随其后，
因为它会降低下一轮排序实验的修改风险。向量后端替换则应坚持以测量为触发条件，避免为尚不存在的 100k 规模
牺牲当前 local-first 单机部署的简洁性。
