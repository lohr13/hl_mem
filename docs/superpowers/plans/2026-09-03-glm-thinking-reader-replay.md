# GLM Thinking Reader Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay the exact three-arm Chinese E2E reader inputs with GLM-5.3-Flash thinking enabled and produce an auditable paired comparison against the existing Qwen3.7-Plus reader.

**Architecture:** Add one evaluation-only injection seam so the existing `_run_qa` prompt and answer parser can use a supplied transport. A dedicated replay CLI validates the three completed reports and hash-locked sample, reconstructs omitted MemDaily choices, reuses every recorded reader-evidence item in order, calls a provider-specific GLM thinking transport, checkpoints after each case, and reuses the frozen E2E scorers. No extraction, embedding, reranking, recall, database, or runtime provider code runs.

**Tech Stack:** Python 3.12+, dataclasses, argparse, httpx, existing `qa_call_with_retry`, existing Chinese E2E manifest/loaders/scorers, pytest/unittest, JSON.

## Global Constraints

- Work only in the existing `extraction-quality-plan-ttl` worktree; do not touch the user's original untracked files.
- This is evaluation-only. Do not change production runtime routing, extractor configuration, release gates, tags, deployment, or publication.
- Source arms are exactly Qwen `run1`, GLM `run1`, and local Qwen `recovery1` from the paths in the approved design.
- Validate schema version 3, `status=completed`, 40 unique expected case IDs, zero source case errors, and original `qa.model=qwen3.7-plus` before a paid call.
- Load `tests/eval/fixtures/chinese_e2e_sample.json` through existing loaders so source hashes and MemDaily choices are verified.
- Reuse the complete recorded `retrieved` sequence, in order, for each case. Do not truncate to Top-5; the original reader received between one and ten items.
- GLM request identity is model `glm-5.3-flash`, `thinking={"type":"enabled"}`, temperature `0.1`, and `max_tokens=4096`.
- The canary is the Qwen-extractor copy of `perltqa:23d905b73c57:dialogues:836f6182a0a9`; it counts as one of exactly 120 logical QA calls.
- Thinking is verified only by non-empty response `reasoning_content` or a positive reported reasoning-token count. Never silently fall back to non-thinking mode.
- Do not persist reasoning content, credentials, request headers, raw provider envelopes, or private source conversation text.
- Use at most three bounded transport attempts per logical call. Do not add semantic retries, answer repair, or an LLM judge.
- Raw replay artifacts stay ignored under `var/eval/v114/cross_reader/glm53-thinking/`.
- No new dependency is allowed.

---

### Task 1: Add an injectable QA transport seam

**Files:**
- Modify: `evaluation/tools/run_memdaily_benchmark.py:725-805`
- Modify: `tests/unit/test_memdaily_perltqa_benchmark_scripts.py`

**Interfaces:**
- Consumes: the existing five-argument `_qa_dashscope_chat` callable contract.
- Produces: `QAChat = Callable[[str, str, str, str, str], tuple[str, int]]` and optional `_run_qa(..., qa_chat: QAChat | None = None)`.
- Preserves: environment credential/model/base-URL resolution, hard-abstention behavior, prompt text, choice formatting, answer parsing, and all existing callers.

- [ ] **Step 1: Add failing tests for explicit and default transports**

Add these tests to `MemDailyAggregationTests`:

Extend the existing import to `from unittest.mock import Mock, patch`.

