## Summary

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor
- [ ] Documentation
- [ ] CI/Tooling

## Contract checklist

- [ ] REST API changed? → update `docs/api-schema.json` snapshot
- [ ] MCP tools changed? → update `docs/mcp-tools.json` snapshot
- [ ] Config env vars changed? → update `.env.example` + compatibility policy
- [ ] SQLite schema changed? → new migration + snapshot upgrade test
- [ ] Breaking change? → deprecation notice in CHANGELOG

## Test plan

- [ ] `python scripts/check_docs_consistency.py` passes
- [ ] `python scripts/check_imports.py` passes
- [ ] `ruff check src/` passes
- [ ] Existing tests not broken
