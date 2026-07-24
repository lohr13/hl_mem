# Phase 18: topic_tags 检索接入（D + B）

## 目标

按 Codex 设计方案，分两步将 topic_tags 接入检索管线：
- Step 1: 方案 D（soft boost）— 候选集内标签匹配加分
- Step 2: 方案 B（独立 tag channel）— tags FTS 查询 + RRF 融合

两步一起实现，feature flag 控制。

---

## Step 1: Soft Boost (D)

### 1.1 新建 query tag 解析模块

文件：domain/claims/query_tags.py（新建）

```python
# 从用户 query 提取 topic_tags
LOW_INFORMATION_TAGS = frozenset({"other", "fact", "state", "choice", "config", "plan", "preference"})

def extract_query_tags(query: str) -> list[str]:
    """确定性中英文 query→tag 映射。

    策略：
    1. 英文单词直接匹配 ALLOWED_TOPIC_TAGS（case-insensitive）
    2. 中文关键词→tag 映射表（architecture→架构, decision→决策, bugfix→修复...）
    3. 返回去重后的 tag 列表
    4. 不包含 LOW_INFORMATION_TAGS
    """
```

中文映射表：
```python
CHINESE_TAG_MAP = {
    "架构": "architecture", "设计": "architecture",
    "决策": "decision", "决定": "decision",
    "需求": "requirement",
    "实现": "implementation",
    "修复": "bugfix", "bug": "bugfix",
    "行为": "behavior",
    "依赖": "dependency",
    "版本": "version",
    "迁移": "migration",
    "评估": "evaluation",
    "工作流": "workflow",
    "测试": "test",
    "部署": "deployment",
    "进程": "process",
    "任务": "job",
    "连接": "connectivity",
    "硬件": "hardware",
    "超时": "timeout",
    "调度": "schedule",
    "路由": "routing",
    "协议": "protocol",
    "框架": "framework",
    "接口": "api",
    "角色": "role",
    "目标": "goal",
    "能力": "capability",
    "约束": "constraint",
    "问题": "issue",
    "原因": "cause",
    "解决": "resolution",
}
```

### 1.2 staged_pipeline.py 加 tag boost

在 RRF semantic 特征计算之后、pre-rank 排序之前：

```python
# Feature: tag overlap boost
if settings.tag_boost_enabled and query_tags:
    for item in candidates:
        claim_tags = set(item.get("topic_tags") or [])
        overlap = query_tags & claim_tags
        # 按信息量加权
        weighted = sum(
            TAG_INFO_WEIGHT.get(tag, 0.5)
            for tag in overlap
            if tag not in LOW_INFORMATION_TAGS
        )
        # 归一化到 [0, 1] 有上限
        item["_tag_boost"] = min(weighted / len(query_tags), 1.0) * settings.tag_boost_weight
        item["feature_by_id"][item["id"]]["tag_boost"] = item["_tag_boost"]
    # 加到 pre_score
    for item in candidates:
        item["_pre_score_with_boost"] = item["_pre_score"] + item.get("_tag_boost", 0.0)
```

关键约束：
- query 无识别标签 → 完全不影响排序
- claim 无标签 → 不加分
- `other` 等低信息量标签不参与
- feature flag 关闭 → 字节级等价

### 1.3 Settings 新增配置

```python
tag_boost_enabled: bool = True
tag_boost_weight: float = 0.05  # 很小的权重，先做 tie-breaker
```

### 1.4 Trace/audit

search_trace 增加：
- `query_tags`: list[str]
- `tag_boost_applied`: bool

---

## Step 2: 独立 Tag Channel (B)

### 2.1 新建 tag FTS 表

文件：storage/migrations/018_claims_tags_fts.sql

```sql
-- 独立的 tags FTS 表
CREATE VIRTUAL TABLE IF NOT EXISTS claims_tags_fts USING fts5(
    tags_text,
    content='claims',
    content_rowid='rowid'
);

-- 同步 triggers
CREATE TRIGGER IF NOT EXISTS claims_tags_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_tags_fts(rowid, tags_text)
    VALUES (new.rowid, COALESCE(new.topic_tags_json, ''));
END;
CREATE TRIGGER IF NOT EXISTS claims_tags_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_tags_fts(claims_tags_fts, rowid, tags_text)
    VALUES ('delete', old.rowid, COALESCE(old.topic_tags_json, ''));
END;
CREATE TRIGGER IF NOT EXISTS claims_tags_au AFTER UPDATE ON claims BEGIN
    INSERT INTO claims_tags_fts(claims_tags_fts, rowid, tags_text)
    VALUES ('delete', old.rowid, COALESCE(old.topic_tags_json, ''));
    INSERT INTO claims_tags_fts(rowid, tags_text)
    VALUES (new.rowid, COALESCE(new.topic_tags_json, ''));
END;
```

### 2.2 回填 tags FTS

新建回填：从 claims 读取 topic_tags_json 填入 claims_tags_fts。

### 2.3 repository 新增 tag 搜索

文件：storage/claims.py

```python
def search_claims_tags(self, query_tags: list[str], namespace: str,
                       limit: int, as_of: str) -> list[dict]:
    """按 tag 列表做 OR 查询。

    构建查询：tags_text MATCH 'architecture OR decision OR ...'
    返回匹配的 active claim。
    """
```

### 2.4 pipeline 三通道融合

staged_pipeline.py 增加第三个通道：

```python
# Tag channel
if settings.tag_channel_enabled and query_tags:
    tag_results = repo.search_claims_tags(query_tags, namespace, tag_candidate_limit, reference)
    tag_scored = [(item, tag_channel_weight * rrf_rank(i))
                  for i, item in enumerate(tag_results)]
    # 合并到 candidates
```

RRF 动态归一化：当 tag channel 无候选时不计入分母。

### 2.5 Settings 新增

```python
tag_channel_enabled: bool = False  # 默认关闭，评测通过后开启
tag_channel_weight: float = 0.15
tag_candidate_limit: int = 20
```

---

## 约束

- 不要修改现有 tests/（可以新增测试文件）
- 不要运行 pytest
- git add src/ tests/ && git commit
- 不要用 git add -A
- 版本 bump 0.9.1 → 0.10.0
- feature flag 关闭时行为字节级等价
- tag boost 在 RRF 之后、pre-rank 之前
- tag channel 默认关闭（需评测后开启）