```python
def test_reader_accepts_an_explicit_transport_without_mutating_default(self) -> None:
    trajectory = _memdaily_trajectory()
    explicit = Mock(return_value=("An event", 19))
    with (
        patch.dict(
            memdaily_runner.os.environ,
            {
                "HL_MEM_EVAL_QA_API_KEY": "reader-key",
                "HL_MEM_EVAL_QA_BASE_URL": "https://reader.example/v1",
                "HL_MEM_EVAL_QA_MODEL": "reader-model",
            },
            clear=True,
        ),
        patch.object(memdaily_runner, "_qa_dashscope_chat") as default_chat,
    ):
        result = memdaily_runner._run_qa(
            object(),
            trajectory,
            [{"rank": 1, "text": "An event happened"}],
            Settings.for_test(),
            qa_chat=explicit,
        )

    explicit.assert_called_once()
    default_chat.assert_not_called()
    self.assertEqual(explicit.call_args.args[:3], ("reader-key", "https://reader.example/v1", "reader-model"))
    self.assertEqual(result["predicted_answer"], "An event")
    self.assertEqual(result["usage"]["total_tokens"], 19)

def test_reader_resolves_the_module_default_transport_at_call_time(self) -> None:
    trajectory = _memdaily_trajectory()
    with (
        patch.dict(memdaily_runner.os.environ, {"LLM_API_KEY": "reader-key"}, clear=True),
        patch.object(memdaily_runner, "_qa_dashscope_chat", return_value=("An event", 23)) as default_chat,
    ):
        result = memdaily_runner._run_qa(
            object(), trajectory, [{"rank": 1, "text": "An event happened"}], Settings.for_test()
        )

    default_chat.assert_called_once()
    self.assertEqual(result["usage"]["total_tokens"], 23)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/unit/test_memdaily_perltqa_benchmark_scripts.py::MemDailyAggregationTests::test_reader_accepts_an_explicit_transport_without_mutating_default `
  tests/unit/test_memdaily_perltqa_benchmark_scripts.py::MemDailyAggregationTests::test_reader_resolves_the_module_default_transport_at_call_time `
  -q --tb=short
```

Expected: the first test fails because `_run_qa` does not accept `qa_chat`.

- [ ] **Step 3: Implement the minimal late-bound transport seam**

Import `Callable` from `collections.abc`, declare the alias next to `QA_FALLBACK_MODEL`, add the keyword-only argument, and replace the direct call:

```python
QAChat = Callable[[str, str, str, str, str], tuple[str, int]]


def _run_qa(
    connection: Any,
    traj: MemDailyTrajectory,
    retrieved: Sequence[Mapping[str, Any]],
    settings: Settings,
    *,
    answerability: Answerability = "supported",
    qa_chat: QAChat | None = None,
) -> dict[str, Any]:
    ...
    selected_chat = qa_chat or _qa_dashscope_chat
    answer_text, total_tokens = selected_chat(
        api_key, base_url, qa_model, system_prompt, user_prompt
    )
```

Do not capture `_qa_dashscope_chat` as a default argument; `tests.eval.chinese_e2e` intentionally replaces it at module scope for the Qwen thinking reader.

- [ ] **Step 4: Run focused and nearby regressions**

Run:

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/unit/test_memdaily_perltqa_benchmark_scripts.py `
  tests/eval/test_chinese_e2e_contract.py -q --tb=short
```

Expected: all pass without a network call.

- [ ] **Step 5: Commit Task 1**

```powershell
git add evaluation/tools/run_memdaily_benchmark.py tests/unit/test_memdaily_perltqa_benchmark_scripts.py
git commit -m "test: allow isolated QA reader transports"
```

---

### Task 2: Validate replay sources and reconstruct exact reader cases

**Files:**
- Create: `evaluation/tools/run_chinese_reader_replay.py`
- Create: `tests/unit/test_chinese_reader_replay.py`

**Interfaces:**
- Consumes: `load_sample_manifest`, `load_sampled_inputs`, `build_perltqa_ingest_trajectory`, and `build_perltqa_question_trajectory` from `tests.eval.chinese_e2e`.
- Produces: `ReplayInputError`, `SourceArm`, `ReplayCase`, `sha256_file`, `build_trajectory_index`, `validate_source_report`, and `load_replay_cases`.
- `load_replay_cases(manifest_path: Path, source_paths: Mapping[str, Path]) -> dict[str, tuple[ReplayCase, ...]]` returns each arm in source-report order with the complete recorded evidence sequence.

