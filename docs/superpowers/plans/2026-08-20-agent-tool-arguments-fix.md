# Agent Tool Arguments Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every frozen agent tool call use its natural object contract while preserving deterministic, fail-closed execution and schema-aware resume behavior.

**Architecture:** Bind each sample's single executable fixture invocation into the model-visible read-only tool schema, derive a v2 agent response schema from that contract, and hash both response schemas into the blind invocation identity. Parse mappings directly, retain strict JSON-string compatibility at the executor boundary, normalize only Python distribution names, ignore stale resume records, include agent identity in judge result keys, retain legacy judge rows under a composite resume identity, and guide only failed judge retries with validator feedback plus exact trace snippets.

**Tech Stack:** Python 3.11+, JSON Schema Draft 2020-12, pytest/pytest-asyncio, Bailian OpenAI-compatible structured output.

## Global Constraints

- Preserve fail-closed behavior for unknown tools, malformed syntax, extra keys, and materially different targets.
- Do not add JSON5 or another parsing dependency.
- Keep the sentinel 9/9 and agent/judge expected-count gates unchanged.
- Keep the paid-run hard budget at or below CNY 14.796848 with pre-call reservations.
- Deliver the implementation, tests, diagnostic evidence, generated report, and plan in one repair commit.

---

### Task 1: Freeze the v2 object contract in regression tests

**Files:**
- Modify: `tests/unit/test_v0291_agent_trace.py`
- Modify: `tests/unit/test_v0291_behavioral_runner.py`

**Interfaces:**
- Consumes: `build_blind_agent_input`, `AgentTraceGenerator.generate`, and resumable agent records.
- Produces: executable expectations for `build_agent_plan_schema(available_tools)`, object/string argument handling, package-name equivalence, and `_select_current_agent_records(records, unique_inputs)`.

- [x] **Step 1: Change the successful fake model output to v2 object arguments**

```python
"schema_version": "hl-mem-agent-plan-v2",
"tool_calls": [
    {
        "tool_name": "inspect_python_install",
        "arguments": {"package": "hl-mem"},
    }
],
```

Assert that the request schema contains the same object under
`tool_calls.items.properties.arguments`, including `const: "hl-mem"`, and
does not contain `arguments_json`.

- [x] **Step 2: Add executor-boundary cases**

Call `AgentTraceGenerator._execute_tools` with a direct mapping, the strict
legacy string `'{"package":"hl-mem"}'`, and the normalized package name
`{"package":"HL_Mem"}`; each must produce the frozen tool result. Add
separate malformed cases for single quotes and trailing commas and assert
`AgentTraceInvalid`.

- [x] **Step 3: Add schema identity and stale-resume cases**

Assert `build_blind_agent_input` exposes `response_schema_sha256`, mutating it
changes `input_sha256`, and a stale successful agent record whose digest is
absent from `unique_inputs` is excluded by `_select_current_agent_records`.

