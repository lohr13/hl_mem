# Phase 13 — 共识方案（Hermes × Codex）

> 2026-07-23 · 双分析师收敛模式产出

## 共识声明

### P1-1 幂等写入竞态 → **AGREE**
- 删除事务外幂等查询，将查询和插入全部放入 `BEGIN IMMEDIATE`
- 并发测试使用两个独立 Database 实例，不共享 connection

### P1-2 去重 TOCTOU → **COMPROMISE**（Codex 修正）
- 事务外只做 claim 规范化 + embedding 计算
- `BEGIN IMMEDIATE` 后依次重新执行 exact dedup → conflict → semantic dedup
- **关键修正（Codex 发现）**：`insert_claim()` 返回 False 时，不能仅停止建 evidence——必须重新查询实际胜出的 claim，给它添加 evidence 并返回其 ID，否则会丢失本次事件的证据链
- audit 尽量在 commit 后发出

### P1-3 Job lease token → **DISAGREE → 采纳 Codex 方案**
- ~~可选 `lease_token=None` 退化为旧逻辑~~ → **删除**
- token **必填**，所有调用方一次性迁移
- SQL 同时匹配 `id` + `status='running'` + `lease_token`
- `fail_job()` 的 attempts 读取与更新在同一事务内按 token 限定
- 提供独立的 `force_finish_job()` 供管理员强制结束（不接收 token）
- migration `015_lease_token.sql`：`ALTER TABLE jobs ADD COLUMN lease_token TEXT`

### P1-4 预算超限 → **COMPROMISE**（采纳 Codex 修正）
- ~~截断第一条写入 `data["text"]`~~ → **删除**
- 无条件检查 `used + cost > token_budget`，超限直接跳过（包括第一条）
- 允许返回空 context（`packed=[]`, `used=0`, `truncated=True`）
- 不修改原始 data 字段

### P1-5 API 体积上限 → **COMPROMISE**（Codex 扩展）
- Pydantic `max_length` 字段限制（返回 422）
- **ASGI middleware 请求体上限**（返回 413），因为 Pydantic 在 JSON 解析后才运行
- 上限值进 Settings / 环境变量，不硬编码
- 同时限制 ID 字段（idempotency_key、tenant_id、session_id 等）

### P1-6 namespace 过滤 → **COMPROMISE**（Codex 扩展范围）
- RecallInput 加 `namespace: str = "default"`（默认值，但存储层参数必填）
- 除 claims 的 FTS/vector/scan/rivals 外，还要过滤：
  - `ExperienceService.list_policies()` 按 namespace 过滤
  - MCP `_recall()` 传递 namespace
- namespace 加长度/格式校验

### P1-7 N+1 查询 → **AGREE**
- 批量加载 evidence/replacement/relations/conflicts
- `IN(...)` 分块（每批 ≤ 500）
- 与 P1-6 在同一 batch 实现（避免重复改 repository 签名）

### P2-8 healthz 测试 → **AGREE**
- 从 `hl_mem.__version__` 获取版本号，只断言稳定字段

### P2-9 并发测试 → **AGREE**
- 新增 `test_concurrency.py`

### P2-10 覆盖率 → **AGREE**（Codex 补充：marker 注册要配 conftest 跳过逻辑）
### P2-11 HTTP 复用 → **AGREE**（Codex 补充：含 conflict consolidator）
### P2-12 静默降级 → **COMPROMISE**（Codex 扩展：extractor 也纳入统一规则）
### P2-13 domain 不纯 → **COMPROMISE**（Codex 关键补充）
- `normalize_entity_id` 改为 `normalize_entity_id(subject, aliases)`
- **关键**：`ClaimRepository.find_active_for_dedup()` 也直接调用 `normalize_entity_id()`，只改 IngestService 会让写入侧和查询侧使用不同 alias 集合 → 新的去重 bug
- 启动时统一加载 alias mapping，注入所有调用方

## 实施批次（采纳 Codex 方案）

| Batch | 内容 | 依赖 |
|-------|------|------|
| **1A** | P1-1 + P1-2 + P2-9（ingest 并发正确性 + 测试） | 无 |
| **1B** | P1-3 + lease 测试（job 状态机） | 无 |
| **2A** | P1-6 + P1-7（recall namespace + N+1 批量） | 无 |
| **2B** | P1-4（预算硬上限） | 无 |
| **3** | P1-5（API 体积限制 + ASGI middleware） | 无 |
| **4A** | P2-8 + P2-10（测试修复 + 覆盖率） | Batch 1A/1B |
| **4B** | P2-11 + P2-12（HTTP 复用 + 降级策略） | 无 |
| **4C** | P2-13（domain 纯化 + alias 注入） | 无 |

每个 batch 独立 commit + push + 验收（220 测试全绿）。

## 测试基线确认
- 全量 220 tests collected（含 tests/eval/）
- unit + integration = 203 tests（Codex 审查时跑的子集）
- 当前 1 failed: `test_healthz`（版本断言过时，P2-8 修复）
