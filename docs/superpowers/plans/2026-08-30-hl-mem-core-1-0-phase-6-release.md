# HL-Mem Core 1.0 Phase 6 Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a reproducible `1.0.0rc1` candidate with mandatory public recall evidence, 80% coverage, release/security automation, an auditable v0.36.1 comparison, and an enforceable seven-day RC observation gate.

**Architecture:** Keep release policy outside production request paths. Reuse pytest, the existing deterministic evaluation stack, and GitHub Actions; add only small validation/reporting scripts where a machine-readable cross-job contract is needed. The public recall corpus is synthetic and zero-network, while stable product behavior continues to be exercised through the real REST/application/storage stack.

**Tech Stack:** Python 3.12-3.14, pytest/coverage, FastAPI TestClient, SQLite, uv, GitHub Actions, CodeQL, pip-audit, CycloneDX JSON.

## Global Constraints

- Baseline is local `main` at `3affd2b`; Phase 5 is already merged.
- Preserve the user's untracked `docs/research/v028-plan-draft.md` byte-for-byte and exclude it from every commit.
- Work in `.worktrees/core-1-0-phase-6` on `codex/core-1-0-phase-6`; do not edit `main` directly during implementation.
- Do not push, tag, publish, create a GitHub release, or spend paid-model quota without separate explicit authorization.
- Python 3.12, 3.13, and 3.14 remain supported and must pass test, build, clean-install, and migration gates.
- The full-suite coverage floor is 80%; do not lower it or exclude new production modules to make the number pass.
- The required public recall gate must fail loudly when any corpus, manifest, protocol, or baseline artifact is absent or has the wrong hash. No fixture-missing skip branch remains.
- The v0.36.1 comparison uses tag `v0.36.1` (`2dbb6a9`) and a protocol frozen before the RC candidate run. Historical C-series thresholds are not reused.
- Public release benchmarking makes zero paid or external model calls. Real-provider suites remain optional diagnostic evidence and are not required for RC qualification.
- Every third-party GitHub Action reference is pinned to a full commit SHA with the human-readable release in a comment.
- Security automation is advisory only where upstream vulnerability data is unavailable; a scanner execution error fails the workflow, and known accepted findings require a checked-in, expiring rationale rather than an inline ignore.
- `1.0.0` is not published until seven consecutive UTC observation days are proven. Any P0/P1, production code, configuration, migration, or stable-contract fix produces a new RC tag and resets the clock.
- Do not add a release framework, generic pipeline DSL, hosted service, graph dependency, private benchmark data, or model-provider feature in this phase.

---

### Task 1: Required public recall fixture and fail-loud CI gate

**Files:**
- Create: `tests/eval/public/recall_core_v1.jsonl`
- Create: `tests/eval/public/recall_core_v1.manifest.json`
- Create: `tests/eval/public/recall_core_v1.protocol.json`
- Create: `tests/eval/public/recall_core_v1.baseline.json`
- Modify: `tests/eval/ci_gate.py`
- Modify: `tests/eval/fixtures/build_ci_snapshot.py`
- Modify: `tests/eval/test_ci_fixture_hash.py`
- Modify: `tests/eval/test_dataset_schema.py`
- Modify: `tests/eval/test_recall_v2_gate.py`
- Modify: `tests/eval/README.md`
- Modify: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: `tests.eval.dataset.load_cases`, `tests.eval.eval_runner.run`, `tests.eval.gate_check.check`, and `build_ci_snapshot(target: Path, dataset: Path)`.
- Produces: `tests.eval.ci_gate.main(argv: list[str] | None = None) -> int` with defaults pointing only to tracked public artifacts.
- Produces: a 32-case public corpus: 8 exact/current, 8 paraphrase/current, 4 preference, 4 historical, and 8 no-answer cases. It contains synthetic product facts only and no personal or private production content.

- [ ] **Step 1: Write failing public-artifact and gate tests**

  Replace skip-based coverage with direct assertions and an executable gate:

  ```python
  PUBLIC = Path(__file__).parent / "public"

  def test_public_recall_release_artifacts_are_complete_and_bound() -> None:
      dataset = PUBLIC / "recall_core_v1.jsonl"
      manifest = json.loads((PUBLIC / "recall_core_v1.manifest.json").read_text(encoding="utf-8"))
      cases = load_cases(dataset)
      assert len(cases) == 32
      assert len({case.case_id for case in cases}) == 32
      assert manifest["dataset_sha256"] == _sha256_utf8_lf(dataset)
      assert manifest["case_count"] == 32

  def test_public_recall_gate_passes_without_private_files(tmp_path: Path) -> None:
      report = tmp_path / "report.json"
      assert ci_gate.main(["--report", str(report)]) == 0
      assert json.loads(report.read_text(encoding="utf-8"))["case_count"] == 32
  ```