- [ ] **Step 1: Write failing validation and reconstruction tests**

Create the test module with pure fixtures and the following assertions:

```python
def test_validate_source_report_preserves_all_ten_reader_items() -> None:
    case = make_case("case-1", retrieved=[{"rank": rank, "text": str(rank)} for rank in range(1, 11)])
    report = make_report([case])
    validated = replay.validate_source_report(report, expected_case_ids={"case-1"})
    assert [item["rank"] for item in validated[0]["retrieved"]] == list(range(1, 11))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report.update(schema_version=2), "schema_version"),
        (lambda report: report.update(status="partial"), "status"),
        (lambda report: report["cases"][0].update(error="boom"), "case errors"),
        (lambda report: report["cases"][0]["qa"].update(model="glm-5.3-flash"), "qwen3.7-plus"),
    ],
)
def test_validate_source_report_rejects_nonofficial_inputs(mutation, message: str) -> None:
    report = make_report([make_case("case-1")])
    mutation(report)
    with pytest.raises(replay.ReplayInputError, match=message):
        replay.validate_source_report(report, expected_case_ids={"case-1"})


def test_load_replay_cases_joins_memdaily_choices_without_copying_messages(monkeypatch, tmp_path: Path) -> None:
    trajectory = make_trajectory(case_id="memdaily:simple:events:1", choices={"A": "left", "B": "right"})
    monkeypatch.setattr(replay, "load_sample_manifest", lambda path: make_manifest())
    monkeypatch.setattr(replay, "load_sampled_inputs", lambda manifest: make_inputs(trajectory))
    source = write_report(tmp_path, [make_case(trajectory.case_id)])
    loaded = replay.load_replay_cases(tmp_path / "manifest.json", {"qwen37": source})

    assert loaded["qwen37"][0].trajectory.choices == {"A": "left", "B": "right"}
    assert loaded["qwen37"][0].retrieved == tuple(make_case(trajectory.case_id)["retrieved"])
    assert not hasattr(loaded["qwen37"][0], "messages")
```

The local fixture constructors must create only synthetic strings and must not read private sources.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_chinese_reader_replay.py -q --tb=short
```

Expected: collection fails because `evaluation.tools.run_chinese_reader_replay` does not exist.

- [ ] **Step 3: Implement source types and strict validation**

Start the module with these stable shapes:

```python
@dataclass(frozen=True)
class SourceArm:
    label: str
    report_path: Path
    report_sha256: str
    extractor_model: str


@dataclass(frozen=True)
class ReplayCase:
    arm: SourceArm
    case_id: str
    dataset: str
    slice_name: str
    trajectory: MemDailyTrajectory
    answer_anchors: tuple[str, ...]
    accepted_rubrics: AcceptedRubrics
    answer_entity_gold: AnswerEntityGold
    retrieved: tuple[dict[str, Any], ...]
    source_case: dict[str, Any]


class ReplayInputError(ValueError):
    pass
```

`validate_source_report` must reject non-mappings, schema/status mismatch, duplicate/missing/unexpected case IDs, non-empty `error`, missing QA, any QA model other than `qwen3.7-plus`, an answerability value outside `supported`/`low_confidence`, and malformed `retrieved` entries. This frozen sample contains no hard-abstention cases, so all 120 logical cases must call the reader. Copy every retrieved mapping with `dict(item)` and do not slice the list.

`build_trajectory_index` must call the existing hash-validating manifest loaders. For PerLTQA, build the ingest trajectory once per bundle and then one question trajectory per question; retain its typed anchors, accepted rubrics, and `AnswerEntityGold`. For MemDaily, index the selected trajectories directly and join `AnswerEntityGold` from the manifest while using empty anchors/rubrics. Reject duplicate IDs and assert exactly 40 IDs.

`load_replay_cases` must validate each source report against the same trajectory ID set, verify that report question/gold answer match the reconstructed trajectory, calculate the source hash, and return immutable tuples. Do not attach trajectory messages to `ReplayCase`; store a copy made with `dataclasses.replace(trajectory, messages=())` so source conversations cannot reach serialization or the reader.

- [ ] **Step 4: Run tests and static checks for Task 2**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_chinese_reader_replay.py -q --tb=short
.venv\Scripts\python.exe -m ruff check evaluation/tools/run_chinese_reader_replay.py tests/unit/test_chinese_reader_replay.py
.venv\Scripts\python.exe -m mypy evaluation/tools/run_chinese_reader_replay.py --ignore-missing-imports
```

