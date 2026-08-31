# HL-Mem 项目交接状态

> 最后更新：2026-08-31

## 当前状态

- **分支**：`main`（仅本地，尚未推送）
- **版本**：v1.0.0rc1
- **阶段**：Core 1.0 RC 本地门禁完成；尚未 push、打 tag、创建 GitHub Release 或发布 PyPI
- **Schema**：59 migrations；全部不可变、仅向前执行
- **运行时**：Python 3.12–3.14；SQLite 为权威存储
- **发布原则**：RC 固定提交、完整证据、连续七个 UTC 日期观察、无开放 P0/P1 后才能提升稳定版

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

## 当前发布阻断

- Git 历史中的已删除 `.env.bak_glm` 含两条真实凭据形状。测试假阳性已按精确 fingerprint 基线化；这两条
  真实凭据不得加入忽略列表，必须先在对应 Provider 侧轮换或吊销。
- 远端 release-gates 和 security 工作流尚未执行，证据 URL 尚未产生。
- RC 尚未发布，因此真实的 168 小时、七个连续 UTC 日期观察尚未开始。

## 升级与恢复

从 `v0.36.1` 升级前，使用新 CLI 生成并验证主库 backup、manifest 和独立 tombstone ledger，并保留旧配置与
旧二进制。配置先运行 `hl-mem config migrate` 查看脱敏计划，再显式 `--apply`。SQLite 不支持 downgrade；
恢复是把完整恢复集还原到独立目标并使用旧配置和旧二进制启动，不能让旧二进制打开已升级数据库。

## 下一步

1. 轮换或吊销历史凭据，确认 Gitleaks 全历史扫描为零未审查结果。
2. 获得明确授权后 push `main`、创建不可变 RC tag 和 GitHub prerelease；不直接发布稳定版。
3. 运行远端 release-gates/security，保存证据 URL 和 artifact hash。
4. 收集连续七天证据；任何代码修复都发布新的 RC 并重新开始观察。
5. 观察门禁通过并再次获得发布授权后，单独准备和发布 `1.0.0`。
