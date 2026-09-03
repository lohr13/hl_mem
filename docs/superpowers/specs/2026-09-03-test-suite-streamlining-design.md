# Test Suite Streamlining Design

**Date:** 2026-09-03
**Status:** Approved for implementation planning
**Scope:** Local test workflow, test data sizing, GitHub Actions routing, and README information architecture

## Context

The current offline suite contains 3,280 passing tests but a serial run on the reference Windows host takes about 376 seconds. The larger problem is repeated execution: the same tests run before and after fast-forward merges, across Python 3.12/3.13/3.14, and again in overlapping CI jobs.

Measured hotspots support a targeted change:

- `tests/unit/test_chinese_reader_replay.py`: 142 tests in 24.68 seconds. Several recovery and tamper tests repeatedly materialize the full three-arm, 120-case replay state.
- `tests/integration/test_provider_plugin_wheel.py`: one test in 29.32 seconds. It builds and installs distributions and is currently reached both through the full suite and dedicated CI/release jobs.
- Creating a fresh migrated database averages about 0.139 seconds, but global database-template or production migration changes would add risk and are explicitly out of scope.

The package's existing Python installation range remains unchanged. Python 3.13 becomes the only authoritative CI test and build environment; compatibility with other installable versions is not claimed by CI.

## Goals

1. Run focused tests during development and no more than one core full-suite run for a final candidate, with a second run allowed only after code changes caused by the first run.
2. Reduce the reference-host core full-suite time from about 376 seconds to a measured target of 180–210 seconds.
3. Remove duplicate CI execution while preserving offline business coverage, coverage enforcement, release checks, quality gates, and supply-chain checks.
4. Preserve every reader-replay security and state-machine assertion while reducing unnecessary fixture size and checkpoint I/O.
5. Keep executable changes test-only and workflow-only; documentation may be simplified, but production database and
   memory behavior must not change.
6. Turn both language READMEs back into concise landing pages instead of release-history and operator runbooks.

## Non-goals

- Changing `requires-python` or promising compatibility outside Python 3.13.
- Caching or cloning a pre-migrated database behind global fixtures.
- Deleting validation branches merely to reduce test count.
- Running real provider/model evaluations.
- Introducing dynamic test selection, test-impact analysis, or timing-based CI failure thresholds.
- Deleting detailed architecture, configuration, compatibility, release-history, or evaluation documentation outside the
  two landing-page READMEs.

## Test Tiers

### Focused development tests

Run only test files directly related to the edited code. Focused invocations remain serial so worker startup does not dominate small runs.

### Core full suite

The final candidate command is:

```powershell
.venv\Scripts\python.exe -W error::ResourceWarning -m pytest tests/ `
  -m "not release_only" -n 4 -q --tb=short --durations=25
