# HL-Mem 1.0 release checklist

This checklist is completed for one immutable RC commit. Evidence must identify the same commit and package version;
do not reuse artifacts from another run.

## Candidate identity

- [ ] The worktree is clean and the reviewed commit is recorded: `________________`.
- [ ] `pyproject.toml`, `hl_mem.__version__`, README badges, changelog, OpenAPI, and built artifact report the same RC.
- [ ] The immutable RC tag resolves to the reviewed commit.
- [ ] The release is marked as a prerelease and is not a draft.

## Required release evidence

- [ ] Core 1.0 release-gates run URL: `________________`.
- [ ] The Python 3.13 core suite passes once with coverage at or above 80%.
- [ ] The serial `release_only` tier passes once, covering empty, historical, and repeated migrations plus the external
  Provider wheel installation and duplicate-ID conflict contract.
- [ ] Core coverage includes backup/restore, streaming request limits, and default zero-model-call behavior without
  rerunning those tests as separate release jobs.
- [ ] Public recall fixture passes with 100% HTTP success and zero forbidden hits.
- [ ] Clean wheel installation, CLI startup, stable evaluation import, and wheel-content checks pass.
- [ ] `release-evidence.json` and `release-evidence.md` validate the six required inputs: `python-3.13`, `release-only`,
  `public-recall`, `pip-audit`, `sbom`, and `wheel-install`.

## Benchmark and security

- [ ] RC Benchmark result is committed and compares successfully with `benchmarks/release/results/v0.36.1.json`.
- [ ] Functional Benchmark fields are reproducible; latency remains inside the frozen P95 formula.
- [ ] CodeQL run URL: `________________`.
- [ ] `pip-audit` reports no known vulnerability and its JSON evidence is retained.
- [ ] Gitleaks reports no unreviewed secret and scans the available Git history.
- [ ] The validated CycloneDX SBOM is retained with the release evidence.
- [ ] Any historical credential finding has been rotated or revoked before its fingerprint is baselined.

## Local deployment observation

- [ ] The immutable RC is deployed with a verified backup and restore set.
- [ ] Real LLM, Embedding, and optional Reranker ingestion-to-recall smoke passes within the approved budget.
- [ ] API, Worker, Hermes, SQLite/WAL, task backlog, Provider failures, and usage cost show no release-blocking defect.
- [ ] Any production code, config, schema, migration, or stable-contract fix creates a new RC; metadata-only promotion does not.

## Stable promotion

- [ ] The exact stable commit passes the normal GitHub Tests workflow before the stable tag is created.
- [ ] Only release metadata changed after the locally observed RC; any executable change created a new RC.
- [ ] Stable version and changelog updates are reviewed as a separate commit.
- [ ] The stable tag resolves to that reviewed commit.
- [ ] PyPI publication is explicitly authorized and its resulting artifact hashes are recorded.
