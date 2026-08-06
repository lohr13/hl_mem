# Contributing to HL-Mem

感谢你帮助改进 HL-Mem。提交应聚焦单一问题，并保持本地优先、证据可追溯和向后兼容的设计原则。

## Development setup

需要 Python 3.11+、Git 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone git@github.com:lohr13/hl_mem.git
cd hl_mem
uv sync --extra dev
cp .env.example .env
```

默认测试使用 fake providers，不需要外部 API key。仅在运行真实 Provider 检查时配置 `.env`，不要提交凭据、数据库或生成的报告。

## Tests and checks

提交前运行与改动范围匹配的检查；行为变更应包含回归测试。

```bash
# Full suite
.venv/Scripts/python.exe -m pytest tests/ -q --tb=short

# Unit tests
.venv/Scripts/python.exe -m pytest tests/unit/ -q --tb=short

# Formatting and import order
uv run black --check --line-length 120 src tests
uv run isort --check-only --profile black src tests

# Architecture import boundaries
uv run python scripts/check_imports.py
```

## Code style

- 使用 Python 3.11+ 和完整的 PEP 484 类型标注。
- Black 行宽为 120；isort 使用 `profile=black`。
- 模块、类和公开函数使用中文 docstring；复杂逻辑使用简洁中文注释。
- `domain/` 与 `core/` 保持纯净，不反向依赖 `storage/`、`api/` 或 `workers/`。
- 运行时模型、端口、路径、超时和密钥必须来自设置、配置文件或环境变量。
- 外部 API 必须设置超时和 retry，并提供可操作的错误信息。
- 已发布 migration 不可修改；schema 变化应新增顺序 migration。

## Pull requests

1. 从最新 `main` 创建短生命周期分支。
2. 一个 PR 只解决一个问题；避免无关重构和格式噪声。
3. 更新相关文档、CHANGELOG 和能力矩阵，并添加或更新测试。
4. 在 PR 描述中说明动机、实现方式、风险、验证命令和结果。
5. 确认没有提交密钥、运行数据库、缓存或 `var/` 数据。
6. 响应评审意见；涉及行为变化时补充可复现证据。

提交信息使用英文 Conventional Commits，例如：

```text
feat(recall): add bounded query expansion
fix(storage): preserve temporal visibility
docs: clarify provider configuration
```
