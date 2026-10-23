# HL-Mem Extraction Claim Budget Design

## 1. Goal

Prevent a dense extraction response from failing or recursively multiplying LLM
calls, while improving memory usefulness by avoiding overly fragmented Claims.
The change must preserve raw Events, evidence provenance, ordinary schema repair,
and output-truncation recovery.

The approved policy is:

- ordinary extraction should produce no more than 12 Claims;
- 16 Claims is the hard per-chunk safety limit;
- related details that change and are recalled together belong in one
  context-rich Claim;
- a valid response above 16 Claims is deterministically reduced to 16 and is
  not retried or split because of its Claim count.

## 2. Evidence and rationale

The current extraction prompt explicitly prioritises exhaustive atomic coverage,
states that dense input often yields 12-30 Claims, and asks the model to separate
distinct quantities and attributes. The compact response schema then rejects
more than 30 Claims. The orchestrator treats that rejection as a reason to split
the chunk recursively.

The current database contains 4,678 Events linked to at least one Claim. Their
post-admission Claim-count distribution is P50=3, P90=10, P95=13, and P99=19.
A simulated importance/confidence truncation gives:

| Hard limit | Events affected | Share of non-empty Events | High Claims dropped | Medium Claims dropped | Low Claims dropped |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 12 | 272 | 5.81% | 48 | 443 | 943 |
| 16 | 126 | 2.69% | 21 | 226 | 365 |
| 20 | 13 | 0.28% | 7 | 146 | 121 |

The local figures are directional rather than a benchmark because they reflect
the existing fine-grained prompt. They nevertheless show that 12 is too tight
as a failure boundary and that 16 provides a useful buffer without retaining the
old 30-Claim incentive.

External reference implementations do not expose a universal per-input fact
limit. Mem0's current extraction prompt instead recommends context-rich rather
than atomic memories, suggests 15-80 words per memory, and says conversations of
10 or more messages typically yield 5-15 memories. LangMem warns that
over-extraction reduces retrieval precision. Graphiti does not bound its fact
list, but pays for graph resolution and deduplication that HL-Mem should not add
for this fix. Recent memory-structure research likewise favours retaining raw
source material alongside selectively structured memories. HL-Mem already
retains the source Events, so the extracted Claim store does not need to serve
as a lossless transcript.

## 3. Claim granularity and prompt contract

Replace the existing coverage-first count guidance with these rules:

1. Extract only information likely to help a future answer or action: durable
   preferences, explicit constraints, adopted decisions, ongoing projects,
   meaningful plans, and independently updateable facts.
2. A Claim boundary represents information that can be independently updated,
   contradicted, expired, or recalled. It does not represent every noun, number,
   or grammatical clause.
3. Keep exact names, dates, quantities, reasons, and transition context inside
   the relevant Claim instead of emitting each as a separate Claim.
4. Combine details about the same subject and change when they share the same
   lifecycle and retrieval purpose. Keep unrelated topics separate.
5. Zero Claims is valid. Do not pad toward a target. Ordinary chunks should stay
   at or below 12 Claims and every chunk must stay at or below 16.
6. Return Claims in descending usefulness: high notability before medium before
   low, then higher confidence first.

The prompt must continue to reject secrets, unsupported inference, generic
assistant chatter, service-health snapshots, and other existing skip classes.
The change must not weaken evidence quotes, source Event indices, numerical
precision, or temporal grounding.

## 4. Deterministic hard-limit handling

Define one shared hard-limit constant of 16 and use it in the compact JSON
schema, prompt construction, saturation/audit details, and overflow handling so
the values cannot drift.

After JSON parsing and existing deterministic repair, inspect the raw `claims`
list before top-level Pydantic validation. When it contains more than 16 items:

1. rank items by recognised raw `notability` (`high`, `medium`, `low`), then
   numeric `confidence`, then original position;
2. retain the first 16;
3. validate the retained response through the unchanged compact Claim schema;
4. emit one bounded audit event containing only generated, retained, and dropped
   counts plus the chunk coordinates.

