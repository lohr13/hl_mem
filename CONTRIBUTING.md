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

提交前可先运行快速契约门禁：

```powershell
uv run --frozen ruff check .
uv run --frozen python -m tests.eval.ci_gate
uv run --frozen python -m pytest tests/release/test_migration_release_gate.py tests/unit/test_config_loader.py tests/unit/test_provider_registry.py tests/unit/test_request_size_limit.py tests/unit/test_phase5_api_contract.py tests/unit/test_phase5_extraction_contract.py tests/unit/test_phase5_recall_contract.py -q --tb=short
```

快速门禁只提供早期反馈；发布权威仍是 Python 3.12-3.14 全矩阵，以及覆盖率不低于 80% 的完整测试：

```powershell
uv run --frozen --extra sqlite-vec python -W error::ResourceWarning -m pytest tests/ -q --tb=short --cov=hl_mem --cov-report=term --cov-fail-under=80
```

发布候选还必须通过 `Core 1.0 release gates` 工作流。它保留 Python 3.12–3.14、迁移、备份恢复、Provider
冲突、请求流限制、零模型调用、公开召回、依赖审计、SBOM 和干净 wheel 安装的可校验证据。该工作流不发布
PyPI 包。

供应链检查可在本地运行：

```powershell
uv run --frozen python scripts/check_actions_pinned.py
uv lock --check
```

所有第三方 GitHub Actions 必须固定到完整提交 SHA，并在行尾保留对应版本注释。依赖漏洞或疑似密钥泄漏
不得通过忽略失败来绕过；需要例外时必须记录可审计的风险判断和失效日期。

RC 观察从不可变 tag 的发布时间开始。任何生产代码、配置、schema、migration 或稳定契约修复都必须发布
`rc2` 或更高候选并从第 1 天重新观察；只有不改变 tagged artifact 和可执行行为的文档修正可以保留原候选。

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