Expected: all commands exit `0`.

- [ ] **Step 5: Commit Task 2**

```powershell
git add evaluation/tools/run_chinese_reader_replay.py tests/unit/test_chinese_reader_replay.py
git commit -m "test: validate Chinese reader replay inputs"
```

---

### Task 3: Implement the provider-specific GLM thinking transport

**Files:**
- Modify: `evaluation/tools/run_chinese_reader_replay.py`
- Modify: `tests/unit/test_chinese_reader_replay.py`

**Interfaces:**
- Consumes: `qa_call_with_retry` from `evaluation.tools.longmemeval.qa_client` and the `QAChat` contract from Task 1.
- Produces: `ParsedGLMResponse`, `ReaderCallMetadata`, `build_glm_thinking_payload`, `parse_glm_thinking_response`, and callable `GLMThinkingTransport`.
- `GLMThinkingTransport.last_call` exposes metadata only; it never retains `reasoning_content` or the provider envelope.

- [ ] **Step 1: Add failing payload, verification, redaction, and retry tests**

Add tests using `httpx.MockTransport`:

```python
def test_glm_payload_uses_provider_thinking_object() -> None:
    payload = replay.build_glm_thinking_payload("glm-5.3-flash", "system", "user")
    assert payload == {
        "model": "glm-5.3-flash",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
        "thinking": {"type": "enabled"},
    }
    assert "enable_thinking" not in payload


def test_transport_verifies_thinking_without_retaining_reasoning_content() -> None:
    client, requests = mock_client(
        [{
            "choices": [{"message": {"reasoning_content": "private chain", "content": "answer"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 7,
                "total_tokens": 17,
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }]
    )
    transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=lambda _: None)
    answer, total = transport("secret", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3-flash", "s", "u")

    assert (answer, total) == ("answer", 17)
    assert transport.last_call is not None
    assert transport.last_call.thinking_verified is True
    assert transport.last_call.reasoning_tokens == 5
    assert "private chain" not in json.dumps(dataclasses.asdict(transport.last_call))
    assert requests[0].headers["authorization"] == "Bearer secret"


def test_transport_retries_transient_failure_at_most_three_times() -> None:
    client, requests = mock_client([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"), success_envelope()])
    transport = replay.GLMThinkingTransport(client, max_attempts=3, sleep=lambda _: None)
    assert transport("secret", BASE_URL, MODEL, "s", "u")[0] == "answer"
    assert len(requests) == 3
    assert transport.last_call is not None
    assert transport.last_call.attempts == 3


def test_response_without_thinking_evidence_is_unverified() -> None:
    parsed = replay.parse_glm_thinking_response({
        "choices": [{"message": {"content": "answer"}}],
        "usage": {"total_tokens": 2},
    })
    assert parsed.thinking_verified is False
```

- [ ] **Step 2: Run the transport tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_chinese_reader_replay.py -q --tb=short
```

Expected: the new transport symbols are missing.

- [ ] **Step 3: Implement payload construction and response minimization**

Use these response and metadata fields:

```python
@dataclass(frozen=True)
class ParsedGLMResponse:
    final_answer: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    thinking_verified: bool


