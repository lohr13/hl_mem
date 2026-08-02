# HL-Mem

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
[![Version: 0.20.2](https://img.shields.io/badge/version-0.20.2-blue.svg)](docs/CHANGELOG.md)
[![CI](https://github.com/REDACTED_USER/hl_mem/actions/workflows/test.yml/badge.svg)](https://github.com/REDACTED_USER/hl_mem/actions/workflows/test.yml)

[中文](#中文) | [English](README_EN.md)

<a id="中文"></a>

## 中文

HL-Mem 是面向 AI Agent 的本地优先、证据驱动长期记忆系统。它不只是向量数据库：系统把不可变事件提取为带证据链的结构化 Claim，以有效时间和记录时间描述事实变化，并通过独立的 Experience 通道保存 Episode、Trace 和可复用 Policy。默认使用 SQLite WAL、FTS5 和向量 BLOB，无需部署外部数据库服务。

## 安装

### 前置条件

- Python 3.11 或更高版本。
- 推荐安装 [uv](https://docs.astral.sh/uv/)；也可使用 pip。
- SQLite 必须包含 FTS5（Python 官方发行版通常已包含）。
- 使用真实提取、Embedding 或 Reranker 时，需要对应服务的 API key。
- 集成 Hermes 时，需要可写的 Hermes 根目录及重启 Hermes 的权限。

### 本地安装

推荐使用 uv：

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

安装和配置后可运行只读诊断：

```bash
uv run hl-mem doctor
```

### Linux / systemd 部署

将下列模板保存为 `/etc/systemd/system/hl-mem.service`，并将 `<HL_MEM_DIR>`、`<RUN_USER>` 替换为实际值：

```ini
[Unit]
Description=HL-Mem local memory service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<RUN_USER>
WorkingDirectory=<HL_MEM_DIR>
ExecStart=<HL_MEM_DIR>/.venv/bin/python <HL_MEM_DIR>/start_server.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

`start_server.py` 从进程当前工作目录加载必需的 `hl_mem.toml` 和可选的 `.env`。因此 systemd 的
`WorkingDirectory` 必须指向放置这两个文件的部署目录；缺少 `hl_mem.toml` 时服务不会启动。安装并启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hl-mem
sudo systemctl status hl-mem
```

## 快速开始

### 1. 配置服务

复制 TOML 配置和密钥模板：

```bash
cp config.example.toml hl_mem.toml
cp .env.example .env
```

`config.example.toml` 只列常用参数，并显式写入推荐的真实能力模式；这些推荐值不是代码默认值。根据启用的组件填写
`.env` 中的独立密钥，或将不需要的组件 mode 改回安全默认值。不要提交包含真实密钥的 `.env`。所有 TOML 字段见
[配置参考](docs/configuration.md)。

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

常驻部署统一使用纯标准库的 `scripts/healthcheck.py` 探测 `/healthz`，进程重启与告警交给 systemd、Windows 服务管理器或容器编排平台。示例见 [服务监督与健康检查](docs/watchdog.md)。

### 3. 集成 Hermes

启动顺序必须是：先启动 HL-Mem 并确认健康检查通过，再安装插件，最后重启 Hermes：

```bash
curl --fail http://127.0.0.1:8200/healthz
uv run python install_to_hermes.py --hermes-home <HERMES_HOME>
# 使用 Hermes 自己的服务管理方式重启 Hermes
```

安装脚本的目标路径是 `<HERMES_HOME>/plugins/hl_mem/`。安装后必须重启 Hermes，让插件扫描器重新加载。适配器通过 HTTP 调用本地 HL-Mem 服务，并提供超时、熔断、预取及 Episode/Trace 同步；服务不可用时会降级，不阻断 Agent 主任务。

### 三步验证清单

1. 验证 HL-Mem 服务健康：

   ```bash
   curl --fail http://127.0.0.1:8200/healthz
   ```

2. 发起一次召回并确认返回 JSON：

   ```bash
   curl --fail -X POST http://127.0.0.1:8200/v1/recall \
     -H "Content-Type: application/json" \
     -d '{"query":"What does Alice prefer?","limit":5}'
   ```

3. 检查 Hermes Agent 日志是否加载并调用 HL-Mem：

   ```bash
   grep -iE "hl[_-]mem|memory provider" <HERMES_HOME>/agent.log
   ```

### 常见问题排查

- Hermes 扫描不到插件：确认文件位于 `<HERMES_HOME>/plugins/hl_mem/`，不要放在旧路径 `<HERMES_HOME>/plugins/memory/hl_mem/`；修正后重启 Hermes。
- 服务启动失败或 FTS 异常：运行 `uv run hl-mem doctor`，根据逐项 `OK/WARN/FAIL` 结果检查数据库、migration、密钥与端口。
- `healthz` 正常但 Hermes 无召回：先确认 Hermes 是在 HL-Mem 启动后重启的，再检查 `agent.log` 和插件目录权限。

## 关键配置

非敏感配置只从当前工作目录的 `hl_mem.toml` 读取；密钥只从 `.env` 或同名进程环境变量读取。常用键如下，完整列表见
[配置参考](docs/configuration.md)。

| TOML 键 | 代码默认值 | 说明 |
|---|---:|---|
| `database.path` | `var/hl_mem.db` | SQLite 数据库路径 |
| `extraction.mode` | `fake` | 提取器：`fake`、`real` 或 `llm` |
| `embedding.mode` | `fake` | 向量化：`fake` 或 `real` |
| `reranker.mode` | `off` | 重排：`off`、`fake`、`on` 或 `real` |
| `image_describer.mode` | `off` | 图片描述：`off` 或 `on` |
| `llm.provider` | `dashscope` | `dashscope`、`zhipu` 或 `openai_compatible` |
| `llm.structured_mode` | `json_object` | `auto`、`json_object` 或 `json_schema` |
| `index.text_mode` | `legacy` | `legacy`、`value_only`、`natural` 或 `answerable` |
| `recall.query_expansion_mode` | `auto` | 多查询召回：`off`、`auto` 或 `always` |
| `relation.discovery_mode` | `off` | 关系发现：`off`、`audit` 或 `auto` |
| `recall.tag_channel_enabled` | `false` | 是否启用独立 Tag 检索通道 |

真实组件和外部调用路径必须提供各自密钥；失败时不会自动切换为 fake。任意 `HL_MEM_*` 环境变量都不再参与应用 `Settings` 配置。
代码默认值与示例部署配置刻意分离：`Settings` 的 `recall.default_limit` / `recall.relevance_reranker_floor` 仍为 `20` / `0.4`，而仓库 TOML 与 `config.example.toml` 显式覆盖为 `5` / `0.15`，并保持 `recall.relevance_keep_top1 = true`。query expansion 使用独立可配置模型，单次/总超时为 5/6 秒。

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

当前基线为 v0.20.2，共 36 个不可变、仅向前执行的 Migration。

## 文档

| 文档 | 内容 |
|---|---|
| [文档索引](docs/README.md) | 所有维护中文档的导航 |
| [配置参考](docs/configuration.md) | TOML 键、默认值、允许值与密钥边界 |
| [架构](docs/architecture.md) | 分层、模块、写入/召回管线、存储和生命周期 |
| [API](docs/api.md) | REST 端点和请求约定 |
| [服务监督与健康检查](docs/watchdog.md) | 跨平台健康探针及 systemd、Windows、容器部署示例 |
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
