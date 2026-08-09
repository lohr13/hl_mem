# v0.24.2 LongMemEval P0 修复设计

## 目标与边界

本轮在 v0.24.1/`fa29a91` 上完成归因报告中的 P0-1 至 P0-7、P1-1 窄版和 P1-4，作为 LongMemEval 500 条全量运行前门禁。不修改评测数据集，不调整生产语义阈值 0.82/0.92/0.95，不引入全量 turn-vector schema。

## 方案选择

采用“局部生产修复 + benchmark 窄适配”方案：时间解析、preference finalization 和 assistant durable-output 提取属于生产行为，直接在现有纯函数/提取 prompt 中修正；speaker-aware ingest、reader 分流、指标聚合和 raw assistant fallback 属于 LongMemEval adapter，保持在 `evaluation/tools`。相比新增 turn-vector schema，此方案满足本轮验收且不会扩大 migration 和在线召回范围；相比只改 reader，它能修复入口证据丢失和事件 speaker 语义错位。

## 模块设计

1. 时间解析：英文数字 token 接受普通数字和合法三位逗号分组数字，并用左右边界拒绝逗号尾部子串。数千年 year offset 不映射到 conversation-relative datetime；所有相对 offset 的 `ValueError`/`OverflowError` 按 match 隔离。`_shift_months` 在调用 calendar/datetime 前校验 1..9999 年。
2. Preference finalization：从已有全局/reranker 顺序中取最多三个 preference 作为保留槽，其余候选继续按原顺序填充，不再把全部 preference 拼到 others 前。
3. Assistant durable output：中英文 compact prompt 明确允许可再次引用的表格行、编号项、脚本设定、联系人和工具映射；仍跳过寒暄、复述、假设、普通建议和无法原子化的整段通识回答，每 chunk 仍最多十条。
4. Speaker-aware ingest：LongMemEval 每个 turn 生成一个 event，`actor_type`/`source_role` 保存真实角色，`session_id` 与 `benchmark_locator.turn_index/span` 保持会话和跨度关系。semantic subject 仍由 claim 内容决定。事件模型版本进入 cache manifest，使旧 session-as-user 缓存明确失效；不需要数据库 migration。
5. Reader：事实题保持闭卷确定性规则；recommendation preference 允许以真实记忆为约束合成新推荐对象，并要求明确说明使用了哪些偏好。Temporal 题要求按问题时间选择当时最新有效基准，再应用星期条件或相对偏移，历史问题禁止借用当前值。
6. 聚合：claim Recall/MRR 仍只在 claim-eligible case 上计算，但 coverage 在所有成功且有 retrieval 记录的 case 上计算；session 指标独立使用 `session_eligible` 子集；报告同时给出 eligible numerator/denominator。
7. Raw assistant fallback：只对 `single-session-assistant` 或问题中明确回指此前列表/表格/脚本的场景启用。对当前 benchmark namespace 的 assistant events 执行宽松 OR-FTS，按最佳 turn 选择 Top-1 session，再保留该 session 的 Top-1 turn/span。该 event 排在 claim evidence 前、按 event_id 去重，并与其他 evidence 共同受 1,200-token 配额约束。
8. 429：不改变共享重试算法；文档明确单进程/低并发、限流窗口后 `--resume`、保持 dataset/model/context/package identity 一致的恢复流程。

## 错误处理与兼容性

异常时间表达只丢弃自身，不影响同一 claim 的其他合法时间，也不影响 case。Raw FTS 空 token、FTS 语法错误或无匹配时返回空 fallback，不影响 claims 路径。逐 turn event 使用现有表字段和 FTS trigger，不新增 migration；旧 benchmark cache 因 manifest 中事件模型版本不匹配而拒绝 `--skip-ingest`/`--resume` 复用。

## 验证

先增加行为回归测试，再实现。遵循用户约束不在本地运行 pytest；可用 `unittest` 执行兼容的定向 RED/GREEN，最终推送 main 后以 GitHub Actions 全量 pytest 为准。本地必须运行 black、isort、全仓 ruff、mypy 与 `check_docs_consistency.py`。
