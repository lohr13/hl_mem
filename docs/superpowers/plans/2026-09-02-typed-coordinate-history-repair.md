# Typed Coordinate and History Repair Implementation Plan

> **For agentic workers:** Use test-driven development and verification-before-completion for every task.

**Goal:** Fix operational-model coordinate drift and safely repair proven obsolete extraction-model history in `v1.1.0rc3`.

**Architecture:** One pure, source-bounded resolver completes typed coordinates before slot validation. Existing ingest conflict machinery owns future state changes. One explicit application service inspects and supersedes only historically proven same-coordinate Claims.

## Global constraints

- Add no LLM, Embedding, Reranker, or image calls.
- Add no migration, table, Worker, configuration key, or latest-wins slot.
- Preserve unresolved Claims instead of guessing.
- Keep TTL expiry unchanged.
- Never mutate production data during tests or release preparation.

### Task 1: Generalize model coordinate resolution

**Files:**
- Modify: `src/hl_mem/ingest/extraction/model_coordinates.py`
- Modify: `src/hl_mem/ingest/llm_extractor.py`
- Modify: `tests/unit/test_extraction_model_coordinates.py`

- [ ] Add failing tests for answering, embedding, and reranking coordinates; ambiguous and arbitrary subjects remain unresolved.
- [ ] Run the focused test and confirm the new cases fail for the expected reason.
- [ ] Implement a closed task registry and one source-bounded resolver; remove duplicated task inference from the compact postprocessor.
- [ ] Run focused extraction, slot, conflict-key, formatting, and typing tests.
- [ ] Commit the resolver independently.

### Task 2: Prove exact-coordinate supersession

**Files:**
- Modify: `tests/unit/test_runtime_config_report.py`
- Modify only if a proven defect exists: current ingest coordinate/conflict modules.

- [ ] Add an integration test with active extraction, answering, embedding, and reranking model Claims.
- [ ] Store a new current extraction Claim and prove only the old extraction Claim is superseded.
- [ ] Prove an unresolved task supersedes nothing.
- [ ] Prefer the existing ingest transaction; add production code only if the red test exposes a real gap.
- [ ] Run conflict, entity, latest-wins, active-Claim, and runtime-projection regressions.

### Task 3: Add count-guarded historical repair

**Files:**
- Create: `src/hl_mem/application/model_coordinate_repair.py`
- Modify: `src/hl_mem/interfaces/cli.py`
- Create: `tests/unit/test_model_coordinate_repair.py`
- Modify: CLI tests and user documentation as needed.

- [ ] Add failing tests for read-only dry-run, exact candidate selection, exclusions, stale expected count, transactional apply, idempotence, and cross-task isolation.
- [ ] Implement the smallest read-only inspection result and transactional apply service using existing repositories.
- [ ] Add `hl-mem coordinates repair-model-history`; default to dry-run and require `--apply --expected-count N --selection-token TOKEN` for mutation.
- [ ] Verify CLI JSON/text output contains IDs and counts but no secrets or sensitive Evidence text.
- [ ] Run focused database, audit, CLI, migration, formatting, typing, and complexity gates.
- [ ] Commit the repair tool independently.

### Task 4: Prepare and publish RC3

**Files:**
- Modify: version sources, `docs/CHANGELOG.md`, architecture/operations documentation, and generated contracts only where required.

- [ ] Set version identity to `1.1.0rc3` and document RC2-to-RC3 behavior.
- [ ] Run the 24-case entity fixture and frozen Core 1.0 comparator; reject recall/no-answer/forbidden-content regressions.
- [ ] Run full tests with strict warnings, Ruff, Black, isort, mypy, complexity, imports, contracts, migrations/restores, build, SBOM/security, and clean wheel install.
- [ ] Obtain an independent code review and fix all material findings.
- [ ] Merge to `main`, rerun focused/full release evidence on the merged SHA, and push `main`.
- [ ] Wait for GitHub Tests and Security for the exact SHA.
- [ ] Create and push annotated `v1.1.0rc3`; wait for Publish and release gates, then verify PyPI wheel/sdist and GitHub release.