- [ ] **Step 2: Run the focused tests and verify the missing artifacts fail**

  Run:

  ```powershell
  uv run --frozen python -m pytest tests/eval/test_ci_fixture_hash.py tests/eval/test_dataset_schema.py tests/eval/test_recall_v2_gate.py -q --tb=short
  ```

  Expected: FAIL because `tests/eval/public/recall_core_v1.jsonl` and its bound metadata do not exist.

- [ ] **Step 3: Materialize the fixed public corpus and single protocol contract**

  Use IDs `C01-C08`, `P01-P08`, `U01-U04`, `H01-H04`, and `N01-N08`. Every answerable row binds one unique synthetic Claim; no-answer rows have `binding: null`, `expected_type: "empty"`, and no expected keywords. The protocol JSON freezes:

  ```json
  {
    "protocol_version": "core-recall-public-v1",
    "top_k": 5,
    "embedding": {"provider": "fake", "model": "fake", "dim": 2048},
    "extractor": {"provider": "fake", "model": "fake-v1"},
    "reranker": {"mode": "off"},
    "index_text_mode": "legacy",
    "max_metric_regression": 0.01,
    "max_slice_regression": 0.05,
    "required_http_success_rate": 1.0,
    "required_forbidden_hits": 0
  }
  ```

  Simplify `ci_gate.py` so it reads only dataset, manifest, protocol, and baseline; validates all hashes/counts/config fields before constructing SQLite; runs with `Settings.for_test()` replacements; and refuses baselines whose `status` is not `public_release_baseline`.

- [ ] **Step 4: Generate and verify the deterministic baseline**

  Run the gate once with an explicit `--write-baseline` mode that refuses to overwrite an existing file, then remove that write option from ordinary CI invocation. The committed baseline records schema/protocol versions, hashes, case/slice counts, metrics, forbidden hits, and HTTP success rate; it does not claim real-provider quality.

  Run:

  ```powershell
  uv run --frozen python -m tests.eval.ci_gate --write-baseline tests/eval/public/recall_core_v1.baseline.json
  uv run --frozen python -m tests.eval.ci_gate --report Temp/phase6-public-recall.json
  ```

  Expected: the first command creates one baseline, and the second prints `Recall public fixture gate: PASSED`.

- [ ] **Step 5: Make the CI recall job unconditional**

  Remove both `hashFiles(...)` branches from `.github/workflows/test.yml`. The job runs exactly:

  ```yaml
  - name: Run required public recall contract
    run: uv run --frozen python -m tests.eval.ci_gate
  ```

  Missing artifacts now fail by normal file-open error.

- [ ] **Step 6: Run focused verification and commit**

  Run:

  ```powershell
  uv run --frozen python -m pytest tests/eval/test_ci_fixture_hash.py tests/eval/test_dataset_schema.py tests/eval/test_recall_v2_gate.py -q --tb=short
  uv run --frozen python -m tests.eval.ci_gate
  ```

  Expected: all PASS with no skips from these files.

  Commit:

  ```powershell
  git add tests/eval/public tests/eval/ci_gate.py tests/eval/fixtures/build_ci_snapshot.py tests/eval/test_ci_fixture_hash.py tests/eval/test_dataset_schema.py tests/eval/test_recall_v2_gate.py tests/eval/README.md .github/workflows/test.yml
  git commit -m "test: require the public Core 1.0 recall gate"
  ```

### Task 2: Coverage, fast feedback, and migration matrices

**Files:**
- Modify: `.github/workflows/test.yml`
- Create: `tests/release/__init__.py`
- Create: `tests/release/test_migration_release_gate.py`
- Modify: `tests/test_migration_upgrade.py`
- Modify: `CONTRIBUTING.md`

**Interfaces:**
- Consumes: `Database.open()`, the immutable SQL migration directory, `tests/fixtures/v010_after_018.sql`, and the v006 snapshot builder.
- Produces: a `fast` PR job that gives early feedback while the required full Python matrix remains authoritative.
- Produces: explicit empty, v006, v010, and repeated-open migration evidence on Python 3.12-3.14.

