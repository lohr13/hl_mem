# HL-Mem 项目交接状态

> 最后更新：2026-08-16

## 当前状态

- **分支**：`main`
- **版本**：v0.28.1
- **阶段**：v0.28.1 热修发版准备；tag、Release 与 PyPI 由维护者验收后执行
- **服务**：FastAPI 默认监听 8200；非敏感配置来自工作目录下的 `hl_mem.toml`
- **存储**：SQLite WAL + FTS5 + 向量 BLOB；默认 `sqlite_scan`，可选 `sqlite_vec`
- **Schema**：44 migrations（SQL 001–044），只允许向前迁移
- **密钥**：`LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`

## v0.28 已交付

- forget、archived cleanup 与 restore 共用可审计的物理删除语义；独立 tombstone sidecar、主库 identity
  绑定、backup manifest v2 和 restore replay 共同防止旧备份复活已删内容，语义不清时 fail-closed。
- migration 044 为关系边增加 valid time，并在终态 Claim 转移时关闭边；relation expansion 同时校验边和
  两端 Claim 可见性。integrity audit 分类报告 evidence/relation/derivation/supersede dangling 引用。
- 提取 Job 写入数在 complete 前逐窗口持久化；canonical-slot 窄修在 v0.27 固定缓存上修复 16/16 误配且
  无新增误配。ExperienceService 改为组合，worker 的 job handler/维护调度边界已抽离。
- 提取关系语义两轮冻结 A/B 都未通过端到端门禁，最终不产品化且不跑 sealed v3；C1–C5/f4 与
  source-first dormant 实验代码已删除，保留通用 scorer、sealed/coverage/pilot 防护工具。

## 当前评测

- 提取：`tests/eval/test_extraction_v2.py`，公开合成 fixture。
- 隔离检索：PerLTQA 64 + MemDaily 48，共 112 case。
- E2E：PerLTQA 28 + MemDaily 12，共 40 case；代码回归使用同一提取缓存和同一 scorer 做版本 A/B。
- 完整 benchmark：LongMemEval、MemDaily、PerLTQA runner 位于 `evaluation/tools/`。

真实或含个人信息的语料统一放在 `~/hl_mem_eval_data/`；缓存和报告放在 `var/eval/`。运行方法见
[`tests/eval/README.md`](../tests/eval/README.md) 和 [`evaluation/README.md`](../evaluation/README.md)。

## 下一步

- 观察 tombstone sidecar 与 restore replay 的生产恢复演练；旧 manifest 无法证明删除历史时保持拒绝，不做
  静默兼容。
- 关系语义主菜已判死并删除，不保留“待默认开启”的隐含发布任务；未来如重启必须提出全新预注册假设。
- 图片输入、Mental Model 和多租户继续作为独立版本决策，不视为 v0.28 未完成项。

## 已知限制

- 生产 relation expansion 仍依赖现有关系边质量；已淘汰的 C 系列实验臂不属于公共配置面。
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
