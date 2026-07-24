# 任务：hl_mem Week 4 — Worker + TTL + Hermes Provider

请先阅读现有代码（src/hl_mem/ 全部文件和 tests/ 全部文件），了解当前架构。

## 目标
1. 独立的后台 Worker 进程，串行化执行所有写操作
2. TTL 自动过期
3. Hermes MemoryProvider 插件（timeout + circuit breaker + 无感降级）

## 1. Background Worker

创建 `src/hl_mem/workers/worker.py`：

从 jobs 表串行消费 job，按 job_type 分发执行。所有持久化写操作（claim 写入、embedding 生成、observation 构建、decay 等）都通过 Worker 执行，API 层只负责写入 events 和创建 job。

### Worker 主循环
```python
class Worker:
    def __init__(self, db_path, config):
        """初始化 Database、Repositories、Extractor、Embedder"""

    def run_once(self) -> dict:
        """拉取一个 pending job，执行，返回执行结果"""
        # 1. lease 一个 pending job (UPDATE ... SET status='running', leased_until=now+5min WHERE id=(SELECT id FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1))
        # 2. 按 job_type 分发
        # 3. 成功 → status='succeeded'，失败 → attempts++ 且 attempts < max_attempts → 'pending'，否则 'dead'

    def run_forever(self, poll_interval=2.0):
        """无限循环，每 poll_interval 秒检查一次"""
```

### Job 类型
- `extract_event`: 对指定 event 执行完整提取管道（filter → extract → dedup → conflict → claim → evidence → embedding → observation）
- `expire_ttl`: 扫描所有 ephemeral claims，将过期的标为 expired
- `retry_failed`: 重试 failed jobs

### Worker CLI
创建 `src/hl_mem/workers/cli.py` 或在 `__main__.py` 中：
```bash
python -m hl_mem.worker run          # run_forever
python -m hl_mem.worker run-once     # run_once
python -m hl_mem.worker status       # 打印 job 队列状态
```

## 2. TTL 自动过期

创建 `src/hl_mem/workers/ttl.py`：

```python
def expire_claims(connection, now: str | None = None) -> dict:
    """将所有 expires_at < now 的 ephemeral claims 标记为 expired"""
    # UPDATE claims SET status='expired' WHERE status='active' AND expires_at IS NOT NULL AND expires_at < ?
    # 返回 {"expired": count}
```

这作为一个 job_type='expire_ttl' 的 job 被 Worker 执行，或者在 Worker 主循环中定期直接执行。

建议：Worker 每 10 分钟自动执行一次 TTL 扫描（不通过 job 队列，直接在 run_forever 的循环中加一个计时器）。

## 3. Hermes Provider

创建 `src/hl_mem/adapters/hermes/provider.py`：

### Provider 类
```python
import time
from collections.abc import AsyncIterator

class HLMemProvider:
    """Hermes MemoryProvider adapter with timeout and circuit breaker."""

    def __init__(self, db_path=None, daemon_url=None, timeout=2.0):
        self.timeout = timeout
        self.daemon_url = daemon_url
        self._failure_count = 0
        self._failure_threshold = 5  # 连续失败5次后熔断
        self._circuit_open_until = 0.0  # 熔断恢复时间戳
        self._last_check = 0.0
        self._health_check_interval = 30.0  # 每30秒检查一次健康

    # --- Hermes MemoryProvider interface ---

    def initialize(self) -> None:
        """连接检查"""

    async def prefetch(self, query: str, limit: int = 10) -> dict:
        """召回，带 timeout 和 circuit breaker"""

    async def sync_turn(self, messages: list[dict]) -> None:
        """异步写入 events"""

    def on_memory_write(self, key: str, content: str, target: str = "memory") -> None:
        """显式记忆写入 → POST /v1/memories"""

    def on_pre_compress(self, messages: list[dict]) -> None:
        """压缩前 flush 未持久化的 events"""

    def shutdown(self) -> None:
        """清理资源"""
```

### Circuit Breaker 逻辑
```python
def _can_call(self) -> bool:
    """检查 circuit breaker 状态"""
    now = time.monotonic()
    if now < self._circuit_open_until:
        return False  # 熔断中
    return True

def _on_success(self):
    self._failure_count = 0

def _on_failure(self):
    self._failure_count += 1
    if self._failure_count >= self._failure_threshold:
        self._circuit_open_until = time.monotonic() + 60  # 熔断60秒
        self._failure_count = 0
```

### Timeout
所有 daemon HTTP 调用用 httpx，timeout=self.timeout（默认 2 秒）。
超时或连接失败时：
- prefetch → 返回空结果 `{"results": [], "error": "timeout"}`
- sync_turn → 静默丢弃（events 会通过 on_pre_compress 补写）
- on_memory_write → 静默丢弃或本地缓存重试

### Daemon 通信模式
Provider 通过 HTTP 与 hl_mem daemon（FastAPI server）通信。
- `POST /v1/events` — sync_turn 写入
- `POST /v1/recall` — prefetch 查询
- `POST /v1/memories` — on_memory_write
- `GET /healthz` — 健康检查

首版 Provider 可以不真正集成到 Hermes（那需要实现 Hermes 的 MemoryProvider ABC），而是先实现一个可独立测试的 adapter 类，后续再写 Hermes 插件注册代码。

## 4. API 改造

### server.py
- `POST /v1/events`：不再同步执行提取。只写 event + 创建 extract_event job。立即返回。
- 新增 `GET /v1/jobs`：返回 job 队列状态（pending/running/failed/dead 计数）

### pipeline.py 重构
将 `_queue_and_extract()` 中的提取逻辑移到 Worker 中执行。server.py 的 `_queue_and_extract()` 简化为只写 event + 创建 job。

## 5. 测试

### 单元测试
- `tests/unit/test_worker.py`：
  - run_once 执行 extract_event job
  - failed job 重试（attempts < max_attempts → pending）
  - dead job（attempts >= max_attempts → dead）
  - lease 机制（同一 job 不会被两个 worker 同时执行）

- `tests/unit/test_ttl.py`：
  - 过期的 ephemeral claim → expired
  - 未过期的不受影响
  - stable claim 不被过期

- `tests/unit/test_provider.py`：
  - 正常调用返回结果
  - daemon 超时 → 返回空结果
  - 连续失败5次 → 熔断60秒
  - 熔断期间不调用 daemon
  - 熔断恢复后正常工作

### 集成测试
- `tests/integration/test_worker_e2e.py`：
  - 发送 event → job 创建 → worker run_once → claim 存在 → recall 能查到
  - 进程重启后 worker 能恢复执行 pending jobs

### 验收标准
1. 所有现有测试通过（28个）
2. 新增测试全绿
3. Worker 串行消费 jobs，不并发
4. TTL 过期生效
5. Provider timeout 2 秒生效
6. Circuit breaker 连续失败5次后熔断
7. daemon 不可用时 Provider 无感降级（返回空结果，不抛异常）
8. 每个文件不超过 200 行
9. 完成后运行 pytest 验证

## 约束
- Worker 是独立进程（可被 CLI 启动），不是 API 进程的线程
- Worker 通过 SQLite WAL 读写（单写者，串行化）
- Provider 不依赖 Hermes 内部 API（可独立测试）
- 不安装新的外部依赖
- 完成后运行 pytest，列出创建/修改的文件和测试结果