- [ ] **Step 1: Write a failing repeated-migration release test**

  ```python
  def test_current_database_reopen_applies_no_migration_twice(tmp_path: Path) -> None:
      database = Database(tmp_path / "repeat.db")
      first = database.open()
      versions = tuple(row[0] for row in first.execute("SELECT version FROM schema_migrations ORDER BY version"))
      database.close()
      reopened = Database(tmp_path / "repeat.db")
      second = reopened.open()
      assert tuple(row[0] for row in second.execute("SELECT version FROM schema_migrations ORDER BY version")) == versions
      assert second.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
      reopened.close()
  ```

  The same file invokes the existing v006/v010 upgrade helpers and checks application-managed seed rows after upgrade.

- [ ] **Step 2: Run the release migration tests**

  Run:

  ```powershell
  uv run --frozen python -m pytest tests/release/test_migration_release_gate.py tests/test_migration_upgrade.py -q --tb=short
  ```

  Expected before implementation: FAIL because `tests/release/test_migration_release_gate.py` is absent.

- [ ] **Step 3: Split early feedback from authoritative full tests**

  Add a `fast` job before the matrix. It runs Ruff, import/config/provider/OpenAPI/MCP checks, the required public recall gate, and the focused configuration/plugin/request-limit/release-migration tests. Keep the full `test` matrix on both pull requests and `main`, but change `--cov-fail-under=60` to `--cov-fail-under=80`.

  The migration matrix runs:

  ```yaml
  uv run --frozen python -m pytest tests/release/test_migration_release_gate.py tests/test_migration_upgrade.py -q --tb=short
  ```

  on each supported Python version. The existing build matrix remains 3.12-3.14.

- [ ] **Step 4: Document local equivalents and verify**

  Add the exact fast and full commands to `CONTRIBUTING.md`, identifying the full matrix as the release authority.

  Run:

  ```powershell
  uv run --frozen python -m pytest tests/release/test_migration_release_gate.py tests/test_migration_upgrade.py -q --tb=short
  uv run --frozen python -m pytest tests/ -q --tb=short --cov=hl_mem --cov-report=term --cov-fail-under=80
  ```

  Expected: migrations PASS; the full suite reports coverage at or above 80%.

- [ ] **Step 5: Commit**

  ```powershell
  git add .github/workflows/test.yml tests/release tests/test_migration_upgrade.py CONTRIBUTING.md
  git commit -m "ci: enforce Core 1.0 test and migration gates"
  ```

### Task 3: Operational release evidence

**Files:**
- Create: `tests/release/test_default_zero_model_calls.py`
- Create: `scripts/write_release_evidence.py`
- Create: `tests/unit/test_write_release_evidence.py`
- Create: `.github/workflows/release-gates.yml`
- Modify: `tests/unit/test_backup_cli.py`

**Interfaces:**
- Consumes: existing backup/restore tests, Provider wheel conflict test, ASGI streaming limit tests, semantic-job gates, and JUnit XML output.
- Produces: `write_release_evidence.main(argv: list[str] | None = None) -> int`, which rejects missing/failing evidence and writes `release-evidence.json` plus `release-evidence.md`.
- Produces: one immutable artifact that names the version, commit, run URL, Python versions, artifact hashes, test counts, and pass/fail state of every release gate.

- [ ] **Step 1: Write the zero-model-call integration test**

  Patch the four real-provider execution methods (`LLMClient.complete`, `Embedder.embed_batch`, `DashScopeReranker.rerank`, and `GovernedImageDescriber.describe`) with one trap, start the application with the explicit test profile, run default maintenance/scheduling and one ordinary recall, then assert the trap count remains zero:

  ```python
  def test_default_runtime_and_maintenance_make_zero_model_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
      calls: list[str] = []
      def unexpected_call(*_args: object, **_kwargs: object) -> NoReturn:
          calls.append("provider")
          raise AssertionError("default path invoked a model provider")
      monkeypatch.setattr(LLMClient, "complete", unexpected_call)
      monkeypatch.setattr(Embedder, "embed_batch", unexpected_call)
      monkeypatch.setattr(DashScopeReranker, "rerank", unexpected_call)
      monkeypatch.setattr(GovernedImageDescriber, "describe", unexpected_call)
      settings = replace(Settings.for_test(), database_path=str(tmp_path / "zero.db"))
      with TestClient(create_app(settings)) as client:
          assert client.post("/v1/recall", json={"query": "absent synthetic fact"}).status_code == 200
          worker = Worker(settings)
          worker._run_maintenance()
          worker.close()
      assert calls == []
  ```

  Do not enable semantic jobs or replace the execution assertion with settings-field checks.

