# v0.30.0 批次 2 候选实验装备设计

## 范围与不变量

本批次只提供离线实验装备和冻结语料，不改变生产摄入、召回、配置或数据库 schema。A 臂原样返回
v0.29.3 七字段 compact JSON；B1 在同一 JSON 上执行确定性 atomicity 与 canonicalization；B2 仅保留显式
callback 接口，未注入时 fail closed。本批次不调用真实 LLM，不执行正式 A/B。

状态坐标严格由 `namespace + canonical_subject + canonical_slot + coordinate_qualifiers` 构成，predicate 不参与。
候选逻辑复用批次 1 的 `StateCoordinate` 和 `coordinate_qualifier_key()`；序列化时对值对象内部规范 JSON 字符串只
解码一次。候选 qualifier 集可比生产 registry 更细，但不修改生产 slot 准入。

## 组件边界

`src/hl_mem/evaluation/state_experiment_arms.py` 接收原始 LLM JSON。它校验七字段形态，以固定 kind 路由、
NFKC/实体别名、状态词规则和限定维度构造坐标；atomicity gate 按可配置的 `split` 或 `reject` 策略处理一条 claim
中的多个状态子句。`run_arm()` 只处理内存对象；`run_arm_file()` 只写调用方显式给出的实验 JSONL 路径。
语料 bundle 不能直接作为 arm 输入：冻结的 A 提取器先消费 `events`，随后由 `make_arm_sample()` 将
`bundle_id` 与七字段原始响应绑定为 `sample_id/raw_llm_json`。A 与 B1 必须重放同一个绑定结果。

`src/hl_mem/evaluation/state_experiment_scoring.py` 不比较自然语言。坐标、原子 claim 和非状态 claim 用结构化
assertion identity 计算 precision/recall/F1；supersede edge 从只读 SQLite 的 `claims.superseded_by_id` 和
`evidence_links(relation='supersedes')` 读取，并由 run manifest 把真实 claim id 映射回 gold assertion id。召回指标
只消费结构化 assertion id 列表。`score_protocol_file()` 在内部读取 dev/sealed gold JSONL，只返回聚合报告，
不向调用方暴露记录。判分报告固定携带协议阈值和逐项 pass/fail。

`src/hl_mem/evaluation/state_counterexample_corpus.py` 提供固定配额生成器和只读脱敏采样库。生产事件源必须经 CLI
参数注入，并以 SQLite URI `mode=ro`、`PRAGMA query_only=ON` 打开。真实来源 bundle 只保留闭集词汇、结构特征、
不可逆 source hash 和占位符；去标识结构作为带“不得提取”标记的模型可见上下文，与受控断言共同组成输入，
因此不同真实种子会改变实验输入但不会泄露原文。合成来源使用固定 seed 的对抗模板。

`evaluation/datasets/` 下 corpus 与 gold 分文件；开发集 280、sealed 120。两者按 70/30 等比分配五类和 50/50
来源。sealed 文件名含 `sealed`；manifest 仅公开数量、分类计数和 SHA-256。交付后开发只读取 dev 内容；sealed
由判分器直接消费并只输出聚合指标。

## 数据流与错误处理

原始响应先经 atomicity gate，再对保留/拆分后的 claim 逐条 canonicalize。每个输出携带稳定的
`sample_id/source_claim_index/atomic_index/assertion_id`，因此 gold 与预测无需文本匹配。非法响应、未知 arm、未知
策略和缺失 B2 callback 均抛出 `ValueError` 或 `NotImplementedError`，不静默降级。

评分器对空集合采用集合指标惯例：gold 与 prediction 同时为空时 precision/recall/F1 为 1；只有一侧为空时相应
指标为 0。真实边的两个端点必须都在 manifest 中才进入实验集合；未知端点计入诊断但不伪造 gold 命中。反例
跨坐标边以 gold coordinate identity 判定，不读取 audit 文本。

## 测试策略

所有 gate 先写失败测试，再写最小实现。fixture 至少覆盖软件版本、非版本状态、复合状态和反例；额外验证输入
幂等、predicate 漂移不改变坐标、qualifier JSON 不二次编码、split/reject 两策略、A passthrough 与 B2 fail closed。
评分器测试用临时 SQLite 构造真实 `superseded_by_id/evidence_links`，并覆盖全部通过线。语料测试在临时目录生成
完整 400 bundle，验证 1000 event、分类/来源/切分配额、gold 100% 覆盖及 sealed manifest，不读取已提交 sealed
内容做断言或调参。

## 自审

设计没有未决项；B2 的未实现状态是协议规定的接口位而非占位实现。所有写路径限于显式实验输出，所有数据库
路径只读。生产 `application/`、`domain/` 不导入本批次模块。
