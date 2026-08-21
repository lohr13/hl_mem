# v0.29.3 patch-point 清单

本文冻结批 1（Recall 拆轻）与批 1b（ingest 拆轻）前，测试直接替换生产符号的
位置。检索范围为 `tests/**/*.py` 中的 `monkeypatch.setattr`、`unittest.mock.patch`
以及会读取 mock 调用参数的 fake。外部库对象（如 `httpx.post`、`time.sleep` 本身）
不作为本批模块搬移契约；但从 `hl_mem` 模块命名空间访问的别名列在“附加点”中。

## Recall：必须保留的调用面

| patch/契约目标 | 现有位置与参数读取 | 批 1 是否允许搬移 | 搬移时兼容要求 |
| --- | --- | --- | --- |
| `hl_mem.application.recall.hybrid_claims` | `test_relevance_gate.py:353` 读取 `kwargs["tracer"]`；`test_m4_recall_semantics.py:175` 读取 `args[4]`（`as_of`）、`kwargs["intent"]`、`kwargs["now"]`；`test_session_context.py:159/181/206/225`；`test_recall_characterization_v0293.py:129/262/299` | 允许搬移实现 | 旧模块属性必须继续可解析，而且 `RecallService.recall()` 必须在旧调用点动态读取该属性，使 monkeypatch 生效。保留前五个位置参数 `repository, query, query_blob, limit, as_of`，以及关键字 `intent`、`now`、`tracer`、`low_recall_expander`。不能把 `as_of` 改成仅关键字参数。 |
| `RecallService._record_access` | `test_relevance_gate.py:369` 附近以实例 patch；fake 接收一个位置参数 `items` | 允许下沉实现，不允许删除入口 | 实例方法名和单参数调用保留；同步路径仍经 `self._record_access(final_claims)`，不能绕过到新模块，否则实例 patch 失效。 |
| `RecallService._assemble_results` | `test_relevance_gate.py:374`；fake 读取两个位置参数 `items, namespace`。`test_recall_score_output.py:71/93/126` 还会直接调用该方法 | 允许下沉实现，不允许删除入口 | 旧方法必须是薄委托，签名/位置顺序保持 `claims, namespace="default"`；`recall()` 继续经实例方法调用。 |
| `RecallService._assemble_observations` | `test_relevance_gate.py:375`；fake 接收一个位置参数 `claim_ids` | 允许下沉实现，不允许删除入口 | 保留实例薄委托和 `list[str] -> list[dict]` 形状；主流程继续从实例分派。 |
| `RecallService._materialize_context_packet` | `test_relevance_gate.py:399`；fake 接收一个位置参数 `bundle` 并读取 `bundle.query_id/answerability/items/used_tokens_estimate/truncated` | 允许下沉实现，不允许删除入口 | 保留实例薄委托；legacy/context_packet/both 与 procedure 分支都继续从该点物化。`retrieval_bundle` 仍在物化前返回。 |
| `low_recall_expander` callback 关键字与旧 pipeline 路径 | `test_session_context.py:201` 从 `kwargs["low_recall_expander"]` 取回 callback；`test_query_expansion.py:292` 直接传该关键字。另有 5 个测试从 `hl_mem.recall.recall_pipeline` 导入 `hybrid_claims`（其中 `test_reranker.py` 还导入 `matching_policies`，`test_tag_boost.py` 还导入 `RecallConfig`） | 允许搬移实现 | `hybrid_claims` 的旧签名必须继续接收 `low_recall_expander=`，callback 形状保持 `(candidate_count, fts_candidate_count)`。`hl_mem.recall.recall_pipeline` 必须继续 re-export `hybrid_claims`、`matching_policies`、`RecallConfig`；旧路径不能只在类型检查时存在。 |

上述第二至第五项即使变成一行委托，也不能在批 1 删除。测试 patch 的是实例属性；
只在新模块保留同名实现不能提供兼容性。

## Recall：grep 发现的附加直接 patch 点

