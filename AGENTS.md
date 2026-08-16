# HL-Mem 项目 AGENTS.md

## 项目概述

HL-Mem 是面向 AI Agent 的本地优先记忆系统。核心设计：事件溯源双通道 + 双时间模型 + 证据链 + slot+tags 分类体系 + importance 联动 TTL + 多因子召回 + 完整生命周期管理。

**当前版本：v0.28.1（2026-08-16）**

## 技术栈

- **运行时**：Python 3.11+，FastAPI + uvicorn
- **存储**：SQLite WAL + FTS5（全文检索）+ 向量 BLOB（默认 `sqlite_scan` 两阶段精确扫描；可选 `sqlite_vec` 后端）
- **LLM 提取**：API 密钥通过 .env 配置，provider/model 通过 TOML 配置，使用结构化 JSON 输出
- **Embedding**：API 密钥通过 .env 配置，provider/model/维度通过 TOML 配置
- **Reranker**：API 密钥通过 .env 配置，provider/model 通过 TOML 配置
- **分类体系**：SLOT_REGISTRY（15 operational slot + 40 topic tags；Phase 18 已接入检索，soft boost 默认开启，独立 tag channel 默认关闭待评测）
- **TTL**：retention 纯函数（scope × importance 三档）
- **近重复治理**：摄入层确定性 near-copy 复用 + 维护层 `dedup_pairs` 轮转审查 + 召回层有界动态折叠；旧 DedupJudge 保持 audit-only
- **包管理**：uv（lockfile: uv.lock）
- **测试**：pytest + pytest-asyncio（asyncio_mode=auto），全量 unittest 由 GitHub Actions 验证

## 代码结构

```
src/hl_mem/
├── api/                    # FastAPI 适配层
│   ├── server.py              # REST API (17 routes)
│   └── schemas.py             # Pydantic DTO
├── application/            # 共享应用服务
│   ├── ingest.py              # IngestService
│   ├── recall.py              # RecallService
│   ├── deletion.py            # DeletionService 物理删除闭包
│   ├── forget.py              # ForgetService 入口适配
│   └── restore.py             # tombstone restore replay
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
│   ├── admission.py          # 纯函数 Claim 准入策略
│   ├── llm_extractor.py       # LLM 提取器
│   ├── extractors.py          # FakeExtractor / LLMExtractor
│   ├── chunking.py            # 结构感知分块
│   ├── embedder.py            # Embedding 向量化
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
│   ├── reranker.py            # Reranker 重排
│   ├── relation_expansion.py  # 一跳关系扩展
│   └── observation.py         # 派生记忆构建
├── storage/                # 存储层（按职责拆分）
│   ├── database.py            # SQLite WAL + migration runner
│   ├── claims.py              # ClaimRepository
│   ├── events.py              # EventRepository
│   ├── evidence.py            # EvidenceRepository
│   ├── experience.py          # ExperienceRepository
│   ├── jobs.py                # JobRepository
│   ├── relation_proposals.py  # 关系候选审计
│   ├── usefulness.py          # 反馈效用聚合
│   ├── backup.py              # 在线备份
│   ├── tombstones.py          # 独立删除账本 sidecar
│   └── migrations/            # 44 SQL migrations (001-044) + Python data migrations
├── workers/                # 后台任务
│   ├── worker.py              # Job 租约/进度/维护循环
│   ├── job_handlers.py        # Job handler 与分派边界
│   ├── integrity.py           # dangling 引用巡检
│   ├── ttl.py                 # TTL 过期
│   ├── decay.py               # 置信度衰减
│   ├── consolidate.py         # LLM 语义归并
│   ├── deduplicate.py         # 跨 subject 语义去重
│   ├── backfill_expires_at.py # TTL 回填工具
│   ├── discover_relations.py  # 关系候选发现
│   ├── mental_models.py       # Mental Model 维护
│   ├── rebuild_usefulness.py  # usefulness 重建
│   └── induce_policies.py     # 策略归纳
├── experience/             # Experience 通道
│   └── service.py             # Episode/Trace/Policy
├── evaluation/             # Benchmark / LongMemEval
├── observability/          # 审计日志与 LLM spans
├── security/               # retention 策略
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

当前全量 unittest 由 GitHub Actions 验证。

## 关键设计决策

### 写入管线
- 7 字段 compact LLM 提取 → AdmissionPolicy → choice/qualifiers/time/entities 完整 schema 后处理；legacy 输出复用同一准入链路
- fact_hash v2（JSON 数组有边界哈希）→ conflict_key（canonical attribute slot）→ 保守 near-copy 复用或 `dedup_pairs` 候选
- 冲突判定：确定性规则优先（ConflictResolver），灰区走 LLM 四分类（ConflictConsolidator）
- **事务原子化**：整个写入流程（update_status + insert_claim + supersede + evidence_link）在单一 BEGIN IMMEDIATE 中

### 召回管线
- FTS5 全文检索 + dense vector 余弦相似度 → RRF 融合
- 多因子排序：recency / importance / access_count / scope / helpful_rate
- 可选 Reranker（密钥与 provider/model 由配置提供）
- 候选窗内对确定性等价组折叠，保留最高分代表并汇总 evidence
- 双时间过滤：valid_from/valid_to + recorded_from/recorded_to
- **上下文预算**：token_budget + context_mode="packed" + 跨类型配额
- 偏好专用召回 intent（RecallIntent.PREFERENCE）
- **派生记忆接入**：recall 自动查询活跃 derivation 并填充 observations

### 生命周期管理
- TTL 过期（ephemeral）→ activation 半衰期衰减（temporal/permanent/identity 分级）→ 归档（embedding 清空）→ 重分类
- 命中刷新 `last_accessed_at`，日常衰减不改写 confidence；legacy 线性路径仅作兼容对照
- **冲突终态收敛**：conflict_cases 状态机（pending → auto_resolved/manual_required → resolved/rejected）
- **删除完整性**：forget/archived cleanup/restore 共用 tombstone 支撑的 fail-closed 物理删除闭包

### 架构分层
- **api/** 是适配层（FastAPI DTO + 路由），不含业务逻辑
- **application/** 是应用服务层，拥有事务边界
- **domain/** + **core/** 是纯函数，不依赖基础设施
- **storage/** 是数据访问层，只依赖 domain 和 core
- **workers/** 是后台调度，通过 application 服务操作数据
- **lifecycle.py** 是状态机守卫，所有状态变更统一经过 assert_transition()

## 配置

非敏感配置只从工作目录的 `hl_mem.toml` 读取；所有 `HL_MEM_*` 环境变量均不参与 `Settings`。四个密钥可来自
`.env` 或同名进程环境变量：`LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`。

重复治理的主要键为 `dedup.enabled/threshold/scan_limit/audit_only` 与
`recall.dedup_threshold/dedup_candidate_limit`。完整配置以 `settings.py` 与 `docs/configuration.md` 为准；
`config.py` 只保留领域常量，`components.py` 负责组件工厂。

## Migration

44 个 SQL migration（001-044），按版本号顺序执行且不可变；另有 `sqlite_vec.py` 等 Python data migration，用于可选向量投影、subject 规范化和派生数据维护。