Malformed retained Claims still follow the existing bounded schema-repair path.
Malformed Claims that are outside the retained set do not need to make the
otherwise usable response fail because they will never be stored.

This is an intentionally lossy index projection. The raw Event remains the
authoritative source and can be reprocessed later.

## 5. Split and retry behaviour

Claim-count saturation or overflow must not call the LLM again. Remove the hard
Claim-overflow branch that bisects chunks after `LLMSchemaValidationError`, and
remove the exact-limit soft split/delta-repair execution path.

Keep automatic chunk splitting for actual output truncation, represented by
`LLMOutputTruncatedError`. Keep the existing bounded correction attempt for
malformed JSON or invalid retained Claim fields.

The existing `extraction.soft_split_enabled` and
`extraction.delta_repair_enabled` configuration keys remain accepted as
deprecated no-ops for compatibility in this release. No replacement setting is
added. Documentation must identify them as inert so operators do not expect
additional coverage calls.

Jobs continue to use the existing `attempts`, `max_attempts`, and `dead` state.
This design adds no dead-letter service or queue migration. A structurally valid
oversized response is reduced and succeeds during the same job attempt.

## 6. Observability

Reuse the extraction audit stream. A new or renamed count-overflow outcome must
record only:

- original Claim count;
- retained Claim count (16);
- dropped Claim count;
- chunk index and source-unit bounds.

Do not log Claim text, evidence text, prompts, or raw model output. No new metrics
service is required. Operations can count this audit outcome to determine the
overflow rate.

## 7. Verification

### Deterministic tests

- the generated compact schema advertises `maxItems=16`;
- Chinese and English prompts remove the 12-30 coverage target and describe the
  12 ordinary/16 hard limits plus context-rich granularity;
- 0, 12, and 16 valid Claims pass without splitting;
- 17 and 30 valid Claims are ranked and reduced to 16 in one extraction call;
- ordering is deterministic for equal notability and confidence;
- malformed retained items use the existing bounded schema retry;
- output-token truncation still bisects within `max_split_depth`;
- count overflow never enters the output-truncation split path;
- the overflow audit contains counts and coordinates but no content;
- extraction jobs succeed instead of returning to `pending` for a valid
  oversized response;
- existing admission, evidence, conflict, deduplication, and secret-rejection
  tests remain unchanged.

### Real-model check using the approved Zhipu Coding Plan

Use the configured Zhipu Coding endpoint for a small, explicitly budgeted smoke
set after deterministic tests pass:

1. a message with no durable memory;
2. a normal message containing three unrelated durable topics;
3. a dense multi-message batch with more than 16 candidate details;
4. a transition containing old state, new state, reason, date, and quantities;
5. a long assistant technical answer that should mostly be skipped.

For each case verify valid JSON, at most 16 retained Claims, evidence support,
and no Claim-count-driven recursive call. Record Claim counts, LLM call counts,
and token use. Do not run an unbounded benchmark or repeat cases merely to tune
the output toward a preferred result.

If available, follow with a small fixed LongMemEval slice covering information
extraction, updates, temporal reasoning, and multi-session reasoning. The new
policy is acceptable when the slice has no answer-quality regression attributable
to a dropped high-value fact and the dense case uses fewer model calls than the
current overflow path.

## 8. Compatibility and scope exclusions

This change adds no database migration, public API field, Provider call, scoring
model, overflow queue, or retrieval change. It does not delete or rewrite
historical Claims. It does not alter raw Event retention or evidence links.

Document ingestion requiring exhaustive preservation is outside the automatic
conversation-memory contract and should use raw storage or an explicit import
path rather than raising the conversational Claim budget.

## 9. Completion definition

The work is complete when the prompt produces coarser, reusable memories; the
schema and application enforce a deterministic 16-Claim ceiling; valid oversized
responses complete without count-driven retries or splitting; true output
truncation remains recoverable; focused and full automated tests pass; and the
budgeted Zhipu smoke confirms the intended live-model behaviour.