- [ ] **Step 2: Run focused operational tests**

  Run:

  ```powershell
  uv run --frozen python -m pytest tests/release/test_default_zero_model_calls.py tests/unit/test_request_size_limit.py tests/unit/test_backup_cli.py tests/integration/test_provider_plugin_wheel.py -q --tb=short
  ```

  Expected before implementation: FAIL on the missing zero-call test; existing streaming, restore, and plugin-conflict cases remain green.

- [ ] **Step 3: Implement strict evidence aggregation**

  `write_release_evidence.py` accepts repeated `--junit NAME=PATH`, `--json NAME=PATH`, `--file NAME=PATH`, plus `--version`, `--commit`, and `--run-url`. It parses every JUnit suite and fails if `failures + errors > 0`, hashes every input with SHA-256, rejects duplicate/missing required names, and emits deterministic sorted JSON plus concise Markdown links to the workflow run and artifact filenames.

  Required names are `python-3.12`, `python-3.13`, `python-3.14`, `migration`, `backup-restore`, `plugin-conflict`, `streaming-limit`, `zero-model-call`, `public-recall`, `pip-audit`, `sbom`, and `wheel-install`.

- [ ] **Step 4: Test evidence aggregation**

  Tests create minimal JUnit XML/JSON files, prove a zero-failure set writes both outputs, and prove a missing name or nonzero failure returns 1 without writing a success manifest.

  Run:

  ```powershell
  uv run --frozen python -m pytest tests/unit/test_write_release_evidence.py -q --tb=short
  ```

  Expected: PASS.

- [ ] **Step 5: Add the release-gates workflow**

  The workflow runs on `workflow_dispatch` and `v*rc*` tags. It uses a 3.12-3.14 matrix for the full suite and migration gate, then a 3.13 operational job for backup/restore, external plugin conflict, streaming limits, zero-model calls, public recall, build, wheel-content check, and clean install. Each command writes JUnit or JSON evidence. A final job downloads all evidence, runs `write_release_evidence.py`, and uploads the two manifest files together with their inputs.

  No release or PyPI upload occurs in this workflow.

- [ ] **Step 6: Verify and commit**

  Run:

  ```powershell
  uv run --frozen python -m pytest tests/release tests/unit/test_write_release_evidence.py tests/unit/test_request_size_limit.py tests/unit/test_backup_cli.py tests/integration/test_provider_plugin_wheel.py -q --tb=short
  uv run --frozen python scripts/check_imports.py
  ```

  Expected: all PASS.

  Commit:

  ```powershell
  git add tests/release scripts/write_release_evidence.py tests/unit/test_write_release_evidence.py tests/unit/test_backup_cli.py .github/workflows/release-gates.yml
  git commit -m "ci: collect auditable Core 1.0 release evidence"
  ```

### Task 4: Security automation and fully pinned Actions

**Files:**
- Create: `SECURITY.md`
- Create: `.github/dependabot.yml`
- Create: `.github/workflows/security.yml`
- Create: `scripts/check_actions_pinned.py`
- Create: `tests/unit/test_check_actions_pinned.py`
- Modify: `.github/workflows/test.yml`
- Modify: `.github/workflows/quality-smoke.yml`
- Modify: `.github/workflows/publish.yml`
- Modify: `.github/workflows/release-gates.yml`
- Modify: `CONTRIBUTING.md`

**Interfaces:**
- Produces: `check_actions_pinned.main(argv: list[str] | None = None) -> int`, parsing all tracked workflow YAML, accepting `./...` local actions, requiring remote actions to use a full 40-hex commit SHA, and requiring Docker image references to use a digest.
- Produces: scheduled and pull-request security jobs for CodeQL, dependency audit, secret scanning, and CycloneDX SBOM generation.

- [ ] **Step 1: Write pin-checker tests**

  ```python
  def test_unpinned_remote_action_is_rejected(tmp_path: Path) -> None:
      workflow = tmp_path / "bad.yml"
      workflow.write_text("steps:\n  - uses: actions/checkout@v5\n", encoding="utf-8")
      assert check_actions_pinned.check_paths([workflow]) == ["bad.yml:2: remote action is not pinned to a full commit SHA"]

  def test_sha_pinned_and_local_actions_are_accepted(tmp_path: Path) -> None:
      workflow = tmp_path / "good.yml"
      workflow.write_text("steps:\n  - uses: actions/checkout@" + "a" * 40 + " # v5\n  - uses: ./local\n", encoding="utf-8")
      assert check_actions_pinned.check_paths([workflow]) == []
  ```

