# Extraction Claim Budget — Zhipu Coding Plan Smoke

Date: 2026-09-02  
Provider/model: Zhipu `glm-5.3-flash`, `reasoning_effort=low`  
Scope: extraction only, verification disabled, temporary usage ledger, no production database writes

## Purpose and method

The approved five synthetic cases were executed once after the initial prompt change. That run exposed semantic omissions
and incorrect merging, so the prompt received one bounded correction: explicit durable categories may not be omitted,
independently changing slots may not be merged, and dates, quantities, frequencies, durations, approval conditions, and
state-transition coordinates may not be rewritten. The same fixed set was then executed once as a post-correction
regression run. No case was repeated further and no output was used to tune toward a preferred Claim count.

The harness recorded only safe synthetic Claim values, counts, logical call counts, and token usage. Credentials, endpoints,
raw provider payloads, and prompts were not persisted. Exact evidence support means every returned `evidence_quote` was a
literal substring of the synthetic source; it is not a semantic-entailment judgment.

## Post-correction run

| case | generated / retained | LLM calls | input tokens | output tokens | exact evidence | qualitative result |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| no durable memory | 0 / 0 | 1 | 2,075 | 13 | yes | Pass: no memory invented. |
| three durable topics | 0 / 0 | 1 | 2,081 | 62 | vacuous | **Fail:** omitted an explicit lasting preference, adopted database decision, and weekly backup plan. |
| dense multi-message | 4 / 4 | 1 | 2,219 | 635 | yes | **Fail:** stayed bounded, but omitted several independent settings and conflated Python preference with date handling; release cadence, retention periods, and dual approval were not preserved. |
| state transition | 2 / 2 | 1 | 2,097 | 251 | yes | **Fail:** values hallucinated an exhibition and misread concurrency values as days/areas despite quoting source evidence. |
| assistant generic chatter | 0 / 0 | 1 | 2,098 | 28 | vacuous | Pass: generic technical explanation was skipped. |

Totals: 5 logical extraction calls, 10,570 input tokens, 989 output tokens. Every case completed in exactly one logical
extraction call, retained at most 16 Claims, and emitted no count-overflow split/retry audit. The model itself never returned
more than 16 items in this run, so live evidence covers prompt/schema compliance and call containment; deterministic tests
cover application-side reduction of 17- and 30-item responses.

## Initial run that triggered the correction

| case | generated / retained | LLM calls | input tokens | output tokens | result |
| --- | ---: | ---: | ---: | ---: | --- |
| no durable memory | 0 / 0 | 1 | 1,999 | 13 | Pass |
| three durable topics | 0 / 0 | 1 | 2,005 | 31 | Fail: omitted all three topics |
| dense multi-message | 4 / 4 | 1 | 2,143 | 610 | Fail: changed cadence/retention semantics and omitted independent settings |
| state transition | 0 / 0 | 1 | 2,021 | 82 | Fail: omitted transition |
| assistant generic chatter | 0 / 0 | 1 | 2,022 | 13 | Pass |

Initial totals: 5 logical calls, 10,190 input tokens, 749 output tokens.

## Interpretation

The operational objective is supported: Claim count did not multiply model calls, and all live responses stayed within the
16-item schema. The semantic-quality objective is **not** established for this Zhipu low-reasoning configuration. Exact
evidence presence alone did not prevent unsupported value synthesis, and the model remained overly conservative on a short
durable input after the prompt correction. These failures should not be represented as a successful extraction-quality
benchmark.

No 20-case LongMemEval slice was run. The workspace contains only the three-case unit fixture
`tests/fixtures/longmemeval_small.json`, not the required fixed 20-item local dataset; no dataset was downloaded and no paid
run was expanded.