@dataclass(frozen=True)
class ReaderCallMetadata:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    latency_seconds: float
    attempts: int
    thinking_verified: bool
```

`parse_glm_thinking_response` must read final `message.content`, detect but immediately discard `message.reasoning_content`, normalize both OpenAI token names and `input_tokens`/`output_tokens`, and read reasoning tokens from either `completion_tokens_details.reasoning_tokens` or `output_tokens_details.reasoning_tokens`. Reject missing/empty final content.

`GLMThinkingTransport.__call__` must build the fixed payload, POST to `<base_url>/chat/completions`, use `qa_call_with_retry(..., max_attempts=3)`, count actual attempts, record elapsed time, assign only `ReaderCallMetadata` to `last_call`, and return `(final_answer, total_tokens)`. Do not place response bodies or headers in exceptions written to replay artifacts.

- [ ] **Step 4: Run focused tests and checks**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_chinese_reader_replay.py -q --tb=short
.venv\Scripts\python.exe -m ruff check evaluation/tools/run_chinese_reader_replay.py tests/unit/test_chinese_reader_replay.py
.venv\Scripts\python.exe -m black --check evaluation/tools/run_chinese_reader_replay.py tests/unit/test_chinese_reader_replay.py
.venv\Scripts\python.exe -m mypy evaluation/tools/run_chinese_reader_replay.py --ignore-missing-imports
```

Expected: all commands exit `0`.

- [ ] **Step 5: Commit Task 3**

```powershell
git add evaluation/tools/run_chinese_reader_replay.py tests/unit/test_chinese_reader_replay.py
git commit -m "feat: add GLM thinking reader transport"
```

---

### Task 4: Add canary, checkpointed replay, deterministic rescoring, and comparison output

**Files:**
- Modify: `evaluation/tools/run_chinese_reader_replay.py`
- Modify: `tests/unit/test_chinese_reader_replay.py`

**Interfaces:**
- Consumes: Tasks 1–3, `score_answer`, `score_answer_entity_packet`, and `aggregate_results` from `tests.eval.chinese_e2e`, plus `read_secret_values` from `hl_mem.config.secrets`.
- Produces: `score_replayed_case`, `classify_flip`, `summarize_arm`, `run_replay`, `_write_json_atomic`, `_parser`, and `main`.
- CLI supports `--manifest`, three fixed source-report overrides, `--env-file`, `--base-url`, `--output-root`, `--canary-only`, and `--resume`.

- [ ] **Step 1: Add failing scorer, flip, canary, resume, and redaction tests**

Add these behavioral tests with a synthetic three-arm, 40-case fixture and a fake callable transport:

```python
@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (True, True, "unchanged_correct"),
        (False, False, "unchanged_wrong"),
        (False, True, "wrong_to_right"),
        (True, False, "right_to_wrong"),
    ],
)
def test_classify_flip(before: bool, after: bool, expected: str) -> None:
    assert replay.classify_flip(before, after) == expected


def test_run_replay_calls_canary_first_and_counts_it_once(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)
    summary = replay.run_replay(sources, transport, output_root=tmp_path, canary_only=False, resume=False)

    assert summary["execution_order"][0] == f"qwen37:{replay.CANARY_CASE_ID}"
    assert len(transport.calls) == 120
    assert sum(item.endswith(replay.CANARY_CASE_ID) for item in summary["execution_order"]) == 3
    assert summary["status"] == "completed"
    assert summary["logical_calls"] == 120


def test_canary_only_checkpoints_one_call_and_resume_does_not_repeat_it(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    first = FakeThinkingTransport(answer="synthetic answer", verified=True)
    partial = replay.run_replay(sources, first, output_root=tmp_path, canary_only=True, resume=False)
    assert partial["status"] == "canary_completed"
    assert len(first.calls) == 1

    second = FakeThinkingTransport(answer="synthetic answer", verified=True)
    complete = replay.run_replay(sources, second, output_root=tmp_path, canary_only=False, resume=True)
    assert len(second.calls) == 119
    assert complete["logical_calls"] == 120


def test_unverified_canary_aborts_before_second_call(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=False)
    summary = replay.run_replay(sources, transport, output_root=tmp_path, canary_only=False, resume=False)
    assert summary["status"] == "mode_unverified"
    assert len(transport.calls) == 1


def test_checkpoint_contains_no_secret_envelope_or_reasoning(tmp_path: Path) -> None:
    sources = synthetic_three_arm_cases(include_canary=True)
    transport = FakeThinkingTransport(answer="synthetic answer", verified=True)
    replay.run_replay(sources, transport, output_root=tmp_path, canary_only=True, resume=False)
    raw = (tmp_path / "qwen37.json").read_text(encoding="utf-8")
    assert "private chain" not in raw
    assert "Bearer " not in raw
    assert '"reasoning_content"' not in raw
```