- [ ] **Step 2: Resolve and pin official action tags**

  Resolve tags from each action's official Git repository immediately before editing. Pin checkout, setup-python, setup-uv, upload/download-artifact, CodeQL, PyPI publish, and secret/SBOM actions to the resolved full SHA; retain the release tag in a comment. Do not copy floating SHAs from third-party examples.

- [ ] **Step 3: Add Dependabot and security policy**

  Dependabot updates `uv`/pip metadata and `github-actions` weekly with a limit of five open PRs per ecosystem. `SECURITY.md` supports the current `1.x` line and current RC, directs reports to GitHub private vulnerability reporting, states a 72-hour acknowledgement target, forbids public security issues before coordination, and documents the data-at-rest/local-service threat boundary without promising enterprise compliance.

- [ ] **Step 4: Add executable security jobs**

  `security.yml` runs on pull requests, `main`, weekly schedule, and manual dispatch:

  - CodeQL analyzes Python.
  - `pip-audit` scans the locked base plus all extras and writes JSON.
  - a pinned secret scanner examines Git history available to the checkout.
  - `uv export --frozen --all-extras` feeds a pinned CycloneDX tool, producing `sbom.cdx.json`.
  - the audit JSON and SBOM are uploaded even when findings fail a later step, while tool execution failures remain fatal.

  Add the pin checker to the fast CI job and release evidence workflow.

- [ ] **Step 5: Verify locally**

  Run:

  ```powershell
  uv run --frozen python -m pytest tests/unit/test_check_actions_pinned.py -q --tb=short
  uv run --frozen python scripts/check_actions_pinned.py
  uv lock --check
  ```

  Expected: all PASS; every remote `uses:` reference is a 40-hex SHA.

- [ ] **Step 6: Commit**

  ```powershell
  git add SECURITY.md .github scripts/check_actions_pinned.py tests/unit/test_check_actions_pinned.py CONTRIBUTING.md
  git commit -m "security: add Core 1.0 automated supply-chain gates"
  ```

### Task 5: Freeze the Core 1.0 public benchmark and v0.36.1 result

**Files:**
- Create: `benchmarks/release/__init__.py`
- Create: `benchmarks/release/core_v1.py`
- Create: `benchmarks/release/core_v1_protocol.json`
- Create: `benchmarks/release/compare_core_v1.py`
- Create: `benchmarks/release/results/v0.36.1.json`
- Create: `tests/eval/test_core_v1_release_benchmark.py`
- Create: `docs/benchmark/core-v1.md`

**Interfaces:**
- Produces: `core_v1.main(argv: list[str] | None = None) -> int`, runnable against an installed HL-Mem wheel with `--dataset`, `--label`, `--commit`, and `--output`.
- Produces: `compare_core_v1.compare(baseline: Mapping[str, Any], candidate: Mapping[str, Any], protocol: Mapping[str, Any]) -> list[str]`.
- Consumes only stable REST endpoints, `Settings.for_test()`, synthetic public data, and fake providers; it makes zero external calls.

- [ ] **Step 1: Write failing protocol and comparator tests**

  ```python
  def test_comparator_rejects_regression_forbidden_hits_and_latency() -> None:
      failures = compare(baseline(), candidate(recall_at_5=-0.02, forbidden_hits=1, p95_ms=999), protocol())
      assert any("recall_at_5" in item for item in failures)
      assert any("forbidden" in item for item in failures)
      assert any("p95" in item for item in failures)

  def test_runner_records_package_commit_and_protocol_hash(tmp_path: Path) -> None:
      output = tmp_path / "result.json"
      assert core_v1.main(["--label", "test", "--commit", "a" * 40, "--output", str(output)]) == 0
      result = json.loads(output.read_text(encoding="utf-8"))
      assert result["package_version"]
      assert result["commit"] == "a" * 40
      assert len(result["protocol_sha256"]) == 64
      assert result["external_model_calls"] == 0
  ```

- [ ] **Step 2: Implement and freeze the protocol before any candidate run**

  The runner starts a temporary database, inserts each public Claim through `POST /v1/memories`, queries through `POST /v1/recall`, binds returned IDs, and records Recall@1/5, MRR, hard/soft no-answer precision/recall, forbidden hits, HTTP success, p50/p95 latency, Python/package versions, dataset/protocol hashes, and external model call count.

  The new protocol freezes these independent release rules:

  ```json
  {
    "protocol_version": "hl-mem-core-1.0-public-v1",
    "baseline_tag": "v0.36.1",
    "max_metric_regression": 0.01,
    "required_forbidden_hits": 0,
    "required_http_success_rate": 1.0,
    "p95_limit": "max(baseline_ms + 150, baseline_ms * 1.25)",
    "required_external_model_calls": 0
  }
  ```

  Commit the protocol code before producing the candidate result so later changes are visible in Git history.

