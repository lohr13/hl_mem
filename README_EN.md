# HL-Mem

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Version: 0.16.1](https://img.shields.io/badge/version-0.16.1-blue.svg)](docs/CHANGELOG.md)
[![CI](https://github.com/REDACTED_USER/hl_mem/actions/workflows/test.yml/badge.svg)](https://github.com/REDACTED_USER/hl_mem/actions/workflows/test.yml)

[中文](README.md#中文) | [English](#english)

<a id="english"></a>

## English

HL-Mem is a local-first, evidence-driven long-term memory system for AI agents. It is more than a vector store: immutable events become structured, evidence-backed Claims; valid time and recorded time describe how facts change; and a separate Experience channel stores Episodes, Traces, and reusable Policies. The default stack is SQLite WAL, FTS5, and vector BLOBs, with no external database service.

## Installation

Python 3.11+ is required. [uv](https://docs.astral.sh/uv/) is recommended:

```bash
git clone git@github.com:REDACTED_USER/hl_mem.git
cd hl_mem
uv sync
```

Alternatively, install the current repository with pip:

```bash
python -m pip install .
```

For development, use `uv sync --dev` or `python -m pip install -e .`. The locked development environment is defined by `uv.lock`.

## Quick Start

### 1. Configure the service

Copy the environment template and add the API keys required by the LLM, embedding provider, and optional reranker:

```bash
cp .env.example .env
```

`.env.example` is the complete, versioned configuration catalog. Do not commit real secrets in `.env`.

### 2. Start the service

```bash
uv run python start_server.py
```

The API and background worker start together and listen on `http://127.0.0.1:8200` by default:

```bash
curl -X POST http://127.0.0.1:8200/v1/memories -H "Content-Type: application/json" \
  -d '{"text":"Alice prefers dark mode","subject":"Alice"}'

curl -X POST http://127.0.0.1:8200/v1/recall -H "Content-Type: application/json" \
  -d '{"query":"What does Alice prefer?","limit":5}'
```

See the [API reference](docs/api.md) for complete request contracts.

### 3. Integrate with Hermes

Start HL-Mem, then deploy its MemoryProvider into Hermes:

```bash
uv run python install_to_hermes.py --hermes-home <HERMES_HOME>
```

Restart Hermes after installation. The adapter calls the local HL-Mem service over HTTP and provides timeouts, circuit breaking, prefetching, and Episode/Trace synchronization. It degrades without blocking the agent when the service is unavailable.

## Key Configuration

These are the main extraction and recall settings. See [`.env.example`](.env.example) for the complete catalog, provider credentials, and experimental flags.

| Variable | Default | Purpose |
|---|---:|---|
| `HL_MEM_ENV` | `dev` | Runtime environment: `dev` or `production` |
| `HL_MEM_DB_PATH` | `var/hl_mem.db` | SQLite database path |
| `HL_MEM_EXTRACTOR` | `llm` (template) | Extractor mode: `fake` or `llm` |
| `HL_MEM_EMBEDDER` | `real` (template) | Embedding mode: `fake` or `real` |
| `HL_MEM_RERANKER` | `on` | Reranking: `off`, `fake`, `on`, or `real` |
| `HL_MEM_LLM_PROVIDER` | `dashscope` | `dashscope`, `zhipu`, or `openai_compatible` |
| `HL_MEM_LLM_ENABLE_THINKING` | unset | Optional boolean override; when unset, the provider field is omitted |
| `HL_MEM_LLM_STRUCTURED_MODE` | `json_object` | `auto`, `json_object`, or `json_schema` |
| `HL_MEM_LLM_SCHEMA_RETRIES` | `2` | Maximum retries after JSON repair or schema validation fails |
| `HL_MEM_INDEX_TEXT_MODE` | `legacy` | FTS/embedding text: `legacy`, `value_only`, or `natural` |
| `HL_MEM_EXTRACTION_CHUNK_TARGET_CHARS` | `12000` | Target size for structure-aware extraction chunks |
| `HL_MEM_EXTRACTION_CHUNK_OVERLAP_TURNS` | `2` | Conversation-turn overlap between chunks |
| `HL_MEM_EXTRACTION_MAX_SPLIT_DEPTH` | `3` | Maximum recursive split depth after truncation |
| `HL_MEM_QUERY_EXPANSION_MODE` | `auto` | Multi-query recall: `off`, `auto`, or `always` |
| `HL_MEM_RELATION_DISCOVERY_MODE` | `audit` | Relation discovery: `off`, `audit`, or `auto` |
| `HL_MEM_TAG_CHANNEL_ENABLED` | `false` | Enable the independent tag retrieval channel |

Production mode requires a real embedder, an enabled reranker, and a non-fake extractor. [Settings](src/hl_mem/settings.py) and the [configuration template](.env.example) define the authoritative behavior.

## Capabilities

- **Memory correctness:** idempotent event ingestion, atomic writes, exact/semantic deduplication, deterministic conflict rules, and LLM-assisted gray-zone consolidation.
- **Extraction governance:** deterministic scope downgrade, predicate projection from canonical attributes, subject guards that isolate invalid subjects, and bounded structured-output repair.
- **Time and evidence:** valid and recorded time, evidence lineage, entity normalization, explicit forgetting, and stale propagation.
- **Hybrid recall:** Chinese-aware FTS5, dense vectors, RRF fusion, multi-factor ranking, optional reranking, relation/query expansion, and token-budgeted context packing.
- **Lifecycle:** importance-aware TTL, confidence decay, archival, reclassification, feedback usefulness, audit logs, and online backups.
- **Experience:** Episodes, Traces, rewards, Policies/Procedures, and derived Observations.
- **Interfaces:** FastAPI REST and the Hermes Provider are stable paths; the five-tool MCP interface is beta.
- **Evaluation:** offline extraction/recall/lifecycle metrics, recall diagnostics, controlled index-text A/B tests, cross-model extraction benchmarks, and a LongMemEval adapter.

See the [capability matrix](docs/capability-matrix.md) for maturity, defaults, and evidence, and the [architecture guide](docs/architecture.md) for data flows.

## Project Status

- **Stable:** events and evidence, atomic writes, LLM extraction, embeddings, FTS + Dense + RRF, dual-time filtering, TTL/decay/archival, conflicts and deduplication, REST, Hermes, backups, and auditing.
- **Beta:** multi-query recall, relation candidate discovery, feedback-driven maintenance, semantic-dedup auditing, MCP Server, benchmarks, and LongMemEval.
- **Experimental:** image evidence, extraction pre-filtering, the independent tag channel, and a PostgreSQL connectivity probe.

The current baseline is v0.16.1 with 33 immutable, forward-only migrations.

## Documentation

| Guide | Contents |
|---|---|
| [Documentation index](docs/README.md) | Navigation for maintained documentation |
| [Architecture](docs/architecture.md) | Layers, modules, pipelines, storage, and lifecycle |
| [API reference](docs/api.md) | REST endpoints and request conventions |
| [Compatibility policy](docs/compatibility.md) | Versioning and public contract guarantees |
| [Capability matrix](docs/capability-matrix.md) | Maturity, defaults, and evidence |
| [Changelog](docs/CHANGELOG.md) | Release history |

## Contributing

Search existing issues before reporting a bug or proposing a feature. Include reproduction steps, expected and actual behavior, environment details, and relevant logs. Keep each pull request focused on one change, explain its motivation and validation, and update tests and documentation when behavior or public contracts change.

```bash
git clone git@github.com:REDACTED_USER/hl_mem.git
cd hl_mem
uv sync --dev
uv run pytest tests/unit/ -q --tb=short
uv run black --check src tests
uv run isort --check-only src tests
uv run ruff check src tests
```

Use English commit messages in the form `type(scope): description`, where `type` is `feat`, `fix`, `refactor`, `test`, `docs`, or `chore`.

## License

Licensed under the [Apache License 2.0](LICENSE). You may use, modify, and distribute this project subject to its terms, including the required copyright and license notices.
