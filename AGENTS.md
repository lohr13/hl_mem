# HL-Mem 项目 AGENTS.md

## 项目概述

HL-Mem 是面向 AI Agent 的本地优先记忆系统。核心设计：事件溯源双通道 + 双时间模型 + 证据链 + slot+tags 分类体系 + importance 联动 TTL + 多因子召回 + 完整生命周期管理。

**当前版本：v0.9.1（2026-07-24）**

## 技术栈

- **运行时**：Python 3.11+，FastAPI + uvicorn
- **存储**：SQLite WAL + FTS5（全文检索）+ 向量 BLOB（暴力余弦，首版）
- **LLM 提取**：glm-5.2（智谱 Coding Plan），JSON mode
- **Embedding**：text-embedding-v4（百炼通用 AK），2048 维
- **Reranker**：gte-rerank-v2（百炼通用 AK）
- **分类体系**：SLOT_REGISTRY（15 operational slot + 40 topic tags；topic_tags 当前用于存储、统计和分类，尚未接入检索）
- **TTL**：retention 纯函数（scope × importance 三档）
- **跨 subject 去重**：DedupJudge（audit-only 默认开启）
- **包管理**：uv（lockfile: uv.lock）
- **测试**：pytest + pytest-asyncio（asyncio_mode=auto）250+ tests

## 代码结构

```
src/hl_mem/
├── api/                    # FastAPI 适配层
│   ├── server.py              # REST API
│   └── schemas.py             # Pydantic DTO（EventInput, RecallInput, MemoryInput 等）
├── application/            # 共享应用服务层（REST + MCP + Worker 统一入口）
│   ├── ingest.py              # IngestService：事件写入 + 记忆保存 + retention 调用
│   ├── recall.py              # RecallService：混合召回 + 上下文组装 + 冲突包
│   └── forget.py              # ForgetService：撤回 + 清除向量 + stale 传播
├── domain/                 # 纯领域逻辑（不依赖基础设施）
│   ├── temporal.py            # RecallIntent + claim_is_visible（双时间可见性）
│   ├── relations.py           # 记忆关系管理（summarizes/supports/follows/about）
│   └── content.py             # 多模态内容协议（ContentPart/TextPart/FileTextPart）
├── core/                   # 纯数学函数
│   └── vector.py              # cosine_similarity + encode/decode
├── ingest/                 # 数据摄入层
│   ├── extractors.py          # FakeExtractor + ExtractedClaim
│   ├── llm_extractor.py       # LLM 提取器（qwen3.7-plus）
│   ├── embeddings.py          # text-embedding-v4 向量化（内联 retry）
│   ├── event_filter.py        # 事件过滤
│   └── budget.py              # 预算控制
├── recall/                 # 召回层
│   ├── recall_pipeline.py     # FTS + 向量 + RRF + 多因子排序 + reranker
│   ├── extended_pipeline.py   # RRF + budget_pack（上下文预算打包）
│   ├── dedup.py               # L1 精确去重 + L2 语义去重
│   ├── conflict.py            # 确定性冲突判定（不调 LLM）
│   ├── attribute_map.py       # canonical_attribute 规范化
│   ├── ranking.py             # 多因子排序
│   ├── reranker.py            # LLM reranker（gte-rerank-v2）
│   ├── observation.py         # ObservationBuilder（派生记忆构建）
│   ├── policy.py              # RecallIntent 路由 + 可见性规则（router.py 已合并）
│   └── router.py              # 向后兼容 re-export
├── storage/                # 存储层
│   ├── database.py            # SQLite 连接池 + migration 执行
│   ├── repository.py          # CRUD（Claim/Event/Job/Evidence/Derivation）
│   ├── backup.py              # 在线备份
│   └── migrations/            # 14 个 SQL migration
├── workers/                # 后台任务
│   ├── worker.py              # Worker（调度 + 7 种 job_type + maintenance）
│   ├── consolidate.py         # LLM 语义冲突归并 + auto_resolve_conflicts
│   ├── decay.py               # 置信度线性衰减（可配置）
│   ├── ttl.py                 # ephemeral TTL 过期
│   ├── reclassify.py          # LLM 重分类 scope/importance
│   ├── mental_models.py       # DerivedMemoryMaintainer（派生记忆构建 + stale 传播）
│   └── induce_policies.py     # 策略归纳
├── experience/             # Experience 通道
│   └── service.py             # Episode/Trace/Policy CRUD + 状态机
├── security/
│   └── retention.py           # 事件保留清理
├── observability/
│   └── audit.py               # 审计日志
├── adapters/hermes/        # Hermes 集成
│   ├── provider.py            # HermesMemoryProvider（唯一实现，httpx）
│   └── plugin/__init__.py     # 薄委托层
├── mcp/
│   └── server.py              # MCP 工具契约（委托 application 服务）
├── lifecycle.py            # ClaimStatus + EpisodeStatus 枚举 + 转换守卫
├── components.py           # 统一组件工厂（embedder/reranker/extractor）
├── config.py               # 集中配置常量（threshold/interval/limit）
├── settings.py             # Settings dataclass + from_env() + 配置校验
├── protocols.py            # EmbedderProtocol/ExtractorProtocol/RerankerProtocol
├── errors.py               # HlMemError 异常族
├── http_utils.py           # retry_http() 统一重试工具
└── cli.py                  # CLI 入口（status + conflicts 审核）
```

## 测试

```bash
.venv/Scripts/python.exe -m pytest tests/unit/ -q --tb=short
```

当前：220 测试通过。

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

14 个 SQL migration（001-014），按版本号顺序执行。不可变。
