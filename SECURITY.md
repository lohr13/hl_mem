# Security policy

## Supported versions

Security fixes are provided for the latest `1.x` release and the current `1.0` release candidate. An RC stops
receiving fixes when it is superseded by a newer RC or the final `1.0.0` release. Older `0.x` releases are not a
supported security line after `1.0.0` is published.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/lohr13/hl_mem/security/advisories/new). Do not open
a public issue or disclose exploit details before a coordinated fix is available.

Please include the affected version, deployment shape, reproduction steps, impact, and any suggested mitigation.
Maintainers aim to acknowledge a complete report within 72 hours. This is an acknowledgement target, not a
resolution SLA.

## Security boundary

HL-Mem is a local-first service. Operators are responsible for host access, network exposure, TLS termination,
provider credentials, filesystem permissions, and backups. The SQLite database and backup files may contain
sensitive memory content and are not encrypted by HL-Mem; protect them with operating-system controls or encrypted
storage. Do not expose the HTTP, MCP, or worker interfaces to untrusted networks without an appropriate security
gateway.

The project validates its supported application boundaries and dependencies, but it does not claim enterprise
identity, multi-tenant isolation, regulatory compliance, or protection against a compromised host.
