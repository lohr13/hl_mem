# 任务：hl_mem Prompt 调优 — 保持中文原文 + 统一 predicate + 冲突检测修复

## 问题诊断

Week 5 端到端测试发现两个问题：

### 问题 1: LLM 把中文值翻译成英文
- 输入："我喜欢深色模式" → LLM 提取 value="dark mode"
- 输入："我们项目用PostgreSQL" → LLM 提取 value="PostgreSQL"（正确，因为本来就是英文）
- 后果：中文 FTS 搜 "深色模式" 搜不到（存的是 "dark mode"）

### 问题 2: 偏好变更的冲突检测没触发
- Event 1："我喜欢深色模式" → LLM 提取 subject="用户" predicate="偏好" value="dark mode"
- Event 2："现在改用浅色模式了" → LLM 提取 subject="用户" predicate="prefers" value="light mode"  
- conflict_key = hash(namespace + subject + predicate + qualifiers)
- predicate "偏好" ≠ "prefers" → conflict_key 不同 → 不会触发 state_change → 旧的不会被 superseded
- 后果：两个 claim 都是 active，recall 时返回矛盾结果

## 修复方案

### Fix 1: 修改 SYSTEM_PROMPT（src/hl_mem/ingest/llm_extractor.py）

在现有 prompt 基础上增加以下约束：

1. **保持原文语言**：
   "value 字段必须保持用户使用的原始语言。用户说中文就输出中文值，说英文就输出英文值。不要翻译。"

2. **统一 predicate 词表**：
   限定 predicate 只能使用以下标准化值之一（中文）：
   - `偏好` — 用户喜欢/不喜欢的事物（深色模式、简短回答）
   - `使用` — 工具、数据库、操作系统等技术选择
   - `状态` — 当前服务状态、运行状态
   - `身份` — 用户名、角色、联系方式
   - `配置` — 端口、路径、参数
   - `计划` — 计划做的事、截止日期
   - `事实` — 不属于以上类别的客观事实

   并在 prompt 中明确：如果新事实与已有同 subject+predicate 的事实不同，在 qualifiers 中加 `"change": true`。

3. **统一 subject 格式**：
   - 默认 subject 为 "用户"
   - 如果对话中明确提到项目名/服务名，subject 用那个名字
   - 不要用 "我"、"他"、"用户" 以外的代词

4. **变更信号检测**：
   如果对话包含 "改用"、"换成"、"现在用"、"不用了"、"改为" 等变更信号词，
   在 qualifiers 中加 `"change": true`，这会帮助 ConflictResolver 判定为 state_change。

### Fix 2: ConflictResolver 增加 predicate 归一化（src/hl_mem/recall/conflict.py）

在 compute_conflict_key 中，predicate 已经做了 casefold。但不同语言的 predicate（"偏好" vs "prefers"）需要统一。

方案：在 `_claim()` 方法（llm_extractor.py）中，将 LLM 返回的 predicate 映射到标准词表。
如果 LLM 返回的 predicate 不在标准词表中，尝试匹配最近的（大小写不敏感的英文→中文映射）。

创建一个 predicate 归一化映射：
```python
PREDICATE_NORMALIZE = {
    "prefers": "偏好", "preference": "偏好", "偏好": "偏好", "喜欢": "偏好",
    "uses": "使用", "use": "使用", "使用": "使用", "用": "使用",
    "status": "状态", "状态": "状态",
    "identity": "身份", "身份": "身份",
    "config": "配置", "配置": "配置",
    "plan": "计划", "计划": "计划",
    "fact": "事实", "事实": "事实",
}
```

在 `_claim()` 中应用：
```python
predicate = item.get("predicate", "事实")
predicate = PREDICATE_NORMALIZE.get(predicate.casefold().strip(), predicate)
```

### Fix 3: ConflictResolver 增加 change 信号检测

在 `resolve()` 方法中，已有 `_signals_change()` 检查 qualifiers 中的 `state_change` 和 `current`。
增加检查 `change` 字段：
```python
return bool(qualifiers.get("state_change") or qualifiers.get("current") or qualifiers.get("change"))
```

### Fix 4: FTS 召回增强

在 claims_fts trigger 中，目前只索引 `predicate || ' ' || value_json`。
增加 `subject_entity_id` 到索引内容：
```sql
CREATE TRIGGER IF NOT EXISTS claims_ai AFTER INSERT ON claims BEGIN
  INSERT INTO claims_fts(rowid, search_text)
  VALUES (new.rowid, 
    coalesce(new.subject_entity_id, '') || ' ' || 
    coalesce(new.predicate, '') || ' ' || 
    coalesce(new.value_json, ''));
END;
```
（其他 trigger 同步修改）

注意：修改 trigger 需要先 DROP 旧的再 CREATE 新的。在 migration 002 中处理。

## 测试

### 新增/修改测试
- `tests/unit/test_llm_extractor.py`：
  - 测试 predicate 归一化（"prefers"→"偏好"）
  - 测试 value 保持原文语言（mock LLM 返回中文值，验证不翻译）

- `tests/unit/test_conflict.py`：
  - 测试不同 predicate 表达但相同语义的 claim 能触发 state_change
  - 测试 qualifiers.change=true 触发 state_change

- `tests/integration/test_conflict_pipeline.py`：
  - 修改现有测试，验证 "深色→浅色模式" 的完整 supersede 流程
  - 确保中文值不翻译

### 验收标准
1. 所有现有测试通过（33个）
2. 新增测试全绿
3. e2e 测试中 "深色模式" 能被中文 FTS 搜到
4. "深色→浅色" 偏好变更正确触发 supersede
5. 每个文件不超过 200 行

## 约束
- 只修改 prompt 文本、predicate 归一化映射、conflict resolver 的 _signals_change、FTS trigger
- 不改架构，不加新表
- migration 002 只改 trigger，不动表结构
- 完成后运行 pytest 验证
