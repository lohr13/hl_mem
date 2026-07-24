# HL-Mem

> v0.10.1 · 292 passed · 1 skipped · 21 migrations · [CHANGELOG](docs/CHANGELOG.md)

面向 AI Agent 的本地优先、跨会话记忆系统。证据驱动、双时间模型、双通道设计、可解释召回、slot+tags 分类体系、importance 联动 TTL。

## 设计理念

| 现有方案 | 优势 | 我们的改进 |
|---------|------|-----------|
| Mem0 | 轻量、LLM 驱动提取 | 增加双时间模型、证据链与 slot+tags 分类 |
| Zep | 时间感知知识图 | 用 SQLite + FTS5 实现本地优先，无需外部服务 |
| LangMem | Profiles/Collections 双轨分类 | slot 管理冲突、TTL、去重，tags 支持开放多值检索 |
| Letta/ADEPT | 长期记忆与自主 Agent | 聚焦记忆基础设施，通过 Hermes Provider 解耦 Agent |

HL-Mem 将这些理念统一为**事件溯源双通道**设计：事实通道处理结构化知识提取、TTL、冲突、去重与证据链，经验通道记录工具调用轨迹（Episode + Trace + Reward），并提供可解释召回与完整遗忘治理。

## 核心架构

```
                          ┌─────────────────────────────────┐
                          │          API Layer              │
                          │  REST (FastAPI)  ·  MCP Server  │
                          └────────────┬────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────┐
                    │       Application Services          │
                    │  IngestService · RecallService      │
                    │  ForgetService · ExperienceService  │
                    └───┬──────────┬──────────┬──────────┘
                        │          │          │
              ┌─────────▼──┐  ┌───▼────┐  ┌──▼──────────┐
              │  Ingest     │  │ Recall  │  │  Workers    │
              │  Extractor  │  │ FTS+Vec │  │  TTL/Decay  │
              │  Embedder   │  │ Rerank  │  │  Consolidate│
              │  Retention  │  │ RRF     │  │  Deduplicate│
              │  Filter     │  │ Slot    │  │  Induce     │
              └──────┬──────┘  └────┬────┘  └──────┬──────┘
                     │              │              │
                     └──────────────┼──────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │       Storage Layer           │
                    │  SQLite WAL + FTS5 + Vector   │
                    │  21 Migrations · 7 Tables     │
                    │  + Audit · Backup · Retention │
                    │  + Dedup Pairs · Slot Tags    │
                    └───────────────────────────────┘
```

### 写入管线

```
用户对话 → POST /v1/events → 幂等写入 events 表 → 创建 extract_event Job
                                                              ↓
Worker (串行消费) ← EventFilter → 过滤低价值事件
                    ↓ 通过
               LLMExtractor → glm-5.2 / qwen3.7-plus 提取
                   （前序上下文窗口 + 时间锚定 + ADD-only）
                    ↓
               fact_hash v2 精确去重 → 命中则合并证据跳过
                    ↓ 新事实
               ConflictResolver → conflict_key 匹配（确定性，无 LLM）
                    ↓ 灰区
               ConflictConsolidator → LLM 四分类归并
                    ↓
               Deduplicator → 语义去重 (cosine > 0.82, best-match)
                    ↓
               Embedder → text-embedding-v4 2048d → BLOB 存储
                    ↓
               ObservationBuilder → ≥2 独立证据 → 生成 Observation
                    ↓
                    全流程在单一 BEGIN IMMEDIATE 事务中（原子化）
```

### 召回管线

```
POST /v1/recall
       ↓
  RecallIntent 路由（general / preference / fact / ...）
       ↓
  FTS5 BM25 + Dense Vector 余弦相似度
       ↓
  RRF (Reciprocal Rank Fusion) 融合
       ↓
  双时间过滤（valid_from/to + recorded_from/to）
       ↓
  多因子排序（recency · importance · access_count · scope · helpful_rate）
       ↓
  可选 Reranker（gte-rerank-v2）
       ↓
  上下文预算打包（token_budget + context_mode="packed"）
       ↓
  派生记忆接入（recall 自动查询活跃 derivation 并填充 observations）
       ↓
  返回带证据链的 Context Packet
```

## 代码结构

