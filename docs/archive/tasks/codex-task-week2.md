# 任务：hl_mem Week 2 — LLM Extractor + Event Filter + Token Budget

请先阅读以下文件了解现有代码：
- src/hl_mem/ingest/extractors.py — 当前 FakeExtractor
- src/hl_mem/api/server.py — 当前 API，_queue_and_extract() 里同步调用 FakeExtractor
- src/hl_mem/storage/repository.py — Repository 层
- src/hl_mem/storage/migrations/001_initial.sql — Schema
- docs/architecture.md Section 6.2 — 后台提取设计
- tests/scenarios/chinese_test_cases.py — 30条中文测试集

## 目标
将 FakeExtractor 升级为真正的 LLM 提取器，加上 event filter 和 token 预算控制。

## 1. LLM Extractor

创建 `src/hl_mem/ingest/llm_extractor.py`：

### Provider 配置（通过环境变量）
```bash
LLM_API_KEY=***           # 百炼 Coding Plan AK
LLM_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
LLM_MODEL=qwen3.7-plus
```

### API 调用
使用标准 HTTP 调用（httpx 或 urllib），**不安装 openai SDK**。
走 OpenAI 兼容协议：`POST {LLM_BASE_URL}/chat/completions`

### 提取 Prompt
系统 prompt 要求模型从对话内容中提取原子事实，输出 JSON Schema 约束的结果：

```json
{
  "claims": [
    {
      "subject": "用户",
      "predicate": "偏好",
      "value": "深色模式",
      "qualifiers": {},
      "confidence": 0.9,
      "volatility": "stable",
      "reason": "用户明确陈述偏好"
    }
  ],
  "entities": [
    {"name": "深色模式", "type": "preference", "aliases": []}
  ],
  "should_memorize": true,
  "sensitivity": "normal"
}
```

prompt 要点：
- 中文 prompt（输入是中文对话）
- 明确告诉模型：只提取值得长期记住的事实，忽略闲聊/寒暄/临时信息
- volatility 分两档：ephemeral（实时状态、临时数据）和 stable（偏好、配置、事实）
- Assistant 自己的回答 source_authority 降为 low（在 claim 记录时体现）

### 接口设计
```python
class LLMExtractor:
    def __init__(self, api_key, base_url, model):
        ...

    def extract(self, content: dict | str, event_context: dict | None = None) -> list[ExtractedClaim]:
        """调用 LLM 提取 claim，返回结构化结果"""
        ...
```

保持与 FakeExtractor 相同的 `extract()` 接口，方便切换。

### 实体归一化（简单版）
提取出的 entity name 做基本归一化：
- 大小写统一（PostgreSQL / postgresql / POSTGRESQL → PostgreSQL）
- 已知别名映射表（PG → PostgreSQL, pg → PostgreSQL）
- 这个映射表硬编码一个小字典即可，不需要 NER 模型

## 2. Event Filter

创建 `src/hl_mem/ingest/event_filter.py`：

在调用 LLM 之前过滤掉不值得提取的 event：

```python
class EventFilter:
    def should_extract(self, event: dict) -> tuple[bool, str]:
        """返回 (是否提取, 原因)"""
        ...
```

过滤规则：
- `event_type == 'tool_result'` 且 content 是纯文件内容/命令输出 → 跳过（reason: "raw_tool_output"）
- content_json.text 长度 < 5 字符 → 跳过（reason: "too_short"）
- actor_type == 'assistant' 且内容只是确认语（"好的""明白了"）→ 跳过（reason: "acknowledgement"）
- event_type == 'explicit_memory' → 高优先级，不过滤
- 其余全部提取

过滤结果记录到日志（不写入 DB，避免噪音）。

## 3. Token Budget

创建 `src/hl_mem/ingest/budget.py`：

```python
class TokenBudget:
    def __init__(self, daily_limit: int = 500000):
        ...

    def can_spend(self, estimated_tokens: int) -> bool:
        """检查今日预算是否足够"""
        ...

    def record_usage(self, actual_tokens: int):
        """记录实际消耗"""
        ...

    def get_stats(self) -> dict:
        """返回今日消耗统计"""
        ...
```

- 预算按自然日重置
- 状态持久化到 jobs 表或单独的 `budget_log` 表（简单起见可以用一个 JSON 文件 `hl_mem_budget.json`）
- 预算耗尽时，extract_event job 保留为 pending，不阻塞 event 写入
- 记录每次 LLM 调用的 token 消耗

## 4. 改造 server.py

修改 `_queue_and_extract()`：
1. 先过 EventFilter，不值得提取的直接跳过
2. 检查 Token Budget，预算不足时只写 job 不执行
3. 有预算时调用 LLMExtractor（替换 FakeExtractor）
4. 如果 LLM 调用失败，job 保持 pending 状态等待重试

新增 API 端点：
- `GET /v1/stats` — 返回当前统计（events 数、claims 数、今日 token 消耗、jobs 积压数）

## 5. FakeExtractor 保留
不删除 FakeExtractor，作为测试用的默认 provider。通过环境变量切换：
```bash
HL_MEM_EXTRACTOR=fake   # 默认（测试用）
HL_MEM_EXTRACTOR=llm    # 生产用
```

## 6. 测试

### 单元测试
- `tests/unit/test_event_filter.py`：测试各种 event 类型的过滤决策
- `tests/unit/test_budget.py`：测试预算检查、重置、耗尽
- `tests/unit/test_llm_extractor.py`：用 mock 测试 LLM 响应解析（不实际调用 API）
  - 测试 JSON 解析容错（模型可能返回 markdown 包裹的 JSON）
  - 测试实体归一化
  - 测试空结果（should_memorize=false 时不生成 claim）

### 集成测试
- `tests/integration/test_extract_pipeline.py`：
  - 发送一个包含明确事实的 event（如"用户使用 PostgreSQL"）
  - 用 FakeExtractor 验证完整管道（filter → extract → claim → evidence → recall）
  - 验证 token budget 记录

### 验收标准
1. 所有现有测试仍然通过（7个）
2. 新增测试全绿
3. EventFilter 正确过滤 tool_result/短文本/确认语
4. TokenBudget 耗尽时不执行提取，job 保留 pending
5. LLM 响应解析容错（markdown 包裹、空 claims、格式错误）
6. 不安装任何 LLM SDK
7. HL_MEM_EXTRACTOR=fake 时行为与 Week 1 完全一致

## 约束
- 所有 LLM 调用用 httpx 或 urllib，不装 openai/dashscope SDK
- LLM 调用要有 timeout（30s）和重试（2次，指数退避）
- 实体归一化字典不超过 20 条
- 每个文件不超过 200 行
- 完成后运行 pytest 验证

完成后列出所有创建/修改的文件和测试结果。
