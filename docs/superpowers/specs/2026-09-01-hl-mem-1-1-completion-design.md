# HL-Mem 1.1.0 Completion Design

## 1. Goal and baseline

HL-Mem 1.1.0 keeps the completed entity-recall, Provider, observability,
plugin-validation, hotspot-refactoring, and retired-surface work at commit
`80b9374`. This final increment adds three product features and a small release
hardening set before publishing 1.1.0:

1. source trust and turn-taint propagation;
2. session-kind admission control;
3. `hl-mem explain claim`;
4. Query Expansion and Hermes deployment hardening.

The design must preserve 1.x compatibility, introduce no new model calls, and
avoid a second provenance store. Provenance describes where information came
from; it is not fact verification, authorization, or a security principal.

## 2. Scope already complete

The following 1.1 work is complete and is not rebuilt in this increment:

- high-confidence entity scope is pushed below FTS and Dense candidate limits;
- real LLM, Embedding, and Reranker smoke evidence is budgeted and recorded;
- the external DashScope reference plugin validates the Provider Plugin API;
- `hl-mem ops report` exposes bounded Provider and runtime health data;
- Recall and Extraction responsibilities have been separated without changing
  their public facades;
- retired PostgreSQL probe, extraction pre-filter, and independent Tag-channel
  surfaces have been removed while migration recognition remains.

## 3. Shared provenance data model

Migration `060` adds exactly two columns to `events`:

| Column | Values | Default |
| --- | --- | --- |
| `origin_class` | `direct_user`, `agent`, `external`, `external_derived`, `system`, `unknown` | `unknown` |
| `session_kind` | `interactive`, `cron`, `heartbeat`, `subagent`, `unknown` | `unknown` |

Both columns are non-null closed enums enforced at the API/domain boundary and
by SQLite checks. Existing rows remain `unknown`; migration does not guess from
`actor_type`, content, or old session identifiers.

The implementation reuses:

- `events.source_uri`, `occurred_at`, `recorded_at`, `actor_type`, and
  `metadata_json`;
- `evidence_links` as the durable Claim-to-Event lineage;
- `claims.assertion_kind`, `source_authority`, `scope`, `observed_at`, and
  `expires_at` as the existing use-policy projection.

It does not add `turn_tainted`, a Claim provenance copy, a provenance table, a
Graph edge, an index, or an LLM extraction field.

One configuration value controls behavioural rollout:

```toml
[provenance]
mode = "enforce" # enforce | observe
```

`observe` records and explains provenance while retaining legacy Claim
admission. `enforce`, the 1.1 default, also applies the rules below. There is no
`off` mode because retaining caller-supplied provenance is lossless and does
not invoke external services.

## 4. Feature 1: source trust and turn-taint propagation

### 4.1 Host-owned classification

The host, not the Extractor, supplies provenance. Generic Event clients may set
the two fields explicitly. Missing or unrecognised metadata becomes `unknown`
and follows legacy behaviour.

Hermes 0.20.5 already supplies two deterministic signals:

- provider initialisation includes `platform`, allowing explicit interactive
  and cron classification;
- completed-turn `messages` include tool messages and Hermes' internal
  `_tool_output_risk` marker for web, browser, and untrusted MCP output.

The HL-Mem adapter scans backwards only to the latest user boundary. If the
current turn contains an explicitly risk-marked tool result, the final
Assistant Event is `external_derived`; otherwise it is `agent`. The user Event
is `direct_user` only for a known interactive session and `system` for a known
cron session. Unsupported contexts stay `unknown`.

The adapter continues to persist the existing user and assistant Events. It
stores only a bounded, sanitised list of external tool names in
`metadata_json`; it does not duplicate raw tool output or parse URLs from model
text. No Hermes core modification or private Hermes import is required.

### 4.2 Claim policy

The Extractor continues to answer only "what was stated" and retains its
current prompt, schema, token budget, and `source_event_indices`. After
extraction, a pure provenance policy consumes the referenced source Events.

