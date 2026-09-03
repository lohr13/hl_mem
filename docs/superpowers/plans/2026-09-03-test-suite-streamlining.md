# Test Suite Streamlining Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce local and CI test latency by removing duplicate execution, isolating release-only validation, shrinking repeated replay fixtures, running the core suite once with four Python 3.13 workers, and restoring both READMEs to concise landing pages.

**Architecture:** Keep production behavior untouched. Introduce an explicit `release_only` pytest tier, retain one exact 120-case replay orchestration test while using small test-local sources for state-machine cases, consolidate ordinary and release workflows around single authoritative results, document a one-final-run policy, and move detailed operational/history material out of the bilingual README landing pages by linking to existing specialist documents.

**Tech Stack:** Python 3.13 CI authority, pytest, pytest-xdist, pytest-cov, GitHub Actions, uv, TOML, JUnit XML.

## Global Constraints

- Do not modify `src/hl_mem/` or any production runtime behavior.
- Do not change `requires-python`; Python 3.13 is the only authoritative CI environment, while the existing install range remains accepted without a CI compatibility promise.
- Do not add database-template caching, dynamic test selection, or timing-based CI failure gates.
- Do not delete reader replay security, privacy, retry, migration, recovery, or state-machine assertions.
- Run focused tests during implementation; do not run the core full suite until Task 5.
- Task 5 may run the core full suite once, with one additional run allowed only if that run leads to code changes.
- Do not repeat the core suite after a fast-forward merge to the identical verified commit.
- Do not call real providers or models.
- Keep `README.md` and `README_EN.md` structurally synchronized, at roughly 150 lines each, without creating new capability,
  compatibility, or benchmark claims.

---

### Task 1: Define the core and release-only test tiers

**Files:**
- Modify: `pyproject.toml:39-73`
- Modify: `uv.lock`
- Modify: `tests/integration/test_provider_plugin_wheel.py:1-16`
- Modify: `tests/release/test_migration_release_gate.py:1-12`
- Modify: `tests/test_migration_upgrade.py:1-12`
- Create: `tests/unit/test_test_suite_policy.py`

**Interfaces:**
- Produces: registered pytest marker `release_only`.
- Produces: development dependency `pytest-xdist` and executable `pytest -n 4` support.
- Produces: module-level `pytestmark = pytest.mark.release_only` on exactly three release modules.
- Consumes: existing pytest configuration in `pyproject.toml`.

- [ ] **Step 1: Write policy tests that fail on the current configuration**

Create `tests/unit/test_test_suite_policy.py` with imports and assertions equivalent to:

```python
from __future__ import annotations

import importlib

import pytest

RELEASE_MODULES = (
    "tests.integration.test_provider_plugin_wheel",
    "tests.release.test_migration_release_gate",
    "tests.test_migration_upgrade",
)


def _mark_names(module_name: str) -> set[str]:
    value = getattr(importlib.import_module(module_name), "pytestmark", ())
    marks = value if isinstance(value, (list, tuple)) else (value,)
    return {mark.mark.name for mark in marks}


@pytest.mark.parametrize("module_name", RELEASE_MODULES)
def test_release_modules_are_explicitly_marked(module_name: str) -> None:
    assert "release_only" in _mark_names(module_name)
```

- [ ] **Step 2: Run only the new tests and confirm RED**

Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_test_suite_policy.py -q --tb=short
```

Expected: failures report missing `release_only` marks.

- [ ] **Step 3: Register the marker, dependency, and module marks**

Add to the dev dependency group:

```toml
"pytest-xdist>=3.6",
```

Add to pytest markers:

```toml
"release_only: packaging and historical-upgrade checks run only before release",
```

Add `import pytest` where absent and place this after imports in all three release modules:

```python
pytestmark = pytest.mark.release_only
```

Regenerate the lock without changing unrelated dependency versions:

```powershell
uv lock
```

- [ ] **Step 4: Run the focused policy tests and marker collection checks**

Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_test_suite_policy.py -q --tb=short
uv run --frozen python -m pytest tests/ --collect-only -m release_only -n 1 -q
```

