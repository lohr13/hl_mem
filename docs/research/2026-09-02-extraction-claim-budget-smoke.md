# Extraction Claim Budget — Zhipu Coding Plan Smoke

Date: 2026-09-02
Provider/model: Zhipu `glm-5.3-flash`, `reasoning_effort=low`
Scope: extraction only, verification disabled, temporary usage ledger, no production database writes

## Purpose and method

The approved five synthetic scenarios were run once after the final prompt wording was fixed. The harness forced UTF-8 on
the PowerShell-to-Python pipe, recorded only safe synthetic Claim values and counts, and used the production provider/model
coordinates with a temporary usage database. Credentials, endpoint details, raw provider envelopes, and prompts were not
persisted.

One immediately preceding harness attempt was discarded before acceptance because its returned evidence exposed that the
shell pipe had replaced Chinese source characters with `?`. The same scenario definitions were then rerun once with
explicit UTF-8; the results below are from that valid run. No case was repeated to tune Claim count.

## Accepted UTF-8 run

| case | generated / retained | LLM calls | input tokens | output tokens | result |
| --- | ---: | ---: | ---: | ---: | --- |
| no durable memory | 0 / 0 | 1 | 2,178 | 13 | Pass: no memory invented. |
| three durable topics | 3 / 3 | 1 | 2,197 | 291 | Pass: retained the response preference, PostgreSQL decision, and weekly backup schedule separately. |
| dense multi-message | 10 / 10 | 1 | 2,410 | 1,107 | Pass: grouped related settings without losing database/runtime choices, ports/timezone, retention, backup cadence, release window, dual approval, worker settings, retry/lease/batch limits, alert thresholds, or reporting preference. |
| state transition | 2 / 2 | 1 | 2,229 | 219 | Pass: retained the effective date, concurrency 5→8, backlog reason, 15-minute lease, and 20-item batch limit. |
| assistant generic chatter | 0 / 0 | 1 | 2,224 | 18 | Pass: generic technical explanation was skipped. |

Totals: 5 logical extraction calls, 11,238 input tokens, and 1,648 output tokens. Every case completed in exactly one
logical extraction call and retained at most 16 Claims. Every returned `evidence_quote` was a literal source substring,
and every retained value was supported by the synthetic source.

The model itself did not exceed 16 items in this bounded live run. Deterministic tests therefore remain the acceptance
evidence for application-side ranking and reduction of 17- and 30-item responses, including malformed overflow handling.

## Interpretation

The smoke supports both intended properties for the configured low-reasoning Zhipu model: the revised granularity keeps
dense extraction compact without dropping the named high-value settings, and Claim count does not create recursive model
calls. This is a smoke check, not a general benchmark or proof of semantic quality across arbitrary conversations.

No LongMemEval run was performed. The workspace contains only the three-case unit fixture
`tests/fixtures/longmemeval_small.json`, not the planned fixed 20-item local slice; no dataset was downloaded and no paid
benchmark was expanded.