```
src/hl_mem/
├── api/                    # FastAPI 适配层
│   ├── server.py              # REST API (14 routes)
│   └── schemas.py             # Pydantic DTO
├── application/            # 共享应用服务
│   ├── ingest.py              # IngestService
│   ├── recall.py              # RecallService
│   └── forget.py              # ForgetService
├── domain/                 # 纯领域逻辑（不依赖基础设施）
│   ├── claims/                # claim 写入/冲突/去重/retention/query_tags
│   ├── temporal.py            # 双时间可见性
│   ├── relations.py           # 记忆关系
│   ├── entity.py              # 实体归一化
│   ├── recall.py              # 召回领域逻辑
│   └── content.py             # 多模态内容协议
├── core/                   # 纯数学
│   └── vector.py              # cosine similarity
├── ingest/                 # 数据摄入
│   ├── llm_extractor.py       # LLM 提取器
│   ├── extractors.py          # FakeExtractor / LLMExtractor
│   ├── chunking.py            # 结构感知分块
│   ├── embedder.py            # text-embedding-v4 向量化
│   ├── event_filter.py        # 事件预过滤
│   └── budget.py              # Token 预算控制
├── llm/                    # LLM 客户端（Provider 解耦）
│   ├── client.py              # LLMClient
│   ├── providers.py           # 百炼/智谱/OpenAI-compatible
│   └── types.py               # LLMRequest/LLMResponse
├── recall/                 # 召回层
│   ├── staged_pipeline.py     # 三通道 RRF (FTS + Dense + Tag)
│   ├── trace.py               # SearchTrace 可观测性
│   ├── ranking.py             # 多因子排序
│   ├── reranker.py            # gte-rerank-v2 重排器
│   ├── relation_expansion.py  # 一跳关系扩展
│   └── observation.py         # 派生记忆构建
├── storage/                # 存储层（按职责拆分）
│   ├── database.py            # SQLite WAL + migration runner
│   ├── claims.py              # ClaimRepository
│   ├── events.py              # EventRepository
│   ├── evidence.py            # EvidenceRepository
│   ├── experience.py          # ExperienceRepository
│   ├── jobs.py                # JobRepository
│   ├── backup.py              # 在线备份
│   └── migrations/            # 21 SQL migrations (001-021)
├── workers/                # 后台任务
│   ├── worker.py              # Job 调度器
│   ├── ttl.py                 # TTL 过期
│   ├── decay.py               # 置信度线性衰减
│   ├── consolidate.py         # LLM 语义冲突归并
│   ├── deduplicate.py         # 跨 subject 语义去重
│   ├── backfill_expires_at.py # TTL 回填工具
│   └── induce_policies.py     # 策略归纳
├── experience/             # Experience 通道
│   └── service.py             # Episode/Trace/Policy
├── adapters/hermes/        # Hermes 集成
│   ├── provider.py            # HermesMemoryProvider
│   └── plugin/                # 薄委托层
├── mcp/
│   └── server.py              # MCP 工具契约
├── components.py           # 统一组件工厂
├── settings.py             # Settings dataclass + 校验
├── protocols.py            # 接口协议
├── errors.py               # 异常族
├── http_utils.py           # 统一重试工具
├── lifecycle.py            # 状态机守卫
└── cli.py                  # CLI 入口
```

## API 端点

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/v1/events` | 写入事件（幂等），创建提取 Job |
| POST | `/v1/recall` | 混合检索（FTS + Dense + RRF + Rerank） |
| POST | `/v1/memories` | 显式保存（pinned claim） |
| DELETE | `/v1/memories/{id}` | 显式遗忘（级联撤回） |
| POST | `/v1/episodes` | 创建 Episode（经验通道） |
| GET | `/v1/episodes` | 列出 Episode |
| GET | `/v1/episodes/{id}` | Episode 详情 |
| PATCH | `/v1/episodes/{id}` | 更新 Episode（状态转换 + 奖励回传） |
| POST | `/v1/episodes/{id}/traces` | 追加 Trace |
| POST | `/v1/feedback` | 反馈归因（helpful_rate 更新） |
| GET | `/v1/policies` | 列出归纳策略 |
| GET | `/v1/jobs` | Job 队列状态 |
| GET | `/v1/stats` | 统计信息 |
| GET | `/healthz` | 健康检查（含版本号） |

## 快速开始

### 环境要求

- Python >= 3.11
- uv（推荐）或 pip

### 安装

```bash
git clone git@github.com:lohr13/hl_mem.git
cd hl_mem
uv sync
```

### 配置

复制 `.env.example` 并填入 API key（需要两把不同的百炼 key）：

```bash
cp .env.example .env
# 编辑 .env 填入你的 key
```

<details>
<summary>.env 配置说明</summary>

```bash
# === LLM 提取 — 百炼 Coding Plan ===
LLM_API_KEY=sk-sp-xxx              # Coding Plan 专用 AK
LLM_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
LLM_MODEL=glm-5.2                  # 或 qwen3.7-plus

