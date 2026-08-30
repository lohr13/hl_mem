# Compatibility Policy

## 1.x policy

This section is the binding compatibility policy for HL-Mem releases in the `1.x` series. Semantic Versioning governs
all `1.x` releases.

### Stable contracts

Stable REST, MCP, CLI, configuration schema, import/export, backup format, and Provider Plugin API contracts remain
backward-compatible within `1.x`. Removing a stable contract or changing it incompatibly requires the next major
version. Minor and patch releases may add optional fields or capabilities without breaking existing consumers.

### Beta and experimental contracts

Beta contracts may change in a minor release, provided the changelog describes the change and includes migration
instructions. Experimental contracts have no compatibility window and must be visibly marked as experimental wherever
they are exposed.

### SQLite upgrades and rollback

SQLite migrations are immutable after release and forward-only. Before an irreversible upgrade, the CLI requires a
verified backup. Rollback means restoring that backup and using the old binary; it never means downgrading a live,
already-migrated schema.

### Version mismatch behavior

Unknown future configuration versions, unknown future backup versions, and Provider Plugin API major-version mismatches
fail explicitly. HL-Mem does not guess how to interpret an unsupported version.

The configuration break from `0.x` to `1.x` is handled only by the Phase 2 `hl-mem config migrate` path. This policy does
not promise indefinite aliases for `0.x` configuration names or behavior.

## Historical 0.x policy

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
