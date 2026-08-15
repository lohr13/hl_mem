# HL-Mem 项目交接状态

> 最后更新：2026-08-15

## 当前状态

- **分支**：`main`
- **版本**：v0.26.0
- **阶段**：v0.26.0 已发布；部署由维护者单独执行
- **服务**：FastAPI 默认监听 8200；非敏感配置来自工作目录下的 `hl_mem.toml`
- **存储**：SQLite WAL + FTS5 + 向量 BLOB；默认 `sqlite_scan`，可选 `sqlite_vec`
- **Schema**：42 migrations（SQL 001–042），只允许向前迁移
- **密钥**：`LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`

## v0.26 已交付

- 提取评测 v2：原子事实、来源 Event、角色方向、专名、speaker、canonical subject、禁止传播、modality、
  多次采样和 dedup 消费者契约均有公开合成 fixture 与确定性指标。
- Active claim 收敛：摄入按整个 conflict 组处理，reclassify 具备碰撞守卫，CLI 提供只读 audit 与显式
  `repair --dry-run/--apply`；不确定组进入 disputed。
- Abstention：`no_evidence` 是阻断 reader 的 hard abstention；`low_confidence` 是继续 QA 的 soft 元数据；
  默认保持 observe。
- E2E scorer：official anchors 保持严格，只对人工审核的开放描述题使用概念组 AND、同义表达 OR 的
  `deterministic-rubric-v2`。

以下实验未进入产品：entities hybrid、额外专名 prompt、默认开启 abstention enforce。冻结数据和失败差额见
[CHANGELOG](CHANGELOG.md)。

## 当前评测

- 提取：`tests/eval/test_extraction_v2.py`，公开合成 fixture。
- 隔离检索：PerLTQA 64 + MemDaily 48，共 112 case。
- E2E：PerLTQA 28 + MemDaily 12，共 40 case；代码回归使用同一提取缓存和同一 scorer 做版本 A/B。
- 完整 benchmark：LongMemEval、MemDaily、PerLTQA runner 位于 `evaluation/tools/`。

真实或含个人信息的语料统一放在 `~/hl_mem_eval_data/`；缓存和报告放在 `var/eval/`。运行方法见
[`tests/eval/README.md`](../tests/eval/README.md) 和 [`evaluation/README.md`](../evaluation/README.md)。

## 下一步

- 对投资关系链做 evidence-group context A/B，要求叶子证据包含完整角色与标的，并保留“推荐≠执行”负例。
- 补强 answer-entity/role coverage，避免 event-level R@5 掩盖答案实体未进入上下文。
- 图片输入、Mental Model 和多租户继续作为独立版本决策，不视为 v0.26 未完成项。

## 已知限制

- “高盛债券/大宗商品”类问题可能命中来源 Event，却因相关叶子 Claim 未进入 reader 上下文而拒答。
- LLM 提取和 QA 具有采样波动；不同提取缓存或不同 scorer 的单轮数字不可直接比较。
- `low_confidence` 只标注、不阻断；调用方需要根据自身风险决定是否展示答案。
- namespace 是数据分区键，不是安全边界。

## 当前规范

- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [REST API](api.md)
- [Capability matrix](capability-matrix.md)
- [Compatibility policy](compatibility.md)
- [Changelog](CHANGELOG.md)
- [Historical archive](archive/)
