# HL-Mem Documentation

这里仅导航 HL-Mem 的当前维护文档。已经实施、被替代或只用于复盘的设计位于
[`archive/`](archive/)，不定义当前 API、默认值、部署步骤或路线图。

## 入门

- [中文快速开始](../README.md#中文)
- [Configuration reference](configuration.md)
- [TOML configuration template](../config.example.toml)
- [Secret template](../.env.example)
- [Architecture](architecture.md)

建议先阅读根 README，再按需查看架构、能力矩阵和接口文档。

## 当前参考

- [REST API](api.md)
- [MCP stdio](mcp.md)
- [Configuration](configuration.md)
- [Compatibility policy](compatibility.md)
- [Capability matrix](capability-matrix.md)
- [Changelog](CHANGELOG.md)
- [Handoff](HANDOFF.md)

`api-schema.json` 和 `mcp-tools.json` 是由代码生成并受 CI 校验的契约快照，不应手工改写。

## 设计与历史

- [Architecture Decision Records](adr/)
- [Current feature design](design/)
- [Historical archive](archive/)

归档索引会说明文档的历史状态和当前规范来源。归档中的模型、路径、配置和评测数字可能已经过期。

## 维护规则

- 发布级变化追加到 `CHANGELOG.md`；`HANDOFF.md` 只保存当前状态、下一步和已知限制。
- Accepted ADR 不改写决策；新方向使用新 ADR。
- API、数据模型和默认行为变化必须同步活文档及生成快照。
- 能力成熟度和默认模式以 capability matrix 为准。
- 真实语料、数据库、模型响应和运行报告不进入仓库。
