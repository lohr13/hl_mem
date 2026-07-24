# topic_tags 接入检索设计

## 1. 结论

推荐分两步接入：

1. **先实施 D（候选集内 soft boost）**，验证标签质量、查询标签识别率和排序收益。它改动小、延迟低、容易灰度和回滚，适合作为第一阶段。
2. **标签质量稳定后实施 B（独立 tag channel）**，真正补充仅靠正文无法召回的候选。标签通道应使用确定性的 query-to-tags 映射、独立候选配额和加权 RRF，不能直接把自然语言查询原样送入标签 FTS。

不推荐 A 作为最终架构：它会把正文相关性和标签相关性混入同一个 BM25 排名，难以单独调权、观测和回滚。当前阶段也不推荐 C：它需要全量重嵌入，并会改变语义空间和去重行为，成本和回归面显著大于预期收益。

| 方案 | 能增加候选 recall | 排序可控性 | 在线延迟 | 离线/迁移成本 | 建议 |
| --- | --- | --- | --- | --- | --- |
| A. tags 加入现有 FTS | 是 | 低 | 近似不变 | 中 | 不推荐 |
| B. 独立 tag channel | 是 | 高 | 低幅增加 | 中 | 推荐，第二阶段 |
| C. tags 加入 dense 文本 | 是 | 低至中 | 查询不变 | 高 | 暂不做 |
| D. 候选集内 soft boost | 否 | 高 | 极低 | 低 | 推荐，第一阶段 |

## 2. 当前检索管线

### 2.1 候选收集

`staged_pipeline._collect_candidates()` 当前固定执行两个通道：

- FTS：`search_claims_fts(query, candidate_limit, ...)`
- dense：`search_claims_vector(query_blob, candidate_limit, ...)`

两个通道共享同一个候选上限：

```text
candidate_limit = min(RECALL_VECTOR_SCAN_LIMIT, max(limit * 5, recall_candidate_floor))
```

FTS 和 dense 返回的 claim 都已在 repository 层按 namespace、status、valid time 和 recorded time 过滤。pipeline 随后再次执行统一可见性检查，以保护仓储实现或测试替身不一致的情况。

### 2.2 FTS channel

`claims_fts` 是一个单列 `search_text` 的 FTS5 external-content 表，`content='claims'`、`content_rowid='rowid'`。migration 002 的触发器把以下内容拼成一个字段：

```text
subject_entity_id + predicate + value_json
```

`search_claims_fts()`：

- 将用户查询按空白切分；
- 把每个 token 引号化；
- 以 `claims_fts MATCH ?` 查询；
- 按 `bm25(claims_fts)` 升序取前 `limit`。

多个 token 在当前表达式中采用 FTS5 默认 AND 语义。`topic_tags_json` 是 migration 016 后新增的 claims 列，现有 FTS 表、触发器和历史回填都不包含它。

### 2.3 dense channel

写入时，`application.ingest.claim_text()` 仅拼接：

```text
subject_entity_id + predicate + value
```

该文本生成 `embedding_dense`。查询时 `search_claims_vector()` 读取 namespace 和时间范围内所有非空向量，在 Python 中计算余弦相似度、全量排序并截断。当前实现不是 ANN；数据量增大时，dense 延迟主要由可见向量数和 2048 维余弦计算决定。

### 2.4 融合和最终排序

pipeline 对 FTS 和 dense 使用 `RRF_K = 60`：

```text
rrf(id) = Σchannel 1 / (60 + rank_in_channel)
```

当前 semantic feature 使用 `2 / (RRF_K + 1)` 归一化，即只有同时在两个通道都排第一才能达到 1.0。semantic 在 pre-rank 权重中占 `0.65`，之后还会与 recency、访问频率、confidence、importance 和 utility 合成。启用 reranker 时，reranker 输入仍只有 subject、predicate、value，不含 tags。

因此，任何新增检索通道都必须同步考虑：

- 候选去重和可见性；
- RRF 满分归一化；
- trace、audit 和阶段耗时；
- reranker 是否需要看到 tags；
- 候选总量及 reranker 调用规模。

## 3. 标签集的检索特性

`ALLOWED_TOPIC_TAGS` 是 44 个受控英文标签，涵盖两类粒度：

- 上位类别：`fact`、`state`、`config`、`plan`、`choice`、`memory`；
- 细分类别：`architecture`、`bugfix`、`dependency`、`deployment`、`framework`、`api` 等。

