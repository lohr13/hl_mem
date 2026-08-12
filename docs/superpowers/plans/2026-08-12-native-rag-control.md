# LongMemEval Native RAG Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reproducible raw-session dense RAG + reader LongMemEval control under `evaluation/`.

**Architecture:** A focused `native_rag.py` module owns raw-session documents, exact cosine selection, temporal
reader packing, and selection diagnostics. The existing LongMemEval runner owns provider construction, per-case
execution, accounting, resume identity, output, and circuit breaking while reusing the existing embedding ablation
client and QA client.

**Tech Stack:** Python 3.11+, NumPy, existing DashScope embedding ablation client, existing LongMemEval QA client.

## Global Constraints

- Do not modify `src/hl_mem/` or benchmark datasets/thresholds.
- Keep `hl-mem`, `full-context`, and `native-rag` execution paths independent.
- Fix retrieval at exact cosine Top-10 over complete raw sessions.
- Do not run pytest locally; GitHub CI owns the full suite.
- Preserve the three pre-existing untracked files.

---

### Task 1: Raw-session retrieval primitives

**Files:**
- Create: `evaluation/tools/longmemeval/native_rag.py`
- Create: `tests/unit/test_longmemeval_native_rag.py`

**Interfaces:**
- Produces: `render_raw_session_documents(case)`, `select_raw_sessions(documents, document_vectors, query_vector,
  top_k)`, and `render_native_rag_user_prompt(case, hits)`.

- [ ] Write contracts proving raw role/content preservation, no `has_answer` leakage, exact cosine order, source-index
  tie breaking, Top-K behavior, and chronological reader packing.
- [ ] Run an import/assert contract before implementation and confirm the new module is absent.
- [ ] Implement immutable document/hit/render records and the three pure functions.
- [ ] Run the same contract and confirm the selected IDs, ranks, and prompt order.
- [ ] Commit the primitive and test files.

### Task 2: Runner mode and trace

**Files:**
- Modify: `evaluation/tools/run_longmemeval_benchmark.py`
- Modify: `tests/unit/test_longmemeval_native_rag.py`

**Interfaces:**
- Consumes: Task 1 pure functions.
- Produces: `--mode native-rag`, `_run_native_rag_case`, `_native_rag_report`, resume validation, and control runner.

- [ ] Add a runner contract that expects a distinct default output and production-pipeline bypass.
- [ ] Run the contract and confirm `native-rag` is rejected before implementation.
- [ ] Add fixed protocol/Top-K/cache constants and mode validation.
- [ ] Reuse Q1 embedding requests and cache, execute Top-10 retrieval, reader, and judge.
- [ ] Persist selected-session scores/ranks, gold coverage, embedding and QA usage/latency/cost, error diagnostics,
  dataset hash, and complete resume identity.
- [ ] Run the contract and inspect the generated one-case mocked report.
- [ ] Commit runner integration and tests.

### Task 3: Documentation and verification

**Files:**
- Modify: `evaluation/README.md`

**Interfaces:**
- Documents the exact command, output prefix, fixed retrieval definition, cost semantics, and comparison caveats.

- [ ] Add the native RAG invocation and result contract next to full-context.
- [ ] Run ruff, black, isort, mypy, documentation consistency, and targeted Python contracts.
- [ ] Run one ordinary and one `0a995998` real smoke case without a full benchmark.
- [ ] Commit documentation, push all commits, and inspect the resulting GitHub CI run.
