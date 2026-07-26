# HL-Mem Documentation

Maintained documentation for HL-Mem. Historical task specifications, reviews, and refactor records live under
[`archive/`](archive/) and are not part of the current documentation path.

## Getting Started

- [Quickstart](../README.md#quickstart)
- [Configuration template](../.env.example)
- [Architecture Overview](architecture.md)

Recommended reading order: start with the root Quickstart, then read Architecture and the Capability Matrix. Use the API
reference while integrating; consult Design, Proposals, and Research when evaluating internals or future work.

## Reference

- [API Endpoints](api.md)
- [Changelog](CHANGELOG.md)
- [Capability Matrix](capability-matrix.md)
- [Audit Log Design](audit-log-design.md)
- [Memory Management Design](memory-management-design.md)

## Design

- [Architecture Decision Records](adr/)
- [Feature Design](design/)
- [Feature Proposals](proposals/)
- [Research Notes](research/)
- [Competitor Comparison](research/competitor-comparison.md)

## Project

- [Completed Milestones and Roadmap](implementation-plan.md)
- [Handoff Status](HANDOFF.md)
- [Historical Archive](archive/)

## Maintenance Rules

- Update `CHANGELOG.md` for release-level changes and `HANDOFF.md` for current operational status.
- Add a new ADR instead of rewriting an accepted historical decision.
- Keep endpoint and data-model changes synchronized with `api.md` and `architecture.md`.
- Treat the capability matrix as the source of truth for maturity and default-mode claims.