`normalize_topic_tags()` 会执行 NFKC、去首尾空白、casefold、将 `-` 替换为 `_`、去重，并丢弃不在 allowlist 中的值。这个受控集合适合精确匹配，但有三个限制：

1. 标签全是英文，中文自然语言查询不会自然命中；
2. `fact`、`state`、`other` 等高频宽泛标签区分度低；
3. `tool_choice` 等下划线标签与自然查询中的 “tool choice” 不是同一 token。

所有方案都应先定义一个共享的 `query -> normalized topic tags` 过程。最低可行版本可使用：

- 查询 token 与 allowlist 的精确匹配；
- `-`、空格和下划线形式归一；
- 一份配置化的中英文 alias 表，例如“架构”→`architecture`、“部署”→`deployment`；
- 最多输出少量高置信标签，并忽略 `fact`、`other` 等低信息量标签，除非查询明确要求类别过滤。

不建议在每次 recall 中调用 LLM 做 query tagging：它增加网络延迟和失败面，也会让检索不可重复。未来如需模型分类，应在本地轻量分类器或缓存层完成，并通过离线评测证明收益。

## 4. 方案 A：topic_tags 加入现有 FTS external content

### 设计

为现有 `claims_fts` 增加独立 tags 列，并由 triggers 将 `topic_tags_json` 写入该列。查询可以跨 `search_text` 和 tags，也可以使用列限定语法。为避免 JSON 标点影响 tokenization，索引值应是规范化标签以空格连接的文本，而不是原始 JSON。

SQLite FTS5 virtual table 不能像普通表一样安全地原地增加列，因此需要新 migration 重建 virtual table、重建三个同步 trigger，并全量回填。已有 migration 不可修改。

### 改动范围

- `src/hl_mem/storage/migrations/`：新增 migration，重建 `claims_fts`、trigger、历史索引；
- `src/hl_mem/storage/claims.py`：查询表达式及 BM25 列权重；
- 可能需要共享 query tag 解析模块；
- FTS repository、migration、召回排序和端到端测试；
- 若 trace 需区分正文命中与 tag 命中，还要扩展 trace；否则两者无法观测。

### 排序影响

如果直接跨列 MATCH，BM25 会把正文命中和标签命中混合成一个序列。可通过 `bm25(claims_fts, text_weight, tag_weight)` 降低 tags 权重，但仍无法让 RRF 知道候选来自正文还是标签。高频宽泛标签可能让大量文档获得相近 BM25 分数，并挤出正文相关候选。

如果用列限定分别查询，实际上已经趋近方案 B，却仍共享同一个索引和 repository 接口。

### 延迟成本

在线仍是一条 FTS 查询，通常只增加少量索引体积和 BM25 计算，延迟近似不变。代价主要在 migration 的全量重建和写入 trigger 的索引维护。

### precision / recall

- recall：英文标签词或 alias 转换后的标签查询会提升；
- precision：细粒度标签可能提升，高频上位标签容易下降；
- 中文查询：没有 query tag 映射时基本无收益；
- 可解释性：较差，难区分结果是正文相关还是标签相关。

### 是否值得做

**不推荐作为最终方案。** 它在线成本最低，但将两个性质不同的信号过早耦合，后续调权、灰度、诊断和回滚都较困难。只有在明确要求“保持单次 SQL 查询”且标签通道无需独立观测时才值得采用。

## 5. 方案 B：独立 tag channel，经 RRF 融合

### 设计

新建只索引规范化标签文本的 `claims_tags_fts`，通过独立 repository 方法查询。pipeline 收集 `fts`、`dense`、`tags` 三个有序通道，再融合。

标签 channel 不应把原始自然语言查询直接交给当前 `sanitize_fts_query()`。例如 `"architecture decision"` 的 AND 查询会要求同一 claim 同时包含两个标签；长中文查询则必然为空。正确数据流是：

```text
query
  -> deterministic query tag extraction
  -> 0..N normalized tags
  -> OR 查询或按标签分别查询
  -> tag candidates
  -> weighted RRF
```

建议给 tag channel 独立、较小的候选上限，并使用加权 RRF：

```text
score = rrf(fts) + rrf(dense) + tag_channel_weight * rrf(tags)
```

`tag_channel_weight` 应由配置注入并通过离线评测确定；初始值应低于正文和 dense 通道。semantic 归一化分母必须从硬编码的 `2 / (K + 1)` 改成“启用通道权重之和除以 `K + 1`”。当 query 未识别出标签时，不能把空 tag channel 计入分母，否则所有候选 semantic 分数会无故下降。