In `enforce` mode:

| Provenance | Claim handling |
| --- | --- |
| interactive `direct_user` | current admission and retention behaviour |
| interactive `agent` without external evidence | current Assistant behaviour |
| `external` or `external_derived` | may be stored; authority is low; `inference` stays inference and other assertions become observations; retention is temporal unless the existing explicit-memory protection applies |
| `system` | low-authority temporal observation/inference |
| `unknown` | exact 1.0 behaviour |

Any referenced external evidence activates the external rule, so an Assistant
restatement cannot launder it. Direct user evidence takes precedence over a
plain Agent restatement only when no external evidence is present. Explicitly
asking to remember an external result may protect retention, but never changes
its external origin or marks the statement verified.

The feature does not automatically check truth, ask the user to confirm each
fact, or add source authority as a new recall-ranking factor.

## 5. Feature 2: session-kind admission control

Session kind is a separate decision from information origin:

| Session | Behaviour in `enforce` mode |
| --- | --- |
| `interactive` | source rules above; ordinary user memory remains unchanged |
| `cron` | Events remain durable; extracted information is limited to low-authority temporal observation/inference |
| `heartbeat` | Event and audit remain; no automatic durable Claim is created |
| `subagent` | Event and audit remain; no automatic durable Claim is created; the parent/delegation path owns promotion |
| `unknown` | exact 1.0 behaviour |

Current Hermes automatically supplies interactive and cron through its
initialisation context. Hermes subagents already skip memory-provider syncing;
the explicit value still protects REST and future host integrations.
Heartbeat and subagent are never inferred from message prose or session-ID
patterns.

The gate operates at both initial admission and deferred execution so queued
work cannot bypass a later policy check. It does not delete, reclassify, or
expire historical Claims.

## 6. Feature 3: explainable Claim command

The stable read-only surface is:

```text
hl-mem explain claim <claim-id>
hl-mem explain claim <claim-id> --json
```

It reports:

- Claim status, assertion kind, authority, observed/expiry times, and current
  supersession relationship;
- direct Evidence links and each Event's origin class, session kind, and
  occurrence time;
- a credential-stripped source hint when `source_uri` is present;
- the current provenance policy interpretation and resulting use limits.

The command explains the current persisted state. It does not claim to
reconstruct a historical admission reason after bounded audit records have
expired. It never prints raw Event content, tool output, secrets, query strings,
URL fragments, or configuration values.

The recall enrichment path performs one bounded batch query for selected Claim
IDs. Existing open evidence dictionaries gain origin/session/time/source-hint
keys, so no top-level Context Packet shape or schema-major change is required.
The Hermes renderer prepends a compact caution only for external or automated
memories; direct-user and legacy-unknown items render exactly as before.

## 7. Release hardening

### 7.1 Disabled Query Expansion is inert

When `recall.query_expansion_mode="off"`, dedicated Query Expansion fields are
parked configuration. Validation and component construction ignore incomplete
or unsupported provider-line values and create no client. Active modes retain
fail-closed validation. Migration preserves parked values.

### 7.2 Hermes environment ownership is explicit

Hermes reads `<HERMES_HOME>/hl_mem.toml` and `<HERMES_HOME>/.env`; the general
CLI/server reads its explicitly selected or working-directory files.
`hermes install/upgrade` prints the exact target paths, warns when they are
missing, and prints the matching `doctor` command. It never copies secrets or
compares secret values between environments.

### 7.3 Runtime identity closes the restart blind spot

The loaded plugin atomically records a non-secret status document at
`<HERMES_HOME>/state/hl_mem-runtime.json` containing package version, resolved
source path, editable Git commit when available, PID, load time, registration
status, attempt time, bounded failure count, and safe exception type.

`doctor` compares that captured identity with its current package identity. A
failed registration or mismatch fails with an explicit gateway-restart action;
missing status warns; malformed status fails. Diagnostic writes remain
best-effort and never hide the original registration outcome. No process scan,
log scrape, heartbeat service, source-tree hash, or automatic restart is added.

