# 任务：hl_mem Week 1 — 项目骨架 + SQLite Schema + Repository + 测试框架

请阅读 docs/architecture.md 的 Section 5（核心数据模型）和 docs/implementation-plan.md 的 Phase 1，然后搭建项目骨架。

## 目标
完成可测试的持久化闭环：event 写入 → FTS 召回 → 证据化返回，不依赖任何外部 LLM/API。

## 具体任务

### 1. Python 项目骨架
- 包管理：`uv`（pyproject.toml）
- Python >=3.11
- 依赖：`sqlite-utils` 或直接用 stdlib `sqlite3`（建议用 stdlib，零外部依赖）
- HTTP 框架：`fastapi` + `uvicorn`（轻量、ASGI 标准）
- 测试：`pytest` + `pytest-asyncio`
- 类型检查：`mypy` 或内置类型注解即可
- 创建 `src/hl_mem/` 下的子包：`api/`, `domain/`, `storage/`, `ingest/`, `recall/`, `workers/`, `adapters/`
- 创建 `tests/` 下的子目录：`unit/`, `integration/`, `scenarios/`

### 2. SQLite Schema（migration 001）
创建 `src/hl_mem/storage/migrations/001_initial.sql`，包含以下表：

#### events
```sql
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,           -- ULID
    idempotency_key TEXT UNIQUE,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    user_id TEXT,
    project_id TEXT,
    agent_id TEXT,
    session_id TEXT,
    event_type TEXT NOT NULL,      -- message|tool_call|tool_result|feedback|task_end|explicit_memory
    actor_type TEXT NOT NULL,      -- user|assistant|tool|system
    actor_id TEXT,
    content_json TEXT NOT NULL,    -- JSON string
    occurred_at TEXT NOT NULL,     -- ISO8601
    recorded_at TEXT NOT NULL,     -- ISO8601
    source_uri TEXT,
    content_hash TEXT,             -- SHA256 of content_json
    sensitivity TEXT DEFAULT 'normal'
);
```

#### claims
```sql
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    namespace_key TEXT NOT NULL DEFAULT 'default',
    subject_entity_id TEXT,
    predicate TEXT,
    value_json TEXT,
    qualifiers_json TEXT,
    conflict_key TEXT,
    valid_from TEXT,
    valid_to TEXT,
    recorded_from TEXT NOT NULL,
    recorded_to TEXT,
    observed_at TEXT,
    expires_at TEXT,
    refresh_after TEXT,
    volatility TEXT NOT NULL DEFAULT 'stable',  -- ephemeral|stable (首版只有这两档)
    status TEXT NOT NULL DEFAULT 'candidate',    -- candidate|active|superseded|disputed|retracted|expired|archived
    confidence REAL DEFAULT 0.5,
    importance REAL DEFAULT 0.5,
    source_authority TEXT DEFAULT 'medium',
    supersedes_id TEXT,
    extractor_version TEXT,
    -- 预留 embedding 列（首版用 BLOB）
    embedding_dense BLOB,          -- float32 array, 2048d
    embedding_sparse BLOB,         -- index→weight map
    embedding_model TEXT,
    embedding_dim INTEGER,
    FOREIGN KEY (supersedes_id) REFERENCES claims(id)
);
CREATE INDEX IF NOT EXISTS idx_claims_conflict_key ON claims(conflict_key) WHERE conflict_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_namespace ON claims(namespace_key);
```

#### evidence_links
```sql
CREATE TABLE IF NOT EXISTS evidence_links (
    id TEXT PRIMARY KEY,
    derived_type TEXT NOT NULL,    -- claim|observation
    derived_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,   -- event|claim
    evidence_id TEXT NOT NULL,
    relation TEXT NOT NULL,        -- supports|contradicts|derived_from|supersedes
    weight REAL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_ev_derived ON evidence_links(derived_type, derived_id);
CREATE INDEX IF NOT EXISTS idx_ev_evidence ON evidence_links(evidence_type, evidence_id);
```

#### jobs
```sql
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload_json TEXT,
    idempotency_key TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|succeeded|failed|dead
    run_after TEXT,
    leased_until TEXT,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type);
```