The canary appears once per arm in the 120-case set, so `count(CANARY_CASE_ID) == 3`; only the Qwen-arm copy must be the first physical call.

- [ ] **Step 2: Run orchestration tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_chinese_reader_replay.py -q --tb=short
```

Expected: new orchestration functions are missing.

- [ ] **Step 3: Implement exact prompt reuse and deterministic rescoring**

For each replay case:

1. Set evaluation-only reader coordinates in a scoped environment: `HL_MEM_EVAL_QA_API_KEY`, `HL_MEM_EVAL_QA_BASE_URL`, and `HL_MEM_EVAL_QA_MODEL=glm-5.3-flash`.
2. Call the existing `_run_qa` with the reconstructed trajectory, complete recorded evidence sequence, source `qa.answerability`, and `qa_chat=transport`.
3. For PerLTQA, update the QA result with `score_answer(predicted, replay_case.answer_anchors, replay_case.accepted_rubrics)`.
4. Copy the source case, replace only `qa`, recompute `answer_entity` with `replay_case.answer_entity_gold` and the existing frozen scorer, and attach the metadata from `transport.last_call` under `reader_call`.
5. Preserve original retrieval and extraction fields unchanged so `aggregate_results` can calculate the same non-reader metrics.

Use this correctness helper for paired flips:

```python
def qa_correct(qa: Mapping[str, Any]) -> bool:
    return bool(qa.get("answer_correct", qa.get("exact_match", False)))
```

- [ ] **Step 4: Implement atomic checkpoints, canary state, resume identity, and summary**

Write `qwen37.json`, `glm53.json`, `qwen38-27b.json`, and `summary.json` atomically after every completed case. Each arm checkpoint must contain source path/hash, reader identity, status, completed case IDs, metrics over completed cases, and replay cases. Resume only when source hash, model, thinking object, prompt/scorer versions, and case set match; otherwise raise `ReplayInputError` before a call.

Order calls as: Qwen canary, remaining Qwen cases in source order, all GLM cases in source order, then all local cases in source order. A successful `--canary-only` exits after persisting the Qwen canary with status `canary_completed`. An unverified canary writes `mode_unverified` and exits nonzero.

`summary.json` must contain per-arm original/replay QA accuracy and F1, four flip buckets with case IDs, token/latency/attempt totals, failed case IDs, paired deltas, the physical `execution_order` as `arm:case_id`, and both original/replay rankings. Do not recompute or apply the v1.1.4 release gate.
It must also record UTC start/completion timestamps, the three source hashes, the frozen prompt/scorer versions, and the original Qwen reader identity (`enable_thinking=true`, thinking budget 2,048, answer budget 512).

Use `read_secret_values(env_path, {"LLM_API_KEY"}, os.environ)` and fail before a request if the value is absent or a placeholder. Never serialize this value.

- [ ] **Step 5: Implement the CLI and run all replay unit tests**

Use these defaults:

```python
DEFAULT_MANIFEST = Path("tests/eval/fixtures/chinese_e2e_sample.json")
DEFAULT_SOURCES = {
    "qwen37": Path("var/eval/v114/candidate/full40/qwen37/run1/report.json"),
    "glm53": Path("var/eval/v114/candidate/full40/glm53/run1/report.json"),
    "qwen38-27b": Path("var/eval/v114/candidate/full40/qwen38-27b/recovery1/report.json"),
}
DEFAULT_OUTPUT_ROOT = Path("var/eval/v114/cross_reader/glm53-thinking")
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
MODEL = "glm-5.3-flash"
CANARY_CASE_ID = "perltqa:23d905b73c57:dialogues:836f6182a0a9"
```

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_chinese_reader_replay.py -q --tb=short
.venv\Scripts\python.exe -m ruff check evaluation/tools/run_chinese_reader_replay.py tests/unit/test_chinese_reader_replay.py
.venv\Scripts\python.exe -m black --check evaluation/tools/run_chinese_reader_replay.py tests/unit/test_chinese_reader_replay.py
.venv\Scripts\python.exe -m mypy evaluation/tools/run_chinese_reader_replay.py --ignore-missing-imports
```

