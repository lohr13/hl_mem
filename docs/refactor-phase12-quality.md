# Phase 12：记忆库数据质量与提取精度全面提升

## 背景

Phase 10-11 修复了冲突检测逻辑，但实测 599 条 active claims 暴露了 5 个系统性问题。本阶段目标是全面提升提取质量和数据治理。

## 实测数据（2026-07-23）

### 问题 1：语义重复（86 条，14%）

semantic dedup 的 cosine 阈值太高，近义改写触发不了去重：
- "通过 HTTP 调 hl_mem，也是实时的" × 4
- "百炼和智谱都买了 coding plan" × 4
- "gpt-5.6-sol" × 5
- "启动和成功后都打印" × 4
- "D:\workspace\hl_agent\hl_mem" × 4

### 问题 2：Subject 实体碎片化（6 组）

同一实体被拆成多个 subject_entity_id：
- `hl_mem` / `hlmem` / `HL_MEM` → 262 条分散在 3 个名字
- `Hermes` / `hermes-agent` / `Hermes 插件` / `Hermes memory` / `hermes` → 46 条
- `Codex` / `Codex CLI` → 24 条
- `llm_extractor` / `LLMExtractor` → 6 条
- `hlmem-watchdog` / `Watchdog` → 6 条
- `scripts/cleanup_data.py` / `cleanup_data.py` → 4 条

### 问题 3：过时数据未过期（27 条）

明确的过期数据仍在 active：
- "v0.2.0"、"180 passed"、"188 passed"、"158 passed"（现在 v0.3.0、195 passed）
- "glm-5.1"（现在用 glm-5.2）
- "架构评分 6.2/10"、"6/10"（现在 9/10）
- "FakeEmbedder 死代码"（已修复）
- "298 行"（版本已变）

### 问题 4：Scope 分类大面积错误（~150 条）

- 144 条标 temporal 但实际 permanent（如"已部署最新代码"、"实现了 FTS5 修复"）
- 6 条标 permanent 但实际 temporal（如"重构约束：现有180测试"）

### 问题 5：canonical_attribute 错配（33+ 条）

LLM 属性分配不准：
- 模型名(glm/gpt/qwen) → 放 choice.tool（应为 choice.model）
- 文件路径(workers/worker.py, api/pipeline.py) → 放 fact.other（应为 config.path）
- URL(github.com) → 放 choice.tool（应为 config.env 或 fact.other）
- 端口号(8200, 127.0.0.1) → 放 config.path（应为 config.port）

## 需要 Codex 评估的方案

### 方案 1：Semantic Dedup 阈值调整

当前 `recall_pipeline.py` 中 semantic dedup 的 cosine 阈值需要检查和调整。请分析当前阈值，提出新的合理值。

### 方案 2：Entity 归一化

建议在 LLM 提取阶段做 subject 归一化。两种可能路径：
- A：代码级——在 `_claim()` 或 `store_extracted()` 中加 entity alias 映射表
- B：Prompt 级——在系统提示词中加"subject 必须使用标准名称"的指令

请评估哪种更好，或两者结合。

### 方案 3：ATTRIBUTE_HINTS 扩充

当前 `attribute_map.py` 的 ATTRIBUTE_HINTS 缺少对模型名、端口、URL 等的精准匹配。请扩充关键词覆盖。

### 方案 4：Scope 提取精度提升

当前 LLM 对 temporal/permanent 判断不准。请评估：
- 提取 prompt 是否需要加强定义和示例
- 是否需要后置规则（如版本号自动标 temporal）

### 方案 5：过时数据治理

当前 decay worker 只按 access_count 调整阈值。请评估是否需要：
- 版本号/数字类 claim 自动标 temporal + 短 TTL
- 或其他自动化过时检测机制

## 关键约束

1. 向后兼容：195 个测试必须全绿
2. 单机单 Agent 场景
3. 不新增依赖
4. 代码修改交给 Codex 实现，Hermes 负责验收
