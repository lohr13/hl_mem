# Phase 17 Stage 1: 加法式 Schema + 双写 + 回填 dry-run

## 目标

纯加法，不影响现有行为。新增 canonical_slot + topic_tags_json 两个字段，
新写入双写（旧 canonical_attribute 继续写，新字段同时写），
现有线上行为仍读旧字段。回填只 dry-run 不 apply。

## 具体改动

### 1. 新建 migration 016_claim_slots_and_tags.sql

```sql
-- Phase 17 Stage 1: 新增 canonical_slot 和 topic_tags_json 字段
ALTER TABLE claims ADD COLUMN canonical_slot TEXT NULL;
ALTER TABLE claims ADD COLUMN topic_tags_json TEXT NULL;

-- 部分索引：只索引非 NULL 的 slot，用于冲突检测候选查询
CREATE INDEX idx_claims_slot ON claims(namespace_key, canonical_slot, status)
    WHERE canonical_slot IS NOT NULL;

-- 不删除旧列 canonical_attribute / conflict_key / legacy_conflict_key
```

注册到 database.py 的 migration 列表。

### 2. 新建 SlotDefinition Registry — 改造 attributes.py

把当前散乱的 PREDICATE_ATTRIBUTE_MAP + MUTUALLY_EXCLUSIVE_SLOTS + ATTRIBUTE_HINTS
合并为统一的 SLOT_REGISTRY。

```python
@dataclass(frozen=True)
class SlotDefinition:
    name: str                    # e.g. "config.port"
    predicate: str               # e.g. "配置"
    description: str             # 中文定义
    participates_in_conflict: bool   # 是否参与互斥冲突
    ttl_class: str               # "none" | "short" | "medium"
    required_qualifiers: list[str]   # 必需的限定键
    aliases: list[str]           # 匹配用的别名
    examples: list[str]          # 正例

SLOT_REGISTRY: dict[str, SlotDefinition] = {
    "preference.ui_theme": SlotDefinition(
        name="preference.ui_theme",
        predicate="偏好",
        description="UI 主题偏好（深色/浅色）",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=[],
        aliases=["theme", "主题"],
        examples=["深色模式", "浅色模式"],
    ),
    "preference.response_style": SlotDefinition(
        name="preference.response_style",
        predicate="偏好",
        description="回复风格偏好（简洁/详细/幽默）",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=[],
        aliases=["style"],
        examples=["简洁", "详细"],
    ),
    "preference.tool_choice": SlotDefinition(
        name="preference.tool_choice",
        predicate="偏好",
        description="工具选择偏好",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=["task"],
        aliases=[],
        examples=["Codex CLI 修改代码"],
    ),
    "choice.tool": SlotDefinition(
        name="choice.tool",
        predicate="使用",
        description="使用的工具",
        participates_in_conflict=False,  # 多工具可共存
        ttl_class="none",
        required_qualifiers=["role"],
        aliases=[],
        examples=["Hermes Agent", "Bitwarden CLI"],
    ),
    "choice.database": SlotDefinition(
        name="choice.database",
        predicate="使用",
        description="使用的数据库",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=["project"],
        aliases=[],
        examples=["PostgreSQL", "SQLite"],
    ),
    "choice.model": SlotDefinition(
        name="choice.model",
        predicate="使用",
        description="使用的 LLM 模型",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=["task"],
        aliases=[],
        examples=["glm-5.2", "qwen3.7-plus"],
    ),
    "choice.provider": SlotDefinition(
        name="choice.provider",
        predicate="使用",
        description="使用的服务商",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=["service"],
        aliases=[],
        examples=["智谱", "百炼"],
    ),
    "choice.memory_system": SlotDefinition(
        name="choice.memory_system",
        predicate="使用",
        description="使用的记忆系统",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=["project"],
        aliases=[],
        examples=["hl_mem"],
    ),
    "config.port": SlotDefinition(
        name="config.port",
        predicate="配置",
        description="服务端口",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=["service"],
        aliases=["port"],
        examples=["8200", "10808"],
    ),
    "config.path": SlotDefinition(
        name="config.path",
        predicate="配置",
        description="文件路径",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=["purpose"],
        aliases=["path"],
        examples=["D:/workspace/hl_agent/hl_mem"],
    ),
    "config.env": SlotDefinition(
        name="config.env",
        predicate="配置",
        description="环境变量",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=["key"],
        aliases=["env"],
        examples=["HL_MEM_PORT=8200"],
    ),
    "config.network": SlotDefinition(
        name="config.network",
        predicate="配置",
        description="网络配置（代理/路由/Tailscale）",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=["target"],
        aliases=["network"],
        examples=["VLESS proxy on 10808"],
    ),
    "state.service_health": SlotDefinition(
        name="state.service_health",
        predicate="状态",
        description="服务健康状态",
        participates_in_conflict=True,
        ttl_class="short",  # 状态变化快
        required_qualifiers=["service"],
        aliases=["health"],
        examples=["running", "stopped"],
    ),
    "identity.name": SlotDefinition(
        name="identity.name",
        predicate="身份",
        description="用户名称",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=[],
        aliases=["name"],
        examples=["本地小马"],
    ),
    "plan.deadline": SlotDefinition(
        name="plan.deadline",
        predicate="计划",
        description="截止日期",
        participates_in_conflict=True,
        ttl_class="none",
        required_qualifiers=["plan"],
        aliases=["deadline"],
        examples=["Phase 17 完成时间"],
    ),
}

# topic_tags 受控标签集
ALLOWED_TOPIC_TAGS = frozenset({
    "fact", "preference", "config", "state", "identity", "plan", "choice", "memory",
    # 事实角色
    "implementation", "issue", "cause", "resolution", "constraint", "capability",
    "membership", "tool_choice", "behavior",
    # 主题
    "architecture", "decision", "requirement", "bugfix", "dependency",
    "version", "migration", "evaluation", "workflow", "test", "deployment",
    "process", "job", "connectivity", "hardware", "timeout", "schedule",
    "routing", "protocol", "framework", "api", "os", "role", "contact", "account",
    "goal", "other",
})
```

