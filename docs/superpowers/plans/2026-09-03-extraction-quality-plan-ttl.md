# Extraction Quality and Plan TTL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an evidence-backed v1.1.4 candidate that preserves attributed personal meaning, distinguishes completed facts from pending plans, and prevents any plan from expiring before its safe recall boundary.

**Architecture:** Keep semantic guidance in the existing frozen bilingual extraction prompts and enforce time safety once at Claim draft construction. Add only evaluation-side seams needed to run the same extractor inputs, caches, and Qwen QA reader across Qwen3.7-Plus, GLM-5.3-Flash, and local Qwen3.8-27B; no runtime model routing or second model pass is introduced.

**Tech Stack:** Python 3.12+, Pydantic v2, pytest, SQLite, existing `LLMExtractor`/`IngestService`, DashScope Coding Plan, Zhipu Coding Plan, local llama.cpp OpenAI-compatible server.

## Global Constraints

- Keep one extraction call per ordinary smoke case; do not add fallback, voting, or semantic-judge calls.
- Keep the ordinary target at 12 Claims and the hard per-chunk limit at 16 Claims.
- Preserve existing JSON/schema repair and true output-truncation recovery.
- Preserve exact evidence, names, quantities, source indices, admission, conflicts, deduplication, and secret filtering.
- Every plan uses `max(recorded_from, occurred_start, occurred_end)` as its TTL anchor; missing occurrence bounds are ignored.
- Durable non-plans keep `observed_at`; episodic non-plans keep `recorded_from`; permanent Claims remain non-expiring.
- Add no database migration, public API field, production configuration key, queue, or runtime Provider routing.
- Do not change retrieval ranking or the strict Nietzsche answer rubric in this work.
- Do not modify, stage, or commit the pre-existing untracked files, including the two operator-owned E2E TOML files.
- Qwen3.7-Plus remains the production quality baseline until the fresh three-arm comparison is reviewed.
- Remote push, deployment, tag creation, publication, and provider-default changes remain outside this plan.

---

### Task 1: Reproducible extraction-quality smoke harness and baseline

**Files:**
- Create: `evaluation/tools/run_extraction_quality_smoke.py`
- Create: `tests/eval/fixtures/extraction_quality_smoke_v1.json`
- Create: `tests/unit/test_extraction_quality_smoke.py`

**Interfaces:**
- Produces: `load_cases(path: Path) -> tuple[SmokeCase, ...]`
- Produces: `score_case(case: SmokeCase, claims: Sequence[ExtractedClaim]) -> SmokeScore`
- Produces CLI: `python -m evaluation.tools.run_extraction_quality_smoke --config PATH --env-file PATH --label NAME --report PATH`
- Produces report schema `extraction-quality-smoke-v1` with model coordinate, prompt hash, per-case target coverage, generated/retained counts, model-call count, input/output tokens, latency, and safe synthetic Claim summaries.
- Consumes: `make_extractor(settings, require_real=True)`, `LLMExtractor.last_llm_call_count`, `last_input_tokens`, and `last_output_tokens`.

- [ ] **Step 1: Add the fixed synthetic fixture**

Create `tests/eval/fixtures/extraction_quality_smoke_v1.json` with this exact shape and case set:

```json
{
  "schema_version": 1,
  "cases": [
    {
      "id": "attributed_viewpoint_and_speaker",
      "occurred_at": "2026-09-03T00:00:00+00:00",
      "messages": [
        {
          "speaker": "user",
          "text": "张岚：我认为真正的自由不是没有约束，而是在承担选择后果时不断超越旧有框架。我会尊重与他人共处的边界，并通过反思自己的决定来实践个人成长。"
        }
      ],
      "required_claims": [
        {
          "subject": "张岚",
          "term_groups": [["自由"], ["承担", "后果"], ["约束"]]
        },
        {
          "subject": "张岚",
          "term_groups": [["边界"], ["反思", "成长"]]
        }
      ],
      "forbidden_subjects": ["user", "用户"],
      "expect_empty": false
    },
    {
      "id": "personal_reason_and_feeling",
      "occurred_at": "2026-09-03T00:01:00+00:00",
      "messages": [
        {
          "speaker": "user",
          "text": "林月：我开始拍纪录片，是因为想让普通人的经历被看见。看到胚胎发育影像时，我感到生命既脆弱又令人敬畏。"
        }
      ],
      "required_claims": [
        {"subject": "林月", "term_groups": [["纪录片"], ["普通人"], ["看见"]]},
        {"subject": "林月", "term_groups": [["胚胎"], ["脆弱"], ["敬畏"]]}
      ],
      "forbidden_subjects": ["user", "用户"],
      "expect_empty": false
    },
    {
      "id": "named_relationship",
      "occurred_at": "2026-09-03T00:02:00+00:00",
      "messages": [
        {
          "speaker": "user",
          "text": "具体人物：周宁\n描述：陈晖的大学同学，两人经常共同制作短片。\n与陈晖的关系：同学和长期合作伙伴。"
        }
      ],
      "required_claims": [
        {"subject": "周宁", "term_groups": [["周宁"], ["陈晖"], ["同学"]]},
        {"subject": "周宁", "term_groups": [["周宁"], ["陈晖"], ["合作", "短片"]]}
      ],
      "forbidden_subjects": [],
      "expect_empty": false
    },
    {
      "id": "structured_event_content",
      "occurred_at": "2026-09-03T00:03:00+00:00",
      "messages": [
        {
          "speaker": "user",
          "text": "活动名称：销售创新研讨会\n地点：北京\n主要内容：分享成功案例并探讨销售创新策略\n目的：促进业内合作。"
        }
      ],
      "required_claims": [
        {"subject": "销售创新研讨会", "term_groups": [["成功案例"], ["销售创新策略"]]},
        {"subject": "销售创新研讨会", "term_groups": [["北京"]]}
      ],
      "forbidden_subjects": [],
      "expect_empty": false
    },
    {
      "id": "completed_decision_is_fact",
      "occurred_at": "2024-01-01T00:00:00+00:00",
      "messages": [
        {
          "speaker": "user",
          "text": "李明：年初我决定将部分资金配置到原油、黄金和铜；三月已经完成配置，目前仍持有这些资产。"
        }
      ],
      "required_claims": [
        {"subject": "李明", "predicate": "事实", "term_groups": [["原油"], ["黄金"], ["铜"], ["完成", "持有"]]}
      ],
      "forbidden_subjects": ["user", "用户"],
      "expect_empty": false
    },
    {
      "id": "historical_pending_plan",
      "occurred_at": "2024-01-01T00:00:00+00:00",
      "messages": [
        {
          "speaker": "user",
          "text": "沈青：这个安排尚未完成，我仍计划在 Meet World 的个人资料中展示兴趣爱好和生物医学科学成果。"
        }
      ],
      "required_claims": [
        {"subject": "沈青", "predicate": "计划", "term_groups": [["Meet World"], ["兴趣爱好"], ["生物医学科学"]]}
      ],
      "forbidden_subjects": ["user", "用户"],
      "expect_empty": false
    },
    {
      "id": "explicit_future_plan",
      "occurred_at": "2026-09-03T00:06:00+00:00",
      "messages": [
        {
          "speaker": "user",
          "text": "宋宁：我将在 2026 年 9 月 11 日提交方案，截止时间是当天 18:00。"
        }
      ],
      "required_claims": [
        {"subject": "宋宁", "predicate": "计划", "term_groups": [["2026"], ["9 月 11 日", "9月11日"], ["18:00"], ["提交方案"]]}
      ],
      "forbidden_subjects": ["user", "用户"],
      "expect_empty": false
    },
    {
      "id": "assistant_and_question_negatives",
      "occurred_at": "2026-09-03T00:07:00+00:00",
      "messages": [
        {"speaker": "assistant", "text": "一般来说，存在主义讨论自由、责任和选择。"},
        {"speaker": "assistant", "text": "作为 AI 助手，我是一个平凡的人，我喜欢散步，明天会提醒你。"},
        {"speaker": "user", "text": "尼采如何理解超越道德？"}
      ],
      "required_claims": [],
      "forbidden_subjects": [],
      "expect_empty": true
    }
  ]
}
```

