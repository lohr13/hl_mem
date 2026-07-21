# hl_mem 优化共识（基于 MemOS/Zep/Mem0 对比）

- 达成时间：2026-07-20
- 参与者：Hermes Agent + Codex
- 原则：不增加架构复杂度，只做修正缺陷和性能优化

## 4 项优化（总改动 ~30 行）

### #1 Observation Date 时间锚定
- SYSTEM_PROMPT 注入 occurred_at，LLM 把相对时间转绝对日期
- LLMExtractor.extract() 的 event_context 传入 occurred_at
- 改动：prompt 3行 + extractor 2行

### #2 前序上下文窗口
- Worker._extract() 查同 session 最近 3 条 event 作为 context
- 无 session_id 时不给上下文，避免跨会话污染
- occurred_at 相同时用 event ID 作稳定排序
- 改动：worker.py 加 get_recent_events 查询 + context 传递

### #3 ADD-only 范式 + 质量约束
- prompt 明确"只提取，不判断冲突"
- 增加 Self-Contained（代词替换）和 Numerically Precise 约束
- 改动：SYSTEM_PROMPT 扩充 5 行

### #4 fact_hash 精确去重快路径
- claims 表加 fact_hash 列 + 索引（migration 003）
- hash(NFKC规范化(subject+predicate+稳定JSON(value)))
- 三层去重职责：fact_hash（精确同一）→ conflict_key（逻辑同槽）→ semantic（兜底近似）
- 改动：migration + pipeline.py 几行

### 暂缓
- #5 adaptive threshold（需先有评测基线）
- value/alpha/priority（需 Experience 通道的 reward 信号）
- Entity Boost / 多通道 RRF / MMR（数据量不够）
