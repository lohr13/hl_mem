# Phase 5：消除重复

## 背景

Codex 审查发现 P1-5（两套 Hermes provider）、P1-7（工厂逻辑分散）、P1-10（Observation 死代码）。

## 项目位置

`D:/workspace/hl_agent/hl_mem/`

---

## 修改 1：合并两套 Hermes provider

### 问题

两套实现：
- `adapters/hermes/provider.py` (194行) — httpx，默认 URL `127.0.0.1:8000`
- `adapters/hermes/plugin/__init__.py` (298行) — urllib，默认 URL `localhost:8200`

功能大量重叠：熔断器、事件映射、episode 同步、错误判断。

### 修复

1. **`adapters/hermes/provider.py` 作为唯一实现**（httpx 版本，功能更完整）
2. 修改默认 URL 为 `http://127.0.0.1:8200`（统一）
3. `adapters/hermes/plugin/__init__.py` 改为薄适配层，从 provider.py 导入核心类：
   ```python
   """Hermes MemoryProvider 插件入口。
   
   实际实现委托给 adapters.hermes.provider.HermesMemoryProvider。
   """
   from hl_mem.adapters.hermes.provider import HermesMemoryProvider
   
   # 保留 Hermes 的 MemoryProvider 接口所需的工厂函数
   def create_provider(*args, **kwargs):
       return HermesMemoryProvider(*args, **kwargs)
   ```
4. 如果 plugin/__init__.py 有 provider.py 没有的功能（如某些 Hermes hook），将那些移到 provider.py 中
5. **关键**：现有 `from hl_mem.adapters.hermes.plugin import HermesMemoryProvider` 的 import 不能断

---

## 修改 2：集中组件工厂

### 问题

embedder/extractor/reranker 的工厂逻辑分散在：
- `api/server.py` 的 `_make_embedder()` + `_make_reranker()`
- `workers/worker.py` 的 `_make_extractor()` + `_make_embedder()`
- `workers/reclassify.py` 的内联 LLMExtractor 创建

### 修复

1. 新建 `src/hl_mem/components.py`：
   ```python
   """统一组件工厂 — embedder、extractor、reranker 的创建逻辑集中在此。"""
   
   from __future__ import annotations
   import os
   from typing import Any
   
   def make_embedder(config: dict[str, Any] | None = None) -> Any:
       """从环境变量和配置创建 embedder。"""
       # 合并 server.py 和 worker.py 的逻辑
   
   def make_reranker(config: dict[str, Any] | None = None) -> Any | None:
       """从环境变量和配置创建 reranker。"""
   
   def make_extractor(config: dict[str, Any] | None = None) -> Any:
       """从环境变量和配置创建 LLM 提取器。"""
   ```

2. `api/server.py` 的 `_make_embedder()` / `_make_reranker()` 改为调用 `components.make_embedder()` / `components.make_reranker()`
3. `workers/worker.py` 的 `_make_embedder()` / `_make_extractor()` 改为调用 `components.make_embedder()` / `components.make_extractor()`
4. `workers/reclassify.py` 的内联创建改为 `components.make_extractor()`
5. **保留** server.py 和 worker.py 的 `_make_*` 方法签名（作为薄委托），向后兼容

---

## 修改 3：清理死代码

### 问题

- `recall/extended_pipeline.py` (61行) — 从未被任何代码导入
- `recall/observation.py` 的 `ObservationBuilder` — 只在已废弃的 `_build_observation()` 中引用
- `api/pipeline.py` 的 `_build_observation()` no-op — 已废弃

### 修复

1. **删除 `recall/extended_pipeline.py`**（确认从未被导入）
2. **保留 `recall/observation.py`** 不删（它没有主动造成问题，删除可能影响 import 链）
3. **保留 `pipeline.py` 的 `_build_observation()` no-op**（测试 monkeypatch 需要）
4. 在 `recall/observation.py` 顶部加注释标记：
   ```python
   """⚠️ ObservationBuilder 当前未接入正式管线。
   
   REST recall 固定返回 observations=[]。
   此模块保留供未来派生记忆功能使用。
   """
   ```

---

## 约束

1. **不要运行 pytest**
2. **不要修改 tests/ 目录下的任何文件**
3. **向后兼容**：现有 180 个测试必须全部通过
4. **不要新增依赖**
5. **不要问任何问题**
6. 完成后 `git add -A && git commit -m "refactor(dedup): merge Hermes providers + centralize factories + remove dead code"`