- [ ] **Step 2: Write failing loader and scorer tests**

In `tests/unit/test_extraction_quality_smoke.py`, require exact IDs and general matching behavior:

```python
from pathlib import Path

from evaluation.tools.run_extraction_quality_smoke import load_cases, score_case
from hl_mem.ingest.extractors import ExtractedClaim


FIXTURE = Path("tests/eval/fixtures/extraction_quality_smoke_v1.json")


def test_fixture_is_versioned_and_fixed() -> None:
    cases = load_cases(FIXTURE)
    assert [case.case_id for case in cases] == [
        "attributed_viewpoint_and_speaker",
        "personal_reason_and_feeling",
        "named_relationship",
        "structured_event_content",
        "completed_decision_is_fact",
        "historical_pending_plan",
        "explicit_future_plan",
        "assistant_and_question_negatives",
    ]


def test_score_case_requires_named_subject_terms_and_predicate() -> None:
    case = next(item for item in load_cases(FIXTURE) if item.case_id == "completed_decision_is_fact")
    passing = [ExtractedClaim("事实", "李明已经完成原油、黄金和铜的配置", subject="李明")]
    wrong_kind = [ExtractedClaim("计划", "李明将配置原油、黄金和铜", subject="李明")]
    wrong_subject = [ExtractedClaim("事实", "用户已经完成原油、黄金和铜的配置", subject="user")]
    assert score_case(case, passing).passed is True
    assert score_case(case, wrong_kind).passed is False
    assert score_case(case, wrong_subject).passed is False


def test_score_case_requires_empty_output_for_negative_case() -> None:
    case = next(item for item in load_cases(FIXTURE) if item.case_id == "assistant_and_question_negatives")
    assert score_case(case, []).passed is True
    assert score_case(case, [ExtractedClaim("事实", "AI 助手喜欢散步", subject="AI 助手")]).passed is False
```

- [ ] **Step 3: Run tests and verify the harness is absent**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_extraction_quality_smoke.py -q --tb=short
```

Expected: collection fails because `evaluation.tools.run_extraction_quality_smoke` does not exist.

- [ ] **Step 4: Implement the fixture types and deterministic scorer**

In `evaluation/tools/run_extraction_quality_smoke.py`, define these stable interfaces:

```python
@dataclass(frozen=True)
class ExpectedClaim:
    subject: str
    term_groups: tuple[tuple[str, ...], ...]
    predicate: str | None = None


@dataclass(frozen=True)
class SmokeCase:
    case_id: str
    occurred_at: str
    messages: tuple[dict[str, str], ...]
    required_claims: tuple[ExpectedClaim, ...]
    forbidden_subjects: frozenset[str]
    expect_empty: bool


@dataclass(frozen=True)
class SmokeScore:
    passed: bool
    covered_targets: int
    target_count: int
    missing_targets: tuple[int, ...]
    forbidden_subject_hits: tuple[str, ...]


def _matches(claim: ExtractedClaim, expected: ExpectedClaim) -> bool:
    searchable = f"{claim.subject} {claim.value}"
    return (
        claim.subject == expected.subject
        and (expected.predicate is None or claim.predicate == expected.predicate)
        and all(any(term in searchable for term in group) for group in expected.term_groups)
    )