保留旧函数（resolve_attribute 等）的兼容接口，但内部改为读 SLOT_REGISTRY。
不要删除旧的 PREDICATE_ATTRIBUTE_MAP 和 MUTUALLY_EXCLUSIVE_SLOTS 常量——
改为从 SLOT_REGISTRY 动态生成，确保单一事实来源。

### 3. 改造提取数据契约

在 ingest/extractors.py 的 ExtractedClaim 中：
- 新增 `canonical_slot: str | None = None`
- 新增 `topic_tags: list[str] = field(default_factory=list)`
- 保留 `canonical_attribute` 字段（兼容）

在 ingest/schemas.py 中：
- JSON schema 新增 `canonical_slot`（enum + null）和 `topic_tags`（array of enum）
- canonical_slot 的 enum 从 SLOT_REGISTRY 动态生成
- topic_tags 的 items enum 从 ALLOWED_TOPIC_TAGS 生成

### 4. 改造 LLM 提取 prompt

在 llm_extractor.py 的 SYSTEM_PROMPT 中：
- 明确区分 canonical_slot 和 topic_tags 的职责
- 展示完整 15 个 slot 的定义、必需 qualifier 和正例
- 明确 abstain 规则："无法确定唯一 operational slot 时返回 null"
- 展示 ALLOWED_TOPIC_TAGS 完整列表
- 从 SLOT_REGISTRY 动态生成 prompt 片段，不要硬编码

### 5. 双写逻辑

在 application/ingest.py 的 _build_claim_drafts 中：
- 新 claim 同时写入 canonical_attribute（旧）和 canonical_slot + topic_tags_json（新）
- canonical_slot 从 ExtractedClaim 中取
- topic_tags 序列化为 JSON 存入 topic_tags_json
- canonical_attribute 仍从旧逻辑计算（兼容）

### 6. 存储层适配

在 storage/claims.py 中：
- insert_claim 接受并写入 canonical_slot 和 topic_tags_json
- _decode_claim 解码 canonical_slot 和 topic_tags_json（JSON parse → list）
- 查询返回的 claim dict 新增这两个字段

### 7. 回填脚本（dry-run only）

新建 storage/migrations/backfill_claim_slots_v1.py：
- 从 DB 读取所有 claims
- 按 SLOT_REGISTRY 白名单回填 canonical_slot
- 旧值不在 registry 的 → canonical_slot = NULL
- 所有旧值转成 topic_tags（保语义）
- 输出统计（多少变 slot、多少变 NULL、tags 分布）
- **默认 dry-run，不写 DB**
- 支持 --apply 参数才真正写入

## 约束

- 不要修改 tests/（Hermes 负责适配测试）
- 不要运行 pytest
- 不要删除任何旧字段或旧函数
- 不要改变现有行为（canonical_attribute 仍是主线）
- git add src/ && git commit
- 不要用 git add -A
- 版本 bump 0.7.0 → 0.7.1