Expected: policy tests pass; `-n 1` proves xdist is installed; collection reports the five tests from the three intended modules and no unrelated module.

- [ ] **Step 5: Commit Task 1**

```powershell
git add pyproject.toml uv.lock tests/unit/test_test_suite_policy.py tests/integration/test_provider_plugin_wheel.py tests/release/test_migration_release_gate.py tests/test_migration_upgrade.py
git commit -m "test: separate release-only validation"
```

---

### Task 2: Shrink repeated reader replay state fixtures

**Files:**
- Modify: `tests/unit/test_chinese_reader_replay.py:1028-2458`

**Interfaces:**
- Produces: `synthetic_three_arm_cases(*, include_canary: bool, case_count: int = replay.EXPECTED_CASE_COUNT)`.
- Produces: fixture `small_replay_sources(monkeypatch) -> dict[str, tuple[ReplayCase, ...]]` with two cases per arm.
- Preserves: exact 40-case source contracts and one complete 120-call orchestration test.
- Does not modify: `evaluation/tools/run_chinese_reader_replay.py`.

- [ ] **Step 1: Add a failing small-source fixture contract**

Add beside `synthetic_three_arm_cases`:

```python
def test_synthetic_three_arm_cases_support_small_state_scenarios(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(replay, "EXPECTED_CASE_COUNT", 2)
    sources = synthetic_three_arm_cases(include_canary=True, case_count=2)
    assert {label: len(cases) for label, cases in sources.items()} == {
        label: 2 for label in replay.ARM_LABELS
    }
    assert replay.CANARY_CASE_ID in {case.case_id for case in sources["qwen37"]}
```

- [ ] **Step 2: Run the single contract and confirm RED**

Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_chinese_reader_replay.py::test_synthetic_three_arm_cases_support_small_state_scenarios -q --tb=short
```

Expected: `TypeError` because `case_count` is not accepted yet.

- [ ] **Step 3: Parameterize the test helper without changing production code**

Change the helper to accept `case_count`, generate that many IDs, and place the canary within the available range:

```python
def synthetic_three_arm_cases(
    *,
    include_canary: bool,
    case_count: int = replay.EXPECTED_CASE_COUNT,
) -> dict[str, tuple[replay.ReplayCase, ...]]:
    case_ids = [f"memdaily:simple:events:{index}" for index in range(1, case_count + 1)]
    if include_canary:
        case_ids[min(13, case_count - 1)] = replay.CANARY_CASE_ID
    # Existing arm, case, digest, and binding construction remains unchanged.
```

Add the fixture:

```python
@pytest.fixture
def small_replay_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, tuple[replay.ReplayCase, ...]]:
    monkeypatch.setattr(replay, "EXPECTED_CASE_COUNT", 2)
    return synthetic_three_arm_cases(include_canary=True, case_count=2)
```

- [ ] **Step 4: Move count-independent state tests to the small fixture**

Replace local calls to `synthetic_three_arm_cases(include_canary=True)` with a `small_replay_sources` parameter in tests for:

- atomic authoritative-state writes and projection repair;
- completed and rejected legacy migration;
- canary verification and privacy persistence;
- identity and case-result tamper rejection;
- projection repair;
- partial/fatal reader and scorer failures;
- preflight preservation, retry exhaustion, stale metadata, and completed-failure resume.

Keep full-size sources in:

- exact source/manifest reconstruction tests;
- `test_run_replay_calls_canary_first_and_counts_it_once`;
- `test_canary_only_checkpoints_one_call_and_resume_does_not_repeat_it`.

For failure positions and metric assertions, derive positions and denominators from the small sources instead of retaining literals `42`, `120`, `119`, `20/39`, or other full-size-only values.

- [ ] **Step 5: Run the reader replay file once with durations**

Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_chinese_reader_replay.py -q --tb=short --durations=15
```

Expected: all existing semantic cases plus the new contract pass; exact 120-call tests remain present; runtime is recorded and materially below the 24.68-second baseline.

- [ ] **Step 6: Confirm no production file changed and commit Task 2**

Run:

```powershell
git diff --name-only HEAD | Select-String '^src/|^evaluation/tools/'
```

Expected: no output.

Then commit:

```powershell
git add tests/unit/test_chinese_reader_replay.py
git commit -m "test: shrink reader replay state fixtures"
```

---

### Task 3: Consolidate ordinary CI around Python 3.13

**Files:**
- Modify: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: marker `release_only` from Task 1.
- Produces: ordinary CI jobs `checks`, `core-tests`, `quality`, and `state-full-chain-smoke`.
- Produces: one core pytest command and one invocation of each deterministic quality gate.

- [ ] **Step 1: Record the current workflow duplication before editing**

Run:

```powershell
rg -n "python-version|pytest tests/|tests\.eval\.ci_gate|test_provider_plugin_wheel|python -m build" .github/workflows/test.yml
```

Expected: output shows the three-version matrices and duplicated pytest, recall, provider-wheel, and build execution described in the design baseline. This configuration-only task has explicit user approval to omit brittle source-text unit tests.

- [ ] **Step 2: Replace ordinary workflow duplication with four jobs**

Rewrite `.github/workflows/test.yml` so it has these responsibilities:

```yaml
jobs:
  checks:
    # Python 3.13; uv sync once; ruff, black, isort, mypy and all deterministic scripts.
  core-tests:
    # Python 3.13; one command:
    # uv run --frozen python -W error::ResourceWarning -m pytest tests/ \
    #   -m "not release_only" -n 4 -q --tb=short \
    #   --cov=hl_mem --cov-report=term-missing --cov-fail-under=80
  quality:
    # Python 3.13; tests.eval.ci_gate once and scripts/run_quality_smoke.py once.
  state-full-chain-smoke:
    # Preserve sqlite-vec installation and sanitized interpreter environment.
```

The `checks` command list must contain each existing unique static gate exactly once:

```text
ruff check .
black --check .
isort --check-only .
mypy src/hl_mem/ --ignore-missing-imports
scripts/check_actions_pinned.py
scripts/check_imports.py
scripts/check_config_schema_snapshot.py
scripts/check_provider_plugin_api.py
scripts/check_ops_report_schema.py
scripts/check_openapi_snapshot.py
scripts/check_mcp_snapshot.py
scripts/check_docs_consistency.py
scripts/check_complexity_budget.py --ratchet
```

Delete the redundant matrix, follow-up migration invocation, and standalone lint/type/format/migrations/import/snapshot/recall/provider-wheel/build jobs.

- [ ] **Step 3: Run focused workflow checks**

Run:

```powershell
uv run --frozen python scripts/check_actions_pinned.py
git diff --check
```

Inspect the resulting job list and commands once. Expected: only Python 3.13 is configured, each unique gate appears once, action pinning passes, and the diff is clean. Do not run the core suite in this task.

- [ ] **Step 4: Commit Task 3**

```powershell
git add .github/workflows/test.yml
git commit -m "ci: consolidate Python 3.13 checks"
```

---

### Task 4: Consolidate release execution and evidence

**Files:**
- Modify: `.github/workflows/release-gates.yml`
- Modify: `scripts/write_release_evidence.py:12-30`
- Modify: `tests/unit/test_write_release_evidence.py`

**Interfaces:**
- Consumes: `release_only` marker and xdist dependency from Task 1.
- Produces: release evidence names `python-3.13`, `release-only`, `public-recall`, `pip-audit`, `sbom`, and `wheel-install`.
- Preserves: manifest schema version 1, SHA-256 validation, JUnit validation, recall validation, dependency audit, and SBOM validation.

- [ ] **Step 1: Change expected evidence in tests first**

Update evidence fixtures and assertions so the exact required set is:

```python
{
    "python-3.13",
    "release-only",
    "public-recall",
    "pip-audit",
    "sbom",
    "wheel-install",
}
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_write_release_evidence.py -q --tb=short
```

Expected: required evidence names do not match the new policy.

- [ ] **Step 3: Update the evidence contract**

Change only `REQUIRED_EVIDENCE` in `scripts/write_release_evidence.py` to the six names above. Keep parsing, status validation, hashing, and schema output unchanged.