def score_case(case: SmokeCase, claims: Sequence[ExtractedClaim]) -> SmokeScore:
    missing = tuple(
        index
        for index, expected in enumerate(case.required_claims)
        if not any(_matches(claim, expected) for claim in claims)
    )
    forbidden = tuple(sorted({claim.subject for claim in claims if claim.subject in case.forbidden_subjects}))
    empty_ok = not claims if case.expect_empty else True
    return SmokeScore(
        passed=empty_ok and not missing and not forbidden,
        covered_targets=len(case.required_claims) - len(missing),
        target_count=len(case.required_claims),
        missing_targets=missing,
        forbidden_subject_hits=forbidden,
    )
```

`load_cases` must reject a schema version other than `1`, duplicate IDs, an empty term group, or `expect_empty=true` with required Claims. Add loader tests that write each invalid shape to `tmp_path` and assert `ValueError` with `schema_version`, `duplicate case id`, `empty term group`, or `empty case cannot require claims`, respectively.

- [ ] **Step 5: Implement one-call execution and safe JSON reporting**

Use `replace(load_settings(...), verification_mode="off", llm_schema_retries=0, llm_max_attempts=1)` and `make_extractor(..., require_real=True)`. Build each source as production-shaped messages with `event_index`, `speaker`, `turn`, `occurred_at`, and `content`. Bind a small recording audit through `audit_scope`; when an `extract/claim_budget/overflow_truncated` event exists, use its generated/retained counts, otherwise both counts equal `len(claims)`.

The report writer must emit this exact top-level contract and must never include credentials, request headers, or raw Provider envelopes:

```python
report = {
    "schema_version": "extraction-quality-smoke-v1",
    "label": args.label,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "model": {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "prompt_hash": PROMPT_HASH,
    },
    "summary": {
        "passed": all(item["passed"] for item in results),
        "cases": len(results),
        "passed_cases": sum(bool(item["passed"]) for item in results),
        "target_coverage": sum(item["covered_targets"] for item in results)
        / max(1, sum(item["target_count"] for item in results)),
        "negative_violations": sum(bool(item["expect_empty"] and item["retained_count"]) for item in results),
        "llm_calls": sum(item["llm_calls"] for item in results),
        "input_tokens": sum(item["input_tokens"] for item in results),
        "output_tokens": sum(item["output_tokens"] for item in results),
    },
    "cases": results,
}
```

Exit `0` only when all cases pass, every case has `llm_calls == 1`, and every retained count is at most 16; otherwise write the complete report and exit `1`.

- [ ] **Step 6: Run offline harness tests**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_extraction_quality_smoke.py -q --tb=short
.venv\Scripts\python.exe -m ruff check evaluation/tools/run_extraction_quality_smoke.py tests/unit/test_extraction_quality_smoke.py
.venv\Scripts\python.exe -m black --check evaluation/tools/run_extraction_quality_smoke.py tests/unit/test_extraction_quality_smoke.py
```

Expected: all commands exit `0` without a network call.

- [ ] **Step 7: Capture the pre-change baseline reports once**

Create `var/eval/v114/baseline/`. Run the smoke once for GLM-5.3-Flash with local `hl_mem.toml`, once for Qwen3.7-Plus with the existing operator-owned cloud TOML, and once for local 27B with the existing operator-owned local TOML. Load `BAILIAN_CODING_KEY` from `.env` into the process only for the Qwen arm; do not print it. Start local llama-server hidden if port 8090 is not already healthy:

```powershell
$server = Start-Process -FilePath 'D:\qwen38-local\llama\llama-server.exe' -ArgumentList @(
  '-m','D:\qwen38-local\Qwen3.8-27B-UD-IQ4_XS.gguf',
  '--host','127.0.0.1','--port','8090','-ngl','99','-c','131072','-np','4','-t','16','--no-webui'
) -RedirectStandardOutput 'D:\qwen38-local\server_v114_baseline.out.log' `
  -RedirectStandardError 'D:\qwen38-local\server_v114_baseline.err.log' -WindowStyle Hidden -PassThru
```

Use the CLI three times, changing only config, label, and report path:

```powershell
.venv\Scripts\python.exe -m evaluation.tools.run_extraction_quality_smoke `
  --config hl_mem.toml --env-file .env --label glm-5.3-flash-baseline `
  --report var/eval/v114/baseline/glm-5.3-flash.json

.venv\Scripts\python.exe -m evaluation.tools.run_extraction_quality_smoke `
  --config evaluation/tools/configs/e2e_cloud_qwen37plus.toml --env-file .env `
  --label qwen3.7-plus-baseline --report var/eval/v114/baseline/qwen3.7-plus.json

.venv\Scripts\python.exe -m evaluation.tools.run_extraction_quality_smoke `
  --config evaluation/tools/configs/e2e_local_qwen38.toml --env-file .env `
  --label qwen3.8-27b-baseline --report var/eval/v114/baseline/qwen3.8-27b.json
