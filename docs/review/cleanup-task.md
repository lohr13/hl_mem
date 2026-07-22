# 记忆质量修复任务

## 背景
hl_mem 数据库有严重的记忆质量问题：(1) 语义重复 claim 大量穿透去重（cosine threshold=0.95 太高）；
(2) 提取器把工具状态报告/版本号/健康检查等噪声也提取成了 claim；(3) Policy 归纳永远产出 0 条，
因为 min_support=3 太高且要求 action 序列完全一致。

## 需要修改的 4 个文件

### 1. src/hl_mem/recall/dedup.py — 降低去重阈值
**当前** (line 11):
```python
class Deduplicator:
    def __init__(self, claim_repo: ClaimRepository, embedder: Any, threshold: float = 0.95) -> None:
```
**改为**:
```python
class Deduplicator:
    def __init__(self, claim_repo: ClaimRepository, embedder: Any, threshold: float = 0.85) -> None:
```
理由：0.95 只能拦截几乎逐字相同的 claim，微小的改写（如"坚持方案B"vs"坚持方案 B，在 hl_mem 内部自研"）的 cosine similarity 通常在 0.85-0.92 之间，当前全部穿透。

### 2. src/hl_mem/ingest/llm_extractor.py — 在 SYSTEM_PROMPT 中增加噪声过滤规则
在现有 SYSTEM_PROMPT 的末尾（line 20 `不要输出 JSON 以外的解释。`**之前**）插入以下段落：

```
跳过以下低价值信息，不要提取为 claim：
- 服务健康状态报告（如 healthz 返回值、服务状态 ok/running/stopped、版本号查询结果）
- 工具自身的实现细节（如 git commit hash、文件行数、测试数量、迁移编号、数据库审计日志条数）
- 脱离上下文的纯数字、纯版本号、纯路径（value 少于 5 个字符或仅为数字和点号的组合时不提取）
- 临时调试输出、中间步骤状态报告（如"正在处理..."、"已启动 Codex"）
- 已被覆盖的旧配置值（如 superseded 的 provider 变更历史）
如果 should_memorize 为 false 或所有 claim 都属于上述类型，返回空 claims 列表。
```

### 3. src/hl_mem/ingest/event_filter.py — 增加系统状态消息过滤
在 EventFilter 类中增加对 assistant 消息中的工具状态报告的过滤。

当前 should_extract 方法只过滤：acknowledgement / too_short / raw_tool_output。
需要增加过滤：assistant 发出的、内容看起来像是工具/服务状态报告的消息（如包含 "healthz"、"服务运行中"、"版本号"、"commit"、"测试通过" 等关键词的纯状态汇报）。

**实现**：在 should_extract 方法中，在 `return True, "eligible"` 之前增加一个检查：
```python
if event.get("actor_type") == "assistant":
    if self._is_status_report(text):
        return False, "status_report"
```
并增加方法：
```python
@staticmethod
def _is_status_report(text: str) -> bool:
    """检测 assistant 的纯状态汇报消息（无用户价值的事实）。"""
    status_patterns = [
        "healthz", "服务运行中", "服务健康", "服务状态",
        "git commit", "测试通过", "测试全绿", "passed",
        "迁移完成", "migration", "已推送",
    ]
    text_lower = text.lower()
    # 只过滤短消息（<200字符），长消息可能包含有价值内容
    if len(text) > 200:
        return False
    return any(p.lower() in text_lower for p in status_patterns)
```

### 4. src/hl_mem/workers/induce_policies.py — 降低 min_support 并放宽 action 匹配
**当前** (line 39): `service = ExperienceService(connection, min_support=3)`
**改为**: `service = ExperienceService(connection, min_support=2)`

同时在聚类逻辑中，把 action 序列匹配从**完全一致**改为**前缀匹配**（取前 3 个 action 作为聚类 key，而非整个序列）：

**当前** (line 30-37):
```python
actions = tuple(
    trace["action"]
    for trace in connection.execute(
        "SELECT action FROM traces WHERE episode_id=? ORDER BY sequence_no", (row["id"],)
    ).fetchall()
)
if actions:
    clusters[(task_type, actions)].append(dict(row))
```
**改为**:
```python
actions = tuple(
    trace["action"]
    for trace in connection.execute(
        "SELECT action FROM traces WHERE episode_id=? ORDER BY sequence_no LIMIT 5", (row["id"],)
    ).fetchall()
)
if actions:
    # 用前3个action作为聚类key，而非完整序列
    prefix = actions[:3]
    clusters[(task_type, prefix)].append(dict(row))
```

## 验收标准
1. 所有修改后 `grep "0.85" src/hl_mem/recall/dedup.py` 能找到
2. `grep "status_report" src/hl_mem/ingest/event_filter.py` 能找到
3. `grep "min_support=2" src/hl_mem/workers/induce_policies.py` 能找到
4. `grep "前3个action" src/hl_mem/workers/induce_policies.py` 或 `prefix` 能找到
5. `grep "低价值" src/hl_mem/ingest/llm_extractor.py` 能找到
6. 不要运行 pytest，只做 git add 和 git commit
