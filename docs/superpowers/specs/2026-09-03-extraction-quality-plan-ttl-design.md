# HL-Mem v1.1.4 Extraction Quality and Plan TTL Design

## 1. Goal

Improve Chinese conversational-memory extraction without adding another model
pass or a routing subsystem, and prevent temporal plans from expiring before
they can be recalled.

The v1.1.4 candidate consists of three bounded changes:

1. refine the existing Chinese and English extraction prompts so attributed
   viewpoints, reasons, feelings, relationships, and structured fields are not
   reduced to an outer action such as "discussed a book";
2. distinguish completed facts from genuinely pending plans more clearly;
3. calculate TTL for every plan from the latest safe plan boundary, regardless
   of whether the Claim is projected to the durable or episodic layer.

The already-merged 12-Claim ordinary target and 16-Claim hard limit remain
unchanged and are part of the same v1.1.4 release candidate. This design does
not add a fallback LLM, a second-pass judge, dynamic provider routing, a new
queue, or a database migration.

## 2. Evidence

The existing 40-case Chinese E2E reports cover 28 PerLTQA cases and 12
MemDaily cases:

| Extractor | QA accuracy | Recall@5 | Extraction coverage |
| --- | ---: | ---: | ---: |
| Qwen3.7-Plus | 37/40 | 0.9625 | 42/42 |
| local Qwen3.8-27B | 31/40 | 0.9125 | 41/42 |

These reports were produced by package v1.1.3 with the old extraction prompt.
The current `main` prompt hash is `7a02a17a7bd3`, so the reports are diagnostic
baselines rather than acceptance evidence for the new prompt.

Failure attribution found three different classes:

- the local 27B extractor omitted durable semantic content such as personal
  viewpoints, reasons, feelings, relationships, or structured event content;
- two local failures had a correct Claim that was classified as a temporal
  plan and expired from a historical event timestamp;
- a shared noisy-query failure was caused by retrieval ranking, not extraction.

Targeted one-call real-model probes used Qwen3.7-Plus, local Qwen3.8-27B, and
GLM-5.3-Flash. A compact viewpoint and speaker-binding rule brought all three
models to full coverage on the Nietzsche probe while keeping generic assistant
knowledge and question-only negatives empty. The same rule set recovered the
four additional omission categories on local 27B and GLM; Qwen missed one
relationship in one probe, confirming that small smoke tests remain stochastic
and cannot select the production model by themselves.

Prompt-only TTL probes were not stable. All three models classified the
commodity case safely, but Qwen3.7-Plus and GLM-5.3-Flash still classified the
Meet World case as a medium-notability plan. Therefore the prompt should improve
classification, while application code must provide the invariant.

The production database currently contains 1,739 plan Claims and has zero
Claims that were dead on arrival or expired before a stored occurrence start or
end. A forward data migration is therefore unnecessary; evaluation caches must
be rebuilt because the extractor fingerprint changes.

## 3. Approaches considered

### A. Prompt-only repair

This is the smallest textual change and improves semantic extraction, but it
cannot guarantee safe TTL because model classification varies. Rejected as an
incomplete fix.

### B. Compact prompt repair plus deterministic plan TTL anchoring

This keeps one extraction call, retains the existing schema and retention
policy, and adds one deterministic invariant at the write boundary. It directly
addresses both observed failure classes with limited code surface. This is the
selected approach.

### C. Model fallback, second-pass judging, or dynamic routing

This could recover some stochastic omissions but adds latency, cost, failure
modes, and operational policy before the simple prompt and TTL fixes have been
fairly measured. Rejected for v1.1.4.

## 4. Extraction prompt contract

Update both frozen prompt variants with semantically equivalent, compact rules:

1. Preserve explicitly attributed personal viewpoints, beliefs,
   interpretations, feelings, behavioral reasons, and practice principles when
   they could help answer a future question. Do not replace the content with
   only the fact that a discussion or activity occurred.
2. In records shaped like `Name: utterance`, bind first-person pronouns and
   personal assertions to `Name`. Do not rewrite the speaker as a generic user.
3. Do not treat a question, an unadopted quotation, or a generic assistant
   explanation as the named person's belief.
4. Skip assistant self-statements about identity, preferences, feelings, plans,
   or conversational promises unless the text is an actual reusable artifact,
   configuration, or adopted project decision.
5. Use `fact` for completed actions, current states, and decisions that later
   context says have already taken effect. Earlier words such as "decided to",
   "will", or "plan to" do not override a later completed result.
6. Use `plan` only for an explicitly pending action, especially one with a
   future date, deadline, recurrence, window, or condition.

These rules supplement rather than weaken the existing requirements for source
evidence, exact names, quantities, temporal grounding, negative filtering, and
context-rich Claim granularity. They must not introduce benchmark-specific
names or examples. The ordinary target remains 12 Claims, the hard limit remains
16, and overflow remains a deterministic one-call truncation path.

The change should replace overlapping wording where possible instead of simply
appending an ever-growing exception list. The expected prompt increase is small
(the probes added roughly 150 input tokens per extraction call). On the fixed
smoke inputs, each provider's reported input-token increase over current `main`
must stay at or below 250 tokens per call.

## 5. Plan TTL invariant

Centralize retention-anchor selection at Claim draft construction. Determine a
plan using the normalized Claim identity already available there:

```text
is_plan = canonical_attribute starts with "plan." or normalized predicate is "计划"
```

