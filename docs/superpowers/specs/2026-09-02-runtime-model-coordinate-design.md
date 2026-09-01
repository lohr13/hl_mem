# HL-Mem Runtime Model Coordinate Repair

## Goal

Prevent current extraction-model statements from drifting into free-form subjects and empty operational coordinates, then publish the repair as `v1.1.0rc2` without broadening automatic latest-wins behavior.

## Root cause

Compact extraction can correctly classify a statement as `choice.model` while dropping its operational slot because `choice.model` requires a `task` qualifier. The current source-bounded rule accepts a task marker only when the same literal appears in both the generated value and the Evidence quote. A valid model statement can instead put “提取/extraction” in its subject, producing a free-form subject such as `hl-mem 本地提取`, an empty `canonical_slot`, and no `conflict_key`.

The existing typed entity, conflict-key v4, explicit `state_change`, supersede transaction, and audit paths are sufficient once the incoming Claim has an exact project subject and `choice.model/task=extraction` coordinate. Extending version-specific latest-wins to model/provider values would duplicate this machinery and risks closing unrelated reader, judge, benchmark, embedding, and reranker Claims.

## Design

### 1. Source-bounded natural extraction repair

Add one focused pure helper under `ingest/extraction/` that recognizes only the extraction task family. It returns `task=extraction` when:

- the Claim is classified as `choice.model`;
- the Evidence quote explicitly contains one extraction alias (`提取`, `抽取`, `extractor`, `extraction`, or `memory_extraction`); and
- the same task meaning occurs in the Claim subject or self-contained value.

When the subject consists only of a known HL-Mem alias plus bounded extraction/configuration decorators, normalize it to `hl_mem`. Do not normalize arbitrary subjects containing HL-Mem as a substring. When the self-contained value and Evidence explicitly state currentness (`当前`, `现在`, `目前`, `实际使用`, `已切换`, `currently`, `now uses`, or `switched to`), add the existing non-coordinate `state_change=true` signal.

Ambiguous or source-unproven inputs retain the current null-slot behavior. No LLM prompt, schema, token use, database schema, public configuration, or general entity resolver changes.

### 2. Deterministic runtime truth projection

At API lifespan startup, when extraction uses a real/LLM Provider, project the effective `llm.provider` and `llm.model` into one `choice.model` Claim for canonical project `HL-Mem` with:

- `task=extraction`;
- `state_change=true`;
- a non-coordinate Provider field;
- an opaque fingerprint of the non-secret effective route;
- high authority, permanent scope, and observation assertion kind;
- a bounded `runtime_config_report` Event containing no API key or business text.

The projection uses `FakeEmbedder` with the configured dimension, so startup adds zero Provider/LLM/Embedding calls. If an active runtime projection already has the same fingerprint, startup performs no write. A changed route goes through the existing typed rekey, deterministic conflict, supersede, Evidence, and audit transaction. Rollback to a previous route is allowed because only active matching projections are considered idempotent.

Test profiles (`extractor_mode=fake`) do not project runtime configuration.

### 3. Stored data handling

Do not add a content-rewriting migration and do not run raw SQL deletion. On first RC2 startup, the trusted runtime projection creates the correct current coordinate and supersedes any compatible legacy extraction-model Claim that the existing v3-to-v4 rekey can prove. Free-form uncoordinated history remains auditable; exact-entity retrieval prefers the new typed current Claim. Any remaining production-only cleanup must use existing correction/conflict APIs after a backup and a dry-run manifest.

## Safety boundaries

- Do not add `choice.model` or Provider slots to `state.latest_wins_slots`.
- Do not infer a task from an LLM-generated subject without matching Evidence.
- Do not merge evaluation, reader, judge, embedding, reranker, or query-expansion models into the extraction coordinate.
- Do not add a migration, table, worker, configuration key, network call, or model call.
- Do not rewrite or delete historical Claims during package installation.

## Verification

- Red/green tests reproduce `hl-mem 本地提取当前实际使用 glm-5.3-flash` and prove the resulting coordinate is `project:hl_mem / choice.model / task=extraction`.
- Negative cases cover missing Evidence proof, non-HL-Mem subjects, and multiple task meanings.
- Runtime projection tests prove zero Provider calls, idempotent unchanged startup, changed-model supersede, legacy v3 rekey, and rollback occurrence.
- Existing 24-case entity fixture and Core 1.0 recall comparator must remain green with unchanged baseline metrics.
- Full unit/release tests, formatting, typing, complexity, contracts, build, clean install, GitHub Tests, Security, and release gates must pass before the immutable `v1.1.0rc2` tag is pushed.
