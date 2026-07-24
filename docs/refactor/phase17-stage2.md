# Phase 17 Stage 2: slot 行为切换 → v0.8.0

## 目标

从"读 canonical_attribute"切换到"读 canonical_slot"。
conflict、dedup、recall、TTL 全部改用新字段。
canonical_attribute 降级为兼容字段，不再主线使用。

## 前提

Stage 1 已完成（commit 1248073）：
- canonical_slot + topic_tags_json 已落地
- SLOT_REGISTRY 有 55 个 attribute，15 个 operational
- 双写已生效
- 回填 dry-run 正常

本阶段需要先 apply 回填（用 --apply 把 slot/tags 写入 DB），然后切换读取逻辑。

## 改动清单

### 1. 执行回填 apply

运行 backfill_claim_slots_v1.py --apply 把 dry-run 结果写入 DB。

### 2. conflict_key 改用 canonical_slot

文件：domain/claims/conflicts.py — compute_conflict_key()

当前：conflict_key = hash(subject + predicate + canonical_attribute)
改为：
- 有 canonical_slot → conflict_key = hash(subject + predicate + canonical_slot + qualifier_key)
  其中 qualifier_key 从 qualifiers 中提取 SLOT_REGISTRY 要求的 required_qualifiers
- 无 canonical_slot (NULL) → conflict_key = NULL（不参与确定性冲突）
- 保留 legacy_conflict_key 字段不变

文件：application/ingest.py — _find_resolution()
- 只查询 conflict_key IS NOT NULL 的冲突候选
- 无 conflict_key 的 claim 不做确定性冲突检查

### 3. 去重逻辑适配

文件：domain/claims/dedup.py — Deduplicator.find_duplicate()

当前：候选要求 canonical_attribute 相同
改为：
- 有 slot 的 claim：按 (namespace, slot, qualifier_key) 查询候选（精确隔离）
- 无 slot 的 claim：按 (namespace, predicate) 查询候选，然后用 embedding/value 判定
- topic_tags 不参与去重隔离（tags 不同的 claim 不被隔离）
- 保留同 subject 的 0.82 语义去重行为

### 4. 召回管线适配

文件：recall/staged_pipeline.py
- preference 判定从 SLOT_REGISTRY 查（predicate == "偏好"），不再字符串搜索 canonical_attribute
- recall 输出携带 canonical_slot 和 topic_tags
- FTS 过滤不变（仍索引 subject/predicate/value）

### 5. scope/importance 标准化改用 slot

文件：ingest/llm_extractor.py
- normalize_scope()：从 SLOT_REGISTRY 的 slot ttl_class 推断 scope（short → temporal，none → permanent），不再依赖 canonical_attribute 字符串前缀
- _is_low_value_claim()：改用 predicate + slot 判断，不依赖字符串前缀

### 6. reclassify 适配

文件：workers/reclassify.py — reclassify_defaults()
- 读 canonical_slot 而非 canonical_attribute
- 更新分类时重算 conflict_key（用新逻辑）

### 7. TTL worker 适配

文件：workers/ttl.py — expire_claims()
- 移除 volatility 条件（不再区分 ephemeral/stable 做过期判断）
- 只看 expires_at <= now()
- 但保留状态守卫（不 expire 正在 active 的 service claim）

### 8. 存储查询适配

文件：storage/claims.py
- find_active_for_dedup() 改为按 canonical_slot 有界查询（不再全 namespace 读后 Python 过滤）
- 新增 find_cross_predicate_candidates()：按 (namespace, predicate) 查无 slot 候选
- update_classification() 原子更新 slot + expires_at

### 9. 版本 bump

0.7.1 → 0.8.0

## 约束

- 不要修改 tests/（Hermes 负责）
- 不要运行 pytest
- canonical_attribute 列保留（降级为兼容），不删除
- legacy_conflict_key 不变
- git add src/ && git commit
- 不要用 git add -A
