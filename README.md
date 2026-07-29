# HL-Mem

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Version: 0.17.3](https://img.shields.io/badge/version-0.17.3-blue.svg)](docs/CHANGELOG.md)
[![CI](https://github.com/REDACTED_USER/hl_mem/actions/workflows/test.yml/badge.svg)](https://github.com/REDACTED_USER/hl_mem/actions/workflows/test.yml)

[中文](#中文) | [English](README_EN.md)

<a id="中文"></a>

## 中文

HL-Mem 是面向 AI Agent 的本地优先、证据驱动长期记忆系统。它不只是向量数据库：系统把不可变事件提取为带证据链的结构化 Claim，以有效时间和记录时间描述事实变化，并通过独立的 Experience 通道保存 Episode、Trace 和可复用 Policy。默认使用 SQLite WAL、FTS5 和向量 BLOB，无需部署外部数据库服务。

## 安装

要求 Python 3.11+。推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
git clone git@github.com:REDACTED_USER/hl_mem.git
cd hl_mem
uv sync
```

也可以使用 pip 安装当前仓库：

```bash
python -m pip install .
```

开发环境可使用 `uv sync --dev` 或 `python -m pip install -e .`；运行测试所需的开发依赖以 `uv.lock` 为准。

## 快速开始

### 1. 配置服务

复制环境变量模板，并填写 LLM、Embedding，以及启用重排时所需的 API key：

```bash
cp .env.example .env
```

`.env.example` 是完整、带版本的配置目录。不要提交包含真实密钥的 `.env`。

### 2. 启动服务

```bash
uv run python start_server.py
```

API 和后台 Worker 默认随服务启动，监听 `http://127.0.0.1:8200`。写入并召回一条记忆：

```bash
curl -X POST http://127.0.0.1:8200/v1/memories -H "Content-Type: application/json" \
  -d '{"text":"Alice prefers dark mode","subject":"Alice"}'

curl -X POST http://127.0.0.1:8200/v1/recall -H "Content-Type: application/json" \
  -d '{"query":"What does Alice prefer?","limit":5}'
```

完整请求契约见 [API 文档](docs/api.md)。

### 3. 集成 Hermes

先启动 HL-Mem，再将仓库内的 MemoryProvider 部署到 Hermes：

```bash
uv run python install_to_hermes.py --hermes-home <HERMES_HOME>
```

安装后重启 Hermes。适配器通过 HTTP 调用本地 HL-Mem 服务，并提供超时、熔断、预取及 Episode/Trace 同步；服务不可用时会降级，不阻断 Agent 主任务。

## 关键配置

以下是提取和召回主路径中的常用配置；完整列表、密钥来源和实验开关见 [`.env.example`](.env.example)。

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `HL_MEM_ENV` | `dev` | 运行环境：`dev` 或 `production` |
| `HL_MEM_DB_PATH` | `var/hl_mem.db` | SQLite 数据库路径 |
| `HL_MEM_EXTRACTOR` | `llm`（模板） | 提取器：`fake` 或 `llm` |
| `HL_MEM_EMBEDDER` | `real`（模板） | 向量化：`fake` 或 `real` |
| `HL_MEM_RERANKER` | `on` | 重排：`off`、`fake`、`on` 或 `real` |
| `HL_MEM_LLM_PROVIDER` | `dashscope` | LLM Provider：`dashscope`、`zhipu` 或 `openai_compatible` |
| `HL_MEM_LLM_ENABLE_THINKING` | 未设置 | 可选布尔覆盖；未设置时不向 Provider 发送该字段 |
| `HL_MEM_LLM_STRUCTURED_MODE` | `json_object` | 结构化输出：`auto`、`json_object` 或 `json_schema` |
| `HL_MEM_LLM_SCHEMA_RETRIES` | `2` | JSON 修复或 Schema 校验失败后的最大重试次数 |
| `HL_MEM_INDEX_TEXT_MODE` | `legacy` | FTS/Embedding 索引文本：`legacy`、`value_only` 或 `natural` |
| `HL_MEM_EXTRACTION_CHUNK_TARGET_CHARS` | `12000` | 结构感知提取分块的目标字符数 |
| `HL_MEM_EXTRACTION_CHUNK_OVERLAP_TURNS` | `2` | 相邻对话分块的重叠轮数 |
| `HL_MEM_EXTRACTION_MAX_SPLIT_DEPTH` | `3` | 截断后递归拆分的最大深度 |
| `HL_MEM_QUERY_EXPANSION_MODE` | `auto` | 多查询召回：`off`、`auto` 或 `always` |
| `HL_MEM_RELATION_DISCOVERY_MODE` | `audit` | 关系发现：`off`、`audit` 或 `auto` |
| `HL_MEM_TAG_CHANNEL_ENABLED` | `false` | 是否启用独立 Tag 检索通道 |

生产模式会校验真实 Embedder、启用的 Reranker 和非 Fake Extractor。配置语义以 [Settings](src/hl_mem/settings.py) 与 [配置模板](.env.example) 为准。

## 能力概览

- **记忆正确性**：幂等事件摄入、事务原子写入、精确/语义去重、确定性冲突规则和 LLM 灰区归并。
- **提取治理**：确定性的 scope 降级、从规范属性执行 predicate 投影、subject 守卫隔离无效主体，以及有界结构化输出修复。
- **时间与证据**：有效时间与记录时间双时间模型、证据链、实体归一化、显式遗忘和 stale 传播。
- **混合召回**：中文 FTS5、稠密向量、RRF 融合、多因子排序、可选 Reranker、关系/查询扩展和按 Token 预算打包上下文。
- **生命周期**：importance 联动 TTL、置信度衰减、归档、重分类、反馈效用、审计日志和在线备份。
- **经验通道**：Episode、Trace、Reward、Policy/Procedure 和派生 Observation。
- **接口**：FastAPI REST 与 Hermes Provider 为稳定主路径；五工具 MCP 接口处于 Beta。
- **评测**：离线提取/召回/生命周期指标、召回诊断、索引文本受控 A/B、跨模型提取 Benchmark 和 LongMemEval 适配器。

能力成熟度、默认开关和证据见 [能力矩阵](docs/capability-matrix.md)，架构与数据流见 [架构文档](docs/architecture.md)。

## 项目状态

- **Stable**：事件与证据链、原子写入、LLM 提取、Embedding、FTS + Dense + RRF、双时间过滤、TTL/衰减/归档、冲突与去重、REST、Hermes、备份与审计。
- **Beta**：多查询召回、关系候选发现、反馈驱动维护、语义去重审计、MCP Server、Benchmark 与 LongMemEval。
- **Experimental**：图片证据、提取预过滤、独立 Tag 通道、PostgreSQL 连通性探针。

当前基线为 v0.17.3，共 33 个不可变、仅向前执行的 Migration。

## 文档

| 文档 | 内容 |
|---|---|
| [文档索引](docs/README.md) | 所有维护中文档的导航 |
| [架构](docs/architecture.md) | 分层、模块、写入/召回管线、存储和生命周期 |
| [API](docs/api.md) | REST 端点和请求约定 |
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
