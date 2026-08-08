# HL-Mem

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Version: 0.24.0](https://img.shields.io/badge/version-0.24.0-blue.svg)](docs/CHANGELOG.md)
[![CI](https://github.com/lohr13/hl_mem/actions/workflows/test.yml/badge.svg)](https://github.com/lohr13/hl_mem/actions/workflows/test.yml)

[中文](#中文) | [English](README_EN.md)

<a id="中文"></a>

## 中文

HL-Mem 是面向 AI Agent 的本地优先、证据驱动长期记忆系统。它不只是向量数据库：系统把不可变事件提取为带证据链的结构化 Claim，以有效时间和记录时间描述事实变化，并通过独立的 Experience 通道保存 Episode、Trace 和可复用 Policy。默认使用 SQLite WAL、FTS5 和向量 BLOB 精确扫描，也可选择 sqlite-vec 后端，无需部署外部数据库服务。

## 五分钟上手

需要 Python 3.11+。先从 PyPI 安装：

```bash
python -m pip install hl-mem
```

在准备存放本地配置和数据库的目录中，生成无需 API key 的离线配置并启动服务：

```bash
hlmem init --offline
hlmem server
```

另开一个终端，写入并召回记忆：

```bash
hlmem remember "Alice 喜欢深色模式"
hlmem recall "Alice 喜欢什么"
```

召回结果会同时给出 Claim ID、分数和证据引用，例如：

```text
[1] Alice 喜欢深色模式
    ID: <claim-id>
    分数: 0.8123
    证据:
      - event/<event-id>
```

`event/<event-id>` 表示这条 Claim 可追溯到对应的不可变原始事件，而不是一段没有来源的模型文本。可用 `hlmem list` 再次查看 Claim ID，并将它用于 `hlmem forget <claim-id>`、REST 详情查询或 MCP 的 `memory_explain`。离线配置是 FTS-only 关键词召回；fake embedding 只保持存储结构兼容，不提供语义检索。

## 进阶安装与集成

### 从源码安装

```bash
git clone https://github.com/lohr13/hl_mem.git
cd hl_mem
uv sync
uv run hlmem init --offline
uv run hlmem server
```

开发环境使用 `uv sync --dev`；安装后可运行 `hlmem doctor` 做只读诊断。SQLite 需要 FTS5，Python 官方发行版通常已包含。

### 启用在线模型

从源码仓库将 `config.example.toml` 复制为本地 `hl_mem.toml`，并按需复制 `.env.example`。把启用组件的独立密钥写入 `.env`：`LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`；再将对应的 `extraction.mode`、`embedding.mode`、`reranker.mode`、`image_describer.mode` 切换到在线模式。完整字段见 [配置参考](docs/configuration.md)。

### 连接 Codex、Claude 与 Cursor

运行 `python -m pip install "hl-mem[mcp]"` 安装 MCP extra 后，可使用官方 SDK 2.x 的 stdio 入口 `hl-mem-mcp` 连接 Codex、Claude Code、Claude Desktop 或 Cursor。配置示例和七个工具的契约见 [MCP 使用说明](docs/mcp.md)。

### 集成 Hermes

先启动 HL-Mem 并确认 `curl --fail http://127.0.0.1:8200/healthz` 成功，再从源码仓库运行：

```bash
uv run python scripts/install_to_hermes.py --hermes-home <HERMES_HOME>
```

插件安装到 `<HERMES_HOME>/plugins/hl_mem/`；完成后必须重启 Hermes。适配器通过本地 HTTP 提供超时、熔断、预取和 Episode/Trace 同步。

### 常驻部署与 systemd

常驻部署使用 `scripts/healthcheck.py` 探测 `/healthz`，将重启和告警交给 systemd、Windows 服务管理器或容器编排平台。systemd 的 `WorkingDirectory` 必须包含 `hl_mem.toml` 和可选 `.env`。

REST 的完整请求契约见 [API 文档](docs/api.md)。

## 关键配置

非敏感配置只从当前工作目录的 `hl_mem.toml` 读取；密钥只从 `.env` 或同名进程环境变量读取。常用键如下，完整列表见
[配置参考](docs/configuration.md)。

| TOML 键 | 代码默认值 | 说明 |
|---|---:|---|
| `database.path` | `var/hl_mem.db` | SQLite 数据库路径 |
| `extraction.mode` | `fake` | 提取器：`fake`、`real` 或 `llm` |
| `embedding.mode` | `fake` | 向量化：`fake` 或 `real` |
| `embedding.text_type` | 未设置 | native 模式可选 `document` 或 `query`；默认不发送 |
| `reranker.mode` | `off` | 重排：`off`、`fake`、`on` 或 `real` |
| `image_describer.mode` | `off` | 图片描述：`off` 或 `on` |
| `llm.provider` | `dashscope` | `dashscope`、`zhipu` 或 `openai_compatible` |
| `llm.structured_mode` | `json_object` | `auto`、`json_object` 或 `json_schema` |
| `index.text_mode` | `natural` | `legacy`、`value_only`、`natural` 或 `answerable`；natural 只拼 subject 与原语言 value |
| `recall.vector_backend` | `sqlite_scan` | `sqlite_scan`（默认）或需安装 `hl-mem[sqlite-vec]` 的 `sqlite_vec` |
| `recall.query_expansion_mode` | `auto` | 多查询召回：`off`、`auto` 或 `always` |
| `relation.discovery_mode` | `off` | 关系发现：`off`、`audit` 或 `auto` |
| `recall.tag_channel_enabled` | `false` | 是否启用独立 Tag 检索通道 |

真实组件和外部调用路径必须提供各自密钥；失败时不会自动切换为 fake。任意 `HL_MEM_*` 环境变量都不再参与应用 `Settings` 配置。
代码默认值与示例部署配置刻意分离：`Settings` 的 `recall.default_limit` / `recall.relevance_reranker_floor` 仍为 `20` / `0.4`，而 `config.example.toml` 显式覆盖为 `5` / `0.15`，并保持 `recall.relevance_keep_top1 = true`。query expansion 使用独立可配置模型，单次/总超时为 5/6 秒。

从 legacy 索引迁移既有数据库时，先只读预览，再显式执行回填；回填会同步 `index_text`、FTS 和 dense embedding，使用 real embedder 的部署需提供对应密钥：

```bash
hlmem backfill-index-text --mode natural --dry-run
hlmem backfill-index-text --mode natural
```

## 能力概览

- **记忆正确性**：幂等事件摄入、事务原子写入、精确/语义去重、确定性冲突规则、LLM 灰区归并和受守卫的冲突终态收敛。
- **提取治理**：6 字段 compact 提取、统一 AdmissionPolicy、完整 Claim schema 后处理、确定性的 scope/predicate 投影、subject 守卫和有界结构化输出修复。
- **时间与证据**：有效时间与记录时间双时间模型、证据链、实体归一化、显式遗忘和 stale 传播。
- **混合召回**：中文 FTS5、两阶段精确向量扫描或可选 sqlite-vec、RRF 融合、多因子排序、可选 Reranker、关系/查询扩展和按 Token 预算打包上下文。
- **生命周期**：importance 联动 TTL、置信度衰减、归档、重分类、反馈效用、审计日志和在线备份。
- **经验通道**：Episode、Trace、Reward、Policy/Procedure 和派生 Observation。
- **接口**：FastAPI REST 与 Hermes Provider 为稳定主路径；七工具 MCP stdio 接口处于 Beta。
- **评测**：离线提取/召回/生命周期指标、LongMemEval-S extract-once/config-compare、50 case 中文记忆测试集、召回诊断和索引文本受控 A/B。

能力成熟度、默认开关和证据见 [能力矩阵](docs/capability-matrix.md)，架构与数据流见 [架构文档](docs/architecture.md)。

## 项目状态

- **Stable**：事件与证据链、原子写入、LLM 提取、Embedding、FTS + Dense + RRF、双时间过滤、TTL/衰减/归档、冲突与去重、REST、Hermes、备份与审计。
- **Beta**：多查询召回、关系候选发现、反馈驱动维护、提取蕴含审计、语义去重审计、MCP Server、Benchmark 与 LongMemEval。
- **Experimental**：图片证据、提取预过滤、独立 Tag 通道、PostgreSQL 连通性探针。

当前基线为 v0.24.0，共 37 个不可变、仅向前执行的 Migration。

## 文档

| 文档 | 内容 |
|---|---|
| [文档索引](docs/README.md) | 所有维护中文档的导航 |
| [配置参考](docs/configuration.md) | TOML 键、默认值、允许值与密钥边界 |
| [架构](docs/architecture.md) | 分层、模块、写入/召回管线、存储和生命周期 |
| [API](docs/api.md) | REST 端点和请求约定 |
| [MCP](docs/mcp.md) | stdio 启动参数、Codex/Claude/Cursor 配置与工具错误语义 |
| [兼容性策略](docs/compatibility.md) | 版本和公共契约保证 |
| [能力矩阵](docs/capability-matrix.md) | 成熟度、默认值和验证证据 |
| [变更日志](docs/CHANGELOG.md) | 发布历史 |

## Contributing / 贡献指南

欢迎通过 Issue 报告缺陷或提出功能建议。提交前请搜索是否已有同类 Issue，并附上复现步骤、预期行为、实际行为、环境信息和必要日志。Pull Request 应聚焦单一改动，说明动机与验证结果，并在行为或公共契约变化时同步更新测试和文档。

开发环境与检查命令：

```bash
git clone git@github.com:REDACTED_USER/hl_mem.git
cd hl_mem
uv sync --dev
uv run pytest tests/unit/ -q --tb=short
uv run black --check src tests
uv run isort --check-only src tests
uv run ruff check src tests
```

提交信息使用英文，格式为 `type(scope): description`，其中 `type` 可选 `feat`、`fix`、`refactor`、`test`、`docs` 或 `chore`。

**English:** Please search existing issues before opening one and include reproduction steps, expected/actual behavior, environment details, and relevant logs. Keep each PR focused, explain the motivation and validation, update tests/docs when contracts change, set up with `uv sync --dev`, and run the checks above.

## License / 许可证

本项目采用 [Apache License 2.0](LICENSE)。你可以在许可证条款允许的范围内使用、修改和分发本项目，并须保留所要求的版权及许可证声明。

**English:** Licensed under the [Apache License 2.0](LICENSE). You may use, modify, and distribute this project subject to its terms, including the required notices.