#### derivations (observations)
首版只用于 observation：
```sql
CREATE TABLE IF NOT EXISTS derivations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'observation',  -- observation (首版只有这一种)
    name TEXT,
    query TEXT,
    body TEXT NOT NULL,
    scope_json TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active|stale|rebuilding|archived
    confidence REAL DEFAULT 0.5,
    generated_by_model TEXT,
    prompt_version TEXT,
    source_watermark TEXT,
    refresh_policy TEXT,
    updated_at TEXT NOT NULL
);
```

### 3. FTS 虚拟表
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    content_text,
    content_json,
    content='events',
    content_rowid='rowid'
);
-- triggers 保持 FTS 同步
```

同时为 claims 创建 FTS（方便召回 claim 文本）：
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(
    search_text,
    content='claims',
    content_rowid='rowid'
);
```

### 4. Repository 层
创建 `src/hl_mem/storage/repository.py`，包含：
- `EventRepository`: insert_event (幂等), get_event, search_events_fts
- `ClaimRepository`: insert_claim, get_claim, update_status, search_claims_fts
- `EvidenceRepository`: add_link, get_links_for_derived, get_links_for_evidence
- `JobRepository`: insert_job, lease_job, complete_job, fail_job
- `DerivationRepository`: insert_observation, get_observation, update_status

所有 Repository 接收一个 `sqlite3.Connection`，不管理连接生命周期。
连接管理在 `storage/database.py` 的 `Database` 类中（WAL 模式、migrations runner）。

### 5. API 端点（FastAPI）
创建 `src/hl_mem/api/server.py`：
- `POST /v1/events` — 接收事件，写入 events 表（幂等），触发 extract_event job，立即返回
- `POST /v1/recall` — 接收查询，做 FTS 搜索 + 时间过滤，返回 Context Packet（带 evidence）
- `GET /healthz` — 健康检查

Context Packet 格式：
```json
{
  "results": [
    {
      "type": "claim",
      "id": "01J...",
      "text": "用户使用 PostgreSQL",
      "status": "active",
      "confidence": 0.9,
      "valid_from": "2026-07-01",
      "evidence": [
        {"type": "event", "id": "01J...", "occurred_at": "2026-07-01T10:00:00"}
      ]
    }
  ],
  "total": 1,
  "query_id": "req-xxx"
}
```

### 6. Fake Extractor + Fake Embedder
创建 `src/hl_mem/ingest/extractors.py`：
- `FakeExtractor`: 从 event content_json 中用简单规则提取 claim（比如检测"我喜欢X"/"记住X"模式）
- `FakeEmbedder`: 返回固定长度的随机向量（测试用）

### 7. 中文测试集
创建 `tests/scenarios/chinese_test_cases.py`，包含 30 条真实风格的中文对话场景：
- 偏好更新（"我喜欢深色模式" → 后改"用浅色模式"）
- 实时信息（"服务X现在挂了" → TTL 过期）
- 矛盾（两个来源给不同值）
- 删除（"忘掉X"）
- 相似任务复用
- 代词消解（"那个项目" → 需上下文）
- 实体别名（"PG"/"PostgreSQL"/"postgres"）

每条包含：输入事件、期望召回结果、期望状态（active/superseded/disputed/expired）

### 8. 集成测试
创建 `tests/integration/test_e2e.py`：
- 测试幂等：同一事件发两次，只有一条记录
- 测试跨会话：写入两个 session 的事件，第三个 session 能召回
- 测试证据：召回结果始终包含 Event Evidence
- 测试进程重启：写入 → 关闭 DB → 重开 → 召回仍然有效

## 验收标准（测试必须全绿）
1. `POST /v1/events` 幂等：重复发送同一 idempotency_key 不产生重复
2. `POST /v1/recall` 能按关键词召回
3. 召回结果包含 evidence_links
4. 进程重启后数据持久
5. 中文测试集可运行

## 约束
- 不要安装任何 LLM SDK（openai/dashscope/zhipuai）
- 不要写 Experience 通道相关代码
- 不要写 MCP Server
- 所有配置通过环境变量或 config 文件
- 保持代码简洁，每个文件不超过 200 行

完成后列出所有创建的文件路径。
