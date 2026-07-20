# HL-Mem

面向 AI Agent 的本地优先、跨会话记忆系统。证据驱动、双时间模型、可解释召回。

## 设计思路

### 为什么自建

现有记忆方案各有侧重但都不完整：

- **Hindsight**：事实提取、时间演化、Observation 归纳做得好，但缺 TTL、Procedure、严格 scope
- **MemOS**：Episode/Trace/Reward/Skill 的经验通道很强，但事实有效期和双时间历史不是它的核心抽象
- **向量库 / 聊天摘要**：只解决相似搜索或压缩，无法处理"何时写入、怎样失效、如何遗忘"

HL-Mem 把两者合并为统一的**事件溯源双通道**设计：事实通道参考 Hindsight，经验通道参考 MemOS，自己实现时间、作用域、遗忘和删除治理。

### 核心架构决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 数据模型 | 不可变事件日志 + 派生记忆 | 原始事件保留后，提取算法可升级重放 |
| 记忆分层 | event → claim → observation | 三层抽象：原始输入、原子事实、归纳知识 |
| 双时间模型 | valid_from/to + recorded_from/to | 区分"事实何时有效"和"系统何时知道" |
| 证据链 | 所有派生记忆必须链接到原始事件 | 防止归纳漂移和自证循环 |
| 矛盾检测 | Conflict Key 收窄 → 确定性规则 | 避免 LLM 全库扫描，先 hash 匹配再分级判定 |
| 存储 | SQLite WAL + FTS5 + 向量 BLOB | 零运维，单文件，暴力余弦首版够用 |
| LLM 提取 | qwen3.7-plus（百炼 Coding Plan） | 已验证的中文提取质量，JSON mode 稳定 |
| Embedding | text-embedding-v4（百炼通用） | MTEB SOTA，dense+sparse 混合，全开源 |

### 首版范围

经过 Hermes × Codex 三轮 review 达成共识（见 [docs/review/consensus.md](docs/review/consensus.md)）：

- **3 种记忆类型**：event + claim + observation
- **2 档 volatility**：ephemeral（带TTL）+ stable
- **2 档 visibility**：private + shared
- **不含**：Experience 通道、Mental Model、MCP Server、多租户（架构设计保留，代码延后）

详见 [ADR-0002](docs/adr/0002-mvp-scope-and-embedding.md)。

## 代码架构

```
src/hl_mem/
├── api/
│   ├── server.py          # FastAPI 服务：POST /v1/events, /v1/recall, /v1/memories, /v1/jobs
│   └── pipeline.py         # 提取管道：dedup → conflict → claim → evidence → observation
├── ingest/
│   ├── extractors.py       # FakeExtractor + ExtractedClaim 数据结构
│   ├── llm_extractor.py    # LLM 提取器（qwen3.7-plus，中文 prompt，predicate 标准化）
│   ├── event_filter.py     # 预过滤：短文本/确认语/工具原始输出 → 跳过 LLM
│   ├── budget.py           # 日 token 预算（自然日重置，JSON 持久化）
│   └── embeddings.py       # text-embedding-v4 Embedder（10条分片，float32 BLOB）
├── recall/
│   ├── conflict.py         # Conflict Key + 确定性 ConflictResolver（无 LLM）
│   ├── dedup.py            # L1 精确去重 + L2 语义去重（cosine > 0.95）
│   └── observation.py      # Observation 生成（≥2 独立证据触发）
├── storage/
│   ├── database.py         # SQLite WAL 连接管理 + migration runner
│   ├── repository.py       # 5 个 Repository：Event/Claim/Evidence/Job/Derivation
│   └── migrations/
│       ├── 001_initial.sql # 5 表 + 2 FTS 虚拟表 + 6 triggers
│       └── 002_claims_fts_subject.sql  # FTS 索引增加 subject
├── workers/
│   ├── worker.py           # 串行 Job 消费者（lease 机制，失败重试 → dead）
│   └── ttl.py              # TTL 自动过期扫描
├── adapters/hermes/
│   └── provider.py         # Hermes Provider（2s timeout + circuit breaker + 无感降级）
└── worker.py               # CLI 入口：python -m hl_mem.worker {run|run-once|status}
```

### 数据流

