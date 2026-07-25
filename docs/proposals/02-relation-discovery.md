# 关系候选自动发现与轻量主题图实现方案

## 目标与边界

对新 Claim 与同 namespace 的有界邻居池做关系判定，先形成可审计 `RelationProposal`，默认只审计。允许的关系仅为 `about`、`follows`、`supports`、`contradicts`、`summarizes`。不引入图数据库、实体节点表或常驻服务；关系仍落在 SQLite adjacency table，主题父节点复用 `derivations(kind='topic_summary')`。

## 现状与集成点

- `src/hl_mem/domain/relations.py::RelationType/add_relation/get_relations_batch` 已有白名单和 claim adjacency。
- `src/hl_mem/storage/migrations/014_memory_relations.sql` 已有 `memory_relations(from_id,to_id,relation,confidence,evidence_json,created_at)`。
- `src/hl_mem/recall/relation_expansion.py::expand_related_claims()` 已按 hop、edge confidence、衰减和 candidate limit 扩展。
- `src/hl_mem/application/ingest.py::IngestService.store_extracted()` 在事务提交后获得稳定的新 Claim ID，适合幂等排队，不能在写入事务内调用 LLM。
- `src/hl_mem/storage/migrations/013_conflict_cases.sql` 与 `ClaimRepository.insert_conflict_case()` 提供冲突审核路径。
- `derivations` 与 `evidence_links` 已支持派生内容和 claim 证据。

## 协议与模型

在 `src/hl_mem/protocols.py` 增加：

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RelationProposal:
    """模型提出但尚未应用的关系。"""

    from_claim_id: str
    to_claim_id: str
    relation: str
    confidence: float
    rationale: str
    supporting_claim_ids: tuple[str, ...]
    model: str


class RelationDiscoveryProtocol(Protocol):
    """从有界 Claim 对中提出白名单关系。"""

    def propose(
        self,
        source_claim: ClaimRow,
        candidates: list[ClaimRow],
        *,
        max_proposals: int,
    ) -> list[RelationProposal]: ...