Update test fixtures to generate JUnit evidence for `python-3.13`, `release-only`, and `wheel-install`; JSON evidence for `public-recall` and `pip-audit`; and file evidence for `sbom`.

- [ ] **Step 4: Rewrite release jobs without duplicate pytest runs**

Change `.github/workflows/release-gates.yml` to:

- remove the Python version matrix and use Python 3.13;
- run the core suite once with `-m "not release_only" -n 4`, coverage, and `evidence/python-3.13.xml`;
- run release-only tests once, serially, with `evidence/release-only.xml`;
- build and inspect distributions once, install the wheel in a clean environment, and retain `evidence/wheel-install.xml`;
- generate public recall, pip-audit, and SBOM artifacts once;
- aggregate only the six required evidence names.

The evidence command must be:

```yaml
--junit python-3.13=evidence/python-3.13.xml \
--junit release-only=evidence/release-only.xml \
--json public-recall=evidence/public-recall.json \
--json pip-audit=evidence/pip-audit.json \
--file sbom=evidence/sbom.cdx.json \
--junit wheel-install=evidence/wheel-install.xml
```

- [ ] **Step 5: Run focused evidence and workflow checks**

Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_write_release_evidence.py -q --tb=short
uv run --frozen python scripts/check_actions_pinned.py
git diff --check
```

Inspect the resulting release job list and commands once. Expected: only Python 3.13 is configured, core/release/recall each appear once, all focused checks pass, and the diff is clean. Do not run core or release-only suites yet.

- [ ] **Step 6: Commit Task 4**

```powershell
git add .github/workflows/release-gates.yml scripts/write_release_evidence.py tests/unit/test_write_release_evidence.py
git commit -m "ci: deduplicate release evidence gates"
```

---

### Task 5: Document policy and perform the bounded final verification

**Files:**
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `AGENTS.md:107-116`
- Modify: `docs/architecture.md:524-536`
- Modify: `docs/release-checklist.md:1-25`
- Modify: `docs/support.md:1-20`
- Modify: `docs/provider-plugins.md:95-112`
- Modify: `docs/CHANGELOG.md:1-12`
- Modify: `tests/unit/test_startup_scripts.py:1-150`

**Interfaces:**
- Consumes: final commands and workflow names from Tasks 1–4.
- Produces: contributor policy limiting core full-suite execution to one final run, with a second only after code changes.
- Produces: explicit Python 3.13 CI-authority wording without changing package install metadata.
- Produces: synchronized Chinese and English landing pages containing one quickstart and links to specialist documentation.
- Preserves: evaluation-launcher coverage in the dedicated `sqlite-vec` environment while allowing the core environment to
  omit that optional extra.

- [ ] **Step 1: Isolate the optional evaluation-launcher checks**

Use `importlib.util.find_spec("sqlite_vec")` in `tests/unit/test_startup_scripts.py` and skip only the two Windows evaluation
launcher subprocess tests when the optional extra is absent. The ordinary repository launcher test remains unconditional
on Windows, and the separate `state-full-chain-smoke` job continues to install and exercise `sqlite-vec`.

Run:

```powershell
uv run --frozen python -m pytest tests/unit/test_startup_scripts.py -q --tb=short
```

Expected without the optional extra: seven pass and the two evaluation-launcher subprocess tests skip.

- [ ] **Step 2: Update documentation with exact commands and support wording**

Document these commands:

```powershell
# Focused development test
.venv\Scripts\python.exe -m pytest tests/unit/test_relevant_file.py -q --tb=short

# Final core suite: normally once
.venv\Scripts\python.exe -W error::ResourceWarning -m pytest tests/ `
  -m "not release_only" -n 4 -q --tb=short --durations=25

# Release-only validation: only before release
.venv\Scripts\python.exe -m pytest tests/ -m release_only -q --tb=short
```

State explicitly:

- Python 3.13 is the only CI-tested runtime.
- Existing package installation metadata remains unchanged; other versions are not CI compatibility promises.
- Identical fast-forward merges reuse the verified commit result.
- The second core run is allowed only after the first run causes code changes.