```
用户对话 → POST /v1/events → 幂等写入 events 表 → 创建 extract_event Job
                                                              ↓
Worker (串行消费) ← EventFilter → 过滤低价值事件
                    ↓ 通过
               LLMExtractor → qwen3.7-plus 提取结构化 claim
                    ↓
               Deduplicator → L1精确去重 + L2语义去重(>0.95)
                    ↓ 新事实
               ConflictResolver → Conflict Key 匹配 → state_change/contradicts/...
                    ↓
               Embedder → text-embedding-v4 2048维 → BLOB 存储
                    ↓
               ObservationBuilder → ≥2独立证据 → 生成 Observation
                    ↓
POST /v1/recall → FTS BM25 + Dense向量 RRF 合并 → 证据化 Context Packet
```

### API 端点

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/v1/events` | 写入事件（幂等），创建提取 Job |
| POST | `/v1/recall` | 混合检索（FTS + Dense），返回带证据的结果 |
| POST | `/v1/memories` | 显式保存（pinned claim，high authority） |
| DELETE | `/v1/memories/{id}` | 显式遗忘（级联：claim→retracted + embedding 清空 + obs→stale） |
| GET | `/v1/jobs` | Job 队列状态 |
| GET | `/v1/stats` | 统计：events、claims、token 消耗 |
| GET | `/healthz` | 健康检查 |

## 快速开始

### 环境要求

- Python >= 3.11
- uv（推荐）或 pip

### 安装

```bash
git clone git@github.com:lohr13/hl_mem.git
cd hl_mem
uv sync
```

### 配置

创建 `.env` 文件（需要两把不同的百炼 key）：

```bash
# LLM 提取 — 百炼 Coding Plan（走 coding 端点）
LLM_API_KEY=sk-sp-xxx          # Coding Plan 专用 AK
LLM_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
LLM_MODEL=qwen3.7-plus

# Embedding — 百炼通用（走 compatible-mode 端点）
EMBEDDING_API_KEY=sk-e72xxx     # 通用 AK（和 Coding Plan 不是同一个 key）
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIM=2048

# 切换模式（默认 fake，测试用）
HL_MEM_EXTRACTOR=llm            # fake | llm
HL_MEM_EMBEDDER=real            # fake | real
```

> **注意**：百炼 Coding Plan AK 只能打 `coding.dashscope` 端点，通用 AK 只能打 `compatible-mode` 端点，两把 key 互不通用。

### 运行测试

```bash
# 离线测试（FakeExtractor + FakeEmbedder，不需要 API key）
python -m pytest tests/ -v

# 真实 API 端到端测试
python tests/e2e_real.py
```

### 启动服务

```bash
# 启动 API server
uvicorn hl_mem.api.server:app --port 9178

# 启动 Worker（另开终端）
python -m hl_mem.worker run
```

## 设计文档

| 文档 | 内容 |
|------|------|
| [docs/README.md](docs/README.md) | 文档入口和阅读顺序 |
| [docs/architecture.md](docs/architecture.md) | 完整架构设计（16 章） |
| [docs/implementation-plan.md](docs/implementation-plan.md) | 分阶段实施计划 |
| [docs/adr/0001-core-strategy.md](docs/adr/0001-core-strategy.md) | ADR：双通道架构选型 |
| [docs/adr/0002-mvp-scope-and-embedding.md](docs/adr/0002-mvp-scope-and-embedding.md) | ADR：首版范围 + Embedding 选型 |
| [docs/review/consensus.md](docs/review/consensus.md) | Hermes × Codex 三轮 review 共识 |
| [docs/research/memos-vs-hindsight.md](docs/research/memos-vs-hindsight.md) | MemOS vs Hindsight 适配分析 |
| [docs/HANDOFF.md](docs/HANDOFF.md) | 项目交接状态 |

## 项目状态

首版 5 周开发完成，35 个测试全绿，真实 API 端到端验证通过。

| 组件 | 状态 |
|------|------|
| SQLite Schema（5表 + FTS + triggers） | ✅ |
| 幂等事件写入 | ✅ |
| LLM 提取（qwen3.7-plus） | ✅ |
| Event Filter + Token Budget | ✅ |
| Embedding（text-embedding-v4 2048d） | ✅ |
| 去重（L1精确 + L2语义） | ✅ |
| 矛盾检测（Conflict Key + 确定性规则） | ✅ |
| Observation 生成 | ✅ |
| 混合召回（FTS + Dense RRF） | ✅ |
| TTL 自动过期 | ✅ |
| 显式遗忘（级联删除） | ✅ |
| Hermes Provider（timeout + circuit breaker） | ✅ |
| 后台 Worker（串行化 + 重试） | ✅ |
| 30 条中文测试集 | ✅ |
| Experience 通道 | 📋 设计完成，代码延后 |
| Mental Model | 📋 设计完成，代码延后 |
| MCP Server | 📋 设计完成，代码延后 |

## License

MIT