```

实现 `LLMRelationDiscoverer`，JSON mode 输出数组。应用层再次校验：两端存在、同 namespace、非自环、关系白名单、confidence 为 `[0,1]`、support IDs 均存在。LLM 只提案，不直接拿 connection。

## 邻居池与 Worker

新建 `src/hl_mem/workers/discover_relations.py`，注册 `discover_relations` job。`store_extracted()` 在成功插入新 Claim 并提交后，以 `relation-discovery:<claim_id>` 幂等键排队；exact/semantic duplicate 不排队。

邻居池在单条参数化 SQL 中生成，必须同 namespace 且 `status IN ('active','disputed')`，排除自己，按以下优先级去重后截断 `pool_limit`（默认 40）：

1. 相同非空 `canonical_slot`；
2. `json_each(topic_tags_json)` 有 tag 交集；
3. `json_each(entities_json)` 有 normalized entity 交集；
4. subject 相同；
5. dense 相似候选补足。

先用 SQL/向量做 bounded pool，再把精简字段发给 LLM；不允许全库 prompt。每 job 最多 40 个候选、10 个 proposal、一次批量模型调用。Worker 复用现有 lease、retry、progress callback 和 `llm_call_spans`。

配置：

```text
HL_MEM_RELATION_DISCOVERY_MODE=off|audit|auto   # 默认 audit；发布初期可由总开关保持 off
HL_MEM_RELATION_DISCOVERY_POOL_LIMIT=40
HL_MEM_RELATION_DISCOVERY_MAX_PROPOSALS=10
HL_MEM_RELATION_AUTO_APPLY_CONFIDENCE=0.90
HL_MEM_RELATION_CONFLICT_CONFIDENCE=0.80
HL_MEM_RELATION_DISCOVERY_MODEL=<现有文本 LLM 配置>
```

rollout 顺序为 `off → audit → auto`。audit 仅写 proposal；auto 仅自动应用高置信且非破坏性的 `about/follows/supports`。`summarizes` 只允许 topic_summary 构建器写入，不从普通 Claim 对自动落边。`contradicts` 达阈值后创建/复用 `conflict_cases`，两条 Claim 置为 disputed 必须走现有 lifecycle guard；不能写一条普通 `memory_relations` 后结束。

## Migration 023：proposal 审计表

完整 DDL：

```sql
CREATE TABLE IF NOT EXISTS relation_proposals (
    id TEXT PRIMARY KEY,
    source_claim_id TEXT NOT NULL,
    target_claim_id TEXT NOT NULL,
    relation TEXT NOT NULL
        CHECK (relation IN ('about','follows','supports','contradicts','summarizes')),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    rationale TEXT NOT NULL,
    supporting_claim_ids_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(supporting_claim_ids_json)),
    model TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('audit','auto')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','applied','conflict_created','rejected','failed')),
    decision_reason TEXT,
    relation_id TEXT,
    conflict_case_id TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY (source_claim_id) REFERENCES claims(id),
    FOREIGN KEY (target_claim_id) REFERENCES claims(id),
    FOREIGN KEY (relation_id) REFERENCES memory_relations(id),
    FOREIGN KEY (conflict_case_id) REFERENCES conflict_cases(id),
    UNIQUE (source_claim_id, target_claim_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_relation_proposals_status
ON relation_proposals(status, created_at);

CREATE INDEX IF NOT EXISTS idx_relation_proposals_source
ON relation_proposals(source_claim_id, created_at);

```

迁移文件为 `src/hl_mem/storage/migrations/023_relation_proposals.sql`。不对既有 `memory_relations` 添加唯一索引，避免合法 v0.11.2 数据中已有重复边导致升级失败。幂等由 proposal 的 `UNIQUE (source_claim_id,target_claim_id,relation)`、`BEGIN IMMEDIATE` 和应用前查询现存边保证；若存在多条旧边，按 `confidence DESC, created_at ASC, id ASC` 选择 winner，不在本 migration 中破坏性清理历史。

## 自动应用事务

proposal 批量校验后开启 `BEGIN IMMEDIATE`：

- 非破坏性关系：幂等插入 `memory_relations`，`evidence_json` 保存 proposal id、模型、rationale 和 supporting IDs；proposal 更新为 `applied`。
- contradicts：用 `compute_claim_pair_key()` 幂等插入 conflict case，执行合法状态转换，proposal 更新为 `conflict_created`。
- 低置信、非法方向或已失活端点：更新为 `rejected` 并记录机器可读 reason。

失败整体 rollback，由 job retry；不留下已应用边但 proposal 仍 pending 的半状态。

## 主题父节点

新建 `src/hl_mem/workers/build_topic_summaries.py`。按 namespace + canonical slot/topic tag 选取至少 `min_support` 个活跃 Claim，用现有 LLM client生成简短主题正文，写入：

```text
derivations.kind = "topic_summary"
derivations.name = "topic:<namespace>:<topic-key>"
derivations.query = <topic-key>
derivations.scope_json = {"namespace": ..., "topic_tag": ... 或 "canonical_slot": ...}
derivations.body = <summary>
derivations.status = "active"
```

每个支持 Claim 写 `evidence_links(derived_type='topic_summary', relation='summarizes', evidence_type='claim')`，使其与现有 `e.derived_type=d.kind` discriminator 一致。父节点不是 Claim，因此不写 `memory_relations`；召回命中 topic_summary 后，通过 evidence links 展开支持 Claim。必须同步扩展三个真实 consumer：`storage/evidence.py::DerivationRepository.list_active_for_claims()` 接受 topic_summary；`recall/recall_pipeline.py::stale_observations()` 将硬编码 observation 改为受控 kind 集并覆盖直接撤回传播；`workers/mental_models.py::MentalModelMaintainer.mark_stale_dependencies()` 周期检查 topic_summary 依赖。proof_count/召回查询统一按 `e.derived_type=d.kind` 处理；support 集变化时重建相同 name 的 derivation，而不是创建不可控树层级。

## SQLite 图遍历

在 `src/hl_mem/domain/relations.py` 增加 `walk_relation_graph()`，参数包括 seed IDs、namespace、max_depth、allowed_relations、min_confidence、limit。核心 SQL：

```sql
WITH RECURSIVE graph(seed_id, node_id, depth, path, weight) AS (
    SELECT :seed_id, :seed_id, 0, '|' || :seed_id || '|', 1.0
    UNION ALL
    SELECT graph.seed_id,
           CASE WHEN mr.from_id = graph.node_id THEN mr.to_id ELSE mr.from_id END,
           graph.depth + 1,
           graph.path || CASE WHEN mr.from_id = graph.node_id THEN mr.to_id ELSE mr.from_id END || '|',
           graph.weight * mr.confidence
    FROM graph
    JOIN memory_relations AS mr
      ON mr.from_id = graph.node_id OR mr.to_id = graph.node_id
    JOIN claims AS next_claim
      ON next_claim.id = CASE WHEN mr.from_id = graph.node_id THEN mr.to_id ELSE mr.from_id END
    WHERE graph.depth < :max_depth
      AND mr.relation IN ('about','follows','supports','contradicts','summarizes')
      AND mr.confidence >= :min_confidence
      AND next_claim.namespace_key = :namespace
      AND instr(graph.path, '|' || next_claim.id || '|') = 0
)
SELECT seed_id, node_id, depth, path, weight
FROM graph
WHERE depth > 0
ORDER BY weight DESC, depth ASC, node_id ASC
LIMIT :limit;
```

实际实现为白名单生成 `IN` placeholders，不能拼接用户字符串；默认 max_depth=1，上限 3。

## 文件变更

- 新建 `workers/discover_relations.py`、`workers/build_topic_summaries.py`、migration 023。
- 修改 `protocols.py`、`settings.py`、`components.py`、`workers/worker.py`、`application/ingest.py`。
- 修改 `domain/relations.py::add_relation/get_relations_batch` 并新增 CTE 遍历。
- 修改 `recall/observation.py` 或新增 topic summary assembler，使派生节点进入 packed context。

## 测试计划

- 邻居池：namespace 隔离、slot/tag/entity/subject 优先级、去重、硬上限、无全表 prompt。
- 协议：非法关系、自环、跨 namespace、越界 confidence、失活端点均拒绝。
- migration：空库和从 022（含重复旧边）升级；proposal 并发幂等；FK 与 CHECK 生效。
- worker：audit 不写边；auto 只应用高置信 about/follows/supports；summarizes 普通提案拒绝。
- conflict：contradicts 只创建一个 pair case，状态转换原子，失败 rollback。
- topic summary：复用 derivations/evidence_links、support 不足不生成、Claim 撤回后 stale。
- CTE：环检测、深度/置信度/limit、namespace 隔离、确定性顺序。
- 召回回归：关系发现关闭时行为不变；已有 relation expansion 可消费自动边。