Then select the anchor as follows:

```text
if is_plan:
    anchor = max(recorded_from, occurred_start, occurred_end)
else if memory_layer is episodic:
    anchor = recorded_from
else:
    anchor = observed_at
```

Only present, valid timestamps participate in the maximum. Existing UTC
normalization and invalid-timestamp behavior remain unchanged.

This rule has the following intended effects:

- a historical plan ingested today receives its TTL from today rather than from
  the historical Event date;
- a future plan remains recallable through its latest known occurrence boundary
  plus the normal importance-based TTL;
- an ordinary historical durable fact keeps its current event-time anchor;
- an episodic non-plan keeps its current ingestion-time anchor;
- a permanent Claim still receives no expiration;
- `valid_from`, `observed_at`, scope classification, importance thresholds, and
  TTL durations do not change.

The helper name and interface should describe general retention anchoring rather
than episodic-only behavior so future call sites cannot accidentally recreate
the layer-dependent plan bug.

## 6. Data flow and failure handling

The resulting write path is:

```text
Event batch
  -> one LLM extraction
  -> existing schema repair / 16-Claim cap
  -> Claim normalization and admission
  -> deterministic plan detection
  -> retention-anchor selection
  -> existing compute_expiration
  -> store Claim and evidence
```

Malformed JSON, invalid retained Claim fields, and true output truncation keep
their existing bounded handling. Semantic omission does not trigger an automatic
second model call. TTL calculation does not call a model and does not change job
retry behavior.

No new public configuration, API field, metric service, or audit payload is
required. The existing extractor fingerprint changes naturally when the prompt
changes, which invalidates stale evaluation extraction caches.

## 7. Verification

### Deterministic tests

- Chinese and English prompts contain equivalent viewpoint, speaker, assistant,
  and fact-versus-plan rules without benchmark-specific names.
- Existing secret rejection, generic assistant rejection, evidence binding,
  Claim budgeting, and schema-repair tests continue to pass.
- A durable temporal historical plan anchors at `recorded_from` rather than an
  old `occurred_at` value.
- A durable temporal future plan expires after its latest occurrence boundary.
- An episodic plan preserves the same safe behavior.
- A durable non-plan remains anchored at `observed_at`.
- An episodic non-plan remains anchored at `recorded_from`.
- Permanent Claims remain non-expiring.
- Missing occurrence bounds and normalized timezone offsets behave
  deterministically.
- The full automated suite passes.

### Fixed real-model smoke

Run the same fixed cases once per extractor with no retries or manual prompt
tuning between arms:

- attributed philosophical viewpoint and practice;
- named-speaker first-person binding;
- personal reason and personal feeling;
- named relationship and structured event content;
- historical completed decision that should be a fact;
- historical pending plan that must not be dead on arrival;
- explicit future plan with a date or deadline;
- generic assistant knowledge, assistant self-description, and question-only
  negatives.

For each arm record extracted target coverage, false-positive negative Claims,
Claim count, model calls, input/output tokens, and latency. Every case must stay
within 16 Claims and use one LLM call unless an unrelated existing schema or
transport failure is explicitly reported.

### Fair 40-case comparison

After deterministic tests pass:

1. run Qwen3.7-Plus, GLM-5.3-Flash, and local Qwen3.8-27B against fresh,
   isolated extraction caches;
2. hold the dataset, embedding model, reranker, QA model, settings, and commit
   constant so only the extractor changes;
3. run all three arms once, then repeat the best two extractor arms once to
   expose stochastic omissions without turning the evaluation into open-ended
   tuning;
4. publish both official benchmark metrics and layer-attributed failures
   (`extraction`, `TTL`, `retrieval`, `QA/scorer`) so a shared retrieval miss or
   rubric false negative is not charged to the extractor.

An extractor is release-eligible when its worst fresh run has at least 36/40 QA
accuracy, at least 41/42 extraction coverage, zero negative violations, no
Claim-count retry storm, and no repeated omission of a critical supported
viewpoint, reason, relationship, or structured field.

Qwen3.7-Plus remains the production quality baseline until this comparison is
complete. A cheaper challenger may replace it only when two fresh runs are no
more than one QA case worse than Qwen, meet the same extraction and safety gates,
and show no new critical omission class. GLM-5.3-Flash is the primary low-cost
challenger. Local Qwen3.8-27B remains a privacy/offline option unless it meets
the same evidence bar. Provider selection stays a deployment configuration
decision, not runtime routing logic.

## 8. Compatibility, rollout, and exclusions

- Prepare the code and release metadata as v1.1.4 after the verification gates
  pass.
- Do not rewrite historical Claims. The production read-only audit found no
  stored plan requiring TTL repair; test and evaluation databases are rebuilt.
- Do not change retrieval ranking or the strict Nietzsche answer rubric in this
  work. Record those failures separately.
- Do not add model fallback, multi-model voting, overflow extraction, or a
  second semantic scoring pass.
- Do not change the 12/16 Claim budget or the existing job retry limits.
- Remote push, deployment, and provider-default changes occur only after the
  final comparison is reviewed.

## 9. Completion definition

The work is complete when both prompts implement the compact contract; every
plan uses the safe retention anchor across durable and episodic layers; focused
and full automated tests pass; the fixed three-model smoke and fresh-cache
40-case comparison are recorded; a provider recommendation is made from the
two-run evidence; and the verified local release candidate is ready for the
explicit integration decision.
