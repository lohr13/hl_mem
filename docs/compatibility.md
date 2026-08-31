# Compatibility Policy

## 1.x policy

This section is the binding compatibility policy for HL-Mem releases in the `1.x` series. Semantic Versioning governs
all `1.x` releases.

### Stable contracts

Stable REST, MCP, CLI, configuration schema, import/export, backup format, and Provider Plugin API contracts remain
backward-compatible within `1.x`. Removing a stable contract or changing it incompatibly requires the next major
version. Minor and patch releases may add optional fields or capabilities without breaking existing consumers.

The committed `docs/config-schema.json` snapshot freezes schema v1 TOML paths, production choices, required fields,
secret environment names, retired paths, and the open `plugins.<id>` namespace. A deliberate change must update the
snapshot through `scripts/check_config_schema_snapshot.py --write` and follow the policy below.

### Deprecation and migration notice

Any planned incompatible change to or removal of a stable REST endpoint or field, MCP tool or input, CLI command or flag,
configuration schema key, import/export format, backup format, or public Provider Plugin API member must first be
deprecated in a `1.x` release, whether or not a replacement exists. The deprecated contract remains functional for the
rest of `1.x`; the incompatible change or removal may take effect only in the next major version.

The deprecating release must mark the relevant contract surface and changelog, explain the compatibility impact and
next-major plan, and provide migration guidance. It must name the supported replacement when one exists; otherwise it must
state that there is no replacement and describe the required consumer or operator action. Notices appear in the relevant
OpenAPI metadata, CLI help or runtime warnings, configuration diagnostics, MCP contract documentation, import/export or
backup format documentation, and Provider Plugin API documentation or manifests, as those surfaces support.

### Beta and experimental contracts

Beta and experimental contracts may change only in a minor release, provided the changelog describes the change and
includes migration instructions. Experimental contracts have no compatibility window and must be visibly marked as
experimental wherever they are exposed.

### SQLite upgrades and rollback

Internal SQLite tables are not a public SQL API. SQLite migrations are immutable after release and forward-only, and
supported forward migrations preserve application-managed data.

The Core 1.0 automation migration terminates only pending semantic jobs and abandons pending resurrection tasks that are
disabled by the new defaults; running and terminal jobs are not rewritten. The relation-provenance migration preserves
existing edges as `legacy`. New application-managed edges record `deterministic`, `manual`, or `approved_proposal`
provenance, and Proposal approval creates the edge and marks the Proposal applied in one transaction.

Before an irreversible upgrade, the CLI requires a verified pre-upgrade recovery set: the main database backup, its
backup manifest, the separately preserved authoritative tombstone ledger (`<database>.tombstones.db`), the prior
configuration, and the old binary. The database backup and manifest must pass validation, and the ledger identity and
schema version must match the manifest.

Rollback means placing the authoritative ledger at `<target>.tombstones.db`, restoring the main database backup with its
manifest, and running the restored snapshot with the prior configuration and old binary. It never means downgrading a live
schema or opening an already-migrated database with the old binary. Writes accepted after the upgrade are not replayed
into the restored snapshot and will be absent after rollback.

### Version mismatch behavior

Unknown future configuration versions, unknown future backup versions, and Provider Plugin API major-version mismatches
fail explicitly. HL-Mem does not guess how to interpret an unsupported version.

### Security fixes

Security fixes may tighten input validation immediately, but must not silently change the semantics of stored memories.

The configuration break from `0.x` to `1.x` is handled only by the Phase 2 `hl-mem config migrate` path. This policy does
not promise indefinite aliases for `0.x` configuration names or behavior.

## Historical 0.x policy (nonbinding)

The remainder of this document records the compatibility policy that applied only to HL-Mem releases in the `0.x`
series. It is retained as historical context and does not weaken or extend the binding `1.x` policy above.

### Versioning

HL-Mem follows Semantic Versioning while pre-1.0. Patch releases preserve supported public contracts. Minor releases may
contain breaking changes when needed, and document them in the changelog.

### REST API

Stable endpoints and fields remain backward-compatible. Beta endpoints may change. A breaking change to a stable endpoint
is deprecated for at least one minor release before removal.

The committed `docs/api-schema.json` snapshot records the current OpenAPI contract. A deliberate contract change must
update that snapshot and the changelog.

### SQLite schema

Schema migrations are forward-only and immutable after release. Downgrades are not supported. Application-managed data is
preserved by supported migrations, but direct SQL access and internal table layouts are not public contracts.

### Configuration

`Settings` and `docs/configuration.md` are the canonical non-secret configuration catalog, and `config.example.toml`
provides a deployable example. `.env.example` contains only the four provider credential names; arbitrary `HL_MEM_*`
environment variables do not configure the application. Stable TOML keys and credential names may be renamed only with a
deprecation alias. Beta and experimental keys may change as documented in the changelog.

### Import and export

JSONL exports include a string `format_version`. Readers maintain backward compatibility with versioned formats and the
legacy unversioned event-only JSONL format. Unsupported future versions fail with an explicit error.

Backup manifests have an independent numeric `format_version`; restore validates both that version and the checksum.

### Audit log

Audit-log field schemas are stable within a major version. New optional fields may be added without a compatibility window.

### MCP tools

MCP tool names and required core parameters are stable. Optional parameters may be added. The committed
`docs/mcp-tools.json` snapshot records the current tool contract.

### OpenAPI and MCP consistency

REST and MCP reuse the same application services, but their transport schemas are independent public contracts:
`docs/api-schema.json` freezes generated OpenAPI, while `docs/mcp-tools.json` freezes MCP tool names and input schemas.
A change to shared request semantics must review both snapshots and run both `check_openapi_snapshot.py` and
`check_mcp_snapshot.py`; updating one snapshot never implies that the other contract changed. This keeps transport-specific
differences explicit while preventing shared business behavior from drifting silently.

### Deployment contract evidence

Starting with v0.29.0, `/healthz` publishes static major versions for the daemon contract, required Hermes plugin contract,
and Context Packet wire schema. The packaged Hermes plugin carries the matching `contract.json`. `hl-mem doctor` compares
the observed daemon and installed plugin against those build-time constants: an offline daemon is a warning, while missing
evidence or a major mismatch is a failure. These checks are read-only release evidence, not a dynamic negotiation protocol.

Migration 049 is an irreversible removal of the legacy `claims_tags_fts` projection. It fails before any drop when the
database schema contains another view or trigger that references that table, and requires the preceding 047/048 migration
evidence. Schema inspection cannot discover external SQL clients, so operators must separately verify that every runtime is
v0.29.0 or newer and close the old-binary rollback window before opening a database with v0.29.1.
