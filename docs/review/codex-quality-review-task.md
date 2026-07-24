# hl_mem v0.6.0 架构品质与代码精良度审查

## 审查目标

**这不是"砍代码"任务，而是品质审查。**

目标：评估 hl_mem 是否是一个**精致的、设计合理的记忆系统**，还是一个堆砌了很多代码但缺乏统一设计语言的"屎山"。

衡量标准不是"能不能删"，而是：
1. 架构设计是否合理——每个模块的存在理由是否成立
2. 代码实现是否精良——是否有粗糙的拼接、不一致的模式、缺乏思考的 copy-paste
3. 功能是否完整且实用——缺失了什么不该缺的，多了什么不该多的
4. 整体是否像一个有品味的人写的——还是一个 AI 在堆砌

## 第一轮审查已确认的事实

以下是第一轮审查（docs/review/codex-dead-code-review-report.md）收集的底层数据，作为第二轮的品质评估基础：

### 有真实数据的功能（核心价值）
| 功能 | 数据量 | 判断 |
|------|--------|------|
| Events/Claims/Evidence | 4132/1001/2092 | 核心在用 |
| Jobs (extract) | 1377 | 核心在用 |
| Episodes/Traces | 59/5179 | Hermes 自动同步，真实在用 |
| Policies | 3 active | 策略归纳有产物 |
| Consolidations | 186 pairs | 语义归并确实运行 |
| Observations (derivations) | 62 | 在用 |
| Audit log | 24309 | 大量写入 |

### 数据可疑或空壳的功能
| 功能 | 数据 | 问题 |
|------|------|------|
| memory_relations | 0 行 | 关系图空表，无写入入口 |
| retrieval_feedback | 1315行但只有2行helpful | 几乎全是自动曝光，不是真反馈 |
| audit_review | 0 行 | 只写不读 |
| MCP server | 无入口 | 只有测试调用 |
| PostgreSQL probe | 无语义 | 只是连接探针 |

### Hermes 实际调用的 API 端点
POST /v1/events, POST /v1/recall, POST /v1/memories, POST /v1/episodes, PATCH /v1/episodes/{id}, POST /v1/episodes/{id}/traces, GET /healthz

## 第二轮审查维度

### 维度 1：架构设计语言一致性

评估整个系统是否有一套统一的设计哲学，还是东拼西凑：
- 命名是否一致？（同一个概念是否在不同地方用不同名字？）
- 错误处理策略是否一致？（有的地方 raise ConfigurationError，有的地方 raise RuntimeError，有的地方 return None）
- 数据流向是否一致？（dict 直穿 vs dataclass vs Pydantic model 的混用程度）
- 事务边界处理是否一致？（有的在 repository 层 commit，有的在 application 层 commit）
- 是否有明显的"两代代码"痕迹？（旧代码用一种模式，新代码用另一种）

### 维度 2：核心功能的实现质量

**逐个评估核心功能的实现是否精良**（不是"能不能用"，而是"写得好不好"）：

**2a. 事件→提取→存储管线（ingest pipeline）**
- 提取的 chunking 策略是否设计合理？
- claim 规范化（predicate 归一化、canonical_attribute 映射）的规则是否清晰？
- 去重→冲突→归并的决策链是否有遗漏或冗余？
- 证据链（evidence_links）的写入是否可靠？

**2b. 召回管线（recall pipeline）**
- FTS + 向量双路融合的 RRF 实现是否正确？
- 可见性过滤（双时间模型）的实现是否精确？
- reranker 降级策略是否健壮？
- Batch 3 拆分后的阶段函数是否有"假阶段"（只转发不做事）？

**2c. Worker 调度**
- lease token + CAS 的并发控制是否可靠？
- maintenance 循环的触发条件和执行顺序是否合理？
- job handler 注册表是否真正替代了 if-elif？

**2d. LLM 集成**
- Provider 抽象层是否设计得简洁有效？
- 结构化输出降级（json_schema → json_object → text）是否健壮？
- 重试策略是否合理？

**2e. Hermes 适配层**
- Hermes provider 拆分后，三个子组件的职责是否真正分离了？
- 熔断器的实现是否正确？
- prefetch 缓存的失效策略是否合理？

### 维度 3：数据模型设计

- SQLite schema 是否设计得好？字段类型、约束、索引是否合理？
- 双时间模型（valid_from/valid_until + created_at）是否在每个查询中都正确应用了？
- Claim 的 status 状态机是否完整且无歧义？
- namespace/tenant_id 的设计是否实际有意义？

### 维度 4：缺失但应该有的

- 有没有"一个精致的记忆系统应该有但当前缺失"的功能？
- 召回质量评估（precision/recall 监控）缺失？
- 记忆生命周期管理（不仅仅是衰减，还有合并、总结、遗忘）是否完善？
- 错误恢复机制（worker crash 后 job 状态恢复）是否健壮？

### 维度 5：代码品味

- 有没有明显的代码异味（code smell）？
- 有没有过度聪明的实现（难以理解的技巧性代码）？
- docstring 是否解释了"为什么"而不只是"做什么"？
- 是否有违反最小惊讶原则的设计？

## 输出格式

```
# hl_mem v0.6.0 架构品质审查报告

## 总体评价
- 品质评分: X/10
- 一句话总结

## 设计语言一致性评估
[具体分析]

## 核心功能实现质量
### 2a. Ingest Pipeline
- 评分: X/10
- 做得好的地方: ...
- 需要改进的地方: ...
### 2b-2e. ...

## 数据模型设计评估
[具体分析]

## 缺失功能分析
[应该有但没有的]

## 代码品味问题
[具体到文件:行号]

## 最值得改进的 5 个品质问题（优先级排序）
1. ...
2. ...
```

## 约束

- **读完全部源码**
- **基于第一轮的数据事实，不要重复收集**
- **不要修改任何代码文件**
- **不要运行 pytest**
- **诚实评估，但不粗暴——指出问题的同时说明为什么这是问题**
- **将报告写入 docs/review/codex-quality-review-report.md**
- **git add docs/ && git commit -m "docs: architecture quality review for v0.6.0"**
