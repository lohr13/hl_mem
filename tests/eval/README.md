# Recall v2 离线评测

本目录实现 M6 的可复现召回评测。数据集保存人可读的关键词绑定，不保存会随重建而变化的 claim/event ID；运行时在指定 SQLite 快照中解析 ID。一个关键词组可以绑定多个同义事实，多段历史则用 `claim_keyword_groups` 合并绑定；任一关键词组完全无匹配时立即报错。

## 运行

```powershell
uv run pytest tests/eval/ -v -m "not real_api"
uv run pytest tests/eval/ -v --eval-db var/eval/recall-v2.db --eval-report var/eval/recall-v2.json
uv run python -m tests.eval.runner --database var/eval/recall-v2.db --report var/eval/recall-v2.json
```

默认使用 FakeEmbedder 且关闭 reranker，不访问外部 API。真实 API 运行前设置 `$env:HL_MEM_EVAL_REAL_API='1'`，并按项目 `.env` 配置 embedding/reranker；真实结果应单独保存，不得覆盖离线基线。

## 构建快照

```powershell
uv run python -m tests.eval.fixtures.build_snapshot --source var/hl_mem.db --target var/eval/recall-v2.db --manifest tests/eval/datasets/recall_v2.manifest.json
```

构建器通过 SQLite backup API 从只读源连接生成一致副本。manifest 只包含哈希、迁移版本、event/claim 数量及 claim 状态计数，不复制敏感原文。快照数据库属于本地测试资产，受仓库的 `*.db` 规则忽略。

## 标签规则

- `binding.claim_keywords`：必须全部出现在同一 claim 的 subject、predicate、value 或 qualifiers 中；所有匹配项都进入 relevant 集合。
- `binding.claim_keyword_groups`：多组 `claim_keywords`，适合旧值/新值分别位于不同 claim 的历史问题；每组至少命中一个 claim。
- `binding.evidence_keywords`：可选；在已绑定 claim 的 event 证据中筛选允许的 evidence ID。
- `expected_keywords` + `keyword_match`：校验返回文本，支持 `all` 或 `any`。
- `expected_type=empty`：不得配置 binding、confidence 或 expected keywords。