# === Embedding — 百炼通用 AK ===
EMBEDDING_API_KEY=sk-e72xxx        # 通用 AK（与 Coding Plan 不同）
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIM=2048

# === Reranker — 百炼通用 AK（可选）===
HL_MEM_RERANKER=on                 # off | fake | on | real
RERANKER_MODEL=gte-rerank-v2

# === 运行模式 ===
HL_MEM_EXTRACTOR=llm               # fake | llm
HL_MEM_EMBEDDER=real               # fake | real
HL_MEM_ENV=dev                     # dev | production
```

> **百炼双 Key 架构**：Coding Plan AK 只能打 `coding.dashscope` 端点，通用 AK 只能打 `compatible-mode` 端点，两把 key 互不通用。

</details>

### 运行测试

```bash
# 全量离线测试（FakeExtractor + FakeEmbedder，不需要 API key）
.venv/Scripts/python.exe -m pytest tests/ -q

# 真实 API 端到端测试
python tests/e2e_real.py
```

### 启动服务

```bash
# 方式 1：一键启动（推荐，自动加载 .env + 启动 Worker + 启动 API）
python start_server.py

# 方式 2：手动分离启动
uvicorn hl_mem.api.server:app --host 127.0.0.1 --port 8200   # API
python -m hl_mem.workers.worker run                            # Worker

# 方式 3：生产模式
start_production.bat    # Windows，强制 real embedder + reranker
```

服务默认运行在 `http://127.0.0.1:8200`。

### Hermes 集成

```bash
# 将 Hermes provider 插件部署到 Hermes Agent
python install_to_hermes.py --hermes-home ~/.hermes
# 重启 Hermes 生效
```

## 项目状态

| 组件 | 状态 |
|------|------|
| SQLite Schema（21 migrations） | ✅ |
| 幂等事件写入 + 事务原子化 | ✅ |
| LLM 提取（前序上下文 + 时间锚定 + ADD-only） | ✅ |
| Event Filter + Token Budget | ✅ |
| Embedding（text-embedding-v4 2048d） | ✅ |
| 去重（fact_hash v2 → conflict_key → semantic 0.82） | ✅ |
| 冲突检测（白名单互斥模型，5 slots） | ✅ |
| 数据质量（实体归一化 + canonical attribute reconcile） | ✅ |
| Observation 生成（≥2 独立证据） | ✅ |
| 混合召回（FTS + Dense + RRF + Reranker） | ✅ |
| Experience 通道（Episode + Trace + Policy + 奖励回传） | ✅ |
| 生命周期管理（TTL + 衰减 + 归档 + 重分类） | ✅ |
| 显式遗忘（级联撤回 + 向量清除 + stale 传播） | ✅ |
| Hermes Provider（2s timeout + circuit breaker） | ✅ |
| MCP Server（4 工具契约） | ✅ |
| 审计日志 | ✅ |
| 在线备份 + CLI 导入导出 | ✅ |
| 可选 PostgreSQL 后端 | ✅ |
| 284 tests passed | ✅ |
| Mental Model 深化 | 📋 基础已实现，推理增强延后 |
| 多租户 | 📋 设计保留 |

## 设计文档

| 文档 | 内容 |
|------|------|
| [CHANGELOG.md](docs/CHANGELOG.md) | 版本变更时间线 |
| [architecture.md](docs/architecture.md) | 完整架构设计 |
| [HANDOFF.md](docs/HANDOFF.md) | 项目交接状态 |
| [implementation-plan.md](docs/implementation-plan.md) | 分阶段实施计划 |
| [adr/0001-core-strategy.md](docs/adr/0001-core-strategy.md) | ADR：双通道架构选型 |
| [adr/0002-mvp-scope-and-embedding.md](docs/adr/0002-mvp-scope-and-embedding.md) | ADR：首版范围 + Embedding 选型 |
| [review/consensus.md](docs/review/consensus.md) | 首版设计共识 |
| [refactor-phase*.md](docs/) | 架构重构各阶段详细记录 |

## License

[Apache License 2.0](LICENSE) (`Apache-2.0`)
