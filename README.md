# HL-Mem

> v0.3.0 · 220 tests · [CHANGELOG](docs/CHANGELOG.md)

面向 AI Agent 的本地优先、跨会话记忆系统。证据驱动、双时间模型、双通道设计、可解释召回。

## 为什么自建

现有记忆方案各有侧重但都不完整：

| 方案 | 优势 | 缺失 |
|------|------|------|
| Hindsight | 事实提取、时间演化、Observation 归纳 | 缺 TTL、Procedure、严格 scope |
| MemOS | Episode/Trace/Reward/Skill 经验通道 | 事实有效期和双时间历史非核心抽象 |
| 向量库 / 聊天摘要 | 相似搜索或压缩 | 无法处理"何时写入、怎样失效、如何遗忘" |

HL-Mem 将两者合并为统一的**事件溯源双通道**设计：事实通道参考 Hindsight，经验通道参考 MemOS，自己实现时间、作用域、遗忘和删除治理。

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
              │  Filter     │  │ RRF     │  │  Induce     │
              └──────┬──────┘  └────┬────┘  └──────┬──────┘
                     │              │              │
                     └──────────────┼──────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │       Storage Layer           │
                    │  SQLite WAL + FTS5 + Vector   │
                    │  14 Migrations · 5 Tables     │
                    │  + Audit · Backup · Retention │
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
│   ├── schemas.py             # Pydantic DTO
│   └── pipeline.py            # 向后兼容 re-export
├── application/            # 共享应用服务（REST + MCP + Worker 统一入口）
│   ├── ingest.py              # IngestService
│   ├── recall.py              # RecallService
│   └── forget.py              # ForgetService
├── domain/                 # 纯领域逻辑（不依赖基础设施）
│   ├── temporal.py            # 双时间可见性
│   ├── relations.py           # 记忆关系
│   ├── entity.py              # 实体归一化
│   └── content.py             # 多模态内容协议
├── core/                   # 纯数学
│   └── vector.py              # cosine similarity
├── ingest/                 # 数据摄入
│   ├── llm_extractor.py       # LLM 提取器
│   ├── embeddings.py          # text-embedding-v4 向量化
│   ├── event_filter.py        # 事件预过滤
│   └── budget.py              # Token 预算控制
├── recall/                 # 召回层
│   ├── recall_pipeline.py     # FTS + Vector + RRF + 多因子排序
│   ├── extended_pipeline.py   # RRF + 上下文预算打包
│   ├── dedup.py               # L1 精确 + L2 语义去重 (0.82)
│   ├── conflict.py            # 确定性冲突判定（白名单互斥模型）
│   ├── attribute_map.py       # canonical_attribute 规范化
│   ├── reranker.py            # gte-rerank-v2 重排器
│   └── observation.py         # 派生记忆构建
├── storage/                # 存储层
│   ├── database.py            # SQLite WAL + migration runner
│   ├── repository.py          # CRUD (5 Repositories)
│   ├── backup.py              # 在线备份
│   └── migrations/            # 14 SQL migrations (001-014)
├── workers/                # 后台任务
│   ├── worker.py              # 7 种 job_type + maintenance 调度
│   ├── ttl.py                 # ephemeral TTL 过期
│   ├── decay.py               # 置信度线性衰减
│   ├── consolidate.py         # LLM 语义冲突归并
│   ├── reclassify.py          # LLM 重分类
│   ├── mental_models.py       # 派生记忆维护
│   └── induce_policies.py     # 策略归纳
├── experience/             # Experience 通道
│   └── service.py             # Episode/Trace/Policy + 状态机
├── security/
│   └── retention.py           # 事件保留清理
├── observability/
│   └── audit.py               # 审计日志
├── adapters/hermes/        # Hermes 集成
│   ├── provider.py            # HermesMemoryProvider (httpx)
│   └── plugin/                # 薄委托层
├── mcp/
│   └── server.py              # MCP 工具契约
├── components.py           # 统一组件工厂
├── config.py               # 集中配置常量
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
| SQLite Schema（14 migrations） | ✅ |
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
| 220 测试全绿 | ✅ |
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

MIT
