# HL-Mem Documentation

Maintained documentation for HL-Mem. Historical task specifications, reviews, and refactor records live under
[`archive/`](archive/) and are not part of the current documentation path.

## Getting Started

- [Quickstart](../README.md#quickstart)
- [Configuration reference](configuration.md)
- [TOML configuration template](../config.example.toml)
- [Secret template](../.env.example)
- [Architecture Overview](architecture.md)

Recommended reading order: start with the root Quickstart, then read Architecture and the Capability Matrix. Use the API
reference while integrating; consult the historical archive only when reconstructing earlier decisions.

## Reference

- [API Endpoints](api.md)
- [MCP stdio setup](mcp.md)
- [v0.20.2 Release Notes](archive/releases/v0.20.2-recall-quality-supervision.md)
- [v0.20.1 Release Notes](archive/releases/v0.20.1-watchdog-observability.md)
- [Changelog](CHANGELOG.md)
- [Capability Matrix](capability-matrix.md)
- [Historical Audit Log Design](archive/design/audit-log-design.md)
- [Historical Memory Management Design](archive/design/memory-management-design.md)

## Design

- [Architecture Decision Records](adr/)
- [Feature Design](design/)
- [Historical Feature Proposals](archive/proposals/)
- [Historical Research Notes](archive/research/)
- [Historical Competitor Comparison](archive/research/competitor-comparison.md)

## Project

- [Handoff Status](HANDOFF.md)
- [Historical Archive](archive/) (including completed plans and one-off reports)

日期化的审计与 benchmark 结果可暂留在文档根目录作为当前运行证据；已完成的执行计划、研究草稿和 Codex
任务说明应移入 `archive/tasks/` 或 `archive/research/`，不作为当前行为的规范来源。

## Maintenance Rules

- Update `CHANGELOG.md` for release-level changes and `HANDOFF.md` for current operational status.
- Add a new ADR instead of rewriting an accepted historical decision.
- Keep endpoint and data-model changes synchronized with `api.md` and `architecture.md`.
- Treat the capability matrix as the source of truth for maturity and default-mode claims.
