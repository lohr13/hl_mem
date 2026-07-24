# Phase 17 Stage 3: 跨 subject 语义去重

## 目标

解决同一事实因 subject 不同被重复存储的问题（如 "CN 域名直连" 出现 3 次）。
新增后台 worker 做跨 subject 候选发现 + LLM 判断 + 审计 + 安全合并。

## 前提

Stage 2 已完成（v0.8.0）：
- canonical_slot 已生效，无 slot 的 claim conflict_key = NULL
- dedup 已适配（有 slot 按 slot 隔离，无 slot 按 predicate 查候选）

## 设计原则（来自 Codex 审查）

- **不在 ingest 写事务中调 LLM** — SQLite 写锁不跨远程调用
- **先 audit-only，不改变 claim 状态** — 评估 precision 后再启用自动合并
- **用专用 dedup_pairs 表**，不复用 consolidation_pairs（避免语义混淆）
- **合并用 supersede 语义**，绝不物理删除，evidence link 迁移

## 改动清单

### 1. 新建 migration 017_dedup_pairs.sql

```sql
CREATE TABLE IF NOT EXISTS dedup_pairs (
    id TEXT PRIMARY KEY,
    pair_key TEXT UNIQUE NOT NULL,  -- hash(sorted(left_id, right_id))
    left_claim_id TEXT NOT NULL,
    right_claim_id TEXT NOT NULL,
    namespace_key TEXT NOT NULL DEFAULT 'default',
    similarity REAL NOT NULL,
    embedding_text_version TEXT,    -- "v1: predicate+value"
    policy_version TEXT,            -- dedup policy 版本
    predicate TEXT,
    decision TEXT,                  -- 'equivalent' | 'distinct' | 'uncertain' | NULL(pending)
    judge_confidence REAL,
    judge_reason TEXT,
    judge_model TEXT,
    reviewed_at TEXT,
    applied_at TEXT,                -- 合并执行时间
    created_at TEXT NOT NULL,
    FOREIGN KEY (left_claim_id) REFERENCES claims(id),
    FOREIGN KEY (right_claim_id) REFERENCES claims(id)
);

CREATE INDEX idx_dedup_pairs_decision ON dedup_pairs(decision) WHERE decision IS NULL;
CREATE INDEX idx_dedup_pairs_namespace ON dedup_pairs(namespace_key);
```

### 2. 新建 DedupJudge

文件：domain/claims/dedup.py 或新建 domain/claims/dedup_judge.py

```python
class DedupJudge:
    """LLM 判断两个 claim 是否语义等价。"""

    def judge(self, left: dict, right: dict) -> tuple[str, float, str]:
        """返回 (decision, confidence, reason)
        decision: 'equivalent' | 'distinct' | 'uncertain'
        """
        # 构建对比 prompt（predicate + value + qualifiers + subject）
        # 调用 LLM
        # 解析结构化输出
```

Judge prompt 要点：
- 明确区分：equivalent（同一事实）vs distinct（不同事实）vs uncertain
- 安全护栏：数字、端口、版本、路径、日期、否定词差异 → distinct
- 返回 confidence + reason

### 3. 新建跨 subject 候选发现

文件：storage/claims.py 新增方法

```python
def find_cross_subject_dedup_candidates(
    self, namespace: str, limit: int = 200
) -> list[dict]:
    """发现可能跨 subject 重复的 claim 对。

    策略：
    1. 取所有 active 且无 slot 的 claim
    2. 按 predicate 分组
    3. 同 predicate 内，按 embedding 相似度 > threshold 配对
    4. 返回候选对（不调用 LLM）
    """
```

### 4. 新建后台 worker

文件：workers/deduplicate.py

```python
def deduplicate_claims(connection, llm_client, *, threshold=0.92, audit_only=True) -> dict:
    """跨 subject 去重 worker。

    步骤：
    1. 发现候选对（embedding > threshold，同 predicate，不同 subject）
    2. 写入 dedup_pairs（decision=NULL，待审）
    3. 对每个待审 pair 调用 DedupJudge
    4. 如果 audit_only=True：只记录 decision，不改变 claim 状态
    5. 如果 audit_only=False 且 decision='equivalent'：
       - supersede right_claim → left_claim
       - 迁移 evidence_links
       - 记录 applied_at
    """
```

### 5. 注册 worker job

文件：workers/worker.py
- 新增 "deduplicate_claims" job handler
- 从 Settings 读取 threshold / audit_only 参数
- 每日调度

### 6. Settings 新增配置

文件：settings.py

```python
# 去重配置
dedup_enabled: bool = True
dedup_threshold: float = 0.92
dedup_audit_only: bool = True  # 首版只审计
dedup_scan_limit: int = 200
dedup_cron: str = "03:00"
```

## 约束

- 不要修改 tests/
- 不要运行 pytest
- 写事务中不调 LLM
- 合并用 supersede 语义，不物理删除
- evidence_links 必须迁移
- git add src/ && git commit
- 不要用 git add -A
- 版本 bump 0.8.0 → 0.8.1