- [ ] **Step 3: Build and measure the immutable v0.36.1 tag**

  Create a temporary worktree for `v0.36.1`, build its wheel, install that wheel into an isolated benchmark venv, and run the committed benchmark module from the Phase 6 repository so `hl_mem` resolves only from the installed old wheel. Record tag `2dbb6a9`, package `0.36.1`, environment data, and exact hashes.

  Run the result twice and byte-compare after excluding measured latency fields; functional metrics and hashes must match. Commit the first complete result as `benchmarks/release/results/v0.36.1.json`.

- [ ] **Step 4: Verify and commit**

  Run:

  ```powershell
  uv run --frozen python -m pytest tests/eval/test_core_v1_release_benchmark.py -q --tb=short
  uv run --frozen python -m benchmarks.release.core_v1 --label current-pre-rc --commit (git rev-parse HEAD) --output Temp/core-v1-current.json
  ```

  Expected: tests PASS; the run reports 32 cases, zero forbidden hits, HTTP success 1.0, and zero external model calls.

  Commit:

  ```powershell
  git add benchmarks/release tests/eval/test_core_v1_release_benchmark.py docs/benchmark/core-v1.md
  git commit -m "bench: freeze the Core 1.0 public protocol"
  ```

### Task 6: RC version, support policy, checklist, and candidate result

**Files:**
- Modify: `scripts/check_docs_consistency.py`
- Modify: `tests/unit/test_check_docs_consistency.py`
- Modify: `src/hl_mem/__init__.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `AGENTS.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/HANDOFF.md`
- Modify: `docs/architecture.md`
- Modify: `docs/capability-matrix.md`
- Modify: `docs/configuration.md`
- Create: `docs/support.md`
- Create: `docs/release-checklist.md`
- Create: `benchmarks/release/results/v1.0.0rc1.json`

**Interfaces:**
- Produces: document/version consistency support for PEP 440 RC versions such as `1.0.0rc1` without weakening stable-version parsing.
- Produces: a checked-in candidate benchmark that is directly comparable to `v0.36.1` under the frozen protocol.

- [ ] **Step 1: Write failing RC-version consistency tests**

  Add tests proving `latest_changelog_entry()` accepts `## v1.0.0rc1`, version extraction preserves `rc1`, and stable `1.0.0` still parses. Run:

  ```powershell
  uv run --frozen python -m pytest tests/unit/test_check_docs_consistency.py -q --tb=short
  ```

  Expected before implementation: FAIL because current regexes accept only three numeric components.

- [ ] **Step 2: Make RC version parsing exact**

  Use one shared regex fragment equivalent to `\d+\.\d+\.\d+(?:rc\d+)?`; do not accept arbitrary suffixes. Update badge/body/changelog consistency checks to compare the complete version.

- [ ] **Step 3: Set `1.0.0rc1` and close release documentation**

  Update both version SSOTs and every document checked by `check_docs_consistency.py`. Add a top changelog entry summarizing Phase 1-6 behavior and compatibility impact. `docs/support.md` states:

  - current `1.x` and current RC receive security fixes;
  - `0.x` is migration-only and receives no new fixes after final 1.0;
  - Python 3.12-3.14 are tested;
  - SQLite is authoritative; external Graph/PostgreSQL/HA are unsupported;
  - Provider plugins are trusted in-process code, not a sandbox.

  `docs/release-checklist.md` lists every required workflow/evidence link, immutable tag/version checks, backup restore, public benchmark comparison, security results, SBOM, seven observation dates, and final promotion rules. It contains no pre-checked boxes or fabricated URLs.

- [ ] **Step 4: Run and compare the RC candidate**

  Run the frozen benchmark against the current worktree package, write `v1.0.0rc1.json`, then compare:

  ```powershell
  uv run --frozen python -m benchmarks.release.core_v1 --label v1.0.0rc1 --commit (git rev-parse HEAD) --output benchmarks/release/results/v1.0.0rc1.json
  uv run --frozen python -m benchmarks.release.compare_core_v1 benchmarks/release/results/v0.36.1.json benchmarks/release/results/v1.0.0rc1.json
  ```

  Expected: comparator PASS, zero forbidden hits, zero external model calls, no gated metric regression over 0.01, and P95 within the frozen formula.

