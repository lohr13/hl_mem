# v0.20.0 热修复任务

## 背景
hl_mem v0.20.0 部署后例行检查发现两个代码层面的问题需要修复。

## 修复一：canonical_attribute schema 正则过严导致 extract_event dead jobs

### 问题
`src/hl_mem/ingest/schemas.py:88` 中 `ExtractedClaimSchema.canonical_attribute` 使用了严格的正则校验：
```python
canonical_attribute: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
```

Pydantic 在反序列化 LLM JSON 响应时先执行正则校验，不通过就整个拒绝。LLM（qwen3.7-plus）经常返回不符合格式的值（中文、空字符串、大写等），导致 58 个 extract_event job 全部 dead。

但应用层 `src/hl_mem/domain/claims/attributes.py:575` 的 `validate_canonical_attribute()` 本来有完整的 normalize + fallback 到 `custom.unknown` 的机制——问题是 Pydantic 层先把它杀了，应用层的归一化逻辑根本没机会执行。

### 修复方案
将 `ExtractedClaimSchema.canonical_attribute` 的校验从严格正则放宽为 `min_length=1`，让应用层 `validate_canonical_attribute()` 来做归一化和回退。

### 验收
1. grep 确认 `schemas.py` 中 canonical_attribute 不再有 `pattern=` 正则
2. py_compile 通过
3. 不要运行 pytest

## 修复二：healthz 端点可能被 DB 锁阻塞

### 问题
`src/hl_mem/api/server.py` 的 healthz 端点依赖 `Depends(get_connection)` → `connection.execute("SELECT 1")`。当 worker 线程执行 `_run_maintenance()` 持有 `BEGIN IMMEDIATE` 事务时，healthz 的 DB 查询会被阻塞，导致整个 HTTP 服务看起来卡死（healthz 超时）。

### 修复方案
healthz 端点改为不查 DB，只返回进程级别的健康状态（version、组件状态等不需要 DB 的信息）。或者用一个独立的短超时连接（busy_timeout 很低）来做 DB 检查，失败时仍返回 200 但标注 db_status=degraded。

### 验收
1. grep 确认 healthz 不再依赖 `Depends(get_connection)` 做必须的 DB 查询
2. py_compile 通过
3. 不要运行 pytest

## 通用要求
- 不要运行 pytest（Windows 后台会崩溃）
- 不要修改其他无关文件
- 改完后 `git add -A && git commit -m "fix: loosen canonical_attribute schema + healthz DB-free"`
