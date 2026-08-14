# HL-Mem

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Version: 0.26.0](https://img.shields.io/badge/version-0.26.0-blue.svg)](docs/CHANGELOG.md)
[![CI](https://github.com/lohr13/hl_mem/actions/workflows/test.yml/badge.svg)](https://github.com/lohr13/hl_mem/actions/workflows/test.yml)

[中文](README.md#中文) | [English](#english)

<a id="english"></a>

## English

HL-Mem is a local-first, evidence-driven long-term memory system for AI agents. It is more than a vector store: immutable events become structured, evidence-backed Claims; valid time and recorded time describe how facts change; and a separate Experience channel stores Episodes, Traces, and reusable Policies. The default stack is SQLite WAL, FTS5, and exact scanning over vector BLOBs, with an optional sqlite-vec backend and no external database service.

## Five-minute quickstart

Python 3.11+ is required. Install HL-Mem from PyPI:

```bash
python -m pip install hl-mem
```

In the directory where you want the local configuration and database, create an API-key-free setup and start the service:

```bash
hlmem init --offline
hlmem server
```

Open another terminal, then save and recall a memory:

```bash
hlmem remember "Alice prefers dark mode"
hlmem recall "What does Alice prefer?"
```

Recall prints the Claim ID, score, and evidence references together:

```text
[1] Alice prefers dark mode
    ID: <claim-id>
    分数: 0.8123
    证据:
      - event/<event-id>
```

`event/<event-id>` means the Claim is traceable to an immutable source event instead of being unsupported model text. Use `hlmem list` to see the Claim ID again, then pass it to `hlmem forget <claim-id>`, the REST detail endpoint, or MCP's `memory_explain`. The CLI output labels are currently Chinese. Offline mode is FTS-only keyword recall; fake embeddings preserve the storage shape but do not provide semantic search.

## Advanced installation and integrations

### Install from source

```bash
git clone https://github.com/lohr13/hl_mem.git
cd hl_mem
uv sync
uv run hlmem init --offline
uv run hlmem server
```

Use `uv sync --dev` for development and `hlmem doctor` for read-only diagnostics. SQLite must include FTS5, which is normally present in official Python distributions.

### Enable online models

From a source checkout, copy `config.example.toml` to a local `hl_mem.toml`, and copy `.env.example` as needed. Put each enabled component's independent key in `.env`: `LLM_API_KEY`, `EMBEDDING_API_KEY`, `RERANKER_API_KEY`, or `IMAGE_API_KEY`. Then enable the matching `extraction.mode`, `embedding.mode`, `reranker.mode`, or `image_describer.mode`. See the [configuration reference](docs/configuration.md) for the full schema.

### Connect Codex, Claude, and Cursor

Install the MCP extra with `python -m pip install "hl-mem[mcp]"` to use `hl-mem-mcp`, an official MCP Python SDK 2.x stdio server for Codex, Claude Code, Claude Desktop, and Cursor. See the [MCP guide](docs/mcp.md) for client configuration, its seven tools, and error behavior.

### Integrate with Hermes

Start HL-Mem and verify `curl --fail http://127.0.0.1:8200/healthz`, then run this from a source checkout:

```bash
uv run python scripts/install_to_hermes.py --hermes-home <HERMES_HOME>
```

The plugin is installed under `<HERMES_HOME>/plugins/hl_mem/`; restart Hermes afterward. The local HTTP adapter provides timeouts, circuit breaking, prefetching, and Episode/Trace synchronization.

### Always-on deployment and systemd

Use the stdlib-only `scripts/healthcheck.py` to probe `/healthz`, and leave restart and alerting to systemd, Windows service management, or the container orchestrator. A systemd unit's `WorkingDirectory` must contain `hl_mem.toml` and the optional `.env`.

See the [API reference](docs/api.md) for complete REST request contracts.

## Key Configuration

Non-secret settings come only from `hl_mem.toml`; secrets come only from `.env` or same-named process environment
variables. Common keys are listed below.

| TOML key | Code default | Purpose |
|---|---:|---|
| `database.path` | `var/hl_mem.db` | SQLite database path |
| `extraction.mode` | `fake` | `fake`, `real`, or `llm` |
| `extraction.batch_max_events` | `5` | Maximum same-session Events per extraction call |
| `extraction.batch_max_wait_seconds` | `120.0` | Maximum wait for a non-full extraction window |
| `embedding.mode` | `fake` | `fake` or `real` |
| `embedding.text_type` | unset | Optional `document` or `query` in native mode; omitted by default |
| `reranker.mode` | `off` | `off`, `fake`, `on`, or `real` |
| `image_describer.mode` | `off` | `off` or `on` |
| `recall.vector_backend` | `sqlite_scan` | `sqlite_scan` (default) or `sqlite_vec`, which requires `hl-mem[sqlite-vec]` |
| `recall.dedup_threshold` | `0.95` | Near-copy folding threshold inside the bounded candidate window; `0` disables folding |
| `recall.dedup_candidate_limit` | `100` | Maximum recall candidates considered for near-copy folding |
| `recall.query_expansion_mode` | `auto` | `off`, `auto`, or `always` |
| `dedup.scan_limit` | `200` | Maximum pending `dedup_pairs` reviewed per maintenance pass |
| `relation.discovery_mode` | `off` | `off`, `audit`, or `auto` |

Real components and external-call paths must be supplied with their own key; there is no automatic fake fallback.
`HL_MEM_*` environment variables no longer participate in application `Settings` configuration. `Settings` and
`config.example.toml` both use `5` / `0.15` for `recall.default_limit` / `recall.relevance_reranker_floor`; the example
deployment only raises `recall.relevance_relative_drop` from the code default `0.15` to `0.30` and keeps
`recall.relevance_keep_top1 = true`. Query expansion uses a separately configurable model with 5/6-second per-call/total timeouts.

### Upgrading from v0.24.0

v0.26.0 is a backward-compatible minor release over v0.25.x; v0.24.1 and v0.24.2 were repository-only transition versions.
When upgrading from v0.24.0 or earlier, back up the database and stop the API, workers, and other writers. Migration 038 scans and
canonicalizes stored Claim subjects under `BEGIN IMMEDIATE`, migration 039 adds nullable Event `metadata_json`, and
migration 040 adds the bounded deferred-task queue.
Plan a maintenance window for a large database; migrations are forward-only. The default `auto` FTS query supports both
legacy raw-only and current raw-plus-stem indexes, so morphology compatibility alone does not require a forced rebuild.

## Capabilities

- **Memory correctness:** idempotent event ingestion, atomic writes, exact deduplication, conservative near-copy control across ingestion reuse, maintenance equivalence edges, and recall folding, plus deterministic conflict rules, LLM-assisted gray-zone consolidation, and guarded terminal conflict convergence.
- **Extraction governance:** bounded same-session microbatches, seven-field compact extraction with source-event mapping, a shared AdmissionPolicy, bilingual atomicity rules for compound facts, relationships, and enumerations, an audit warning at the 20-claim output boundary, full Claim-schema post-processing, deterministic scope/predicate projection, subject guards, and bounded structured-output repair.
- **Time and evidence:** valid and recorded time, evidence lineage, entity normalization, explicit forgetting, and stale propagation.
- **Hybrid recall:** Chinese-aware FTS5, two-stage exact vector scanning or optional sqlite-vec, RRF fusion, multi-factor ranking, optional reranking, relation/query expansion, and token-budgeted context packing.
- **Lifecycle:** importance-aware TTL, confidence decay, archival, reclassification, feedback usefulness, audit logs, and online backups.
- **Experience:** Episodes, Traces, rewards, Policies/Procedures, and derived Observations.
- **Interfaces:** FastAPI REST and the Hermes Provider are stable paths; the seven-tool MCP stdio interface is beta.
- **Evaluation:** extraction v2, isolated retrieval over PerLTQA 64 + MemDaily 48, a 40-case Chinese E2E suite, and full LongMemEval/MemDaily/PerLTQA runners.

### Evaluation Results (published frozen protocols)

| Benchmark | Setup | Result |
|---|---|---:|
| LongMemEval · HL-Mem v0.25.2 | holdout50, Top-10 structured evidence | **43/50 (86.0%)** |
| LongMemEval · Full-Context upper bound | all sessions passed directly to the reader | **46/50 (92.0%)** |
| LongMemEval · Native RAG baseline | raw-session dense RAG, Top-10 | **45/50 (90.0%)** |
| MemDaily | 180 cases | **97.2% accuracy** |
| PerLTQA | 378 questions, 10 characters | **R@5 84.9%, MRR 69.6%** |

The LongMemEval comparison uses the `deepseek-v4-flash-0731` reader throughout, with reader thinking enabled and judge
thinking disabled; the benchmark reader and production recall/context packing are separate contracts. See the
[evaluation guide](tests/eval/README.md) for current isolated-retrieval and E2E regression semantics, and the
[results index](evaluation/results/README.md) for local artifact naming.

See the [capability matrix](docs/capability-matrix.md) for maturity, defaults, and evidence, and the [architecture guide](docs/architecture.md) for data flows.

## Project Status

- **Stable:** events and evidence, atomic writes, LLM extraction, embeddings, FTS + Dense + RRF, dual-time filtering, TTL/decay/archival, conflicts and deduplication, REST, Hermes, backups, and auditing.
- **Beta:** multi-query recall, relation candidate discovery, feedback-driven maintenance, extraction-entailment auditing, semantic-dedup auditing, MCP Server, benchmarks, and LongMemEval.
- **Experimental:** image evidence, extraction pre-filtering, the independent tag channel, and a PostgreSQL connectivity probe.

The current baseline is v0.26.0 with 40 immutable, forward-only migrations.

## Documentation

| Guide | Contents |
|---|---|
| [Documentation index](docs/README.md) | Navigation for maintained documentation |
| [Configuration reference](docs/configuration.md) | TOML keys, defaults, allowed values, and secret boundary |
| [Architecture](docs/architecture.md) | Layers, modules, pipelines, storage, and lifecycle |
| [API reference](docs/api.md) | REST endpoints and request conventions |
| [MCP guide](docs/mcp.md) | stdio arguments, Codex/Claude/Cursor setup, and tool-error behavior |
| [Compatibility policy](docs/compatibility.md) | Versioning and public contract guarantees |
| [Capability matrix](docs/capability-matrix.md) | Maturity, defaults, and evidence |
| [Changelog](docs/CHANGELOG.md) | Release history |

## Contributing

Issues and focused pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the seven CI
preflight checks, data-handling rules, and commit conventions.

## License

Licensed under the [Apache License 2.0](LICENSE). You may use, modify, and distribute this project subject to its terms, including the required copyright and license notices.