- [ ] **Step 5: Verify docs/package and commit**

  Run:

  ```powershell
  uv run --frozen python scripts/check_docs_consistency.py
  uv run --frozen python -m pytest tests/unit/test_check_docs_consistency.py tests/eval/test_core_v1_release_benchmark.py -q --tb=short
  uv run --frozen python -m build
  uv run --frozen python scripts/check_wheel_contents.py --reject-v030 dist/*.whl
  ```

  Expected: all PASS and artifacts are named `hl_mem-1.0.0rc1...`.

  Commit:

  ```powershell
  git add scripts/check_docs_consistency.py tests/unit/test_check_docs_consistency.py src/hl_mem/__init__.py pyproject.toml README.md README_EN.md AGENTS.md docs benchmarks/release/results/v1.0.0rc1.json
  git commit -m "release: prepare HL-Mem 1.0.0rc1"
  ```

### Task 7: Enforced seven-day RC observation

**Files:**
- Create: `scripts/check_rc_observation.py`
- Create: `tests/unit/test_check_rc_observation.py`
- Create: `.github/workflows/rc-observation.yml`
- Create: `.github/ISSUE_TEMPLATE/bug.yml`
- Create: `.github/ISSUE_TEMPLATE/feature.yml`
- Create: `.github/ISSUE_TEMPLATE/config.yml`
- Modify: `docs/release-checklist.md`
- Modify: `CONTRIBUTING.md`

**Interfaces:**
- Produces: `check_rc_observation.evaluate(release: Mapping[str, Any], artifacts: Sequence[Mapping[str, Any]], issues: Sequence[Mapping[str, Any]], now: datetime) -> list[str]` as a pure policy function.
- Produces: CLI mode that uses GitHub REST only when given `--repository`, `--tag`, and `--token-env GITHUB_TOKEN`.
- Consumes observation artifact names `rc-observation-<tag>-YYYY-MM-DD` and issue labels `priority:P0` / `priority:P1`.

- [ ] **Step 1: Write failing policy tests with frozen timestamps**

  Cover: six days rejected; seven consecutive UTC dates accepted; a missing date rejected; an open P0/P1 rejected; a release younger than 168 hours rejected; artifact tag/commit mismatch rejected.

  ```python
  def test_seven_green_consecutive_days_pass() -> None:
      failures = evaluate(release(age_hours=169), artifacts_for_dates("2026-09-01", 7), [], NOW)
      assert failures == []
  ```

- [ ] **Step 2: Implement fail-closed observation validation**

  Each artifact JSON contains `tag`, `commit`, `utc_date`, `run_url`, `quality_smoke`, `public_recall`, `migration`, and `security` with value `passed`. Duplicate dates do not replace missing dates. The CLI refuses a draft/non-prerelease GitHub release, mutable tag mismatch, absent artifact payload, non-success workflow run, and open P0/P1 issue created since RC publication.

- [ ] **Step 3: Add daily immutable-tag observation workflow**

  On schedule and manual dispatch, discover the latest GitHub prerelease matching `v1.0.0rc*`, check out that exact tag, run quality smoke, public recall, migration release gate, and security pin/lock checks without model credentials, write one dated JSON payload, and upload it as `rc-observation-<tag>-<UTC date>`.

  The workflow never moves a tag and never publishes a final release.

- [ ] **Step 4: Add issue intake and reset policy**

  The bug template requests version, reproducible steps, data/migration impact, and redacted logs; the feature template separates stable-contract impact from optional behavior. Repository config directs security reports to `SECURITY.md`. Document that any production code/config/schema/migration/stable-contract fix requires `rc2` or later and restarts day 1; documentation-only corrections may stay on the same candidate only if the tagged artifacts are unchanged.

- [ ] **Step 5: Verify and commit**

  Run:

  ```powershell
  uv run --frozen python -m pytest tests/unit/test_check_rc_observation.py -q --tb=short
  uv run --frozen python scripts/check_actions_pinned.py
  ```

  Expected: all PASS.

  Commit:

  ```powershell
  git add scripts/check_rc_observation.py tests/unit/test_check_rc_observation.py .github/workflows/rc-observation.yml .github/ISSUE_TEMPLATE docs/release-checklist.md CONTRIBUTING.md
  git commit -m "ci: enforce the seven-day RC observation window"
  ```

### Task 8: Final local release gate, integration, and external handoff

