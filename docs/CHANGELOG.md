# HL-Mem 变更记录

本文件记录发布级变更摘要。测试数字是对应版本的发布基线；migration 数是该版本结束时的 SQL migration 总数。

## v0.13.1 — 2026-07-26

### Fixed

- 修复 CI dev extra 与 coverage 门禁。
- 收紧 recall fold 语义保护，并为 TTL 扫描增加 180 天候选窗口。

### Changed

- 校正文档、能力矩阵、MCP 工具数和 PostgreSQL 实验性状态。

- **Migrations**：29（无新增）。
- **测试**：按发布约束未运行 pytest；使用定向 unittest 与静态/导入检查验证。

## v0.13.0 — 2026-07-26

### Added

- 新增能力成熟度矩阵，以及格式、构建、Python 3.12、空库 migration 和依赖方向 CI 门禁。

### Changed

- 完成工程收敛并修正文档 SSOT。

- **Migrations**：29（无新增）。
- **测试**：443 passed，1 skipped（沿用 v0.12.4 发布基线，本版本按发布约束未重跑 pytest）。

## v0.12.4 — 2026-07-26

### Fixed

- 修复 temporal cleanup/TTL 并发竞态。
- 收敛召回折叠语义和成本。

### Added

- 关系提案按 `run_id` 保留审计历史。
- 新增 TTL/cleanup 扫描索引。

- **Migrations**：29（新增 028、029）。
- **测试**：443 passed，1 skipped。

## v0.12.3 — 2026-07-26

### Added

- 新增默认关闭的 deterministic extraction pre-filter，在 LLM 调用前过滤低价值运行时事件。

### Changed

- 过滤结果保留审计，规则异常时回退正常提取。

- **Migrations**：27（无新增）。
- **测试**：433 passed，1 skipped。

## v0.12.2 — 2026-07-26

### Added

- 召回输出 score，并增加相似度折叠和 temporal 回填维护。

### Changed

- 清理语义重复 Claim，增强提取 prompt。

- **Migrations**：27（无新增）。
- **测试**：411 passed，1 skipped。

## v0.12.1 — 2026-07-26

### Fixed

- 修复 usefulness 类型约束、TTL 双时间和关系并发写入。
- 收敛组件降级、查询扩展并发、召回副作用和 benchmark 时间语义。

- **Migrations**：27（新增 025–027）。
- **测试**：401 passed，1 skipped。

## v0.12.0 — 2026-07-26

### Added

- 交付多查询召回、关系候选发现、Benchmark suite、图片证据入口、反馈驱动维护和 Tool/Procedure intent。

- **Migrations**：24（新增 023、024）。
- **测试**：373 passed，1 skipped。

## v0.11.2 — 2026-07-25

### Fixed

- 补齐 trigram FTS 行为与 migration 022 回归，并完成数据清理。

### Changed

- CI 扩展为全量测试套件。

- **Migrations**：22（无新增）。
- **测试**：342 passed。

## v0.11.1 — 2026-07-24

### Fixed

- 修复空 trigger、异常边界和 reranker retry。

### Changed

- 统一配置 Enum、HTTP retry、状态校验和核心类型/docstring。

- **Migrations**：22（无新增）。
- **测试**：325 passed。

## v0.11.0 — 2026-07-24

### Added

- 新增 LLM spans、Job 进度、中文 FTS 评测、后端协议、dry-run extraction 和 ConsolidationScope。

### Changed

- 将 Claim FTS 切换为 trigram。

- **Migrations**：22（新增 019–022）。
- **测试**：325 passed。

## v0.10.1 — 2026-07-24

### Added

- 增加 MRR/nDCG 和独立 behavioral scenarios。

### Changed

- 冻结排序因子，统一 RecallConfig，并类型化召回上下文。

- **Migrations**：18（无新增）。
- **测试**：292 passed，1 skipped。

## v0.10.0 — 2026-07-24

### Added

- 完成 topic tags soft boost、可选独立 tag channel 和确定性 query-to-tag 解析。

- **Migrations**：18（新增 018）。
- **测试**：292 passed，1 skipped。

## v0.9.1 — 2026-07-24

### Fixed

- 修复 qualifier 降级、TTL UTC 统一和回填 CAS。

- **Migrations**：17（无新增）。
- **测试**：277 passed。

## v0.9.0 — 2026-07-24

### Added

- 交付 slot+tags 分类、跨 subject 审计去重，以及 importance 联动 TTL。

- **Migrations**：17（新增 016、017）。
- **测试**：发布记录未保留精确计数；v0.9.1 基线为 277 passed。

## v0.7.0 — 2026-07-24

### Added

- 完成 canonical attribute、scope 后置规则、TTL policy 和 decay priority。

- **Migrations**：15。
- **测试**：发布记录未保留精确计数。

## v0.3.0 — 2026-07-23

### Added

- 完成冲突检测、事务原子化、fact_hash v2、MCP application 委托与初始架构分层。

- **Migrations**：13。
- **测试**：发布记录未保留精确计数。
