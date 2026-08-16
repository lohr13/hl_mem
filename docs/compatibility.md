# Compatibility Policy

This policy applies to HL-Mem releases in the `0.x` series.

## Versioning

HL-Mem follows Semantic Versioning while pre-1.0. Patch releases preserve supported public contracts. Minor releases may
contain breaking changes when needed, and document them in the changelog.

## REST API

Stable endpoints and fields remain backward-compatible. Beta endpoints may change. A breaking change to a stable endpoint
is deprecated for at least one minor release before removal.

The committed `docs/api-schema.json` snapshot records the current OpenAPI contract. A deliberate contract change must
update that snapshot and the changelog.

## SQLite schema

Schema migrations are forward-only and immutable after release. Downgrades are not supported. Application-managed data is
preserved by supported migrations, but direct SQL access and internal table layouts are not public contracts.

## Configuration

`Settings` and `docs/configuration.md` are the canonical non-secret configuration catalog, and `config.example.toml`
provides a deployable example. `.env.example` contains only the four provider credential names; arbitrary `HL_MEM_*`
environment variables do not configure the application. Stable TOML keys and credential names may be renamed only with a
deprecation alias. Beta and experimental keys may change as documented in the changelog.

## Import and export

JSONL exports include a string `format_version`. Readers maintain backward compatibility with versioned formats and the
legacy unversioned event-only JSONL format. Unsupported future versions fail with an explicit error.

Backup manifests have an independent numeric `format_version`; restore validates both that version and the checksum.

## Audit log

Audit-log field schemas are stable within a major version. New optional fields may be added without a compatibility window.

## MCP tools

MCP tool names and required core parameters are stable. Optional parameters may be added. The committed
`docs/mcp-tools.json` snapshot records the current tool contract.

## OpenAPI and MCP consistency

REST and MCP reuse the same application services, but their transport schemas are independent public contracts:
`docs/api-schema.json` freezes generated OpenAPI, while `docs/mcp-tools.json` freezes MCP tool names and input schemas.
A change to shared request semantics must review both snapshots and run both `check_openapi_snapshot.py` and
`check_mcp_snapshot.py`; updating one snapshot never implies that the other contract changed. This keeps transport-specific
differences explicit while preventing shared business behavior from drifting silently.