```

CI adds coverage collection and keeps the existing 80% minimum. Four fixed workers are preferred over `auto` for reproducible resource use. Parallelism is explicit in the full-suite command rather than global pytest `addopts`, so focused tests stay lightweight.

The core suite runs once per final candidate. It may run a second time only if the first run leads to code changes. A fast-forward merge to the exact verified commit is checked by commit identity and clean status, not by another full-suite run.

### Release-only validation

The `release_only` marker covers work that validates packaging or historical upgrade compatibility rather than ordinary business behavior:

- `tests/integration/test_provider_plugin_wheel.py`
- `tests/release/test_migration_release_gate.py`
- `tests/test_migration_upgrade.py`

These tests run serially before a release. Distribution build, wheel-content inspection, and clean-environment installation are also release-only workflow steps. Ordinary PR and `main` CI do not perform them.

Marker collection must be tested so the three intended modules are selected and no unrelated test is silently excluded.

## Reader Replay Test Data

Production replay remains fixed at three extractor arms, 40 cases per arm, and 120 physical reader calls.

Tests retain:

- exact 40-case source/manifest contract coverage;
- one complete `3 x 40` orchestration test that verifies canary ordering, 120 calls, arm ordering, checkpointing, and summary totals;
- all existing transport, privacy, retry, migration, tamper, recovery, and state-transition assertions.

State-machine tests that do not depend on the production count use a test-local small protocol fixture with one or two cases per arm. The fixture may monkeypatch module constants and construct corresponding source identities, but it must not change production function signatures or relax validators in shipped code.

Repeated setup is consolidated into test helpers. Parser boundary cases and materially distinct corrupt-state cases remain parameterized; only identical large fixture construction and checkpoint work is removed.

## Parallel Execution Safety

`pytest-xdist` is added as a development dependency. Core tests run with `-n 4`; release-only tests remain serial.

If parallel execution exposes shared state, the affected test must first reproduce serially and in isolation. A concrete process-global test may receive narrow grouping or serialization. The suite must not silently fall back to serial execution, disable assertions, or add broad ordering constraints.

## GitHub Actions Design

### Ordinary PR and `main` workflow

The existing jobs are consolidated to:

1. **checks** — one Python 3.13 environment runs Ruff, Black, isort, mypy, action pinning, import boundaries, complexity budget, documentation consistency, and configuration/provider/OpenAPI/MCP/operations snapshots.
2. **core-tests** — one Python 3.13 run executes the core suite with four workers, coverage, and the 80% threshold.
3. **quality** — public recall gate and deterministic quality smoke each run once.
4. **state-smoke** — remains isolated because it installs `sqlite-vec` and validates sanitized interpreter-environment boundaries.

The following duplicated jobs or steps are removed: Python 3.12/3.14 matrices, focused pytest subsets already covered by core tests, repeated migration tests, standalone lint/type/format/import/snapshot jobs, duplicate recall gate, provider-wheel PR job, and PR build matrices.

### Release workflow

The release workflow contains:

1. one Python 3.13 core suite with coverage and one authoritative JUnit result;
2. one serial release-only job for historical migrations, external provider wheel behavior, distribution build, wheel inspection, and clean installation;
3. the public recall artifact, dependency audit, SBOM generation, and evidence aggregation.

Evidence aggregation references the single authoritative core result and the distinct release/quality artifacts. It must not require rerunning a test solely to create a separately named JUnit file.

## Documentation and Agent Policy

`AGENTS.md` and the relevant testing documentation will state:

- focused tests during implementation;
- one parallel core run at final-candidate time;
- a second core run only after further code changes;
- no repeat after an identical fast-forward merge;
- release-only checks only before release;
- Python 3.13 as the sole authoritative CI environment without changing package installation metadata.

### README landing pages

`README.md` and `README_EN.md` remain synchronized Chinese and English entry points. Each targets roughly 150 lines and
keeps only the product definition, evidence/data-flow overview, one non-duplicated quickstart, concise source install and
model/MCP/Hermes integration guidance, eight to ten common configuration keys, a compact capability summary, and links to
the maintained specialist documentation.

The READMEs remove version-by-version migration sections, editable-deployment internals, contaminated-host troubleshooting,
Windows supervisor instructions, repair/cleanup command recipes, injection-fixture instructions, historical benchmark
tables, detailed migration inventories, and duplicated project-status prose. Those details are not copied elsewhere during
this change: architecture, configuration, compatibility, changelog, capability matrix, evaluation documentation, and CLI
help remain the authoritative destinations. The landing pages make no new capability, compatibility, or benchmark claim.

## Verification

Implementation verification proceeds in this order:

1. Focused tests for marker selection, replay fixture sizing, workflow policy, and any xdist isolation issue.
2. Serial `release_only` collection and execution once.
3. One final parallel core run with `--durations=25` and coverage.
4. A second final core run only if step 3 causes code changes.
5. Static checks and workflow consistency checks; these do not trigger another pytest full-suite run.
6. Documentation consistency and link-target checks cover both synchronized README landing pages; README-only edits are
   completed before the one permitted post-fix core-suite rerun.

Acceptance requires:

- all intended core and release-only tests accounted for;
- core coverage at least 80%;
- the core suite passes with four workers;
- measured reference-host runtime is reported, with 180–210 seconds as the target rather than a flaky hard gate;
- workflow files contain no Python 3.12/3.14 test or build matrices;
- no duplicated pytest suite or quality command remains across ordinary CI jobs;
- no real API or model call occurs.

If the time target is missed, the implementation reports the measured hotspots and stops. Database-template caching is not added automatically; it requires a separate design decision.

## Risks and Mitigations

- **Parallel-only failures:** reproduce the individual test serially and under xdist, then isolate only the proven shared resource.
- **Accidental coverage loss through markers:** add collection-policy tests and compare selected node IDs/counts before accepting the change.
- **Release evidence drift:** update evidence aggregation to consume the consolidated artifacts and keep schema validation tests.
- **False compatibility signal:** documentation explicitly says Python 3.13 is the CI authority while package installation metadata remains unchanged.
