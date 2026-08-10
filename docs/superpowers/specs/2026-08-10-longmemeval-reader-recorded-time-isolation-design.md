# LongMemEval Reader Recorded-Time Isolation Design

## Problem and scope

LongMemEval sessions carry dataset time in `haystack_dates` and `question_date`, but benchmark ingestion writes event
`recorded_at` and claim `recorded_from` from the wall clock. Cached databases built on 2026-08-10 therefore expose a
2026 recorded timeline beside a 2023/2024 benchmark timeline. The reader prompt currently asks the model to combine
both timelines, so otherwise sufficient evidence can be rejected as recorded after the question.

The defect is confined to the evaluation reader projection. Production dual-time storage and recall under `src/hl_mem/`
must remain unchanged.

## Decision

Project only benchmark-semantic time into the reader prompt:

- Claims retain `valid_from`, `valid_to`, `occurred_start`, and `occurred_end`.
- Evidence events retain `occurred_at`.
- `recorded_from`, `recorded_to`, and `recorded_at` remain in the database and benchmark retrieval reports, but are not
  serialized into the reader prompt.
- The system prompt resolves updates from occurred/valid time plus Current Date. The existing temporal baseline and
  historical-value rules remain unchanged.

This is preferred to explaining away contaminated fields in a longer prompt: hiding an invalid benchmark signal is
deterministic and simpler. Rewriting recorded time during ingestion is rejected because it changes persistent dual-time,
TTL, and visibility semantics and would require rebuilding ingest caches.

## Temporal behavior

LongMemEval temporal questions derive their timeline from `question_date`, `haystack_dates`, and explicit dates in event
text. Normalization maps these to Current Date and event `occurred_at`; extraction receives the same `occurred_at`, so
claim valid/occurrence fields remain available. Recorded time is not dataset ground truth and is therefore not needed for
benchmark as-of reasoning.

## Compatibility and verification

The reader-context protocol changes from `session-turn-window-v1` to `session-turn-window-v2`. Existing ingest databases
and manifests remain reusable because storage and extraction are unchanged. Resume/merge identities must reject reports
created with v1 so answers rendered from different prompt projections are not mixed.

Unit coverage will assert that both normal evidence and assistant fallback prompts exclude ingestion timestamps while
preserving valid/occurred time, and that temporal system instructions remain active without referring to recorded time.
Behavioral verification will reuse the fixed Top-10 evidence and databases for `1d4e3b97` and `60d45044` with only a small
number of reader calls. Local pytest is intentionally not run; pushed CI is the test authority.
