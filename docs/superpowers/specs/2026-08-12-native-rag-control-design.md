# LongMemEval Raw-Session Dense RAG Control Design

## Goal

Add an evaluation-only `native-rag` control that measures a conventional raw-session dense RAG pipeline against
`hl_mem+reader` and `full-context+reader`, without changing `src/hl_mem/`.

## Definition

- Retrieval unit: one complete original LongMemEval session, including its timestamp and original user/assistant
  messages. No claim extraction, summaries, overlap, or truncation.
- Embedding: the configured `qwen3.7-text-embedding` native endpoint, 2048 dimensions, without `text_type`, query
  instructions, sparse output, or a reranker.
- Query: the question text only.
- Ranking: exact cosine similarity over every session in the case; select exactly Top-10, or every session when the
  case contains fewer than ten.
- Reader packing: select by similarity, then order selected sessions by `occurred_at` and source index before giving
  the unchanged raw text to the existing thinking-enabled reader.
- Answering: reuse the current reader generation budget, official-compatible judge, retry policy, and 300-second
  control timeout.

## Isolation and identity

The runner exposes `--mode native-rag`, defaults to a `longmemeval_nativerag_*` result path, and bypasses the
production database, extractor, maintenance, query expansion, FTS, reranker, and recall pipeline. Reports identify
the control as `native-rag` with protocol `raw-session-dense-rag-v1`, dataset SHA-256, fixed Top-10, embedding
identity, reader/judge identity, and resume guards.

## Trace and accounting

Each case records every selected session's cosine score, retrieval rank, reader rank, timestamp, raw message count,
and whether it is a gold session. The report records embedding document/query tokens, API/cache activity and
latency, reader/judge token details, retrieval coverage, and pinned cold-start cost estimates. Cache hits remain
visible and do not silently change the logical cold-start comparison.

## Failure behavior

HTTP/provider failures are sanitized and persisted with the same circuit breaker as the full-context control. Raw
content is not filtered or rewritten. A provider content rejection therefore remains an observable control result.

## Verification

Unit contracts cover rendering, stable cosine/tie ranking, temporal reader order, CLI/output identity, production
pipeline bypass, trace, usage, cost, and resume identity. Local verification does not run pytest per task constraint;
targeted Python contract checks plus ruff, black, isort, mypy, and documentation consistency run locally, while
GitHub CI runs the full test suite. One ordinary case and `0a995998` are the intended real engineering smoke cases.
