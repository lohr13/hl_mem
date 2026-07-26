# HL-Mem

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Version: 0.14.2](https://img.shields.io/badge/version-0.14.2-blue.svg)](docs/CHANGELOG.md)
[![CI](https://github.com/REDACTED_USER/hl_mem/actions/workflows/test.yml/badge.svg)](https://github.com/REDACTED_USER/hl_mem/actions/workflows/test.yml)

> Local-first, evidence-driven long-term memory for AI agents. / 面向 AI Agent 的本地优先、证据驱动长期记忆。

## What is HL-Mem?

HL-Mem turns agent events into persistent, structured memories that remain explainable and correct over time. Unlike a
plain vector store, it combines event sourcing, dual-temporal facts, evidence chains, lifecycle governance, and a separate
experience channel—all on SQLite, with no external database service.

## Quickstart

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Install
git clone git@github.com:REDACTED_USER/hl_mem.git
cd hl_mem
uv sync

# 2. Configure providers
cp .env.example .env
# Edit .env and add the required LLM and embedding API keys.

# 3. Start API + worker (default: http://127.0.0.1:8200)
uv run python start_server.py

# 4. Store and recall a memory
curl -X POST http://127.0.0.1:8200/v1/memories -H "Content-Type: application/json" \
  -d '{"text":"Alice prefers dark mode","subject":"Alice"}'
curl -X POST http://127.0.0.1:8200/v1/recall -H "Content-Type: application/json" \
  -d '{"query":"What does Alice prefer?","limit":5}'
```

See the [configuration template](.env.example) and [API reference](docs/api.md) for production options and complete
request contracts.

## Features

- **Memory correctness — Stable:** idempotent event ingestion, atomic writes, exact/semantic deduplication, deterministic
  conflict rules, and LLM-assisted gray-zone consolidation.
- **Temporal knowledge — Stable:** valid time and recorded time, evidence lineage, entity normalization, and explicit
  forgetting with stale propagation.
- **Retrieval — Stable:** Chinese-aware FTS5, dense vectors, RRF fusion, multi-factor ranking, optional reranking, relation
  expansion, query expansion, and token-budgeted context packing.
- **Lifecycle — Stable:** importance-aware TTL, confidence decay, archival, reclassification, feedback usefulness, audit
  logs, and online backups.
- **Agent experience — Stable:** Episodes, Traces, rewards, Policies/Procedures, and derived Observations.
- **Interfaces — Stable/Beta:** FastAPI REST and Hermes Provider are stable; the five-tool MCP surface is beta.
- **Evaluation — Stable:** offline extraction/retrieval/lifecycle metrics and a LongMemEval adapter.

## Architecture Overview

```text
REST / MCP / Hermes → Application services → Domain + ingest/recall/workers → SQLite WAL + FTS5 + vectors
```

The fact channel turns immutable events into evidence-backed Claims and Observations. The experience channel records
Episodes and Traces, then derives reusable Policies. See [Architecture](docs/architecture.md) for the module map, write and
recall pipelines, data model, and component boundaries.

## Documentation

| Guide | Contents |
|---|---|
| [Documentation index](docs/README.md) | Navigation for all maintained documentation |
| [Architecture](docs/architecture.md) | Layers, module tree, pipelines, storage, and lifecycle |
| [API reference](docs/api.md) | REST endpoints and request conventions |
| [Compatibility policy](docs/compatibility.md) | Versioning and public contract guarantees |
| [Configuration](.env.example) | Runtime settings and provider options |
| [Capability matrix](docs/capability-matrix.md) | Maturity, defaults, and evidence |
| [Competitor comparison](docs/research/competitor-comparison.md) | Detailed Mem0, Zep, LangMem, and Letta comparison |
| [Changelog](docs/CHANGELOG.md) | Release history |

## Comparison

| Project | Strength | HL-Mem difference |
|---|---|---|
| Mem0 | Lightweight, LLM-driven extraction | Adds dual time, evidence chains, and slot + tag classification |
| Zep | Temporal knowledge graph | Runs local-first on SQLite + FTS5 without an external database service |
| LangMem | Profile/collection memory model | Uses slots for conflict, TTL, and dedup; open multi-value tags for retrieval |
| Letta/ADEPT | Long-term memory inside autonomous agents | Focuses on memory infrastructure and decouples the agent through adapters |

See the [full architectural comparison](docs/research/competitor-comparison.md) for deployment models, trade-offs, and
selection guidance.

## Project Status

### Stable (default-on, regression-tested)

- Event ingestion, evidence chains, atomic writes
- LLM extraction, embedding, FTS + Dense + RRF hybrid recall
- Dual-temporal filtering, TTL, decay, archival, explicit forgetting
- Three-layer dedup + conflict detection
- REST API, Hermes Provider, online backup, audit logs

### Beta (usable with safe defaults, needs more observation)

- Multi-query recall (default: auto)
- Relation candidate discovery (default: audit)
- Feedback-driven maintenance (default: observe)
- Semantic dedup (default: audit)
- MCP Server (5 tools)
- Benchmark suite + LongMemEval adapter

### Experimental (opt-in only)

- Image evidence (default: off)
- Extraction pre-filter (default: off)
- Independent tag channel (default: off)
- PostgreSQL connectivity probe (no storage semantics)

### Planned

- Mental model reasoning enhancements
- Multi-tenancy
- PostgreSQL storage adapter (behind protocol boundary)

Current baseline: v0.14.2, 29 migrations. Detailed maturity claims live in the
[capability matrix](docs/capability-matrix.md).

## 中文

HL-Mem 是面向 AI Agent 的本地优先长期记忆系统。它不只是向量库：系统将不可变事件提取为带证据链的结构化
Claim，以有效时间与记录时间描述事实变化，并通过独立的 Experience 通道保存 Episode、Trace 与可复用策略。
默认存储为 SQLite WAL + FTS5 + 向量 BLOB，无需部署外部数据库服务。

### 快速开始

```bash
git clone git@github.com:REDACTED_USER/hl_mem.git
cd hl_mem
uv sync
cp .env.example .env       # 填写 LLM 与 Embedding API key
uv run python start_server.py
```

服务默认监听 `http://127.0.0.1:8200`。使用方式见上方 Quickstart；完整配置、接口与架构分别见
[`.env.example`](.env.example)、[API 文档](docs/api.md) 和[架构文档](docs/architecture.md)。

核心能力包括：幂等且事务原子的写入、三层去重与冲突处理、双时间模型、证据链、中文全文与向量融合召回、
可选重排、importance 联动 TTL、反馈驱动维护、显式遗忘、审计与备份。当前 REST/Hermes 主路径稳定，MCP
工具面为 Beta；详细成熟度以[能力矩阵](docs/capability-matrix.md)为准。

## License

[Apache License 2.0](LICENSE)