Expected: all commands exit `0`.

- [ ] **Step 6: Commit Task 4**

```powershell
git add evaluation/tools/run_chinese_reader_replay.py tests/unit/test_chinese_reader_replay.py
git commit -m "feat: replay Chinese QA readers from fixed evidence"
```

---

### Task 5: Run the GLM thinking canary and complete the 120-case replay

**Files:**
- Generate, do not commit: `var/eval/v114/cross_reader/glm53-thinking/qwen37.json`
- Generate, do not commit: `var/eval/v114/cross_reader/glm53-thinking/glm53.json`
- Generate, do not commit: `var/eval/v114/cross_reader/glm53-thinking/qwen38-27b.json`
- Generate, do not commit: `var/eval/v114/cross_reader/glm53-thinking/summary.json`
- Modify: `docs/research/2026-09-03-extraction-quality-plan-ttl-evaluation.md`

**Interfaces:**
- Consumes: the exact committed Task 4 tree and the ignored secret file `D:\workspace\hl_agent\hl_mem\var\eval\softsplit_ab_20260827\.env_flash`.
- Produces: verified canary evidence, 120 completed logical QA results, paired reader deltas, and an updated committed research report.

- [ ] **Step 1: Re-run the focused tests immediately before paid calls**

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/unit/test_chinese_reader_replay.py `
  tests/unit/test_memdaily_perltqa_benchmark_scripts.py `
  tests/eval/test_chinese_e2e_contract.py -q --tb=short
```

Expected: all pass with no network call.

- [ ] **Step 2: Run exactly one canary call**

```powershell
.venv\Scripts\python.exe -m evaluation.tools.run_chinese_reader_replay `
  --env-file D:\workspace\hl_agent\hl_mem\var\eval\softsplit_ab_20260827\.env_flash `
  --canary-only
```

Expected: exit `0`; `summary.json` has `status="canary_completed"`, `logical_calls=1`, Qwen-arm canary ID exactly as specified, non-empty final answer, and `thinking_verified=true`. If not, stop and report the provider/protocol incompatibility; do not run the remaining 119 calls.

- [ ] **Step 3: Resume and finish the remaining 119 logical calls**

```powershell
.venv\Scripts\python.exe -m evaluation.tools.run_chinese_reader_replay `
  --env-file D:\workspace\hl_agent\hl_mem\var\eval\softsplit_ab_20260827\.env_flash `
  --resume
