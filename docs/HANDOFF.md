# HL-Mem 项目交接状态

> 最后更新：2026-09-02

## 当前状态

- **分支**：`main`（1.1 稳定线）
- **版本**：v1.1.0
- **阶段**：1.1 稳定基线；可执行行为与已验证的 `v1.1.0rc3` 一致
- **Schema**：60 migrations；全部不可变、仅向前执行
- **运行时**：Python 3.12–3.14；SQLite 为权威存储
- **发布原则**：稳定提交先通过常规 GitHub Tests，再创建不可变标签并由 Trusted Publishing 发布

## Core 1.0 与 1.1 已交付

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
- 1.1 增加只读运行报告与统一费用观测、高置信实体约束、真实外部 Provider 插件实证，并完成 Recall 与
  LLMExtractor 职责拆分；未增加 Graph、额外实体 LLM 或新的权威存储。
- 1.1 增加 Event 来源/session 治理、Hermes 确定性传播、自动会话门控、只读 `explain claim` 与有界 Context
  来源提示；不新增模型调用，不执行事实核验或历史回填。
- RC2 对自然提取声明补充来源约束的 canonical 坐标，并在服务启动时幂等记录当前提取 Provider/模型；该路径
  零外部调用、不新增 schema，并已在生产数据库在线副本验证旧模型替代与任务隔离。
- RC3 将同一来源约束扩展到受控模型任务，并增加只读优先、精确计数保护的历史修复命令；不同任务不会进入
  同一替代链，TTL 清理保持独立且不变。

## 当前发布状态

- Git 历史中的已删除 `.env.bak_glm` 含两条真实凭据形状。测试假阳性已按精确 fingerprint 基线化；这两条
  真实凭据未加入忽略列表；维护者已明确接受其仍有效的残余风险，本项不记为安全清单通过。
- 不可变标签 `v1.1.0rc1`、`v1.1.0rc2`、`v1.1.0rc3` 均已发布至 GitHub 与 PyPI；RC3 的 Tests、Release
  Gates 与 Publish 全绿，本地服务和 Hermes 已切换到 RC3 验证。
- 不可变标签 `v1.1.0` 指向 `299c05f550306821df67caa3c12540c7f21d8f39`；GitHub Release 与 PyPI wheel/sdist
  已发布且未撤回，`main` 的 Tests/Security、稳定标签的 Tests/Publish 均通过。稳定版只提升版本与发布元数据，
  不改变 RC3 已验证的可执行行为或数据库 schema。
- 1.1 的本地发布前门禁为 3105 passed、4 skipped、108 subtests，覆盖率 88.16%；公开召回门禁为
  Recall@5 0.9583、MRR 0.9306；稳定提交还通过 wheel 内容、干净 Python 3.13 安装和 CLI 版本验证。
- 本机 HL-Mem 服务已运行 `1.1.0`，`/healthz` 为 `ok`，Worker 正常、维护失败 0、开放冲突 0；Hermes Gateway
  已重启。Hermes loaded-runtime 证据按实际 Memory Provider 加载惰性刷新，不能仅凭 Gateway 启动判定已注册。

## 升级与恢复

从 `v0.36.1` 升级前，使用新 CLI 生成并验证主库 backup、manifest 和独立 tombstone ledger，并保留旧配置与
旧二进制。配置先运行 `hl-mem config migrate` 查看脱敏计划，再显式 `--apply`。SQLite 不支持 downgrade；
恢复是把完整恢复集还原到独立目标并使用旧配置和旧二进制启动，不能让旧二进制打开已升级数据库。

## 下一步

1. 1.1.x 只接受兼容缺陷与安全修复；公开标签和 PyPI 版本不可覆盖，任何修复使用递增补丁版本。
2. 真实使用中关注模型任务坐标、Provider 失败增量、Worker 维护失败和 Hermes loaded-runtime 证据；历史模型
   修复先执行只读预览，不在没有精确计数与 selection token 时应用。
3. 下一轮功能开发先从真实使用证据形成独立设计和分支，不继续扩大 1.1 稳定线，也不为未来能力预建 Graph、
   新存储或后台清理循环。
