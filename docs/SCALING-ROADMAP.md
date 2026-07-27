# HL-Mem 规模演进路线图

## 1. 当前规模画像

当前个人实例约有 388 条 claims、4,139 条 events，且这些数据只来自几天的高频使用。这个规模下，SQLite
WAL、FTS5 和逐条计算 cosine 的 dense scan 都足够简单、可观测且可靠。当前优先事项仍是修复
`index_text` 表示质量和提取覆盖缺口，不以未来规模为理由提前引入新的召回通道或存储系统。

388 条 claims 不能视为稳定上限。个人 Agent 的事件写入速度明显高于传统笔记应用，规模决策应依据真实增长率、
延迟和召回质量埋点，而不是只依据当前绝对数量。

## 2. 规模增长预估

| 用户类型 | 使用强度 | claims 月增估算 | 半年量级 |
|---|---:|---:|---:|
| 轻度用户 | 每日约 50 turns | 约 500 | 约 3,000 |
| 高强度用户 | 每日 200+ turns | 约 2,000–3,000 | 约 12,000–18,000 |

以上是容量规划区间，不是提取率承诺。实际增长取决于 EventFilter 命中率、`should_memorize` 判定、每个 event
产出的 claims 数、去重率和 TTL 到期率。按周记录这些指标，使用最近 28 天斜率预测达到下一拐点的时间。

## 3. 各组件的规模拐点

### 3.1 向量搜索

当前 SQLite 后端读取时间视图内全部带 embedding 的 claims，逐条计算标准 cosine，再排序截断。在约 10,000
条以内继续使用该实现，避免 ANN 的索引构建、增量同步和召回率损失。

达到以下任一条件时，对 ANN 做离线对照实验，而不是直接替换：

- 活跃且带 embedding 的 claims 持续超过 10,000；
- dense 通道 p95 延迟连续 7 天超过 100 ms；
- 单次扫描解码的向量字节量造成可观测的进程内存或 CPU 压力。

对照实验至少比较 Recall@50、p50/p95 延迟、索引构建时间、增量写入成本和磁盘占用。只有 ANN 在目标诊断集上
不降低召回且延迟收益明确时才进入迁移设计。

### 3.2 `index_text` 与重嵌入

`HL_MEM_INDEX_TEXT_MODE` 支持 `legacy`、`value_only` 和 `natural`。格式变化只影响新写入 claim；已有
`index_text` 和 embedding 不会自动改变，因此生产切换前必须先用只读 A/B 脚本确认收益，再安排全量重嵌入。

近期不新增 schema。为了避免规模增长后一次性重算不可控，应在 claims 接近 10,000 前补齐可恢复的增量更新能力：

- 按稳定游标或 claim id 分批处理；
- 记录目标 mode、embedding model、成功/失败数量和最后游标；
- 支持幂等重跑、限速和失败重试；
- 切换前验证新旧数量一致，并保留回滚到旧 mode 的执行计划。

### 3.3 提取管道

当前继续逐 event 提取，因为它的事务和失败边界清晰。高频写入时，先通过埋点确认瓶颈属于无效 LLM 调用、schema
重试、长 event 分块还是写入吞吐。

仅当提取队列 p95 等待时间持续超过 60 秒，或 LLM 调用吞吐达到 provider 限流的 70% 以上时，评估小批量提取。
批量方案必须保留 event 到 claim 的 evidence 映射和单 event 重试能力。预筛只针对埋点证实的稳定低价值模式，
每条新规则都需要对漏提取样本回放；不以宽泛关键词过滤项目、硬件、配置或工作流事实。

### 3.4 召回管道

现阶段保持 FTS + dense 的主路径和已有 soft tag boost：

- reranker：当目标 claim 已进入融合候选但最终排序持续不佳，且诊断集达到至少 50 条稳定样本时再开启对照；
- 独立 tag channel：当 tag 命中能稳定补回 FTS/dense 都未进入前 50 的目标 claim 时再启用；
- relation expansion：只有关系数据覆盖率和证据质量足以支撑真实查询时才启用；
- candidate limit：当正确 claim 经常位于单通道第 51–100 名时，先测提高候选上限的成本和收益。

这些能力不能修复不存在的 claim。每次实验只改变一个变量，并分别记录候选召回与最终排序指标。

## 4. 决策埋点清单

### 写入与提取

- events 每日新增量，按 `actor_type`、`event_type` 分组；
- EventFilter 的 eligible/过滤数量和 reason 分布；
- 每 event 的内容长度、分块数和自动二分深度；
- `should_memorize` true/false 数量及原因；
- 每 event 提取、写入、低 importance 跳过、精确去重、语义去重和冲突处理的 claims 数；
- schema retry、JSON repair、LLM 调用次数；
- input/output/total tokens、调用延迟、错误类型和 provider 限流次数；
- 提取队列深度、排队时间 p50/p95、完成时间和死信数量。

### 存储与增长

- events、全部 claims、活跃 claims、带 embedding claims 的每日存量和 7/28 天增长率；
- SQLite 文件、WAL 和 embedding BLOB 的磁盘占用；
- TTL 到期、归档、撤回和 supersede 的每日数量；
- 重嵌入批次吞吐、失败率、剩余量和新旧模型/mode 分布。

### 召回与质量

- FTS、dense、tag 各通道候选数量和查询耗时 p50/p95；
- dense scan 的参与条数、向量解码字节量和 cosine 计算耗时；
- 诊断集目标 claim 的 dense rank、融合 rank、Recall@10/50、MRR；
- reranker 前后 rank delta、额外延迟和 API/token 成本；
- 无目标 claim、目标未进候选、候选内排序失败三类失败占比；
- 用户 helpful/unhelpful 反馈率，并按查询意图和规模区间分组。

指标按天聚合，原始 DEBUG 日志只保留满足排障需要的窗口。升级决策要求连续趋势或可复现的诊断集结果，不由单次慢查询触发。

## 5. 当前不做清单

- 不立即引入 ANN/vector database；先观察约 10,000 条附近的真实扫描延迟。
- 不新增 schema 或 migration 来保存 `index_text` mode；当前配置用于新写入，重嵌入工具负责显式切换。
- 不立即实现批量 LLM 提取、复杂路由或多模型分层。
- 不新增 subject/entity 独立召回通道、always-on profile block 或新的图检索架构。
- 不因单个漏提取样本全面放宽 prompt；先用 event、过滤判定、提取日志和 evidence 链定位丢失环节。
- 不默认开启 reranker、独立 tag channel 或 relation expansion；达到上述证据门槛后逐项 A/B。
- 不建立大规模人工标注平台；近期维持小而稳定的真实失败诊断集，随规模和失败类型增长再扩展。
