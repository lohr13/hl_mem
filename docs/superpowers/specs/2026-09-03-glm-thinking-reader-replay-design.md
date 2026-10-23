# GLM Thinking Reader Replay Design

**Date:** 2026-09-03
**Status:** Approved

## 1. Objective

Measure whether the fixed Qwen3.7-Plus QA reader materially affects the apparent
ranking of the three v1.1.4 extraction arms. Re-answer the existing 120 cases
with GLM-5.3-Flash thinking enabled while holding each case's question and exact
reader evidence sequence constant.

This is a cross-reader sensitivity experiment. It is not extraction `run2`, a
release-gate retry, or a runtime product feature.

## 2. Inputs and isolation boundary

The replay consumes the three completed official artifacts:

- Qwen3.7-Plus extractor: `var/eval/v114/candidate/full40/qwen37/run1/report.json`
- GLM-5.3-Flash extractor: `var/eval/v114/candidate/full40/glm53/run1/report.json`
- Local Qwen3.8-27B extractor:
  `var/eval/v114/candidate/full40/qwen38-27b/recovery1/report.json`

It also consumes `tests/eval/fixtures/chinese_e2e_sample.json` and its locked
private-source paths solely to reconstruct the choice text omitted from the
reports. `load_sample_manifest` and `load_sampled_inputs` must verify the
manifest and source hashes before any paid call. No source conversation or
message text is sent to the reader.

For every case, the replay reuses the recorded question, answerability contract,
gold answer and rubrics, plus every recorded `retrieved` item in its recorded
order. The current Qwen reader received this entire sequence, which contains
between one and ten items; `R@5` is a scoring cutoff, not a reader-input cutoff.
MemDaily choice text is joined by case ID from the hash-validated sampled input.
The replay must not run extraction, embedding, reranking, recall, TTL evaluation,
or database writes.

The replay records the SHA-256 of every source report. A source artifact must be
rejected unless it has schema version 3, `status=completed`, exactly 40 unique
case IDs, no case errors, and `qa.model=qwen3.7-plus` for the original answers.
The original control identity is Qwen3.7-Plus with `enable_thinking=true`, a
2,048-token thinking budget, and a 512-token final-answer budget.

## 3. Reader request

Use the existing Zhipu Coding Plan credential and endpoint without persisting
either value. The request identity is:

- model: `glm-5.3-flash`
- thinking: `{ "type": "enabled" }`
- temperature: `0.1`
- `max_tokens`: `4096`, covering reasoning plus the existing concise answer
  contract
- system/user prompts: byte-for-byte equivalent to the current Chinese E2E QA
  reader prompts

The Zhipu protocol uses the provider-specific `thinking.type` object, not the
DashScope-style `enable_thinking` boolean. The implementation must preserve only
the final answer. It records aggregate reasoning-token usage when the API
reports it, but must not persist reasoning content.

Use the Qwen-extractor copy of
`perltqa:23d905b73c57:dialogues:836f6182a0a9` as the first-call canary. It is
counted as one of the 120 replay calls and must not be called again after it
passes. The canary passes only if the request succeeds, returns non-empty final
content, and exposes either non-empty `reasoning_content` or a positive reported
reasoning-token count. If neither signal exists, mark the mode as unverified and
stop rather than silently treating a normal answer as a thinking result.

## 4. Replay and scoring

After a successful canary, make one reader request per case for all three source
arms: 120 logical QA calls in total. Transport retries use the existing maximum
of three attempts with bounded backoff; there is no semantic retry, answer
repair, or second judge.

Score each GLM final answer with the same frozen deterministic QA rubric and
answer-entity scorer used by the source artifacts. The scorer implementation and
gold data must not be changed during the experiment.

For each extractor arm, report:

- QA accuracy and F1 under the original Qwen reader and GLM thinking reader;
- Qwen-correct to GLM-wrong and Qwen-wrong to GLM-correct case IDs;
- unchanged-correct and unchanged-wrong counts;
- total/reported reasoning tokens, latency, failures, and retry counts;
- whether the extractor ranking by QA accuracy changes.

Also report paired deltas across the 40 shared cases. Do not present a one-case
difference as statistically decisive; this experiment has one GLM reader run.

## 5. Outputs

Write ignored per-arm artifacts and `summary.json` below
`var/eval/v114/cross_reader/glm53-thinking/`. Add a concise aggregate and
case-flip table to the existing v1.1.4 evaluation report. Raw credentials,
headers, provider envelopes, reasoning content, and private source text must not
be committed.

The aggregate must identify the source-report hashes, reader model, thinking
mode and verification evidence, prompt/scorer versions, timestamps, usage, and
all failed case IDs.

## 6. Decision rules

- If all three arms move similarly and their order is unchanged, reader choice
  changes the absolute score but does not overturn extractor selection.
- If the GLM extractor improves disproportionately under the GLM reader, record
  a model-style compatibility effect; do not call it causal bias from one run.
- If Qwen or local improves similarly or more, GLM thinking may be a stronger
  QA reader for this benchmark.
- If GLM performs worse, retain Qwen as the evaluation reader.

Regardless of outcome, this replay does not satisfy extraction `run2`, rewrite
the approved release gates, switch the runtime extractor, or change production
configuration. Any such decision requires a separate explicit review.

## 7. Verification and failure handling

Add unit coverage for source validation, exact reader-evidence preservation,
choice reconstruction, request construction, reasoning-content redaction,
deterministic rescoring, flip
classification, and partial-report recovery. Before the real calls, run those
focused tests.

Abort the full replay on an unsupported thinking parameter, missing final
content, source identity mismatch, or scorer mismatch. Bounded transient HTTP
failures are recorded per case; completed cases may be resumed without being
called again. A partial or mode-unverified artifact is never used for model
ranking.
