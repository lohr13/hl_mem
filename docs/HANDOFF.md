# HL-Mem 项目交接状态

> 最后更新：2026-08-31

## 当前状态

- **分支**：`main`（已推送）
- **版本**：v1.0.0rc1
- **阶段**：`v1.0.0rc1` 已发布至 GitHub 与 PyPI，并完成本地真实链路观察
- **Schema**：59 migrations；全部不可变、仅向前执行
- **运行时**：Python 3.12–3.14；SQLite 为权威存储
- **发布原则**：RC 固定提交、本地真实链路观察、稳定提交通过常规 GitHub Tests 后提升稳定版

## Core 1.0 已交付

- 生产配置 schema v1、确定性 `config migrate`、Provider 中立 `init` 和只读 `doctor`；生产配置缺失或使用
  Fake Provider 时 fail-closed。
- 受治理的 `hl_mem.providers` Entry Point、显式 allowlist、版本协商、冲突即失败和宿主代理；内置 LLM、
  Embedding、Reranker 与实验 Image Provider 走同一 Registry。
- 四条 Provider 调用统一执行 HTTP、安全校验、原子预算、审计和结算；插件是可信进程内代码，不是沙箱。
- 自动任务按确定性与语义副作用拆分；语义任务在入队和执行两端显式门控，关系发现只进入 Proposal，批准
  后正式边保留 provenance。
- 提取、召回交付、HTTP 路由、Worker 和评测职责已解耦；稳定评测留在 wheel，历史研究装备只留在
  `benchmarks/archive/`。
- 测试覆盖 Python 3.12–3.14，发布门槛 80%；SQLite 资源、请求流限制、migration、备份恢复、Provider
  冲突、零模型调用和公开召回均有阻断门禁。
- 发布证据聚合、CodeQL、依赖审计、SBOM、Git 历史密钥扫描及完整 SHA Action 固定已接入。
- 本地全量门禁为 2701 passed、4 skipped、108 subtests，覆盖率 87.48%；Python 3.12、3.13、3.14
  全新环境的 wheel 安装、导入和 CLI 启动均通过。

## 当前发布状态

- Git 历史中的已删除 `.env.bak_glm` 含两条真实凭据形状。测试假阳性已按精确 fingerprint 基线化；这两条
  真实凭据未加入忽略列表；维护者已明确接受其仍有效的残余风险，本项不记为安全清单通过。
- 不可变标签 `v1.0.0rc1` 指向 `d99ed926b1951011013905dd858893d95d407ed1`；GitHub Release 为非草稿
  prerelease，PyPI wheel 与 sdist 已通过 Trusted Publishing 上传。
- Core 1.0 release-gates、Tests 与 Security 已通过并保存在 GitHub Actions；本地真实 Provider、摄入、召回与
  用量链路已完成冒烟观察。
- 稳定版只允许修改版本与发布元数据；稳定提交必须先通过常规 GitHub Tests，再创建不可变标签。

## 升级与恢复

从 `v0.36.1` 升级前，使用新 CLI 生成并验证主库 backup、manifest 和独立 tombstone ledger，并保留旧配置与
旧二进制。配置先运行 `hl-mem config migrate` 查看脱敏计划，再显式 `--apply`。SQLite 不支持 downgrade；
恢复是把完整恢复集还原到独立目标并使用旧配置和旧二进制启动，不能让旧二进制打开已升级数据库。

## 下一步

1. 单独准备 `1.0.0` 版本与发布元数据，不修改产品代码、配置、schema、migration 或稳定契约。
2. 将稳定提交推送至 `main`，等待常规 GitHub Tests 全绿。
3. 在同一提交创建不可变 `v1.0.0` 标签并发布；不得移动 RC 标签或把 RC 版本冒充稳定版。
