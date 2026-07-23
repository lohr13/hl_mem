# Phase 9：5 个改进项

## 项目位置
`D:/workspace/hl_agent/hl_mem/`

---

## 改进 1：轻量层次/关系组织

### 修复

新建 migration `014_memory_relations.sql`：
```sql
CREATE TABLE IF NOT EXISTS memory_relations (
    id TEXT PRIMARY KEY,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    relation TEXT NOT NULL,  -- summarizes/supports/follows/about/contradicts
    confidence REAL DEFAULT 1.0,
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (from_id) REFERENCES claims(id),
    FOREIGN KEY (to_id) REFERENCES claims(id)
);
CREATE INDEX IF NOT EXISTS idx_relations_from ON memory_relations(from_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON memory_relations(to_id);
INSERT INTO schema_migrations (version, applied_at) VALUES ('014_memory_relations', datetime('now'));
```

新建 `src/hl_mem/domain/relations.py`：
- `RelationType` 枚举
- `add_relation(connection, from_id, to_id, relation, confidence)` 函数
- `get_relations(connection, claim_id, direction="both")` 函数

在 `recall.py` 的 `_assemble_results()` 中，为每条 claim 附带 `relations: [...]`。

---

## 改进 2：多模态输入支持（ContentPart 协议）

### 修复

新建 `src/hl_mem/domain/content.py`：
```python
from __future__ import annotations
from typing import Any, Protocol

class ContentPart(Protocol):
    """多模态内容部分的协议。"""
    mime_type: str
    def to_text(self) -> str: ...
    def source_uri(self) -> str | None: ...

class TextPart:
    """纯文本内容。"""
    def __init__(self, text: str) -> None:
        self.text = text
        self.mime_type = "text/plain"
    def to_text(self) -> str:
        return self.text
    def source_uri(self) -> str | None:
        return None

class FileTextPart:
    """从文件提取的文本内容。"""
    def __init__(self, text: str, filename: str, source_uri: str | None = None) -> None:
        self.text = text
        self.filename = filename
        self.mime_type = "text/plain"
        self._source_uri = source_uri
    def to_text(self) -> str:
        return f"[file: {self.filename}]\n{self.text}"
    def source_uri(self) -> str | None:
        return self._source_uri

def parse_content(content: dict[str, Any] | str) -> list[TextPart | FileTextPart]:
    """从事件 content 中解析内容部分。"""
    if isinstance(content, str):
        return [TextPart(content)]
    parts: list[TextPart | FileTextPart] = []
    if text := content.get("text"):
        parts.append(TextPart(text))
    if files := content.get("files"):
        for f in files:
            if isinstance(f, dict) and f.get("text"):
                parts.append(FileTextPart(f["text"], f.get("filename", "unknown"), f.get("uri")))
    return parts or [TextPart(str(content))]
```

更新 `llm_extractor.py` 的 `extract()` 方法：使用 `parse_content()` 统一解析输入。

---

## 改进 3：提取器扩展点

### 修复

1. 在 `protocols.py` 中添加：
```python
class ExtractorProtocol(Protocol):
    def extract(self, content: dict[str, Any], context: dict[str, Any] | None = None) -> list[Any]: ...
```

2. 在 `components.py` 中添加 `make_extractor_for_type(event_type: str)` 函数：
```python
_EXTRACTOR_REGISTRY: dict[str, str] = {
    "message": "llm",
    "explicit_memory": "explicit",
    "tool_result": "llm",
}

def make_extractor_for_type(event_type: str, config: dict[str, Any] | None = None) -> Any:
    """根据事件类型选择合适的提取器。"""
    extractor_name = _EXTRACTOR_REGISTRY.get(event_type, "llm")
    if extractor_name == "explicit":
        return "explicit"  # 特殊标记，由 worker 处理
    return make_extractor(config)
```

---

## 改进 4：偏好专用召回策略

### 修复

1. 在 `domain/temporal.py` 的 `RecallIntent` 枚举中添加 `PREFERENCE`：
```python
class RecallIntent(str, Enum):
    CURRENT_STATE = "current_state"
    HISTORICAL = "historical"
    PREFERENCE = "preference"
```

2. 在 `recall_pipeline.py` 的 `hybrid_claims()` 中，当 intent=PREFERENCE 时：
   - 优先返回 `canonical_attribute` 包含 "preference"/"preference_" 的 claim
   - 保证至少 3 条偏好类 claim（如果有）
   - 偏好 claim 使用更高的 recency 权重

3. 在 `route_recall_intent()` 中增加偏好检测关键词。

---

## 改进 5：配置集中校验

### 修复

新建 `src/hl_mem/settings.py`：
```python
"""集中化配置入口。启动时解析一次，校验组合合法性。"""
from __future__ import annotations
import os
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Settings:
    """全局配置快照。"""
    environment: str = "dev"
    database_path: str = "var/hl_mem.db"
    
    # Embedder
    embedder_mode: str = "fake"
    embedding_dim: int = 2048
    embedding_model: str = "text-embedding-v4"
    
    # Reranker
    reranker_mode: str = "off"
    
    # LLM
    llm_model: str = "qwen3.7-plus"
    
    # Worker
    worker_poll_interval: float = 2.0
    worker_maintenance_interval: float = 600.0

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.getenv("HL_MEM_ENV", "dev").lower()
        production = env == "production"
        s = cls(
            environment=env,
            database_path=os.getenv("HL_MEM_DB_PATH", "var/hl_mem.db"),
            embedder_mode=os.getenv("HL_MEM_EMBEDDER", "real" if production else "fake"),
            embedding_dim=int(os.getenv("EMBEDDING_DIM", "2048")),
            embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
            reranker_mode=os.getenv("HL_MEM_RERANKER", "real" if production else "off"),
            llm_model=os.getenv("LLM_MODEL", "qwen3.7-plus"),
            worker_poll_interval=float(os.getenv("HL_MEM_WORKER_POLL_INTERVAL", "2.0")),
            worker_maintenance_interval=float(os.getenv("HL_MEM_WORKER_MAINTENANCE_INTERVAL", "600")),
        )
        s._validate()
        return s

    def _validate(self) -> None:
        """校验配置组合合法性。"""
        if self.environment == "production":
            if self.embedder_mode != "real":
                raise ConfigurationError("HL_MEM_EMBEDDER must be 'real' in production")
            if self.reranker_mode not in {"on", "real"}:
                raise ConfigurationError("HL_MEM_RERANKER must be enabled in production")
            if not os.getenv("LLM_API_KEY"):
                raise ConfigurationError("LLM_API_KEY is required in production")
            if not os.getenv("EMBEDDING_API_KEY"):
                raise ConfigurationError("EMBEDDING_API_KEY is required in production")

    def snapshot(self) -> dict:
        """返回非敏感配置快照（用于 healthz/audit）。"""
        return {
            "environment": self.environment,
            "embedder_mode": self.embedder_mode,
            "embedding_dim": self.embedding_dim,
            "reranker_mode": self.reranker_mode,
            "llm_model": self.llm_model,
        }
```

在 `healthz` 端点返回配置快照。

---

## 约束
1. 不要运行 pytest
2. 不要修改 tests/ 目录下的任何文件
3. 向后兼容：现有测试必须全部通过
4. 不要新增依赖
5. 不要问任何问题
6. 完成后 `git add -A && git commit -m "feat: 5 improvements — relations, multimodal, extractor routing, preference recall, settings"`
