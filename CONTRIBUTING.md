# Contributing to HL-Mem

感谢你帮助改进 HL-Mem。提交应聚焦单一问题，并保持本地优先、证据可追溯和向后兼容。

## 开发环境

需要 Python 3.12+、Git 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/lohr13/hl_mem.git
cd hl_mem
uv sync --dev
cp .env.example .env
```

默认测试使用 fake provider，不需要外部 API key。真实评测必须显式使用 `real_api` marker；凭据、数据库、缓存、
模型响应和包含个人信息的 gold 不得提交。

源码 checkout 中推荐通过 launcher 运行 Python，避免宿主进程注入不兼容的 `PYTHONPATH`/`PYTHONHOME`：

```bash
bash scripts/hlmem-python.sh -m pytest tests/unit/ -q --tb=short
```

Windows `cmd.exe` 使用 `scripts\hlmem-python.cmd`。

## 提交前检查

先运行与改动范围匹配的 targeted 测试。以下七项与 CI 契约一致：

```bash
bash scripts/hlmem-python.sh -m black --check .
bash scripts/hlmem-python.sh -m isort --check-only src tests scripts evaluation
bash scripts/hlmem-python.sh -m ruff check .
bash scripts/hlmem-python.sh -m mypy src/hl_mem/ --ignore-missing-imports
bash scripts/hlmem-python.sh scripts/check_docs_consistency.py
bash scripts/hlmem-python.sh scripts/check_openapi_snapshot.py
bash scripts/hlmem-python.sh scripts/check_mcp_snapshot.py
```

行为变化必须有回归测试。涉及 OpenAPI 或 MCP 契约的有意变化，先审查 diff，再分别使用对应脚本的
`--update` 更新 snapshot。

构建发布产物后运行 `python scripts/check_wheel_contents.py --reject-v030 dist/*.whl`。稳定的 `hl-mem eval`
必须随 wheel 安装；`benchmarks/archive/` 只为历史复现保留，不属于普通 CI 或发布支持面。

## 代码与数据规则

- Python 使用完整类型标注；Black 行宽 120，isort 使用 Black profile。
- `domain/` 和 `core/` 不反向依赖 `storage/`、`api/` 或 `workers/`。
- 外部 API 必须设置 timeout/retry，并保留可操作、已脱敏的失败信息。
- 已发布 migration 不可修改；schema 变化新增顺序 migration。
- 非敏感配置来自 TOML，密钥只来自 `.env` 或同名进程环境变量。
- synthetic fixture 可以 tracked；真实或个人语料放在 `~/hl_mem_eval_data/`，本地产物放在 `var/eval/`。

## Pull Request

1. 从最新 `main` 创建短生命周期分支。
2. 一个 PR 只处理一个问题，避免无关重构和格式噪声。
3. 说明动机、实现、风险、验证命令和结果。
4. 行为或公共契约变化需同步测试、活文档和 CHANGELOG。
5. 确认没有提交密钥、运行数据库、缓存或私有评测数据。

提交信息使用 Conventional Commit 类型；subject 可使用中文或英文，例如：

```text
feat(recall): add bounded query expansion
fix(storage): 修复冲突组未收敛
docs: 精简评测文档
```
