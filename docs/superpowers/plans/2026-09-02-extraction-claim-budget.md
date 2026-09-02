# Extraction Claim Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make conversational extraction produce context-rich Claims with a 12-Claim ordinary target and a deterministic 16-Claim hard ceiling, without Claim-count-driven LLM splitting or retries.

**Architecture:** Keep the budget policy inside the extraction package: shared constants live beside the response schema, raw compact responses are deterministically ranked and capped before top-level validation, and the orchestrator emits a bounded overflow audit. Actual provider output truncation retains the existing recursive split path; legacy soft-split configuration remains accepted but no longer triggers model calls.

**Tech Stack:** Python 3.12+, Pydantic v2, pytest, SQLite-backed audit capture, existing `LLMExtractor` fake clients, Zhipu OpenAI-compatible Coding Plan endpoint.

## Global Constraints

- Ordinary extraction should produce at most 12 Claims; 16 Claims is the hard per-chunk limit.
- A valid response above 16 Claims must be reduced in the same LLM call, never count-split or retried.
- Rank overflow by recognised notability, then confidence, then original position; add no scoring-model call.
- Keep `LLMOutputTruncatedError` chunk splitting and bounded malformed-schema repair.
- Keep raw Events, evidence links, admission, conflict, deduplication, and secrets policy unchanged.
- Add no database migration, queue, public API field, Provider call, or replacement configuration key.
- Keep `extraction.soft_split_enabled` and `extraction.delta_repair_enabled` accepted as deprecated no-ops.
- Touch only files listed below and preserve unrelated dirty-worktree files.

---

### Task 1: Shared budget and deterministic raw-response cap

**Files:**
- Modify: `src/hl_mem/ingest/extraction/schema.py`
- Modify: `src/hl_mem/ingest/extraction/parsing.py`
- Modify: `tests/unit/test_ingest_schemas.py`
- Modify: `tests/unit/test_extraction_chunking.py`

**Interfaces:**
- Produces: `ORDINARY_CLAIM_TARGET: Final[int] = 12`
- Produces: `MAX_CLAIMS_PER_CHUNK: Final[int] = 16`
- Produces: `ClaimBudgetResult(NamedTuple)` with `payload`, `generated_count`, `retained_count`, and `dropped_count`
- Produces: `cap_extraction_claims(payload: dict[str, Any], limit: int = MAX_CLAIMS_PER_CHUNK) -> ClaimBudgetResult`
- Consumes: raw response dictionaries after `repair_extraction_json` and before `CompactExtractionResponseSchema.model_validate`

- [ ] **Step 1: Change the schema test to require 16**

```python
def test_extraction_response_claim_limit_matches_hard_budget() -> None:
    assert extraction_response_json_schema()["properties"]["claims"]["maxItems"] == 16
```

- [ ] **Step 2: Add failing cap-order tests**

Add tests that construct 17+ raw compact items and assert:

```python
result = cap_extraction_claims(payload)
assert result.generated_count == 18
assert result.retained_count == 16
assert result.dropped_count == 2
assert [item["value"] for item in result.payload["claims"]] == expected_priority_order
```

Cover `high > medium > low`, confidence descending within one notability,
stable original order for ties, a non-list `claims` value left untouched for
ordinary schema validation, and an input dictionary that is not mutated.

- [ ] **Step 3: Run the new tests and confirm the old behavior fails**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_ingest_schemas.py tests/unit/test_extraction_chunking.py -q --tb=short
```

Expected: schema assertion reports `30 != 16`; cap tests fail to import `cap_extraction_claims`.

- [ ] **Step 4: Add the shared constants and cap helper**

In `schema.py`:

```python
from typing import Final

ORDINARY_CLAIM_TARGET: Final[int] = 12
MAX_CLAIMS_PER_CHUNK: Final[int] = 16

class CompactExtractionResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[CompactExtractedClaimSchema] = Field(max_length=MAX_CLAIMS_PER_CHUNK)
    should_memorize: bool