**Files:**
- Modify: `docs/superpowers/plans/2026-08-30-hl-mem-core-1-0-roadmap.md`
- Verify: all Phase 6 files and release artifacts

**Interfaces:**
- Produces: a clean local `main` at a verified `1.0.0rc1` candidate commit.
- Does not produce remote state; pushing/tagging/releasing is a separate authorized action after this local gate.

- [ ] **Step 1: Run formatting, static, architecture, contract, and security-policy gates**

  ```powershell
  uv run --frozen ruff check .
  uv run --frozen black --check .
  uv run --frozen isort --check-only --gitignore .
  uv run --frozen mypy src
  uv run --frozen python scripts/check_complexity_budget.py --ratchet
  uv run --frozen python scripts/check_imports.py
  uv run --frozen python scripts/check_config_schema_snapshot.py
  uv run --frozen python scripts/check_provider_plugin_api.py
  uv run --frozen python scripts/check_openapi_snapshot.py
  uv run --frozen python scripts/check_mcp_snapshot.py
  uv run --frozen python scripts/check_docs_consistency.py
  uv run --frozen python scripts/check_actions_pinned.py
  ```

  Expected: every command exits 0.

- [ ] **Step 2: Run public benchmark, release operations, and full tests**

  ```powershell
  uv run --frozen python -m tests.eval.ci_gate
  uv run --frozen python -m benchmarks.release.compare_core_v1 benchmarks/release/results/v0.36.1.json benchmarks/release/results/v1.0.0rc1.json
  uv run --frozen python -m pytest tests/release tests/unit/test_request_size_limit.py tests/unit/test_backup_cli.py tests/integration/test_provider_plugin_wheel.py -q --tb=short
  uv run --frozen --extra sqlite-vec python -W error::ResourceWarning -m pytest tests/ -q --tb=short --cov=hl_mem --cov-report=term --cov-fail-under=80
  uv run --frozen python -m pytest benchmarks/archive/v030/tests -q --tb=short
  ```

  Expected: all PASS, no ResourceWarning, no required recall skip, and coverage at least 80%.

- [ ] **Step 3: Build, inspect, and clean-install the exact wheel**

  ```powershell
  uv run --frozen python -m build
  uv run --frozen python scripts/check_wheel_contents.py --reject-v030 dist/*.whl
  ```

  Install the wheel into an empty Python 3.12, 3.13, and 3.14 environment outside the repository checkout. In each, verify `import hl_mem`, `hl-mem --version`, `hl-mem init --help`, `hl-mem doctor --help`, `hl-mem config migrate --help`, and `hl-mem eval --help`. Verify `benchmarks` cannot be imported from the wheel.

- [ ] **Step 4: Close the local roadmap and commit**

  Mark Phase 6 `Local RC complete; remote publication and seven-day observation require explicit authorization`. Do not mark final 1.0 complete.

  ```powershell
  git add docs/superpowers/plans/2026-08-30-hl-mem-core-1-0-roadmap.md
  git commit -m "docs: close the local Core 1.0 RC gate"
  git diff 3affd2b --check
  git status --short
  ```

  Expected: the user's draft is the only untracked main-worktree file and is absent from all commits.

- [ ] **Step 5: Merge locally and reverify**

  Fast-forward `codex/core-1-0-phase-6` into local `main`, rerun the strict full suite plus public benchmark comparator, then remove only `.worktrees/core-1-0-phase-6` and delete the merged branch. Do not push.

- [ ] **Step 6: Request the one external authorization that remains**

  Report the candidate commit, complete local evidence, benchmark comparison, and residual fact that final `1.0.0` cannot exist before real time passes. Ask once for permission to push `main`, create signed tag `v1.0.0rc1`, and create the GitHub prerelease. After authorization, run the release workflows, observe seven consecutive UTC days, and promote to `1.0.0` only if `check_rc_observation.py` passes and no reset condition occurred.

## Self-Review Checklist

- [ ] Every fixed Phase 6 deliverable maps to a task: 80% coverage, public recall, fast/full CI, Python matrix, security/SBOM, frozen benchmark, operational evidence, RC, observation, and final promotion policy.
- [ ] No private fixture, API credential, paid call, graph feature, plugin expansion, or production behavior is added.
- [ ] Public benchmark thresholds are new and frozen before candidate execution; no C-series result is treated as release evidence.
- [ ] Workflow evidence comes from executable commands and parsed results, not tests that merely search prose.
- [ ] The only unavoidable pause is the real seven-day RC window after separately authorized remote publication.
