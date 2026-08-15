# HL-Mem 项目交接状态

> 最后更新：2026-08-15

## 当前状态

- **分支**：`main`
- **版本**：v0.27.0
- **阶段**：v0.27.0 发版准备完成；tag、Release 与 PyPI 由维护者验收后执行
- **服务**：FastAPI 默认监听 8200；非敏感配置来自工作目录下的 `hl_mem.toml`
- **存储**：SQLite WAL + FTS5 + 向量 BLOB；默认 `sqlite_scan`，可选 `sqlite_vec`
- **Schema**：42 migrations（SQL 001–042），只允许向前迁移
- **密钥**：`LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`

## v0.27 已交付

- 冲突治理按整个 conflict group 收敛；组级 ResolutionService、migration 041 激活触发器和事务内
  postcondition 共同保护互斥组 active≤1，存量异常继续走显式 audit/repair。
- Context Packet 对关系 claim 渲染 role/action/object；answer-entity gold schema v3 与
  `answer-entity-packet-v1` scorer 保持冻结，既有 anchors 判分并行保留。
- 受控 archived-only 复活与 activation 半衰期完成 A/B；默认分别为 `auto` 和
  `activation_halflife`。要保持 v0.26 行为须显式配置 `off` / `legacy_linear`。
- C 系列三轮关系实验未让 C4 通过 sealed 产品门禁，C4 保持休眠且 reader 不切换；权重敏感性没有通过
  v0.28 bandit 硬门。冻结结果与失败差额见 [CHANGELOG](CHANGELOG.md)。

## 当前评测

- 提取：`tests/eval/test_extraction_v2.py`，公开合成 fixture。
- 隔离检索：PerLTQA 64 + MemDaily 48，共 112 case。
- E2E：PerLTQA 28 + MemDaily 12，共 40 case；代码回归使用同一提取缓存和同一 scorer 做版本 A/B。
- 完整 benchmark：LongMemEval、MemDaily、PerLTQA runner 位于 `evaluation/tools/`。

真实或含个人信息的语料统一放在 `~/hl_mem_eval_data/`；缓存和报告放在 `var/eval/`。运行方法见
[`tests/eval/README.md`](../tests/eval/README.md) 和 [`evaluation/README.md`](../evaluation/README.md)。

## 下一步

- 观察 resurrection/activation 新默认的生产指标；需要回滚时显式设置 `off` / `legacy_linear`，无需降版。
- C4 只有在新预注册协议和未烧毁 sealed 集上重新过门后才可进入默认召回路径；当前不安排 reader 切换。
- 图片输入、Mental Model 和多租户继续作为独立版本决策，不视为 v0.27 未完成项。

## 已知限制

- 关系扩展 C4 仍是 feature-flag 实验路径，尚未达到默认启用门槛。
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
