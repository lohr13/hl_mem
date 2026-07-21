# HL-Mem 项目 AGENTS.md

## 项目概述

HL-Mem 是面向 AI Agent 的本地优先记忆系统。核心设计：事件溯源双通道 + 双时间模型 + 证据链 + 多因子召回。

详细架构见 `docs/architecture.md` 和 `docs/implementation-plan.md`。

## 技术栈

- **运行时**：Python 3.11+，FastAPI + uvicorn
- **存储**：SQLite WAL + FTS5（全文检索）+ 向量 BLOB（暴力余弦，首版）
- **LLM 提取**：qwen3.7-plus（百炼 Coding Plan），JSON mode
- **Embedding**：text-embedding-v4（百炼通用 AK），2048 维
- **包管理**：uv（lockfile: uv.lock）
- **测试**：pytest + pytest-asyncio（asyncio_mode=auto）

## 代码结构

```
src/hl_mem/
├── api/           # FastAPI 服务 + 提取管道
│   ├── server.py          # REST API: /v1/events, /v1/recall, /v1/memories, /v1/jobs
│   └── pipeline.py         # 提取管道: dedup → conflict → claim → evidence → observation
├── ingest/        # 数据摄入层
│   ├── extractors.py       # FakeExtractor + ExtractedClaim 数据结构
│   ├── llm_extractor.py    # LLM 提取器（qwen3.7-plus）
│   ├── embeddings.py       # text-embedding-v4 向量化
│   ├── event_filter.py     # 事件过滤
│   └── budget.py           # 预算控制
├── recall/        # 召回层
│   ├── dedup.py            # L1 精确去重 + L2 语义去重
│   ├── conflict.py         # 矛盾检测
│   ├── ranking.py          # 多因子排序
│   ├── reranker.py         # LLM reranker
│   └── observation.py      # observation 归纳
├── storage/       # 存储层
│   ├── database.py         # SQLite 连接 + schema 初始化
│   └── repository.py       # CRUD 操作
├── workers/       # 后台任务
│   ├── worker.py           # Worker 基类
│   ├── decay.py            # 衰减任务
│   ├── ttl.py              # TTL 过期
│   └── reclassify.py       # 记忆重分类
├── observability/
│   └── audit.py            # 审计日志
└── adapters/
    └── hermes/
        └── provider.py     # Hermes provider 适配器
```

## 开发命令

```bash
# 安装依赖
uv sync

# 运行全部测试
uv run pytest tests/ -v

# 运行单元测试（不依赖外部 API）
uv run pytest tests/unit/ -v

# 运行集成测试
uv run pytest tests/integration/ -v

# 启动服务
uv run python start_server.py

# 数据库迁移（如有 schema 变更）
# 迁移脚本在 storage/database.py 的 init_db() 中自动执行
```

## 关键约束

### 存储层
- **SQLite WAL 模式**：不可改为其他模式，WAL 是并发读写的基础
- **FTS5 表**：全文索引必须与主表同步，写入时同步更新
- **向量存储**：当前用 BLOB（pickle），后续可换 sqlite-vec 或 pgvector

### API 层
- 所有 API key 从 `.env` 读取，禁止硬编码
- 百炼双 key 架构：Coding Plan AK（LLM）和通用 AK（Embedding）不互通
- LLM timeout 通过 `LLM_TIMEOUT` 环境变量配置（默认 90s）

### 召回层
- 三层去重链：fact_hash 精确 → conflict_key 矛盾 → semantic（cosine > 0.95）
- 多因子排序权重：recency + importance + access_count + scope_match
- Reranker 是必要组件，不是可选的（设计哲学：以终为始）

### 双时间模型
- `valid_from` / `valid_to`：事实本身的有效时间区间
- `recorded_from` / `recorded_to`：系统知道这个事实的时间区间
- 两个时间维度独立，不要混淆

## 测试约定

- 单元测试不依赖外部 API（用 FakeExtractor / mock）
- 集成测试可以调用真实 API（但需要 .env 配置）
- 测试数据用中文（验证中文提取质量）
- 每个新功能必须有对应测试
- pytest-asyncio 已配 `asyncio_mode = "auto"`

## 环境变量（.env）

```bash
# LLM（百炼 Coding Plan AK）
LLM_API_KEY=sk-sp-xxx
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3.7-plus
LLM_TIMEOUT=90

# Embedding（百炼通用 AK）
EMBEDDING_API_KEY=sk-e72xxx
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
```

## 当前状态

- 首版功能已实现（event + claim + observation）
- 93 测试通过
- Hermes provider 适配器已完成（commit 248093f）
- 衰减策略：temporal 90/180d, permanent 180/365d