```

For the Qwen command only, load and set the key without printing it:

```powershell
function Get-DotEnvValue([string]$Name) {
  $prefix = "$Name="
  $line = Get-Content -LiteralPath '.env' | Where-Object { $_.StartsWith($prefix) } | Select-Object -First 1
  if (-not $line) { throw "missing .env variable: $Name" }
  return $line.Substring($prefix.Length).Trim().Trim('"').Trim("'")
}
$env:LLM_API_KEY = Get-DotEnvValue 'BAILIAN_CODING_KEY'
```

A nonzero baseline exit is acceptable only for semantic target misses; each JSON report must still exist and contain all eight completed cases. Keep the baseline JSON files ignored under `var/`; do not stage them. If Task 1 started llama-server, stop exactly `$server.Id` after the local baseline and confirm port 8090 is closed.

- [ ] **Step 8: Commit Task 1**

```powershell
git add evaluation/tools/run_extraction_quality_smoke.py tests/eval/fixtures/extraction_quality_smoke_v1.json tests/unit/test_extraction_quality_smoke.py
git commit -m "test: add extraction quality smoke harness"
```

---

### Task 2: Compact bilingual semantic and status prompt contract

**Files:**
- Modify: `src/hl_mem/ingest/extraction/prompts.py:33-75,141-195`
- Modify: `tests/unit/test_extraction_prompt_quality.py`

**Interfaces:**
- Produces: semantically equivalent Chinese and English rules for attributed viewpoints, named-speaker binding, assistant boundaries, completed facts, and pending plans.
- Preserves: `_with_assertion_kind_gate`, field anchors, Claim budget constants, and all existing evidence/entity/secret rules.
- Changes: `PROMPT_HASH` and therefore `LLM_EXTRACTOR_VERSION` through existing fingerprinting only.

- [ ] **Step 1: Add failing exact-once prompt tests**

Add `test_prompts_define_personal_semantics_speaker_and_status_contract` to `tests/unit/test_extraction_prompt_quality.py`:

```python
def test_prompts_define_personal_semantics_speaker_and_status_contract(self) -> None:
    zh_lines = (
        "- 个人语义：显式归因给某人的观点、信念、理解、感受、行为原因和实践原则若对未来问答有用，必须保留其内容；不得只记该人物讨论过某主题。",
        "- 说话人绑定：形如「姓名：发言」时，第一人称代词和个人陈述属于冒号前姓名，不得改成泛化的“用户”；提问、未采纳引语和助手通识不得当作该人物的观点。",
        "- 助手边界：助手关于自身身份、偏好、感受、计划或对话承诺的陈述不进入长期记忆，除非内容本身是可复用交付物、配置或已采用项目决策。",
        "- fact：已完成的动作、当前状态、已生效决定及其他客观事实；后文已确认完成时，不得因前文出现“决定将”“将”或“计划”仍分类为 plan。",
        "- plan：明确仍待执行的行动，尤其是有未来日期、截止时间、周期、时间窗或条件的安排。",
    )
    en_lines = (
        "- Personal meaning: preserve the content of an explicitly attributed viewpoint, belief, interpretation, feeling, behavioral reason, or practice principle when it can help a future answer; do not retain only that the person discussed a topic.",
        "- Speaker binding: in `Name: utterance`, first-person references and personal assertions belong to the name before the colon, never a generic `user`; a question, unadopted quotation, or generic assistant explanation is not that person's viewpoint.",
        "- Assistant boundary: skip assistant self-statements about identity, preferences, feelings, plans, or conversational promises unless the content itself is a reusable deliverable, configuration, or adopted project decision.",
        "- fact: a completed action, current state, effective decision, or other objective fact; when later context confirms completion, earlier words such as `decided to`, `will`, or `plan to` must not keep it classified as a plan.",
        "- plan: an explicitly pending action, especially one with a future date, deadline, recurrence, time window, or condition.",
    )
    for line in zh_lines:
        self.assertEqual(SYSTEM_PROMPT.count(line), 1)
    for line in en_lines:
        self.assertEqual(ENGLISH_SYSTEM_PROMPT.count(line), 1)
    for benchmark_name in ("张小红", "尼采", "徐佳", "Meet World"):
        self.assertNotIn(benchmark_name, SYSTEM_PROMPT)
        self.assertNotIn(benchmark_name, ENGLISH_SYSTEM_PROMPT)
```

Update `test_prompt_defines_all_six_kinds` to assert the new exact `fact` and `plan` lines rather than the old descriptions.

- [ ] **Step 2: Run the prompt test and verify red**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_extraction_prompt_quality.py -q --tb=short
```

Expected: the new exact lines are absent and the old `fact`/`plan` assertions no longer match the intended contract.

- [ ] **Step 3: Replace overlapping prompt wording in both frozen prompts**

Insert the three semantic/speaker/assistant lines after the existing explicit-action guidance in each language. Replace the `fact` and `plan` kind descriptions with the exact lines from Step 1. Do not add proper-name benchmark examples, a second-pass instruction, or a new count target.

Keep the base prompt upgrade anchors (`kind 分类：`, `Kinds:`, field-count text) unique so `_with_assertion_kind_gate` continues to run deterministically.

- [ ] **Step 4: Run prompt, fingerprint, and extraction regressions**

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/unit/test_extraction_prompt_quality.py `
  tests/unit/test_ingest_extractor_version.py `
  tests/unit/test_phase5_extraction_contract.py `
  tests/unit/test_llm_extractor.py -q --tb=short
```

Expected: all pass; `PROMPT_HASH` is a new 12-character lowercase hex value and `LLM_EXTRACTOR_VERSION` remains `llm-v2+<hash>`.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/hl_mem/ingest/extraction/prompts.py tests/unit/test_extraction_prompt_quality.py
git commit -m "fix: preserve attributed meaning in extraction"
```

---

### Task 3: General plan-safe retention anchor

**Files:**
- Create: `tests/unit/test_plan_retention_anchor.py`
- Modify: `src/hl_mem/application/ingest.py:159-173,1019-1038`

**Interfaces:**
- Produces: `_retention_anchor(observed_at: str, recorded_from: str, *, memory_layer: str, is_plan: bool, occurred_start: str | None, occurred_end: str | None) -> str`
- Consumes: normalized `observed_at`, normalized `recorded_from`, normalized predicate/canonical attribute, memory layer, and optional occurrence bounds.
- Removes: `_episodic_retention_anchor` after all callers and tests use the general helper.

- [ ] **Step 1: Add an integration-style expiration helper and failing cases**

Create `tests/unit/test_plan_retention_anchor.py` with a helper that inserts one Event, calls `IngestService.store_extracted`, and returns `expires_at`, `valid_from`, and `observed_at` from the stored Claim:

```python
def _store_times(tmp_path: Path, claim: ExtractedClaim, *, event_time: str, recorded_from: str) -> tuple[str | None, str, str]:
    database = Database(tmp_path / f"{uuid.uuid4().hex}.db")
    connection = database.open()
    event = {
        "id": uuid.uuid4().hex,
        "tenant_id": "default",
        "actor_type": "user",
        "event_type": "message",
        "content": {"text": claim.value},
        "occurred_at": event_time,
        "recorded_at": recorded_from,
    }
    EventRepository(connection).insert_event(event)
    result = IngestService.store_extracted(
        connection,
        claim,
        event,
        recorded_from,
        FakeEmbedder(8),
        policy=TTLPolicy(temporal_ttl_days_low=3, temporal_ttl_days_normal=7, temporal_ttl_days_high=14),
    )
    row = connection.execute(
        "SELECT expires_at,valid_from,observed_at FROM claims WHERE id=?",
        (result.claim_id,),
    ).fetchone()
    database.close()
    return row["expires_at"], row["valid_from"], row["observed_at"]