## 8. Compatibility, privacy, and cost

- Event request fields are additive and default to `unknown`.
- Existing databases upgrade forward through migration `060`; rollback uses a
  pre-upgrade database/config backup with HL-Mem 1.0.0.
- REST, MCP, Provider Plugin API, backup format, and `ops report` remain
  compatible.
- Source URI rendering strips userinfo, query, and fragment; raw tool content
  never enters provenance metadata or the explanation command.
- Provenance adds zero LLM, Embedding, or Reranker calls.
- Turn analysis scans one current turn; Context enrichment uses one bounded
  batch query for the delivered Top-K.
- Only external/automated rendered items pay the small source-label token cost.

## 9. Rejected work

1.1 does not add:

- LLM provenance classification, fact checking, or sentence-span lineage;
- manual confirmation as a prerequisite for external observations;
- historical provenance backfill or automatic historical Claim mutation;
- a new recall channel, ranking factor, Graph database, or Graph API;
- an OpenClaw memory adapter;
- a generic admission-policy framework or a new plugin capability;
- secret synchronisation, process management, or a monitoring service.

## 10. Verification gates

### Provenance and session gates

- migration succeeds for empty, 1.0, and current databases; old rows are
  `unknown`;
- Event API rejects invalid enum values and old clients retain legacy
  behaviour;
- current-turn parsing handles direct, external, multiple-tool, short-result,
  unknown, and previous-turn external cases without scanning unrelated turns;
- external-derived evidence cannot become medium/high authority through Agent
  restatement;
- cron, heartbeat, subagent, interactive, and unknown paths match the table;
- `observe` records without changing Claim results;
- no new Provider call or token-accounting entry is produced.

### Explanation and rendering

- human and JSON output are deterministic for active, superseded, expired,
  external, automated, unknown, and missing Claims;
- source hints and runtime-status output pass secret/URL redaction tests;
- Context Packet direct-user and unknown fixtures remain byte-equivalent;
- external/automated items carry a compact caution and no raw tool body.

### Existing product gates

- all exact-entity cases and the frozen Core 1.0 recall comparison pass with no
  forbidden-status or no-answer regression;
- Provider live smoke remains inside its existing hard budget;
- full tests, coverage gate, Ruff, Black, isort, mypy, import boundary,
  complexity ratchet, OpenAPI, MCP, config, Plugin API, migration,
  backup/restore, build, and clean-wheel installation all pass.

## 11. Integration and release sequence

1. Implement release hardening on an isolated short branch and merge it into
   `develop/1.1` after focused and full verification.
2. Implement migration/domain/API/Hermes provenance using tests first.
3. Implement session admission, Context enrichment, renderer changes, and the
   explanation command using tests first.
4. Run all local gates and an independent code review; resolve every P0/P1
   finding before deployment.
5. Back up the live 1.0 database and configuration, install the candidate wheel,
   and run it locally for 24 hours across an interactive turn, an external-tool
   turn, and one cron execution. Inspect jobs, Worker status, WAL, Provider
   usage, and provenance explanations.
6. Merge `develop/1.1` to `main`, push `main`, and wait for the Linux/Python
   GitHub Tests matrix to pass.
7. After explicit final release authorisation, tag the verified commit
   `v1.1.0`, publish PyPI/GitHub Release, and verify a clean PyPI installation,
   version endpoints, migration, extraction, recall, and Hermes registration.

No separate seven-day RC cron or duplicate Release Gates workflow is required.
Any production-semantics fix found during the 24-hour candidate run is applied
and the affected observation is repeated before tagging.

## 12. Completion definition

HL-Mem 1.1.0 is complete only when all three product features are observable in
the shipped product, the hardening checks prevent the known upgrade failures,
existing entity/core recall remains within its frozen gates, no new model call
is introduced, the live candidate run is clean, and the exact commit published
to PyPI is the commit that passed GitHub Tests.