### 改动范围

- `src/hl_mem/storage/migrations/`：新增 tags FTS 表、同步 triggers、历史回填；
- `src/hl_mem/storage/claims.py`：新增 `search_claims_tags()`；
- `src/hl_mem/recall/staged_pipeline.py`：第三通道、加权 RRF、动态归一化、候选合并；
- query tag 解析模块和配置；
- `src/hl_mem/recall/trace.py`、audit detail：记录 tags channel、识别出的 query tags 和耗时；
- `settings.py` / 配置：tag candidate limit、channel weight、开关；
- repository、RRF、可见性、trace、排序回归和端到端测试。

### 排序影响

这是四种方案中最可控的：

- 仅标签命中的 claim 能进入候选集，真正增加 recall；
- 同时被正文/dense/tag 命中的 claim 会获得多通道一致性奖励；
- channel 权重和候选配额可以独立调节；
- 对宽泛标签可设置停用列表、IDF 门槛或较低权重。

风险是未经加权的第三通道会过度奖励标签命中，并改变现有 semantic 分数分布。还要避免将 tag-only 候选大量送入 reranker；较小 tag 配额和现有统一 `candidate_limit` 截断可控制成本。

### 延迟成本

增加一次本地 FTS 查询和一次小规模列表融合。相对于当前 Python 2048 维向量全扫，通常是低幅增加；但必须单独记录 `tags_us` 验证。query tag 提取若保持确定性本地执行，成本可忽略。

### precision / recall

- recall：四种方案中最直接、最可控的提升，尤其适合正文未显式出现“架构/部署/依赖”等抽象主题的 claim；
- precision：细粒度标签和多通道共同命中时提升；宽泛标签、错误标签或过大的 channel weight 会下降；
- 中文查询：依赖中英文 alias 覆盖率；
- 标签噪声不会污染正文 BM25 或 dense 空间。

### 是否值得做

**值得做，推荐作为目标架构。** 前提是先建立 query-tag 映射和离线评测集，并用 feature flag 灰度。它是唯一同时满足“新增召回候选”和“信号独立可控、可观测”的方案。

## 6. 方案 C：tags 作为 dense embedding 文本的一部分

### 设计

将写入侧 embedding 文本改为结构化形式，例如：

```text
subject: ...
predicate: ...
value: ...
topics: architecture decision
```

查询 embedding 保持自然语言输入。已有 claim 必须用相同模板全量重嵌入，并记录新的 embedding 模板/版本，避免新旧向量混用。

### 改动范围

- `src/hl_mem/application/ingest.py`：统一 embedding 文本构造；
- embedding 版本配置和组件工厂；
- 新的数据 migration/backfill job：批量重新生成所有 claim 向量；
- 若 reranker 文本也加入 tags，则修改 `staged_pipeline._claim_text()`；
- dense、语义去重、跨 subject 去重、consolidation 相关回归测试；
- 运维文档、失败重试、断点续跑和新旧版本切换。

### 排序影响

标签会改变整个向量，而不是提供独立分数。细粒度主题可能帮助自然查询靠近相关 claim，但通用标签可能让不同事实因共享标签而过度接近。当前同一 `embedding_dense` 还参与语义去重/归并逻辑，因此变化不限于 recall 排序，也可能改变写入去重和冲突候选行为。

无法单独解释“正文相似”和“标签相似”，也很难通过一个权重快速关闭标签影响。重复拼接标签以调权属于脆弱的 prompt 技巧，不建议。

### 延迟成本

在线 query embedding 次数和向量扫描成本不变；claim 文本略长的写入成本可忽略。真正成本是全量重嵌入的 API 费用、时间、失败恢复和双版本迁移。若不重嵌入，历史与新增数据处于不同向量分布，结果不可接受。

### precision / recall

- recall：可能改善同主题不同措辞的查询；
- precision：取决于 embedding 模型是否正确理解标签，效果不透明；宽泛标签可能降低；
- 对中文 query 匹配英文 tags，跨语言 embedding 可能有收益，但不能假定；
- 由于向量全扫只取 top-N，标签改变近邻分布后可能挤出原本正文相关候选。

### 是否值得做

**当前不值得。** 只有离线评测证明独立 tag channel 仍漏召回、且 tags 加入 embedding 对 NDCG/Recall@K 有稳定增益时再考虑。实施前必须把 embedding 模板版本化，并评估去重与 consolidation 的连带变化。

