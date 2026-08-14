# Quality Trend Infrastructure

## Status and scope

This proposal defines offline quality and operational trend measurement for HL-Mem. It is design-only: no runner,
dataset, workflow, or results file is introduced by this proposal. Full evaluation runs nightly or by manual dispatch,
never as a per-PR requirement. Pull requests run only a deterministic smoke subset of 5–10 cases.

## Goals

- Detect gradual quality, latency, model-cost, migration, and worker-reliability regressions across releases.
- Produce reproducible evidence that can be compared programmatically and reviewed by humans.
- Keep PR feedback fast and stable while moving model-dependent and performance-sensitive evaluation off the critical path.

## Fixed offline dataset

The benchmark uses a committed, versioned dataset manifest. Each dataset release has an immutable identifier, content hash,
schema version, and documented provenance. Cases contain fixed inputs and expected outcomes for retrieval, Chinese FTS,
temporal visibility, supersede decisions, and relation proposals. Secrets and production text are prohibited.

Deterministic fake embedding, reranking, extraction, and clocks are used for deterministic metrics. A separately labelled
external-model profile may measure call count and end-to-end behavior; its results must never be mixed with deterministic
scores. Dataset changes create a new dataset version rather than rewriting prior cases.

## Metrics

Every full run records:

- Recall@5 and Recall@10.
- Mean reciprocal rank (MRR).
- Chinese FTS regression score.
- Temporal query accuracy.
- False supersede rate.
- False relation proposal rate.
- P50 and P95 recall latency.
- External model calls per ingest.
- Migration count and database-size growth.
- Worker Job failure rate and retry rate.

Rate metrics include numerator, denominator, and the resulting ratio. Latencies use a documented warm-up and sample count.
Database growth uses the same seed snapshot and reports both bytes and percentage. Worker metrics use a fixed job workload
with deterministic injected failures.

## Result contract and trend storage

Each run emits one JSON document containing `schema_version`, dataset version and hash, HL-Mem version and commit, UTC
timestamp, execution profile, environment metadata, metric values, sample counts, and failure details. Non-finite numbers
are forbidden. A JSON Schema validates output before publication.

An append-only JSONL results file stores one document per completed run. Existing rows are never edited; invalid runs append
a failed result with diagnostics. Comparisons select runs with the same dataset version and execution profile. Release
reports compare the current run with the latest release and a configurable rolling baseline, displaying absolute and
percentage deltas without automatically claiming statistical significance.

## Automation

A separate nightly GitHub Actions workflow runs the full deterministic suite and optionally the external-model profile when
required secrets are available. It also supports `workflow_dispatch`. The workflow uploads raw JSON and logs as artifacts,
then appends validated results through a dedicated, reviewable update mechanism with least-privilege permissions.

The pull-request workflow runs a committed 5–10-case smoke set covering one case each for basic recall, Chinese FTS,
temporal filtering, supersede safety, relation safety, migration, and worker retry behavior. Smoke thresholds are fixed and
do not depend on network services or historical trend files.

## Failure and governance policy

Nightly infrastructure failures are distinct from metric regressions. Missing secrets skip only the external-model profile.
Dataset, result-schema, metric-definition, or threshold changes require documentation and a version bump. Alerts identify
the affected metric, comparable baseline, delta, dataset version, and artifact link. The first implementation should report
trends without blocking releases; promotion to release gates requires an explicit governance decision backed by stable run
history.