```

Expected: exit `0`; the final summary has `status="completed"`, `logical_calls=120`, `failed_cases=[]`, and every per-arm artifact contains exactly 40 unique cases. The first canary is reused, not called twice.

- [ ] **Step 4: Validate completeness and secret/reasoning redaction**

Run:

```powershell
.venv\Scripts\python.exe -c "import json,pathlib; root=pathlib.Path('var/eval/v114/cross_reader/glm53-thinking'); summary=json.loads((root/'summary.json').read_text(encoding='utf-8')); reports=[json.loads((root/name).read_text(encoding='utf-8')) for name in ('qwen37.json','glm53.json','qwen38-27b.json')]; assert summary['status']=='completed'; assert summary['logical_calls']==120; assert not summary['failed_cases']; assert all(len(d['cases'])==40 and len({c['case_id'] for c in d['cases']})==40 for d in reports)"
$leak = Get-ChildItem var/eval/v114/cross_reader/glm53-thinking -File | Select-String -Pattern '"reasoning_content"\s*:|Authorization\s*:|Bearer\s+'
if ($leak) { throw "secret or reasoning content found in replay artifacts" }
```

Expected: validation exits `0` and `$leak` is empty.

- [ ] **Step 5: Update the existing evaluation report from `summary.json`**

Add a `## GLM-5.3-Flash thinking reader replay` section to `docs/research/2026-09-03-extraction-quality-plan-ttl-evaluation.md`. Record:

- the three source report hashes and unchanged extractor identities;
- GLM reader request identity and positive canary verification signal;
- Qwen-reader versus GLM-reader QA/F1 for each arm;
- all four flip counts and case-ID lists;
- original and replay rankings;
- aggregate token, latency, attempt, and failure counts;
- the applicable decision-rule interpretation;
- an explicit statement that this is not extraction run2, does not change the approved release gate, and does not switch runtime configuration.

Copy values mechanically from the completed summary; do not transcribe private source text or reasoning content.

- [ ] **Step 6: Verify and commit only the research report**

```powershell
git diff --check -- docs/research/2026-09-03-extraction-quality-plan-ttl-evaluation.md
git add docs/research/2026-09-03-extraction-quality-plan-ttl-evaluation.md
git diff --cached --stat
git commit -m "docs: compare Qwen and GLM thinking readers"
```

Expected: the commit contains only the research report; raw JSON remains ignored.

---

### Task 6: Verify the replay implementation and hand back to the v1.1.4 plan

**Files:**
- No planned tracked changes.

**Interfaces:**
- Consumes: the exact committed Task 5 tree.
- Produces: fresh verification evidence for the replay code and a handoff back to Task 7 of the existing v1.1.4 implementation plan.

- [ ] **Step 1: Run focused reader-replay regressions**

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/unit/test_chinese_reader_replay.py `
  tests/unit/test_memdaily_perltqa_benchmark_scripts.py `
  tests/eval/test_chinese_e2e_contract.py -q --tb=short
```

Expected: all pass without network calls.

- [ ] **Step 2: Run the complete non-paid suite**

```powershell
.venv\Scripts\python.exe -W error::ResourceWarning -m pytest tests/ -q --tb=short
```

Expected: zero failures; `real_api` tests are skipped unless explicitly selected.

- [ ] **Step 3: Run formatting, type, and scope checks**

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m black --check .
.venv\Scripts\python.exe -m isort --check-only .
.venv\Scripts\python.exe -m mypy src/hl_mem/ --ignore-missing-imports
git diff --check
git status --short --branch
git log --oneline -10
```

Expected: checks exit `0`; tracked files are clean; raw replay artifacts remain ignored; only pre-existing user-owned untracked files remain.

- [ ] **Step 4: Report the experiment outcome and resume the parent plan**

Report exact Qwen-reader and GLM-reader scores, flip counts, ranking, canary evidence, usage, and test counts. State that nothing was pushed, tagged, deployed, or published. Then resume Task 7, “Full verification and local release handoff,” in `docs/superpowers/plans/2026-09-03-extraction-quality-plan-ttl.md`; do not silently reinterpret its release gate.
