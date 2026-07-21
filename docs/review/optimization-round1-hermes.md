# Round 1: Hermes 对 hl_mem 优化的修正建议

## 背景
对比了 MemOS、Zep/Graphiti、Mem0 的源码后，初步列出了 14 项优化建议。但用户正确指出：优化应考虑架构简洁性、维护难易和可扩展性，不要把别人家的复杂度搬过来。

## 重新评估：哪些值得做

### ✅ 应该做（修正明显缺陷，不增加复杂度）

**1. Observation Date 时间锚定（来自 Mem0）**
- 问题：用户说"昨天部署了 PG"，6个月后召回时"昨天"完全无意义
- 修复：提取 prompt 注入对话发生日期，LLM 把相对时间转绝对日期
- 改动：SYSTEM_PROMPT 加 3 行
- 复杂度增加：零

**2. 前序上下文窗口（来自 Mem0）**
- 问题：用户说"那个项目用 PG"，没有上下文 LLM 不知道"那个项目"是什么
- 修复：提取当前 event 时，最近 3 条 event 的 text 作为 context 一起喂给 LLM
- 改动：pipeline.py 的 extract 调用加一个 context 参数
- 复杂度增加：极低

**3. ADD-only 范式明确化（来自 Mem0）**
- 问题：当前 prompt 没有明确告诉 LLM "你只负责提取，冲突检测不归你管"
- 修复：prompt 加一句"只提取事实，不要判断是否与已有记忆冲突"
- 改动：SYSTEM_PROMPT 加 1 行
- 复杂度增加：零

### 🤔 可以做（性能优化，不改架构）

**4. fact_hash 去重快路径（来自 Zep）**
- 问题：每次新 claim 都要做语义去重（cosine 计算），大量重复事实浪费计算
- 修复：claims 表加 fact_hash 列（对 subject+predicate+value 取 hash），写入前先常数级查重
- 改动：migration 003 加一列 + 索引，pipeline 加一行 hash 计算
- 复杂度增加：极低，纯性能优化

**5. adaptive threshold（来自 MemOS）**
- 问题：当前 recall 固定返回 top-N，不管结果是否都相关
- 修复：RRF 合并后，丢弃 score < top_score × 0.4 的结果
- 改动：hybrid_claims() 函数加 2 行
- 复杂度增加：零

### ❌ 不应该做（增加复杂度但首版收益有限）

**6. value/alpha/priority 字段（来自 MemOS）**
- 问题：value 需要 reward 信号来源，但 hl_mem 没有 Experience 通道。value 永远是 0，字段就是僵尸列
- 结论：等 Experience 通道实现时再加

**7. 三信号 Entity Boost 检索（来自 Mem0）**
- 问题：需要维护独立的 entity 索引和 entity-to-claim 映射，基础设施量大
- 结论：首版 FTS+Dense 够用，等数据量上来再考虑

**8. 多通道 RRF（来自 MemOS）**
- 问题：pattern LIKE 对中文短词有效，但 error signature 需要 events 中提取错误签名——又是新逻辑
- 结论：暂不做，FTS5 的 trigram tokenizer 对中文已经够好

**9. Smart-seed MMR**
- 问题：首版数据量小，结果重复概率低，MMR 收益有限
- 结论：等数据量上来再加

**10. η reliability 机制（来自 MemOS）**
- 问题：又是需要 reward 信号的，没有 Experience 通道没意义
- 结论：等 Experience 通道

## 修正后的建议

只做 1-5，全部是"不改架构、不加表、不加复杂逻辑"的改动：
- 3 个 prompt 级改动（1,2,3）
- 1 个加列+索引（4）
- 1 个加2行代码（5）

总计改动量：约 30 行代码，0 个新文件，0 张新表。

## 请 Codex 回应
1. 你是否同意这个范围？有没有你认为应该加或减的？
2. 前序上下文窗口（#2）的实现：是在 Worker._extract() 里从 events 表查最近 3 条，还是在 API 层传入？
3. fact_hash（#4）的规范化策略：只 hash(subject+predicate+value) 还是包含 qualifiers？