- [x] **Step 4: Run the tests and verify RED**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -I -m pytest tests/unit/test_v0291_agent_trace.py tests/unit/test_v0291_behavioral_runner.py -q --tb=short
```

Expected: failures show the existing v1 `arguments_json` schema, missing
response-schema digest, lack of mapping/package normalization support, and the
missing current-record selector.

### Task 2: Implement the strict object contract and resume selector

**Files:**
- Modify: `evaluation/v0291_behavioral/agent.py`
- Modify: `evaluation/v0291_behavioral/runner.py`
- Modify: `evaluation/v0291_behavioral/scorer.py`
- Modify: `scripts/run_v0291_behavioral_eval.py`

**Interfaces:**
- Consumes: model-visible `available_tools` and sample `deterministic_tool_results`.
- Produces: `build_agent_plan_schema(available_tools: Sequence[Mapping[str, Any]]) -> dict[str, Any]` and `_select_current_agent_records(agent_records, unique_inputs) -> dict[str, Mapping[str, Any]]`.

- [x] **Step 1: Bind the executable fixture arguments**

While building the blind input, deep-copy each allowed tool, verify its fixture
argument keys equal the parameter schema's required keys, and add each fixture
value as a `const` on the matching property. Reject manifests with more than
one available tool for a frozen input.

- [x] **Step 2: Derive and use the v2 response schema**

Create `build_agent_plan_schema`. For zero tools set both plan/call arrays to
`maxItems: 0`; for one tool set both to `maxItems: 1`, constrain `tool_name` to
that name, and copy its bound parameter object to the `arguments` field. Use
schema name `hl_mem_agent_plan_v2`, validate against the derived schema, and
hash the plan and final schemas into `response_schema_sha256`.

- [x] **Step 3: Parse arguments without weakening syntax**

Use mappings directly. For a string, call only `json.loads`; require the result
to be an object. Compare fixture keys and values exactly, except normalize
`inspect_python_install.package` with lowercase and replacement of every run
of `-`, `_`, or `.` by `-`.

- [x] **Step 4: Select only current resumable agent records**

Filter loaded successful records to digests present in `unique_inputs` before
computing missing work or enforcing `valid_count == expected_count`.

- [x] **Step 5: Run the focused tests and verify GREEN**

Run the Task 1 command. Expected: all tests pass with zero failures.

- [x] **Step 6: Repair first-reachable judge resume and retry defects**

Include `agent_input_sha256` in judge result keys and load resumable judge
records by the backward-compatible `(result_key, agent_input_sha256)` pair.
Keep the first judgment blind; after a
`JudgmentInvalid`, attach its exact error and previous output, generate bounded
exact text candidates from allowed trace sources, and constrain retry `quote`
values to those candidates without weakening `validate_judgment`.

### Task 3: Verify the repository and rerun the full behavioral evaluation

**Files:**
- Modify: `docs/v0291-behavioral-eval-report.md`
- Modify: `scripts/run_v0291_behavioral_report.py`
- Modify: `tests/unit/test_v0291_behavioral_report.py`
- Modify: `tests/unit/test_v0291_scorer.py`
- Generated but ignored: `evaluation/results/v0291_behavioral_20260820/*`

**Interfaces:**
- Consumes: the v2 tool contract, frozen fixtures, existing valid 9/9 sentinel artifact, and budget ledger.
- Produces: 131 valid agent traces, complete judge records, aggregate artifacts, and the updated human report.

- [x] **Step 1: Run the full unit suite**

```powershell
& '.\.venv\Scripts\python.exe' -I -m pytest tests/unit/ -q --tb=short
```

Expected: exit code 0 and no failures.

- [x] **Step 2: Run all evaluation phases under the hard budget**

```powershell
& '.\.venv\Scripts\python.exe' -m scripts.run_v0291_behavioral_eval --phase all --budget-cny 14.796848
```

Expected: structural and sentinel gates pass; agent reaches 131/131 before
judge calls; the command exits 0 with no outstanding budget reservations.

- [x] **Step 3: Regenerate and inspect the report**

```powershell
& '.\.venv\Scripts\python.exe' -m scripts.run_v0291_behavioral_report
```

Read the aggregate, budget summary, and report; confirm the counts, model
snapshot, gate verdicts, and costs agree.

- [x] **Step 4: Run final focused verification**

```powershell
& '.\.venv\Scripts\python.exe' -I -m pytest tests/unit/test_v0291_agent_trace.py tests/unit/test_v0291_behavioral_runner.py tests/unit/test_v0291_behavioral_report.py -q --tb=short
```

Expected: exit code 0 and no failures.

- [x] **Step 5: Review and create the single repair commit**

```powershell
git diff --check
git status --short
git diff --stat
git add evaluation/v0291_behavioral/agent.py evaluation/v0291_behavioral/runner.py evaluation/v0291_behavioral/scorer.py scripts/run_v0291_behavioral_eval.py scripts/run_v0291_behavioral_report.py tests/unit/test_v0291_agent_trace.py tests/unit/test_v0291_behavioral_runner.py tests/unit/test_v0291_behavioral_report.py tests/unit/test_v0291_scorer.py docs/v0291-behavioral-eval-report.md
git add -f docs/superpowers/specs/2026-08-20-agent-tool-arguments-design.md docs/superpowers/plans/2026-08-20-agent-tool-arguments-fix.md
git commit -m "fix(eval): use structured agent tool arguments"
```

Expected: one new commit containing only this repair and its evidence.
