# HL-Mem 首版共识方案

- 达成时间：2026-07-20
- 参与者：Hermes Agent（Reviewer）+ Codex（gpt-5.6-sol）
- 状态：双方一致同意

---

## 一、首版范围（5周）

### 包含
| 模块 | 内容 |
|------|------|
| 记忆类型 | 3种：`event` + `claim` + `observation` |
| volatility | 2档：`ephemeral`（带TTL）+ `stable` |
| visibility | 2档：`private` + `shared`（scope字段从Day 1保留） |
| 存储层 | SQLite WAL + FTS5 + 向量BLOB（暴力余弦） |
| 写入 | 幂等 events 写入 + idempotency_key |
| 提取 | LLM batch提取 + event filter + 日token预算 |
| 检索 | FTS/BM25 + Dense Embedding + 时间过滤 + RRF |
| Embedding | 阿里 text-embedding-v4（Qwen3-Embedding），默认2048维，记录模型版本 |
| 矛盾 | Conflict Key + 确定性规则 + LLM分类 |
| 遗忘 | 显式 forget 级联删除（claim+evidence+向量）+ 最小 tombstone |
| Hermes | Provider 骨架（timeout 2s + circuit breaker + 无感降级） |

### 不包含（延后）
- ~~Phase 0 基线对比~~
- ~~Experience 通道（Episode/Trace/Policy/Procedure）~~ — 不建表、不写 Repository，仅保留 architecture.md 设计
- ~~Mental Model~~
- ~~MCP Server~~
- ~~自动 re-extract 回填~~（保留 extractor_version 字段 + CLI 手动触发）
- ~~多租户隔离~~（单用户本地部署，scope 字段预留）

### 技术决策
- **Embedding**：首版默认用阿里 text-embedding-v4（Qwen3-Embedding）2048维。理由：(1) MTEB 多语言 SOTA，中文表现更强；(2) 支持 dense+sparse 混合输出，sparse 向量与 FTS 互补；(3) 0.6B/4B/8B 全开源，未来可本地部署；(4) 同价格 0.0005元/千token，Batch 半价。保留模型版本字段和多 embedding column 接口设计，智谱 embedding-3 作为 fallback 选项。后续用中文测试集实测对比确认。
  - **Batch 限制**：text-embedding-v4 批量上限 10条/批（vs embedding-3 的 64条），请求数约增至 6.4 倍。缓解：异步并发 + 10条满批 + 离线 Batch API（半价）+ 增量提取缓存 + QPS 动态限并发 + 重试退避。
  - **Sparse 向量存储**：首版小规模可序列化为 BLOB 反序列化计算；定义稳定的 index→weight 格式 + 版本 + 端序。规模增长后建倒排表（term→doc/weight）。
- **Experience 通道**：双方一致同意首版不建表不写代码。迁移路径 = 未来加一个 migration 创建新表 + 新 Repository 实现，对现有数据零影响。
- **Python 包管理**：uv（HANDOFF.md 已建议）

---

## 二、逐条建议表态汇总

| # | 建议 | Hermes | Codex | 结论 |
|---|------|--------|-------|------|
| 1 | 砍 Phase 0 | 同意 | 同意 | ✅ 砍掉 |
| 2 | 首版激进精简 | 同意 | 部分同意→同意修正 | ✅ 3类型2档2档，Experience不建表 |
| 3 | LLM提取成本策略 | 同意 | 同意 | ✅ batch+filter+budget |
| 4 | 中文NER测试集 | 同意 | 同意 | ✅ Phase 1就建 |
| 5 | SQLite写并发 | 同意 | 同意 | ✅ 串行Worker+批量insert |
| 6 | Embedding策略 | 同意→修正 | 部分同意→共识 | ✅ 改用 text-embedding-v4 2048维 |
| 7 | 快速失败机制 | 同意 | 同意 | ✅ timeout+breaker+降级 |
| 8 | content_hash scope | 需定义 | 需定义 | ✅ 首版需明确 |
| 9 | Observation阈值 | 需定义 | 需定义 | ✅ 首版需明确 |
| 10 | Procedure阈值 | 延后 | 冻结语义 | ✅ 延后但冻结 |

---

## 三、Codex 额外问题的优先级

| 优先级 | 问题 | 首版处理 |
|--------|------|---------|
| P0 | 幂等写入 | 必须实现 idempotency_key |
| P0 | forget 级联删除 | claim+evidence+向量+tombstone，observation→stale |
| P1 | 中文检索质量评测集 | 30-50条真实对话，同时测召回 |
| P1 | extractor_version + CLI重提取 | 保留字段，手动触发 |
| 延后 | 自动回填 | 未来 batch re-extract job |
| 延后 | 多租户隔离 | scope 字段预留 |

---

## 四、排期

| 周次 | 内容 |
|------|------|
| Week 1 | 项目骨架(uv+SQLite+测试) + Schema(events/claims/evidence_links/jobs) + Repository接口 + 中文测试集 |
| Week 2 | event写入(幂等) + LLM batch提取 + event filter + 证据链 |
| Week 3 | 向量检索(text-embedding-v4) + 去重 + Observation规则 + Conflict Key |
| Week 4 | 单写Worker + TTL/expire + forget级联 + Hermes Provider(timeout+breaker) |
| Week 5 | 联调 + 离线评测 + 压测 + 替换Hindsight跑一周 |

---

## 五、首版验收标准

1. ✅ 端到端流程通过：event → extract → claim → recall
2. ✅ 重复事件不产生重复写入（idempotency_key）
3. ✅ forget 后原文、claim、evidence_link、向量均不可检索
4. ✅ 中文评测集验证召回质量（具体阈值联调后定）
5. ✅ CLI 重提取可执行
6. ✅ Provider timeout/circuit breaker 生效
7. ✅ daemon 故障时 Hermes 无感降级
8. ✅ Experience 通道仅存在于设计文档