```

In `parsing.py`, return a copied payload and use this deterministic key:

```python
class ClaimBudgetResult(NamedTuple):
    payload: dict[str, Any]
    generated_count: int
    retained_count: int
    dropped_count: int

def _raw_claim_priority(indexed: tuple[int, Any]) -> tuple[int, float, int]:
    index, item = indexed
    if not isinstance(item, dict):
        return (0, 0.0, -index)
    rank = {"high": 3, "medium": 2, "low": 1}.get(item.get("notability"), 0)
    try:
        confidence = float(item.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if not math.isfinite(confidence):
        confidence = 0.0
    return (rank, confidence, -index)

def cap_extraction_claims(
    payload: dict[str, Any], limit: int = MAX_CLAIMS_PER_CHUNK
) -> ClaimBudgetResult:
    bounded = deepcopy(payload)
    claims = bounded.get("claims")
    if not isinstance(claims, list):
        return ClaimBudgetResult(bounded, 0, 0, 0)
    generated = len(claims)
    if generated > limit:
        bounded["claims"] = [
            item for _, item in sorted(enumerate(claims), key=_raw_claim_priority, reverse=True)[:limit]
        ]
    retained = min(generated, limit)
    return ClaimBudgetResult(bounded, generated, retained, generated - retained)
```

Reject `limit < 1` with `ValueError("claim limit must be positive")` and test it.

- [ ] **Step 5: Run focused tests**

Run the command from Step 3. Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add src/hl_mem/ingest/extraction/schema.py src/hl_mem/ingest/extraction/parsing.py tests/unit/test_ingest_schemas.py tests/unit/test_extraction_chunking.py
git commit -m "feat: add deterministic extraction claim budget"
```

---

### Task 2: Integrate capping and remove Claim-count model recursion

**Files:**
- Modify: `src/hl_mem/ingest/extraction/orchestrator.py`
- Modify: `tests/unit/test_extraction_chunking.py`
- Modify: `tests/unit/test_llm_extractor.py`
- Modify: `tests/unit/test_worker.py`

**Interfaces:**
- Consumes: `cap_extraction_claims` and `MAX_CLAIMS_PER_CHUNK` from Task 1
- Produces: extraction audit outcome `extract/claim_budget/overflow_truncated`
- Preserves: `_extract_chunk_with_auto_split` recovery for `LLMOutputTruncatedError`

- [ ] **Step 1: Replace obsolete saturation/split tests with failing budget tests**

Create tests with `_SequenceClient` responses that assert:

```python
claims = extractor.extract(source)
assert len(claims) == 16
assert len(client.requests) == 1
assert [event[2] for event in audit.events] == ["overflow_truncated"]
assert audit.events[0][3] == {
    "generated_claim_count": 17,
    "retained_claim_count": 16,
    "dropped_claim_count": 1,
    "chunk_index": 0,
    "start_unit": 0,
    "end_unit": 1,
}
```

Run this once with `soft_split_enabled=False` and once with
`soft_split_enabled=True, delta_repair_enabled=True`; both must use exactly one
request. Add an exact-16 case that emits no overflow event.

- [ ] **Step 2: Add a worker-level failing regression**

Queue one extraction job whose fake LLM returns 17 valid compact Claims. Assert
that one worker iteration leaves the job `succeeded`, with `attempts == 1` and
no second queued attempt. Reuse the existing worker fixtures and fake component
factory in `tests/unit/test_worker.py`.

- [ ] **Step 3: Run the focused tests and confirm count recursion remains**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_extraction_chunking.py tests/unit/test_llm_extractor.py tests/unit/test_worker.py -q --tb=short
```

Expected: the new tests fail because over-limit output raises/splits and the
overflow audit does not exist.

- [ ] **Step 4: Cap before compact validation and emit the bounded audit**

In `_request_chunk`, immediately after `repair_extraction_json`:

```python
budget = cap_extraction_claims(repaired)
repaired = budget.payload
if budget.dropped_count:
    current_audit().emit(
        "extract",
        "claim_budget",
        "overflow_truncated",
        detail={
            "generated_claim_count": budget.generated_count,
            "retained_claim_count": budget.retained_count,
            "dropped_claim_count": budget.dropped_count,
            "chunk_index": chunk.index,
            "start_unit": chunk.start_unit,
            "end_unit": chunk.end_unit,
        },
    )
```

Apply the same cap before legacy response validation so the hard product limit
does not depend on which compatible response shape the provider selected.

- [ ] **Step 5: Remove count-driven recursion and saturation work**

Simplify `_extract_chunk_with_auto_split` so a successful `_extract_one_chunk`
returns its postprocessed/verified Claims immediately. Remove:

- `is_claim_count_overflow` handling and its `LLMSchemaValidationError` split;
- exact-limit `claim_limit_reached` audit;
- `compact_soft_saturated` state;
- soft split and delta-repair calls;
- private delta-repair methods and imports that become unreachable.

Keep the `except LLMOutputTruncatedError` branch byte-for-byte in behavior,
including `depth + 1`, merge, and `max_split_depth` enforcement.

- [ ] **Step 6: Run focused tests**

Run the command from Step 3. Expected: all selected tests pass, including the
existing output-truncation recursion test.

- [ ] **Step 7: Commit Task 2**

```powershell
git add src/hl_mem/ingest/extraction/orchestrator.py tests/unit/test_extraction_chunking.py tests/unit/test_llm_extractor.py tests/unit/test_worker.py
git commit -m "fix: stop claim overflow from multiplying extraction calls"
```

---

### Task 3: Context-rich Prompt and deprecated compatibility switches

**Files:**
- Modify: `src/hl_mem/ingest/extraction/prompts.py`
- Modify: `tests/unit/test_extraction_prompt_quality.py`
- Modify: `docs/configuration.md`
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: `ORDINARY_CLAIM_TARGET` and `MAX_CLAIMS_PER_CHUNK` from Task 1
- Produces: matching Chinese and English Prompt rules with no 12-30 atomic target
- Preserves: existing compact field names, kinds, evidence, time, and secret rules

- [ ] **Step 1: Write failing Chinese/English Prompt contract tests**

Assert both production prompts contain their language-equivalent requirements:

```python
assert "普通内容应不超过 12 条" in SYSTEM_PROMPT
assert "硬上限为 16 条" in SYSTEM_PROMPT
assert "可独立更新、冲突、过期或召回" in SYSTEM_PROMPT
assert "12–30" not in SYSTEM_PROMPT

assert "Ordinary content should stay at or below 12 claims" in ENGLISH_SYSTEM_PROMPT
assert "The hard limit is 16 claims" in ENGLISH_SYSTEM_PROMPT
assert "independently updated, contradicted, expired, or recalled" in ENGLISH_SYSTEM_PROMPT
assert "12–30" not in ENGLISH_SYSTEM_PROMPT
```

Also assert the schema's `maxItems` value appears in each Prompt to catch future
constant drift.

- [ ] **Step 2: Run Prompt tests and confirm failure**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_extraction_prompt_quality.py -q --tb=short
```

Expected: assertions fail on the current coverage-first 12-30 wording.

- [ ] **Step 3: Replace atomic-coverage wording in both prompts**

Use shared constants through a small formatting helper rather than duplicating
numeric literals. Preserve the frozen schema-field upgrade functions by keeping
their string anchors unchanged. The final rules must say:

- store future-useful durable information, not every clause;
- split only when information can change independently;
- merge exact names, dates, quantities, reasons, and transitions into their
  governing Claim;
- do not pad; ordinary output stays at or below 12 and hard output at or below
  16;
- order high/medium/low notability and confidence descending.

- [ ] **Step 4: Mark legacy count-coverage switches as deprecated no-ops**

In `docs/configuration.md`, retain both TOML keys and document that 1.1.3+ accepts
them for compatibility but Claim-count saturation no longer causes extra calls.
In `docs/architecture.md`, replace the exact-limit warning/split description
with deterministic overflow reduction and clarify that true provider output
truncation can still split.

- [ ] **Step 5: Run Prompt and configuration tests**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_extraction_prompt_quality.py tests/unit/test_config.py tests/unit/test_settings.py -q --tb=short
```

If one listed configuration test file does not exist, use `rg --files tests/unit
| rg 'config|settings'` and run every matching existing unit-test file. Expected:
all selected tests pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/hl_mem/ingest/extraction/prompts.py tests/unit/test_extraction_prompt_quality.py docs/configuration.md docs/architecture.md
git commit -m "feat: prefer context-rich bounded memory extraction"
```

---

### Task 4: Regression cleanup, real Zhipu smoke, and release evidence

**Files:**
- Modify: `tests/unit/test_softsplit_ab_equipment.py` only if obsolete production-outcome assertions fail
- Modify: `docs/CHANGELOG.md`
- Create: `docs/research/2026-09-02-extraction-claim-budget-smoke.md`

**Interfaces:**
- Consumes: completed deterministic extraction behavior from Tasks 1-3
- Produces: bounded live-model evidence with Claim counts, LLM call counts, token use, and qualitative support notes

- [ ] **Step 1: Run the complete extraction-focused unit set**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_llm_extractor.py tests/unit/test_extraction_chunking.py tests/unit/test_extraction_prompt_quality.py tests/unit/test_softsplit_ab_equipment.py tests/unit/test_worker.py -q --tb=short
```

Expected: all pass. If `test_softsplit_ab_equipment.py` fails only because it
expects removed production audit outcomes, update those assertions to treat the
legacy experiment switches as accepted no-ops; do not remove its report-parser
coverage.

- [ ] **Step 2: Add the changelog entry**

Under the current unreleased section, record:

```markdown
- Extraction now targets at most 12 context-rich Claims and deterministically
  retains at most 16 per chunk. Oversized valid responses no longer trigger
  Claim-count-driven splitting or retry; true output truncation recovery remains.
```

- [ ] **Step 3: Run a five-case Zhipu Coding Plan smoke**

Use the repository's existing live-smoke or evaluation entry point and current
`hl_mem.toml` provider settings. Do not print or copy API keys. Run exactly the
five cases in the approved design once each. If the existing entry point cannot
report `llm_call_count`, use the public extractor result state rather than adding
a new production surface.

Record a Markdown table with these columns:

```text
case | generated/retained claims | llm calls | input tokens | output tokens | supported | notes
```

The dense case passes only with `retained <= 16`, no Claim-count split, and no
schema failure. The no-memory and assistant-chatter cases pass only if they do
not invent durable user facts.

- [ ] **Step 4: Run a fixed small LongMemEval slice if its local dataset exists**

Use the existing checked-in evaluation tooling and a fixed 20-item slice with
information-extraction, update, temporal, and multi-session cases. Do not
download data or expand the paid run if the dataset is absent. Compare against
the most recent compatible local baseline and record exact case IDs, accuracy,
and token totals in the smoke report.

- [ ] **Step 5: Run full verification**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/ -q --tb=short
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m black --check src tests
.venv\Scripts\python.exe -m isort --check-only src tests
.venv\Scripts\python.exe -m mypy src
git diff --check
```

Expected: every command exits 0. If the repository exposes a canonical aggregate
gate in `pyproject.toml`, `Makefile`, or `scripts/`, run it as an additional check,
not as a replacement for the focused tests.

- [ ] **Step 6: Review scope and commit**

Confirm `git diff --name-only` contains only the planned files and the smoke
report, and that pre-existing untracked files remain untouched. Then:

```powershell
git add tests/unit/test_softsplit_ab_equipment.py docs/CHANGELOG.md docs/research/2026-09-02-extraction-claim-budget-smoke.md
git commit -m "test: verify bounded extraction with live zhipu smoke"
```

If `tests/unit/test_softsplit_ab_equipment.py` was unchanged, omit it from
`git add`.
