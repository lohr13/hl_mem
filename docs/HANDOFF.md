# HL-Mem 项目交接状态

> 最后更新：2026-08-22

## 当前状态

- **分支**：`main`
- **版本**：v0.29.3
- **阶段**：v0.30.0 批次3代码接线完成，等待 sealed 终验；无 push、无 tag、无部署
- **服务**：FastAPI 默认监听 8200；非敏感配置来自工作目录下的 `hl_mem.toml`
- **存储**：SQLite WAL + FTS5 + 向量 BLOB；默认 `sqlite_scan`，可选 `sqlite_vec`
- **Schema**：49 migrations（SQL 001–049），只允许向前迁移
- **密钥**：`LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`

## v0.30.0 批次3代码状态（待 sealed 终验）

- 状态快照使用 namespace 隔离的稳定 owner/slot/qualifier 坐标；严格有序 current observation 才能自动关链，
  历史与不确定语境不授权 supersede。历史或双时间召回不刷新 access，但 exposure 仍记录。
- `scope` 管保留语义；`volatility` 只提示变化速度/TTL 分类，不参与坐标、转移、supersede 或 recall intent。
- 无配置、migration、REST/MCP wire 或 Hermes plugin 变更；sealed 120 与部署尚未执行。

## v0.29.3 已交付（待 Hermes 验收）

- temporal 为价格/计量快照增加序列坐标：同序列不同坐标自动推进，不同度量/标的共存，同坐标修订和隐式替换
  继续人工；火山 11 案由 10 uncertain + 1 not-applicable 收敛为 7 distinct-series + 2 snapshot-advance +
  1 uncertain + 1 not-applicable。
- `RecallService.recall()` 与 `IngestService.store_extracted()` 分别拆轻到 68/163 行，procedure flow 与 ingest
  resolution 独立，characterization 保证既有行为和 patch 面不变。
- 新增模块行数、callable 参数和函数长度三预算 CI 棘轮；默认上限为 600 行、10 参数、150 行，allowlist 只降不升。
- 无 schema migration、配置变更或破坏性 API 变化，`daemon_contract=1`；行为变化仅 temporal 三分支且无旧行为开关。

## v0.29.2 基线（继续有效）

- v0.29.0 migration 047 新增 `assertion_kind=unknown|observation|inference`。存量 `unknown` 只可观测，不授权
  supersede，也不改变召回或注入行为。
- migration 048 为 `dedup_pairs` 增加确定性的 `pair_source` 与 `new_claim_id` 注入信号；存量行标记为
  `legacy`，不猜测新写入端点。
- 注入治理顺序固定为 echo filter → reranker → freshness decorate → packing。v0.29.2 仅把 echo/freshness
  默认值翻转为 `enforce` / `render`；显式配置 `off` 可退回旧行为，策略实现与三机现有显式配置均未改变。
- `hl-mem --db <副本> dedup drain-below-floor` 默认只读报告；`--apply` 必须带精确
  `--expected-count`。597 条生产形状 fixture 只终结 pair，不改 Claim。
- expired 回收默认 `observe`，删除资格为超过 90 天历史保留窗、无下游 evidence 消费者、无 open conflict；
  apply 需精确 expected-count、每轮最多 100 条并复用独立 tombstone 删除闭包。
- migration 049 在同一事务中先验证 047/048 版本证据与数据库内 view/trigger 消费者，再移除 legacy
  `claims_tags_fts` 及三触发器；SQLite 无法证明外部查询不存在，三机版本门槛和旧二进制回滚窗口由发布流程保证。
- `scripts/run_v0291_injection_replay.py` 从固定 spec 构造 200 个 recall point，按 echo filter → fixed reranker →
  freshness decorate → packing 回放 2×2，并把每臂去正文决策限制在 1,000 条；线上 observe 质量评估不在本交付内。
- 终态 conflict generation 保持不可变：同一 active winner 的精确重申只追加 evidence；不同当前值复用现有
  group/candidate/revision 基建创建下一代单 open case，不扩展为 issue platform。
- A2 `temporal-v1` 只让新写入的 `observation` 授权原子 online/offline 或显式旧值锚定的价格更正。非互斥 slot
  明确拒绝，灰区进入既有 pair conflict 管线。生产只读副本回放为价格 14/14、precision 1.0、Tailscale 顺序
  2/2、120 条 path 与 4 条 network 共存样本误接链 0。
- A3 证明关链后的 current-state results、packed context 与 Context Packet 都只含当前 tip；historical 仍保留旧链。
  recency 权重维持 `0.08`，没有新机制或配置。
- F 将 daemon/plugin/Context Packet wire 的静态 major 暴露为 `/healthz` 与 Hermes `contract.json` 证据；doctor
  分项诊断兼容性，离线 WARN、缺证据或 major 不匹配 FAIL。没有动态协商、持久状态或自动升级。

- conflict case 已升级为 `(namespace, group_key, generation)` 下的单案多候选，revision 保护人工裁决；维护只处理
  持久 dirty queue 的当前活跃 generation，并受 case 数/时间预算、失败退避和候选上限约束。终态候选会自动关案。
- 提供只读检查 + expected-count fail-closed 的存量非互斥异常修复命令；线上执行前必须离线备份并停止其他写入者。
- 运维历史按表独立事务有界清理；pending/running Job、pending dedup pair 和已标注 feedback 保留。摄入期 pending
  dedup pair 有容量上限并暴露跳过计数。

- 召回 REST/MCP 主路径使用 WAL 只读连接；access、exposure、自动复活、召回审计与 query-expansion span
  均移出请求线程，持久副作用由 `deferred_tasks` 幂等重试并最终一致落库。Hermes 按需召回超时默认 8 秒。
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

- 由 Hermes 验收 v0.29.3 发版准备提交；验收后按 SOP merge 回 main、push、tag、等待 CI 并部署三机。
  本 worktree 不修改部署配置、不 push、不打 tag。

- 观察 tombstone sidecar 与 restore replay 的生产恢复演练；旧 manifest 无法证明删除历史时保持拒绝，不做
  静默兼容。
- 关系语义主菜已判死并删除，不保留“待默认开启”的隐含发布任务；未来如重启必须提出全新预注册假设。
- 图片输入、Mental Model 和多租户继续作为独立版本决策，不视为 v0.28 未完成项。

## 已知限制

- 注入治理 20 条冻结 stable 验收集为 19/20；毛刺样本双臂对称且不是机制伤害，保留为已知边界，
  不针对单 case 调优。
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
