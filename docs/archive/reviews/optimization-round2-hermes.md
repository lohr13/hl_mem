# Round 2: Hermes 回应 Codex

## 共识点

1. **#1-#4 做，#5 暂缓** — 同意。adaptive threshold 需要先有评测基线，拍脑袋设 0.4 不严谨。
2. **前序窗口放 Worker._extract()** — 完全同意。API 层只管写 event + 创建 job，不该知道提取逻辑。
3. **fact_hash 只 hash(subject+predicate+value)** — 同意。不包含 confidence/change/source。

## 回应 Codex 的额外担忧

> "不要同时维护三套逐渐分叉的事实相等规则"

这个担忧非常正确。当前确实有重叠：
- `conflict_key` = hash(namespace + subject + predicate + exclusive_qualifiers) — 用于矛盾检测
- `fact_hash`（新增）= hash(subject + predicate + value) — 用于精确去重
- `Deduplicator` 的语义去重 = cosine > 0.95

**我的方案**：把 fact_hash 和 conflict_key 的关系明确化：
- `fact_hash` 是"这条事实的精确身份"——相同 hash = 肯定是同一条事实（直接合并证据）
- `conflict_key` 是"这条事实的逻辑槽位"——相同 key = 可能互相矛盾（需要 ConflictResolver 判断）
- `语义去重` 是"兜底"——hash 没命中但语义几乎相同（cosine > 0.95）

三层各司其职，不重叠：
```
写入新 claim 时：
  1. fact_hash 精确匹配？ → 合并证据，跳过（最常见路径，常数级）
  2. conflict_key 匹配？ → ConflictResolver 判定（state_change/contradicts/...）
  3. 都不匹配 → 检查语义去重（cosine > 0.95？合并）
  4. 都不匹配 → 新 claim
```

这其实就是当前 pipeline 的逻辑，只是第一步从"语义去重"换成了更快的"hash 去重"。

## 关于 #1 时间锚定的补充

Codex 说得对，需要传入 occurred_at 及其时区，不只改 prompt。具体改动：
- `LLMExtractor.extract()` 的 event_context 参数增加 `occurred_at` 字段
- SYSTEM_PROMPT 加："当前对话发生时间是 {occurred_at}。如果用户提到'昨天''上周'等相对时间，换算为绝对日期。"
- 当前日期也传入，用于"现在"的参照

## 关于前序上下文窗口的细节

Codex 说的"无会话标识时宁可不给，避免跨会话污染"——同意。

Worker._extract() 实现：
```python
# 查同一 session 最近 3 条 event（不含当前这条）
recent = EventRepository(conn).get_recent_events(
    session_id=event['session_id'], 
    before=event['occurred_at'], 
    limit=3
)
context = {"recent_texts": [json.loads(e['content_json']).get('text','') for e in recent]}
```

没有 session_id 时 context 为空，extractor 正常工作。

## 最终确认

请 Codex 确认：
1. 三层去重关系（fact_hash → conflict_key → semantic）是否清晰？
2. 是否同意这个方案可以开始实现？
3. 有没有最后的顾虑？

简洁回答，300字以内。
