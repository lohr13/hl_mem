# HL-Mem 项目 AGENTS.md

## 项目概述

HL-Mem 是面向 AI Agent 的本地优先记忆系统。核心设计：事件溯源双通道 + 双时间模型 + 证据链 + slot+tags 分类体系 + importance 联动 TTL + 多因子召回 + 完整生命周期管理。

**当前版本：v0.10.0（2026-07-24）**

## 技术栈

- **运行时**：Python 3.11+，FastAPI + uvicorn
- **存储**：SQLite WAL + FTS5（全文检索）+ 向量 BLOB（暴力余弦，首版）
- **LLM 提取**：glm-5.2（智谱 Coding Plan），JSON mode
- **Embedding**：text-embedding-v4（百炼通用 AK），2048 维
- **Reranker**：gte-rerank-v2（百炼通用 AK）
- **分类体系**：SLOT_REGISTRY（15 operational slot + 40 topic tags；Phase 18 已接入检索，soft boost 默认开启，独立 tag channel 默认关闭待评测）
- **TTL**：retention 纯函数（scope × importance 三档）
- **跨 subject 去重**：DedupJudge（audit-only 默认开启）
- **包管理**：uv（lockfile: uv.lock）
- **测试**：pytest + pytest-asyncio（asyncio_mode=auto）284 tests

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
│   ├── decay.py               # 置信度衰减
│   ├── consolidate.py         # LLM 语义归并
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

## 测试

```bash
.venv/Scripts/python.exe -m pytest tests/unit/ -q --tb=short
```

当前：284 tests passed。

## 关键设计决策

### 写入管线
- fact_hash v2（JSON 数组有边界哈希）→ conflict_key（canonical attribute slot）→ semantic dedup（cosine > 0.82, best-match）
- 冲突判定：确定性规则优先（ConflictResolver），灰区走 LLM 四分类（ConflictConsolidator）
- **事务原子化**：整个写入流程（update_status + insert_claim + supersede + evidence_link）在单一 BEGIN IMMEDIATE 中

### 召回管线
- FTS5 全文检索 + dense vector 余弦相似度 → RRF 融合
- 多因子排序：recency / importance / access_count / scope / helpful_rate
- 可选 reranker（gte-rerank-v2）
- 双时间过滤：valid_from/valid_to + recorded_from/recorded_to
- **上下文预算**：token_budget + context_mode="packed" + 跨类型配额
- 偏好专用召回 intent（RecallIntent.PREFERENCE）
- **派生记忆接入**：recall 自动查询活跃 derivation 并填充 observations

### 生命周期管理
- TTL 过期（ephemeral）→ 线性衰减（temporal/permanent 分级）→ 归档（embedding 清空）→ 重分类
- 访问频率延缓衰减（每 10 次召回 +30 天）
- **冲突终态收敛**：conflict_cases 状态机（pending → auto_resolved/manual_required → resolved/rejected）
- stale 传播：claim 撤回时关联的 derivation 自动标记 stale

### 架构分层
- **api/** 是适配层（FastAPI DTO + 路由），不含业务逻辑
- **application/** 是应用服务层，拥有事务边界
- **domain/** + **core/** 是纯函数，不依赖基础设施
- **storage/** 是数据访问层，只依赖 domain 和 core
- **workers/** 是后台调度，通过 application 服务操作数据
- **lifecycle.py** 是状态机守卫，所有状态变更统一经过 assert_transition()

## 配置

环境变量（关键项）：
- `HL_MEM_ENV` — dev/production
- `HL_MEM_DB_PATH` — SQLite 路径
- `HL_MEM_EMBEDDER` — fake/real
- `HL_MEM_RERANKER` — off/fake/on/real
- `LLM_API_KEY` — 百炼 Coding AK
- `EMBEDDING_API_KEY` — 百炼通用 AK
- `HL_MEM_DECAY_TEMPORAL_DAYS` / `HL_MEM_DECAY_PERMANENT_DAYS` — 衰减策略
- `HL_MEM_DEDUP_THRESHOLD` — 语义去重阈值

集中配置：`config.py`（常量）+ `settings.py`（Settings 校验）+ `components.py`（工厂）

## Migration

21 个 SQL migration（001-021），按版本号顺序执行。不可变。