| patch 目标 | 参数依赖 | 批 1 约束 |
| --- | --- | --- |
| `hl_mem.application.recall.recall_procedure` | `test_recall_characterization_v0293.py:300` 的 fake 接受任意参数并返回有序 `MemoryCandidate` 列表 | 可以搬移实现；应用模块仍需保留运行时可 patch 的别名，`_recall_experience()` 继续经该别名调用。 |
| `hl_mem.storage.experience.ExperienceRepository.list_policies` | `test_relevance_gate.py:354` 的 fake 不读取参数 | 非批 1 搬移目标；若改装配边界，旧仓储方法仍须存在。 |
| `hl_mem.storage.events.EventRepository.get_recent_events` | `test_session_context.py:185/226` 的 fake 不读取位置参数，只用于强制异常 | 非批 1 搬移目标；session context 仍须通过该仓储入口读取。 |
| `hl_mem.api.server.RecallService` | `test_session_context.py:76` patch 适配层导入别名 | `api.server` 必须继续导出/引用可替换的 `RecallService` 名称，路由构造不能改成函数内重新导入新类。 |
| `hl_mem.application.recall.ClaimRepository.record_access` | `test_p1_9_recall_side_effects.py:36`；fake 接收任意参数并计数重试 | 可拆 side-effect 实现；旧模块中的 `ClaimRepository` 别名及 `_record_access` 的重试调用面要保留。 |
| `hl_mem.application.recall.current_audit` | `test_p1_9_recall_side_effects.py:57`；零参数 fake | 可下沉；旧模块必须保留运行时查找的零参数 provider，不能在函数默认值中提前绑定。 |
| `hl_mem.application.recall.time.sleep` | `test_p1_9_recall_side_effects.py:37` | 可重构重试器，但在迁移该测试前，旧模块的 retry 仍需经此别名。 |

## Ingest：必须保留的调用面

| patch/契约目标 | 现有位置与参数读取 | 批 1b 是否允许搬移 | 搬移时兼容要求 |
| --- | --- | --- | --- |
| `hl_mem.application.ingest.cosine_similarity` | `test_pipeline.py:125`；fake 接收任意位置参数并返回固定 `0.88` | 允许搬移实现 | 旧模块属性必须继续可解析，语义候选路径仍通过该可替换名字调用。若新模块直接绑定 `hl_mem.core.vector.cosine_similarity`，旧 patch 将失效，必须加兼容委托或同步 patch 点。 |
| `IngestService.store_extracted` | `test_worker.py:127/137` 从 `worker_module.IngestService` 保存原方法并 patch；大量单测直接调用 `hl_mem.application.ingest.IngestService.store_extracted` | 允许拆内部阶段，不允许搬走公开入口 | 保留静态方法及前五个位置参数 `connection, extracted, event, now, embedder`，其余选项继续可用关键字传递。worker 必须继续经其导入的 `IngestService.store_extracted` 分派，使失败注入能截断第二次写入。 |
| `IngestService._queue_event` | `test_extraction_batching.py:265` 读取/调用 `self, event_id, now, commit`；`test_comprehensive_fixes.py:110` 通过完整旧路径 patch | 允许下沉，不允许删除入口 | 保留实例薄委托和 `commit=False` 默认值；单条与批量事件入口继续经实例方法调用，以维持事务失败注入。 |

`store_extracted` 的事务 characterization 还使用 duck-typed connection 代理记录
`execute/commit/rollback/in_transaction`。批 1b 若给 connection 增加具体类型检查，会破坏
`test_ingest_transaction_characterization_v0293.py` 的冻结层；没有必要把这种检查加入生产路径。

## 其余直接 patch 生产符号的 grep 结果

这些点不属于 Recall/ingest 拆分，但后续若改对应模块也应遵循相同原则：patch 的是“使用
点”，不是新实现的定义点。

| 家族 | 当前被 patch 的生产符号 |
| --- | --- |
| 组件/启动 | `hl_mem.components.Embedder`、`hl_mem.components.make_llm_client`、`server.components.make_extractor`、`hl_mem.api.server.run_server`、`hl_mem.api.server.RecallService` |
| CLI/脚本装配 | 多个测试 patch `hl_mem.cli.load_settings`、`hl_mem.cli.make_embedder`；诊断脚本 patch 自身的 `load_settings/make_embedder`；v0.29.1 runner patch `CompatibleStructuredTransport`、`load_cwd_api_key`、`run_sentinel_phase`、`run_behavioral_phase`、`_print_summary` |
| worker 调度 | `decay_claims`、`cleanup_stale_temporal_claims`、`expire_claims`、`review_pending_near_duplicates`、`maintain_expired_claims`、`auto_resolve_conflicts`、`process_recall_side_effect_tasks` |
| 仓储/预算 | `TokenBudget._connect`、`JobRepository.insert_job` |
| 维护与回放脚本 | `hl_mem.workers.reclassify.classify_batch`、temporal replay 的 `_price_cases/_coexistence_cases` |
| 可观测性/重试别名 | `hl_mem.http_utils.time.sleep`、answerable-index backfill 模块的 `time.sleep` |
| MCP/runtime | MCP runtime 的 `run_stdio` |

以上附表不要求批 1/1b 保留无关实现位置；要求是在未来真的搬移对应实现时，旧使用点的
可解析符号或一层兼容委托必须持续存在，直到这些测试显式迁移。
