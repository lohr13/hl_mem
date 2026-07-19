# Round 1: Hermes Reviewer 建议

## 保持的设计亮点
1. 双通道分离（事实vs经验）
2. Evidence-first
3. 双时间模型
4. Conflict Key 收窄 LLM 扫描
5. ADR + HANDOFF 体系

## 核心建议

### 建议1: 砍掉 Phase 0
用户已跑 Hindsight，清楚其优缺点；MemOS benchmark 费时且不改变架构方向。

### 建议2: 首版激进精简
- 8种记忆类型 -> 3种（event+claim+observation）
- 4档volatility -> 2档（ephemeral+stable）
- 5档visibility -> 2档（private+shared）
- 砍Experience通道（Phase 4）
- 延后Mental Model和MCP Server

### 建议3: LLM提取成本策略
batch提取、event filter、日token预算上限。

### 建议4: 中文NER
Phase 1就建30-50条中文测试集。

### 建议5: SQLite写并发
Worker串行化、events批量insert、Repository接口提前抽象。

### 建议6: Embedding策略
复用智谱embedding-3或text-embedding-v4、多embedding column、BLOB+暴力余弦。

### 建议7: 快速失败机制
Provider timeout(2s)、circuit breaker、daemon不可用时无感降级。

## 技术问题
- 问题8: content_hash去重scope未定义
- 问题9: Observation触发条件模糊
- 问题10: Procedure退化阈值未定义

## 建议排期
- Week 1-2: Phase1精简
- Week 3-4: Phase2精简
- Week 5: 联调
