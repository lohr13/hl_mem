# Phase 15 Batch 5: 剩余 P2 清理

## 概述

修复 P2-2、P2-4、P2-5、P2-6、P2-7、P2-8 六个 P2 问题。

---

## P2-2: 减少测试对私有成员的耦合

### 问题
测试直接 monkeypatch `server._queue_event`、调用 `_make_*`、`Worker._dispatch`、`LLMExtractor._claim`，
检查 Hermes provider 的 `_failure_count/_circuit_open_until`。
若干测试仍导入 `hl_mem.api.pipeline`。

### 修复方案

1. **对组件工厂、job handler、claim normalizer、circuit breaker 提供窄的公开测试边界**：
   - 工厂函数提供 `create_embedder_for_test()` 等 test helper
   - circuit breaker 暴露 `state` 只读属性而非依赖 `_failure_count`
   - job handler 提供可独立调用的入口

2. **HTTP 测试用 client 注入替代 patch 全局 httpx.post**

3. **只保留少量白盒测试验证算法纯函数**

**注意：本次不改 tests/。只改 src/ 侧增加公开属性/helper，为后续测试更新提供目标。具体改动：**
- circuit breaker 增加只读 `state` property（返回 open/closed/half_open）
- 工厂函数增加 `*_for_test` 变体（接收依赖注入而非读 settings）
- job handler 提取为可独立调用的模块级函数

---

## P2-4: 拆分 Hermes provider 上帝类

### 问题
`adapters/hermes/provider.py`（358行）一个类同时承担：
同步/异步 HTTP、熔断、后台预取缓存、事件转换、Episode/Trace 推导、Hermes 生命周期 hook。

### 修复方案

**保持对外 `HLMemProvider` 不变，内部组合三个小对象：**

```python
# adapters/hermes/
    provider.py       ← HLMemProvider（对外不变，内部组合下面三个）
    http_client.py    ← HLMemHttpClient（同步/异步 HTTP + 错误处理）
    prefetch.py       ← PrefetchCache（后台预取 + 缓存）
    episode_mapper.py ← EpisodeMapper（事件→Episode/Trace 推导）
```

HLMemProvider 变成薄协调层：
```python
class HLMemProvider:
    def __init__(self, settings):
        self._client = HLMemHttpClient(settings)
        self._cache = PrefetchCache(self._client)
        self._mapper = EpisodeMapper()
        # hooks 委托给这三个对象
```

---

## P2-5: Pydantic 提取 schema 严格性修正

### 问题
`extra="forbid"` + 枚举验证适合不可信 LLM 输出。
但校验前 `_parse_legacy_defaults()` 会补齐全部必填字段（包括空 value），之后再被严格 schema 拒绝部分补值。
"新协议必须严格"与"旧模型可兼容"的边界不清晰。

### 修复方案

1. **将 legacy 兼容做成显式版本 adapter**：
   - 先记录原始缺失字段（不做默认填充）
   - 再验证
   - 如果验证失败且字段标记为 legacy 缺失，走显式兼容路径

2. **新 Provider 默认走严格路径**（不做 legacy 补全）

3. **保留 `_parse_legacy_defaults` 但改为仅在检测到旧格式签名时触发**（而非无条件执行）

---

## P2-6: namespace/tenant 文档明确

### 问题
namespace 只是局部字段，不是端到端隔离。对当前单 Agent 不是缺陷，但需要文档明确。

### 修复方案

1. 在 `api/schemas.py` 和 `application/ingest.py` 的 namespace/tenant_id 相关位置加 docstring：
   ```python
   # NOTE: namespace is a soft label, not an isolation boundary.
   # Background tasks (maintenance, policy induction, archive) use "default" namespace.
   # Multi-tenant isolation requires a dedicated NamespaceContext (future project).
   ```

2. 在 `README.md` 或 `docs/` 加一段说明当前是单租户

**纯文档改动，不改逻辑。**

---

## P2-7: 关系扩展多跳预备

### 问题
`ExpandedCandidate` 只存 `seed_id + 单边`，trace 只记录一条 edge。
升级多跳时必须改候选模型、遍历算法、去环和 trace。

### 修复方案

1. **扩展候选模型**：
   ```python
   @dataclass
   class ExpandedCandidate:
       seed_id: str
       candidate_id: str
       path: tuple[RelationHop, ...]  # 一跳时长度为1，多跳时更长
       cumulative_weight: float       # 路径累积衰减
   ```

2. **配置增加 `max_depth`（默认=1）**：
   ```python
   relation_expansion_max_depth: int = 1  # 1=当前行为
   ```

3. **算法改为有界 BFS**：
   - visited set 防环
   - 每跳衰减
   - 总扩展预算上限
   - `max_depth=1` 时行为与当前完全一致

4. **trace 记录完整 path**

---

## P2-8: 集中 magic number

### 问题
packed context 默认 2000、候选下限 50、RRF 常量 60、偏好加权 0.12、Hermes 熔断阈值/窗口、策略归纳 7天/3次等散落在实现中。
`config.py` 声称"所有 magic number 都在此定义"与实际不符。

### 修复方案

1. **需要运维/实验调整的策略值放到 `Settings`**：
   - packed context 默认长度
   - 候选下限
   - 偏好加权
   - 熔断阈值/窗口

2. **纯算法常量保留在所属模块但命名**：
   - RRF 常量 `RRF_K = 60`
   - 不要把每个数字都变成环境变量

3. **`config.py` 注释更新**：明确说明哪些在 Settings、哪些是算法常量

---

## 约束

1. **不要修改 tests/ 目录下的任何文件**
2. **不要运行 pytest**
3. **完成后运行**：`git add src/ && git commit -m "refactor(cleanup): split hermes provider, fix schema defaults, prep multi-hop, centralize magic numbers"`
4. **不要用 `git add -A`**
5. **HLMemProvider 对外接口不变**（hooks 签名、方法名不变）
6. **max_depth=1 必须与当前一跳行为完全一致**（向后兼容）
7. **版本号 bump**：从 Batch 1 后的版本再 +1（如 0.5.0 → 0.6.0）
