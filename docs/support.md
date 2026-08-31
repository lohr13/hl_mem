# HL-Mem support policy

## Supported release lines

- The latest stable `1.x` release receives compatibility and security fixes.
- During the 1.0 release process, only the current release candidate receives RC fixes; an older RC is superseded
  immediately by a newer RC or by `1.0.0`.
- After final `1.0.0`, `0.x` is migration-only. It receives no new fixes and is supported only as an input to the
  documented configuration and database upgrade path.
- Python 3.12, 3.13, and 3.14 are the tested runtime matrix.

## Deployment boundary

SQLite is the authoritative store. The supported product does not include PostgreSQL, an external Graph database,
distributed workers, high availability, or multi-tenant isolation. Provider plugins are trusted in-process code; the
allowlist and version negotiation are governance controls, not a security sandbox.

Migrations are forward-only. Recovery uses a verified pre-upgrade database backup, manifest, authoritative tombstone
ledger, prior configuration, and prior binary as described in [compatibility.md](compatibility.md). Opening an upgraded
database with an older binary is unsupported.

For vulnerability reporting and the operational threat boundary, see [SECURITY.md](../SECURITY.md).
