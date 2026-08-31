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
- [ ] Python 3.12, 3.13, and 3.14 suites pass with coverage at or above 80%.
- [ ] Empty, historical, and repeated migrations pass.
- [ ] Backup validation and restore pass against a disposable target.
- [ ] External Provider wheel installation and duplicate-ID conflict handling pass.
- [ ] Streaming request limits and default zero-model-call behavior pass.
- [ ] Public recall fixture passes with 100% HTTP success and zero forbidden hits.
- [ ] Clean wheel installation, CLI startup, stable evaluation import, and wheel-content checks pass.
- [ ] `release-evidence.json` and `release-evidence.md` validate every required input hash.

## Benchmark and security

- [ ] RC Benchmark result is committed and compares successfully with `benchmarks/release/results/v0.36.1.json`.
- [ ] Functional Benchmark fields are reproducible; latency remains inside the frozen P95 formula.
- [ ] CodeQL run URL: `________________`.
- [ ] `pip-audit` reports no known vulnerability and its JSON evidence is retained.
- [ ] Gitleaks reports no unreviewed secret and scans the available Git history.
- [ ] The validated CycloneDX SBOM is retained with the release evidence.
- [ ] Any historical credential finding has been rotated or revoked before its fingerprint is baselined.

## Seven-day observation

- [ ] The observation uses the immutable RC tag; no workflow checks out a moving branch as the candidate.
- [ ] Any production code, config, schema, migration, or stable-contract fix created a new RC and restarted day 1.
- [ ] Documentation-only corrections kept the same RC only when tagged artifacts and executable behavior were unchanged.
- [ ] UTC day 1 evidence: `________________`.
- [ ] UTC day 2 evidence: `________________`.
- [ ] UTC day 3 evidence: `________________`.
- [ ] UTC day 4 evidence: `________________`.
- [ ] UTC day 5 evidence: `________________`.
- [ ] UTC day 6 evidence: `________________`.
- [ ] UTC day 7 evidence: `________________`.
- [ ] At least 168 hours have elapsed since RC publication.
- [ ] No P0 or P1 issue opened since RC publication remains unresolved.

## Stable promotion

- [ ] The final observation validator passes against the immutable RC tag and seven consecutive UTC artifacts.
- [ ] Only release metadata and documented RC defects changed during observation; any code change started a new RC.
- [ ] Stable version and changelog updates are reviewed as a separate commit.
- [ ] The stable tag resolves to that reviewed commit.
- [ ] PyPI publication is explicitly authorized and its resulting artifact hashes are recorded.