## 7. 方案 D：tags 作为 soft boost

### 设计

不新增候选通道。先从 query 提取规范化标签，然后对已经由 FTS/dense 召回的 claim 计算标签重叠特征，例如：

```text
tag_match = weighted_overlap(query_tags, claim.topic_tags)
final_pre_score = current_memory_score + tag_boost_weight * tag_match
```

建议使用有上限的小权重，并按标签信息量降权：`architecture`、`dependency` 等细粒度标签高于 `fact`、`state`，`other` 默认不 boost。不要直接按命中标签数量线性累加，否则多标签 claim 会获得结构性优势。

标签 boost 应在 RRF semantic 特征之后、pre-rank 排序之前应用，并在 trace/audit 中单独记录，避免伪装成 semantic 分数。启用 reranker 后，当前 `blend_reranker_score()` 只组合 reranker 分数与非 semantic priors；若希望 boost 在 rerank 后仍生效，需要明确将 tag feature 纳入 blend，否则 reranker 会覆盖大部分收益。

### 改动范围

- query tag 解析模块；
- `src/hl_mem/recall/staged_pipeline.py`：候选标签重叠、pre-rank boost、rerank 后策略；
- `settings.py` / 配置：开关和 boost weight；
- trace/audit：query tags、claim overlap、boost 分数；
- 排序单元测试和离线检索评测；
- 无 schema migration，无重嵌入。

### 排序影响

仅重排现有候选，不会召回 FTS 和 dense 都遗漏的 claim。小权重时可作为 tie-breaker 或主题一致性先验，提高候选集内 precision；过大时会让标签质量压过正文与 dense 相关性。

为保持现有排序稳定，初版应满足：

- query 未识别标签时结果完全不变；
- claim 无标签时结果完全不变；
- boost 有严格上限；
- `other` 等低信息量标签不参与；
- feature flag 关闭时结果与当前实现字节级等价。

### 延迟成本

只增加本地字符串集合运算，且候选规模受 `candidate_limit` 限制，成本极低。无需额外 SQL、网络调用或向量计算。

### precision / recall

- precision：对候选集中主题相符项预计有小幅提升；
- recall：集合层面不变，Recall@K 可能因重排改善，但无法找回候选集之外的结果；
- 对错误标签敏感度可由低权重和停用标签控制；
- 特别适合先验证标签是否与用户查询相关。

### 是否值得做

**值得立即作为第一阶段做。** 它不是 B 的替代品，而是低风险验证和长期可保留的排序特征。若离线评测显示标签覆盖率或准确率不足，可直接关闭，不留下索引迁移负担。

## 8. 推荐落地顺序与评测门槛

### 第一阶段：D，验证标签信号

建立包含中英文查询的离线集合，至少覆盖：

- query 明示细粒度主题；
- query 只隐含主题；
- 宽泛标签；
- 多标签 claim；
- 标签错误或缺失；
- query 无可识别标签。

对比关闭/开启 boost 的 Recall@K、MRR、NDCG@K、无关结果率，并按标签分别统计。还应记录 query tag 识别覆盖率、claim 标签覆盖率和标签频率分布。

### 第二阶段：B，补充候选

仅当评测表明存在“正文和 dense 均未进入 candidate pool、但标签正确”的漏召回时启用独立 channel。上线必须具备：

- feature flag；
- 独立 tag channel weight 和 candidate limit；
- 动态 RRF 归一化；
- `tags_us`、tag candidate IDs 和 query tags 审计；
- 对高频低信息标签的停用或降权；
- migration 后 FTS 行数/一致性校验和回滚方案。

### 不建议组合

- 不同时实施 A 与 B：同一标签会通过两个 lexical 路径重复计分；
- 不在缺少 embedding 版本化时实施 C；
- 不让 D 和 B 各自以完整权重奖励同一次标签命中。若两者并用，D 应降为很小的候选内 tie-breaker，或只奖励正文/dense 与 tag channel 的交叉一致性。

## 9. 最终建议

目标架构选择 **B + 轻量 D**：

- B 负责“找得到”，解决 topic_tags 当前完全不进入候选生成的问题；
- D 负责“排得准”，对已有候选提供可解释的小幅主题先验；
- 两者共享同一个确定性 query-tag 解析器、停用标签策略和评测体系。

实施顺序是 **D → 评测 → B**。A 的低延迟优势不足以抵消信号耦合，C 则应等待真实评测证明其收益足以覆盖重嵌入及去重行为变化。