Add a v1.1.4 changelog bullet describing test-routing and CI changes, with no runtime behavior claim.

- [ ] **Step 3: Replace both READMEs with synchronized landing pages**

Keep these sections in both `README.md` and `README_EN.md`, in the same order:

1. badges and language switch;
2. product definition and evidence-first data-flow diagram;
3. one quickstart covering install, init, server, remember, and recall;
4. concise source install plus model, MCP, and Hermes integration links;
5. a table of eight to ten common configuration keys;
6. compact capability summary and documentation index;
7. contribution and license links.

Remove version-by-version upgrade sections, editable-install internals, contaminated-host troubleshooting, Windows
supervisor instructions, maintenance/repair recipes, injection-fixture instructions, historical benchmark tables,
migration inventories, and duplicated project-status prose. Do not copy those details into a new README appendix; link to
`docs/architecture.md`, `docs/configuration.md`, `docs/compatibility.md`, `docs/capability-matrix.md`,
`docs/CHANGELOG.md`, `tests/eval/README.md`, and CLI help as appropriate.

- [ ] **Step 4: Run documentation and static checks before the final pytest rerun**

Run:

```powershell
uv run --frozen python scripts/check_docs_consistency.py
uv run --frozen python scripts/check_actions_pinned.py
uv run --frozen python -m ruff check .
uv run --frozen python -m black --check .
uv run --frozen python -m isort --check-only .
uv run --frozen python -m mypy src/hl_mem/ --ignore-missing-imports
git diff --check
```

Expected: all pass. Formatting-only corrections do not authorize an extra core run.

- [ ] **Step 5: Verify collection partitions without executing the core suite**

Run:

```powershell
uv run --frozen python -m pytest tests/ --collect-only -m release_only -q
uv run --frozen python -m pytest tests/ --collect-only -m "not release_only" -q
uv run --frozen python -m pytest tests/ --collect-only -q
```

Expected: release-only collection contains exactly the five intended tests; core plus release-only node counts equal the unfiltered collection count.

- [ ] **Step 6: Run release-only validation once, serially**

Run:

```powershell
uv run --frozen python -W error::ResourceWarning -m pytest tests/ -m release_only -q --tb=short --durations=10
```

Expected: all five release-only tests pass. Do not repeat without a subsequent code change affecting them.

- [ ] **Step 7: Run the one permitted final core-suite rerun with four workers**

The first core run completed in 204.45 seconds with 3,262 passing tests, six skips, 108 passing subtests, 87.51% coverage,
and two Windows-only failures because the core environment omitted optional `sqlite-vec`. Step 1 changes the affected test
isolation, so one final core rerun is permitted. Run it exactly once:

```powershell
uv run --frozen python -W error::ResourceWarning -m pytest tests/ `
  -m "not release_only" -n 4 -q --tb=short --durations=25 `
  --cov=hl_mem --cov-report=term-missing --cov-fail-under=80
```

Expected: zero failures, coverage at least 80%, and elapsed time recorded. The target is 180–210 seconds on the reference Windows host, but time is reported rather than enforced.

If this run fails, diagnose only the failures and report them. A third core run is not allowed without explicit user approval.

- [ ] **Step 8: Record measured results and commit Task 5**

Update the v1.1.4 changelog bullet with actual reader-file, release-only, core-suite, and coverage measurements. Do not claim the time target if it was missed.

Then commit:

```powershell
git add README.md README_EN.md AGENTS.md docs/architecture.md docs/release-checklist.md docs/support.md docs/provider-plugins.md docs/CHANGELOG.md tests/unit/test_startup_scripts.py
git commit -m "docs: define bounded test execution policy"
```

- [ ] **Step 9: Final repository audit without rerunning pytest**

Run:

```powershell
git status --short
git log --oneline --decorate -8
git diff main...HEAD --check
git diff --stat main...HEAD
```

Expected: tracked worktree clean; only the intended test, workflow, evidence, dependency, and documentation files changed; no `src/hl_mem/` changes; no provider/model artifacts.