```

Add these exact cases:

```python
def test_durable_historical_plan_anchors_at_recording_time(tmp_path: Path) -> None:
    times = _store_times(
        tmp_path,
        ExtractedClaim("计划", "李明仍计划配置大宗商品", canonical_attribute="plan.other", scope="temporal", importance=0.6),
        event_time="2024-01-01T00:00:00+00:00",
        recorded_from="2026-09-03T00:00:00+00:00",
    )
    assert times == ("2026-09-10T00:00:00+00:00", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00")


def test_durable_future_plan_anchors_after_latest_occurrence_boundary(tmp_path: Path) -> None:
    expires_at, _, _ = _store_times(
        tmp_path,
        ExtractedClaim(
            "计划",
            "宋宁将在 9 月 11 日提交方案",
            canonical_attribute="plan.other",
            scope="temporal",
            importance=0.6,
            occurred_start="2026-09-11T09:00:00+08:00",
            occurred_end="2026-09-11T18:00:00+08:00",
        ),
        event_time="2026-09-03T00:00:00+00:00",
        recorded_from="2026-09-03T00:00:00+00:00",
    )
    assert expires_at == "2026-09-18T10:00:00+00:00"


def test_durable_temporal_non_plan_keeps_event_anchor(tmp_path: Path) -> None:
    expires_at, _, _ = _store_times(
        tmp_path,
        ExtractedClaim("事实", "历史事件已经完成", canonical_attribute="fact.other", scope="temporal", importance=0.6),
        event_time="2024-01-01T00:00:00+00:00",
        recorded_from="2026-09-03T00:00:00+00:00",
    )
    assert expires_at == "2024-01-08T00:00:00+00:00"


def test_episodic_non_plan_keeps_recording_anchor(tmp_path: Path) -> None:
    expires_at, _, _ = _store_times(
        tmp_path,
        ExtractedClaim(
            "事实",
            "历史上完成了一次搬家",
            canonical_attribute="fact.other",
            memory_layer="episodic",
            scope="temporal",
            importance=0.3,
        ),
        event_time="2024-01-01T00:00:00+00:00",
        recorded_from="2026-09-03T00:00:00+00:00",
    )
    assert expires_at == "2026-09-06T00:00:00+00:00"


def test_permanent_plan_remains_non_expiring(tmp_path: Path) -> None:
    expires_at, _, _ = _store_times(
        tmp_path,
        ExtractedClaim("计划", "长期计划", canonical_attribute="plan.other", scope="permanent", importance=0.8),
        event_time="2024-01-01T00:00:00+00:00",
        recorded_from="2026-09-03T00:00:00+00:00",
    )
    assert expires_at is None
```

The first two tests must fail on current code; the final three characterize unchanged behavior.

- [ ] **Step 2: Run the focused test and verify the durable-plan failures**

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_plan_retention_anchor.py -q --tb=short
```

Expected: historical and future durable plan assertions fail because durable Claims currently use `observed_at`.

- [ ] **Step 3: Replace the episodic-only helper with the general selector**

In `src/hl_mem/application/ingest.py`, implement:

```python
def _retention_anchor(
    observed_at: str,
    recorded_from: str,
    *,
    memory_layer: str,
    is_plan: bool,
    occurred_start: str | None,
    occurred_end: str | None,
) -> str:
    """Select one normalized TTL anchor without changing the Claim's event time."""
    if not is_plan:
        return recorded_from if memory_layer == "episodic" else observed_at
    anchors = [recorded_from]
    for field_name, value in (("occurred_start", occurred_start), ("occurred_end", occurred_end)):
        if value:
            anchors.append(normalize_utc_iso(value, field_name))
    return max(anchors, key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))
```

At Claim draft construction, replace the layer branch with one call:

```python
memory_layer = getattr(extracted, "memory_layer", "durable")
retention_anchor = _retention_anchor(
    observed_at,
    recorded_from,
    memory_layer=memory_layer,
    is_plan=canonical_attribute.startswith("plan.") or predicate == "计划",
    occurred_start=getattr(extracted, "occurred_start", None),
    occurred_end=getattr(extracted, "occurred_end", None),
)
```

Do not change `valid_from`, `observed_at`, `compute_expiration`, policy durations, or scope normalization.

- [ ] **Step 4: Run focused and existing episodic tests**

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/unit/test_plan_retention_anchor.py `
  tests/unit/test_extraction_language_episodic_time.py -q --tb=short
```

Expected: all pass, including the existing episodic future-plan and timezone regressions.

- [ ] **Step 5: Commit Task 3**

```powershell
git add src/hl_mem/application/ingest.py tests/unit/test_plan_retention_anchor.py
git commit -m "fix: anchor ttl after plan occurrence"
```

---

### Task 4: Isolated three-arm Chinese E2E controls

**Files:**
- Modify: `evaluation/tools/run_memdaily_benchmark.py:739-780`
- Modify: `tests/unit/test_memdaily_perltqa_benchmark_scripts.py`
- Modify: `tests/eval/test_chinese_e2e.py:31-38`
- Modify: `tests/eval/test_chinese_e2e_contract.py`
- Modify: `tests/eval/README.md`

**Interfaces:**
- Produces env override: `HL_MEM_EVAL_QA_API_KEY` for the QA reader only.
- Produces env override: `HL_MEM_CHINESE_E2E_CACHE_ROOT` for an isolated extraction cache root.
- Preserves: `LLM_API_KEY`/settings fallback, default cache path, existing fixed sample, scorer, embedding, reranker, and QA behavior.

- [ ] **Step 1: Add a failing QA-key precedence test**

In `tests/unit/test_memdaily_perltqa_benchmark_scripts.py`, add:

```python
def test_reader_prefers_dedicated_qa_api_key(self) -> None:
    trajectory = _memdaily_trajectory()
    settings = replace(Settings.for_test(), llm_api_key="extractor-key")
    with (
        patch.dict(
            memdaily_runner.os.environ,
            {
                "LLM_API_KEY": "process-extractor-key",
                "HL_MEM_EVAL_QA_API_KEY": "reader-key",
                "HL_MEM_EVAL_QA_BASE_URL": "https://reader.example/v1",
                "HL_MEM_EVAL_QA_MODEL": "qwen3.7-plus",
            },
            clear=True,
        ),
        patch.object(memdaily_runner, "_qa_dashscope_chat", return_value=("An event", 11)) as qa_call,
    ):
        memdaily_runner._run_qa(
            object(), trajectory, [{"rank": 1, "text": "An event happened"}], settings
        )
    assert qa_call.call_args.args[0] == "reader-key"
```

- [ ] **Step 2: Add a failing cache-root override test**

In `tests/eval/test_chinese_e2e.py`, introduce `_configured_cache_root() -> Path`. In `tests/eval/test_chinese_e2e_contract.py`, import the entrypoint module and assert:

```python
def test_live_entrypoint_accepts_isolated_cache_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.eval import test_chinese_e2e as entrypoint

    cache_root = tmp_path / "qwen-run-1"
    monkeypatch.setenv("HL_MEM_CHINESE_E2E_CACHE_ROOT", str(cache_root))
    assert entrypoint._configured_cache_root() == cache_root
```

- [ ] **Step 3: Run the two tests and verify red**

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/unit/test_memdaily_perltqa_benchmark_scripts.py::MemDailyAggregationTests::test_reader_prefers_dedicated_qa_api_key `
  tests/eval/test_chinese_e2e_contract.py::test_live_entrypoint_accepts_isolated_cache_root -q --tb=short
```

Expected: the first uses `LLM_API_KEY`; the second fails because `_configured_cache_root` is absent.

- [ ] **Step 4: Implement the evaluation-only overrides**

In `_run_qa`, change only API-key resolution:

```python
api_key = (
    os.environ.get("HL_MEM_EVAL_QA_API_KEY")
    or os.environ.get("LLM_API_KEY")
    or os.environ.get("DASHSCOPE_API_KEY")
    or settings.llm_api_key
)
```

In `tests/eval/test_chinese_e2e.py`:

```python
def _configured_cache_root() -> Path:
    return Path(os.getenv("HL_MEM_CHINESE_E2E_CACHE_ROOT", str(DEFAULT_CACHE_ROOT)))
```

Pass `_configured_cache_root()` to `run_chinese_e2e`. Do not change cache fingerprint validation.

- [ ] **Step 5: Document the exact environment controls**

In `tests/eval/README.md`, add a short three-arm note under “中文 E2E 40 case” showing that each run must set a unique `HL_MEM_CHINESE_E2E_CACHE_ROOT`, a unique report path, `HL_MEM_CHINESE_E2E_REFRESH=1`, and a dedicated Qwen reader coordinate through:

```text
HL_MEM_EVAL_QA_API_KEY
HL_MEM_EVAL_QA_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
HL_MEM_EVAL_QA_MODEL=qwen3.7-plus
```

State that the QA key never changes the extraction Provider configured by TOML.

- [ ] **Step 6: Run evaluation-contract regressions**

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/unit/test_memdaily_perltqa_benchmark_scripts.py `
  tests/eval/test_chinese_e2e_contract.py -q --tb=short
```

Expected: all pass without network calls.

- [ ] **Step 7: Commit Task 4**

```powershell
git add evaluation/tools/run_memdaily_benchmark.py tests/unit/test_memdaily_perltqa_benchmark_scripts.py tests/eval/test_chinese_e2e.py tests/eval/test_chinese_e2e_contract.py tests/eval/README.md
git commit -m "test: isolate chinese e2e model arms"
```

---

### Task 5: Prepare the v1.1.4 candidate identity

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/hl_mem/__init__.py`
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `AGENTS.md`
- Modify: `docs/architecture.md`
- Modify: `docs/capability-matrix.md`
- Modify: `docs/configuration.md`
- Modify: `docs/CHANGELOG.md`
- Modify (generated): `docs/api-schema.json`

**Interfaces:**
- Produces: package/runtime/OpenAPI/document baseline version `1.1.4`.
- Preserves: 60 immutable SQL migrations and config schema version 1.

- [ ] **Step 1: Update the version single-source mirrors**

Change `pyproject.toml` and `src/hl_mem/__init__.py` to `1.1.4`. Update both README badges and baseline paragraphs, `AGENTS.md`, `docs/architecture.md`, `docs/capability-matrix.md`, and the first `docs/configuration.md` baseline sentence from `1.1.3` to `1.1.4`. Leave historical “从 v1.1.3 起” statements unchanged.

Add the latest changelog entry:

```markdown
## v1.1.4（2026-09-03）

- 提取 Prompt 现在保留明确归因的个人观点、理解、感受、行为原因和实践原则，并把“姓名：发言”中的第一人称绑定到姓名；问题、未采纳引语、助手通识及助手自我陈述继续排除。
- 已完成动作、当前状态和已生效决定归为 fact；只有明确仍待执行的行动归为 plan。每个 plan 的 TTL 统一从记录时间与最新发生边界中的较晚者起算，durable 与 episodic 不再分叉。
- 继续使用普通 12 条、硬上限 16 条和单次确定性截断；未增加二次模型、动态路由、Migration 或历史 Claim 改写。
```

- [ ] **Step 2: Refresh lock and OpenAPI snapshots**

```powershell
uv lock --offline
.venv\Scripts\python.exe scripts/check_openapi_snapshot.py --update
```

Expected: the editable root package in `uv.lock` reports `1.1.4`; OpenAPI reports `1.1.4`.

- [ ] **Step 3: Verify candidate identity**

```powershell
.venv\Scripts\python.exe scripts/check_docs_consistency.py
.venv\Scripts\python.exe scripts/check_openapi_snapshot.py
.venv\Scripts\python.exe -m pytest tests/unit/test_report_version.py tests/integration/test_e2e.py -q --tb=short
```

Expected: all exit `0`, document consistency reports v1.1.4 and 60 migrations.

- [ ] **Step 4: Commit Task 5**

```powershell
git add pyproject.toml uv.lock src/hl_mem/__init__.py README.md README_EN.md AGENTS.md docs/architecture.md docs/capability-matrix.md docs/configuration.md docs/CHANGELOG.md docs/api-schema.json
git commit -m "chore: prepare hl-mem 1.1.4"
```

---

### Task 6: Three-model candidate smoke and fresh 40-case comparison

**Files:**
- Create: `docs/research/2026-09-03-extraction-quality-plan-ttl-evaluation.md`
- Read only: `var/eval/v114/baseline/*.json`
- Generate but do not commit: `var/eval/v114/candidate/**`
- Read only, never stage: `hl_mem.toml`, `.env`, `evaluation/tools/configs/e2e_cloud_qwen37plus.toml`, `evaluation/tools/configs/e2e_local_qwen38.toml`

**Interfaces:**
- Consumes: Task 1 smoke reports, Task 4 cache/key overrides, fixed 40-case manifest, unchanged embedding/reranker/Qwen reader.
- Produces: official and layer-attributed metrics for Qwen3.7-Plus, GLM-5.3-Flash, and local Qwen3.8-27B.
- Produces: explicit provider recommendation; does not mutate runtime Provider configuration.

- [ ] **Step 1: Run the candidate fixed smoke exactly once per model**

Create unique candidate report paths under `var/eval/v114/candidate/smoke/`. Use the same commands and local server coordinate as Task 1, changing labels to `*-candidate`. Do not tune or repeat a failed case. Stop before the 40-case spend if any arm violates the hard safety properties: more than 16 retained Claims, more than one LLM call for an ordinary case, schema retry storm, or a negative-memory violation.

Compare each case's candidate `input_tokens` with the matching baseline case. The reported increase must be at most 250 tokens per call for every provider. A Provider that reports no input/output split must be marked `not reported`, not guessed from total tokens.

- [ ] **Step 2: Prepare isolated first-run directories**

Use these exact roots and reports:

```text
var/eval/v114/candidate/full40/qwen37/run1/cache
var/eval/v114/candidate/full40/qwen37/run1/report.json
var/eval/v114/candidate/full40/glm53/run1/cache
var/eval/v114/candidate/full40/glm53/run1/report.json
var/eval/v114/candidate/full40/qwen38-27b/run1/cache
var/eval/v114/candidate/full40/qwen38-27b/run1/report.json
```

For every run set:

```powershell
$env:HL_MEM_CHINESE_E2E_REFRESH = '1'
$env:HL_MEM_EVAL_QA_BASE_URL = 'https://coding.dashscope.aliyuncs.com/v1'
$env:HL_MEM_EVAL_QA_MODEL = 'qwen3.7-plus'
$env:HL_MEM_EVAL_QA_API_KEY = $bailianCodingKey
```

Load `$bailianCodingKey` from the `.env` variable `BAILIAN_CODING_KEY` with the Task 1 helper and never print it. For Qwen extraction only, also set `$env:LLM_API_KEY = $bailianCodingKey`. Before GLM and local runs, remove that process override with `Remove-Item Env:LLM_API_KEY -ErrorAction SilentlyContinue`; GLM then reads the original Zhipu `LLM_API_KEY` from `.env`, while local llama-server accepts that non-empty configured value without sending it off loopback.

- [ ] **Step 3: Run all three fresh 40-case arms**

For each arm set its config, cache, and report, then invoke:

```powershell
.venv\Scripts\python.exe -m pytest tests/eval/test_chinese_e2e.py -m real_api -s -q
```

Use:

```text
Qwen:  evaluation/tools/configs/e2e_cloud_qwen37plus.toml
GLM:   hl_mem.toml
27B:   evaluation/tools/configs/e2e_local_qwen38.toml
```

Set `HL_MEM_CHINESE_E2E_CONFIG`, `HL_MEM_CHINESE_E2E_CACHE_ROOT`, and `HL_MEM_CHINESE_E2E_REPORT` to the matching values. The existing `memdaily_noisy.recall_at_5` gate may make pytest exit nonzero; accept the artifact only when `status == "completed"`, all 40 cases executed, and every nonzero gate failure is explicitly classified in the research report.

- [ ] **Step 4: Attribute every incorrect case before selecting the repeat arm**

For each official QA miss, assign exactly one primary layer using these rules:

```text
extraction: the gold Event is uncovered or no stored Claim linked to it contains the supported target meaning
TTL: a target Claim exists but is outside visibility because expires_at precedes the question as_of
retrieval: a target Claim is visible but no supporting Claim appears in top 5
QA/scorer: supporting evidence appears in top 5 but the answer or deterministic rubric fails
```

Choose the repeat challenger among GLM and local 27B by: fewer extraction-layer misses, then higher extraction coverage, then higher official QA accuracy, then fewer extraction tokens. Qwen is always the second repeated arm because it is the quality baseline.

- [ ] **Step 5: Repeat Qwen and the selected challenger once**

Use new `run2/cache` and `run2/report.json` paths; never reuse run1 caches. Keep the same commit, source hashes, Qwen reader, embedding, reranker, and settings. Do not repeat the third arm or rerun individual cases.

An extractor is release-eligible only when its worse run satisfies all of:

```text
QA accuracy >= 36/40
extraction coverage >= 41/42
negative violations == 0
no Claim-count retry storm
no repeated critical omission of a supported viewpoint, reason, relationship, or structured field
```

A challenger may replace Qwen only if both fresh runs are at most one QA case worse than Qwen's matching runs and the extraction/safety criteria are no worse. Otherwise recommend Qwen3.7-Plus and leave Provider settings unchanged.

- [ ] **Step 6: Write the evidence report**

Create `docs/research/2026-09-03-extraction-quality-plan-ttl-evaluation.md` with:

```text
evaluated code commit and prompt hash
source/config invariants and fresh-cache paths
fixed smoke baseline/candidate token and coverage table
first-run three-arm official metrics
Qwen/challenger second-run official metrics
case-by-case failure attribution table
TTL assertions (dead on arrival and expires before occurrence counts)
release-gate decision
provider recommendation and cost/latency caveat
known out-of-scope retrieval/scorer failures
```

For the production read-only TTL assertion, run this query through a SQLite connection opened with URI `mode=ro` and record all four counts:

```sql
SELECT
  COUNT(*) AS all_plans,
  SUM(CASE WHEN expires_at IS NOT NULL AND expires_at <= recorded_from THEN 1 ELSE 0 END) AS dead_on_arrival,
  SUM(CASE WHEN expires_at IS NOT NULL AND occurred_start IS NOT NULL AND expires_at < occurred_start THEN 1 ELSE 0 END) AS expires_before_start,
  SUM(CASE WHEN expires_at IS NOT NULL AND occurred_end IS NOT NULL AND expires_at < occurred_end THEN 1 ELSE 0 END) AS expires_before_end
FROM claims
WHERE canonical_attribute LIKE 'plan.%' OR predicate = '计划';
```

Do not include secrets, raw request headers, `.env` values, private source text, or raw Provider envelopes. Model names, aggregate token counts, synthetic smoke Claims, benchmark case IDs, and failure-layer explanations are allowed.

- [ ] **Step 7: Stop the local model and commit only the report**

Stop the exact llama-server process started for this evaluation and confirm port 8090 is no longer listening. Do not recursively kill unrelated processes.

```powershell
git add docs/research/2026-09-03-extraction-quality-plan-ttl-evaluation.md
git commit -m "test: record 1.1.4 extraction model comparison"
```

---

### Task 7: Full verification and local release handoff

**Files:**
- No tracked file changes.
- Generate but do not commit: `dist/*`

**Interfaces:**
- Consumes: the exact committed Task 6 tree.
- Produces: verified local v1.1.4 artifacts and a clean handoff; no push, tag, deploy, or publication.

- [ ] **Step 1: Run focused behavior regressions**

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/unit/test_extraction_quality_smoke.py `
  tests/unit/test_extraction_prompt_quality.py `
  tests/unit/test_plan_retention_anchor.py `
  tests/unit/test_extraction_language_episodic_time.py `
  tests/unit/test_memdaily_perltqa_benchmark_scripts.py `
  tests/eval/test_chinese_e2e_contract.py -q --tb=short
```

Expected: all pass with no network call.

- [ ] **Step 2: Run the complete non-paid suite**

```powershell
.venv\Scripts\python.exe -W error::ResourceWarning -m pytest tests/ -q --tb=short
```

Expected: zero failures; the repository conftest skips `real_api` tests unless `-m real_api` is explicitly selected. Do not report success from an older run.

- [ ] **Step 3: Run static and document checks**

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m black --check .
.venv\Scripts\python.exe -m isort --check-only .
.venv\Scripts\python.exe -m mypy src/hl_mem/ --ignore-missing-imports
.venv\Scripts\python.exe scripts/check_docs_consistency.py
.venv\Scripts\python.exe scripts/check_openapi_snapshot.py
git diff --check
```

Expected: every command exits `0`.

- [ ] **Step 4: Build and inspect local artifacts**

```powershell
.venv\Scripts\python.exe -m build
$wheel = (Get-ChildItem 'dist\hl_mem-1.1.4-*.whl' -File -ErrorAction Stop | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
$sdist = (Get-ChildItem 'dist\hl_mem-1.1.4.tar.gz' -File -ErrorAction Stop | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
.venv\Scripts\python.exe scripts/check_wheel_contents.py $wheel
$forbidden = tar -tf $sdist | Select-String '(^|/)(\.env|hl_mem\.toml|var/|evaluation/|docs/HANDOFF\.md)'
if ($forbidden) { throw "forbidden sdist members: $($forbidden -join ', ')" }
```

Expected: one wheel and one sdist named for `1.1.4`; neither artifact includes `.env`, `hl_mem.toml`, `var/`, evaluation caches, or `docs/HANDOFF.md`.

- [ ] **Step 5: Verify scope and hand off**

```powershell
git status --short --branch
git log --oneline -8
```

Confirm all planned commits are present, tracked files are clean, generated evaluation data stays under ignored `var/`, and all original untracked user files are unchanged. Report the exact test count, static-check results, model comparison, selected recommendation, artifact names, and the fact that nothing was pushed, tagged, deployed, or published.
