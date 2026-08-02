# HL-Mem

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Version: 0.20.2](https://img.shields.io/badge/version-0.20.2-blue.svg)](docs/CHANGELOG.md)
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

Copy the TOML configuration and secret templates:

```bash
cp config.example.toml hl_mem.toml
cp .env.example .env
```

`hl_mem.toml` is required in the process working directory. `config.example.toml` contains common settings and explicitly
enables recommended real capabilities; those values are not code defaults. Add each enabled component's independent key
to `.env`, or switch unused modes back to their safe defaults. Do not commit real secrets. See the
[configuration reference](docs/configuration.md) for every TOML key.

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

For always-on deployments, use the stdlib-only `scripts/healthcheck.py` to probe `/healthz`, and leave process restarts and alerting to systemd, Windows service management, or the container orchestrator. See [Deployment Supervision and Health Checks](docs/watchdog.md) for examples.

### 3. Integrate with Hermes

Start HL-Mem, then deploy its MemoryProvider into Hermes:

```bash
uv run python install_to_hermes.py --hermes-home <HERMES_HOME>
```

Restart Hermes after installation. The adapter calls the local HL-Mem service over HTTP and provides timeouts, circuit breaking, prefetching, and Episode/Trace synchronization. It degrades without blocking the agent when the service is unavailable.

## Key Configuration

Non-secret settings come only from `hl_mem.toml`; secrets come only from `.env` or same-named process environment
variables. Common keys are listed below.

| TOML key | Code default | Purpose |
|---|---:|---|
| `database.path` | `var/hl_mem.db` | SQLite database path |
| `extraction.mode` | `fake` | `fake`, `real`, or `llm` |
| `embedding.mode` | `fake` | `fake` or `real` |
| `reranker.mode` | `off` | `off`, `fake`, `on`, or `real` |
| `image_describer.mode` | `off` | `off` or `on` |
| `recall.query_expansion_mode` | `auto` | `off`, `auto`, or `always` |
| `relation.discovery_mode` | `off` | `off`, `audit`, or `auto` |

Real components and external-call paths must be supplied with their own key; there is no automatic fake fallback.
`HL_MEM_*` environment variables no longer participate in application `Settings` configuration. Code defaults intentionally differ from the
example deployment: `Settings` keeps `recall.default_limit` / `recall.relevance_reranker_floor` at `20` / `0.4`, while
the repository TOML and `config.example.toml` explicitly set `5` / `0.15` and keep
`recall.relevance_keep_top1 = true`. Query expansion uses a separately configurable model with 5/6-second per-call/total
timeouts.

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

The current baseline is v0.20.2 with 36 immutable, forward-only migrations.

## Documentation

| Guide | Contents |
|---|---|
| [Documentation index](docs/README.md) | Navigation for maintained documentation |
| [Configuration reference](docs/configuration.md) | TOML keys, defaults, allowed values, and secret boundary |
| [Architecture](docs/architecture.md) | Layers, modules, pipelines, storage, and lifecycle |
| [API reference](docs/api.md) | REST endpoints and request conventions |
| [Deployment supervision](docs/watchdog.md) | Cross-platform health probe and systemd, Windows, and container examples |
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
