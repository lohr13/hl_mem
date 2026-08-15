# C 系列关系召回实验预注册协议

- 协议版本：`c-series-relation-protocol-v1`
- 证据充分性版本：`evidence-sufficiency-v1`
- intent 判定版本：`relation-multihop-intent-v1`
- gold/scorer：manifest schema v3，`answer-entity-packet-v1`
- 状态：**待 Hermes/用户确认；未授权实施或跑批**
- 日期：2026-08-15

本文只冻结实验问题、实验臂、判定信号、预算和门禁。它不授权实现 C 系列功能，不授权打开封存题面，也不授权调用付费模型。确认后的实现、design/dev 筛臂和最终封存验证必须分别派单。

## 1. 研究问题与边界

实验要回答四个问题：

1. 现有关系边做一跳或两跳扩展，能否稳定提高 hard relation 题，而不损害已答对题？
2. intent 门控能否保留常开扩展的收益，并降低无关扩展、延迟和错误关系污染？
3. 关系路径原子打包能否避免“取到桥接节点却丢掉路径另一端”？
4. 证据仍不足时，raw 事件兜底和慢路径 planner 哪一种在相同触发门槛下更有效、更安全？

本协议不改 extraction prompt/schema/provider、AdmissionPolicy、近重复阈值、写入冲突逻辑、关系发现/自动写边策略或 reader 判分旧口径。关系边与 extraction cache 在各臂间只读共享。所有臂只改变召回扩展、packet 组装或不足时的补救路径。

## 2. 数据分区与隔离

### 2.1 design

design 集可见，用于：信号可观测性检查、代码调试、错误分类、预算压测和提出候选阈值。现有 40 条 Chinese E2E 可以进入 design，但不得据此修改封存题。

### 2.2 dev

dev 集在开跑前冻结，至少包含 200 条中文查询，其中 `needs_relation_or_multihop=true/false` 各不少于 100 条，并覆盖关系、两跳、枚举、冲突新值、推荐/执行和 no-answer。dev 用于：

- 测 intent 准确率与误判率；
- 一次性校准 `evidence-sufficiency-v1` 的固定阈值；
- 运行 C0-C5、`f4` 并筛选唯一候选臂。

阈值冻结后不得按单题结果继续调参。若必须改信号、阈值、prompt、预算或 arm 定义，协议版本递增并从 design/dev 重新开始。

### 2.3 sealed holdout

24 条关系链集只用于最终预注册验证。仓库只保存 `tests/eval/fixtures/relation_chain_holdout_manifest.json`；题面位于 `~/hl_mem_eval_data/datasets/relation_chain_holdout_v1.json`，payload SHA-256 固定为：

```text
1e4be5bbc93cfefd31d1d78a0c7b96cddccbe5ac8a3f71f90e77f8471b24f0a1
```

参与 arm 设计、实现、调参和 dev 筛臂的人不得打开题面。最终执行者只能在全部预注册字段已填满、唯一候选臂已选定且 Hermes/用户书面确认后显式解封。封存集只运行 C0 与该唯一候选臂，不运行全部候选臂。

若封存门禁失败，只报告失败；不得针对这 24 条修改实现后重跑并沿用 `v1` 名称。下一轮须使用新协议版本和新封存集。

### 2.4 运行时隔离

评测 runner 与 scorer 分进程：runner 只接收 events、question、question time 和 namespace；gold、forbidden 集与答案只在所有输出落盘后交给 scorer。召回服务、raw fallback、planner prompt、日志和 trace 均不得接触 gold。封存报告只写 case ID、arm、指标、成本和错误码，不写题面或答案。

## 3. 冻结的公共运行条件

除 arm 表显式列出的差异外，所有条件相同：

| 项 | 冻结值 |
|---|---|
| 基础通道 | FTS5 + dense，RRF 与现有多因子排序不变 |
| 向量后端 | `sqlite_scan`，scan limit 200 |
| reranker | 同一 provider/model/revision/prompt；参数逐字节一致 |
| query expansion | `off` |
| tag boost/channel | boost 保持基线值；独立 tag channel `off` |
| relation discovery | `off`；不产生或自动应用新边 |
| relevance gate | `observe`；各臂不得改变候选集合 |
| 基础 candidate floor | 50 |
| Top-5 seed | 关系扩展前、基础融合与多因子排序后的前 5 个 claim；按现有 claim ID 规则破同分 |
| 最终 claim 上限 | 10 |
| packet 总预算 | 2,000 tokens，包含 claim、关系路径和 raw 片段 |
| 可见性 | 固定 `question_at/as_of/known_as_of`，禁止未来事件与跨 namespace 数据 |
| scorer | 旧 `deterministic-rubric-v2` 与新 `answer-entity-packet-v1` 双口径并行 |

`entity coverage@5` 的 packet 范围固定为：最终 packet 中，基础 `pre_rank<=5` 的 seed 本身，以及能追溯到这些 seed 的关系扩展项。它不是“最终排序前五条”，也不允许从 seed 6 以后补算。gold entity 与 packet `entities` 均先做 NFC，再精确匹配；不扩同义词。每题先算命中比例，再做 macro-case 平均。

## 4. C0-C5 与 f4 的精确定义

共同关系白名单为当前集合：`summarizes`、`supports`、`follows`、`about`、`derived_from`。每跳分数沿用 `seed_semantic_score × cumulative_weight`；`relation_weight=0.35`，每跳衰减按当前实现的 `relation_weight / 2`。所有扩展均受 namespace、双时间可见性和终态过滤约束。

| Arm | intent 门控 | 最大跳数 | 扩展预算 | packet/补救行为 |
|---|---:|---:|---:|---|
| C0 | 无 | 0 | 0 | 当前 claim-only 基线；无关系扩展、无 raw、无 planner |
| C1 | 常开 | 1 | seed 5，候选 12 | 普通全局排序与 2,000-token 打包 |
| C2 | `relation-multihop-intent-v1` | 1 | seed 5，候选 12 | intent 为假时逐字节等价 C0 |
| C3 | 同 C2 | 2 | seed 5，每层 frontier/总候选均不超过 20 | 普通全局排序与打包 |
| C4 | 同 C2 | 2 | 同 C3 | C3 + 关系路径原子打包 |
| C5 | intent 且证据不足 | 2 | 同 C4 | C4 + 受限 raw 事件兜底；无 planner |
| `f4` | intent 且证据不足 | 2 | 同 C4 | C4 + 单次慢路径 planner；无 raw 兜底 |

### 4.1 C4 路径原子打包

先按 C3 生成候选与路径。若存在满足 query-side role/action/object 请求掩码的完整路径，则按 `expansion_score DESC, path_length ASC, claim_id ASC` 选择最佳路径：

- 为该路径预留最多 800 tokens、最多 4 个 claim；
- 同一路径要么完整进入 packet，要么整条不进入，不允许只保留桥接节点；
- 余下预算按 C3 原顺序填充；
- 无完整路径或路径超过预留预算时，行为退化为 C3；
- 最终仍受 10 claims/2,000 tokens 总上限约束。

### 4.2 C5 raw 事件兜底

C5 先完整执行 C4，再计算 `relation-multihop-intent-v1 AND evidence-sufficiency-v1.insufficient`。只有结果为真才进入 raw fallback：

1. 候选来源只允许同 namespace 且在问题双时间窗口内的事件；
2. 合并两组候选：Top-5 seed/关系路径所链接的 evidence events，以及使用原问题对事件正文做 FTS 的 Top-20；
3. 去重后按“已链接 evidence 优先、BM25、occurred_at、event_id”确定性排序；
4. 最多加入 6 个事件片段，每片最多 256 字符，raw 子预算最多 800 tokens；
5. raw 内容替换最低排名的普通 claim，packet 总预算仍为 2,000 tokens；
6. raw 搜索为空、超时或解析失败时回退到 C4，不扩大 namespace，不关闭时间过滤。

C5 不新增 LLM 调用。它验证 extraction 丢失或 claim 粒度不足时，受控原始证据能否补齐答案。

### 4.3 f4 慢路径 planner

`f4` 与 C5 使用完全相同的 intent 与充分性判定，但补救手段改为 planner，以便做干净的 paired comparison：

- 输入：原问题、Top-5 seed 的 ID/结构化 entities/slot、可见关系边及脱敏 trace；不含 gold、参考答案或 raw 正文；
- 输出：冻结 JSON schema，最多 2 个子目标、2 跳关系遍历；禁止自由回答；
- 只执行 claim/关系检索，不执行 raw fallback；关系白名单、namespace 和双时间约束与 C3 相同；
- 每题最多 1 次 planner 调用；输入最多 1,200 tokens，输出最多 256 tokens，超时 2.0 秒；
- planner 失败、越界或 schema 无效时回退 C4；packet 仍为 10 claims/2,000 tokens。

planner model、revision、temperature、JSON schema 和完整 prompt SHA-256 必须在预注册 manifest 中冻结。`f4` 只是未来臂定义，本批不实现。

## 5. 证据充分性信号

### 5.1 候选信号评估

| 信号 | 可观测性 | 额外延迟 | 主要风险 | 结论 |
|---|---|---:|---|---|
| `no_evidence` | 现有 response/trace 直接给出 | 近零 | keep-top1 或阈值漂移可能掩盖真实空证据；只能发现最严重情况 | 硬触发，不能单独覆盖灰区 |
| `low_confidence` | 现有 answerability 直接给出 | 近零 | reranker/model 校准漂移；通过抬分可被 gaming | 作为连续分量，不单独等同失败 |
| role-action-object 不完整度 | query 请求掩码、claim 字段、关系路径可计算 | 线性扫描 packet，目标 <1 ms | 填充空泛关系或复制实体可虚假“完整” | 采用，但只计有 evidence provenance 的路径 |
| packet entity 覆盖度 | query 实体与 packet 结构化 entities 可计算 | 线性集合运算，目标 <1 ms | entity stuffing；对答案中新增实体不可见 | 采用 query-side retention，不使用 gold coverage |

严禁把 `answer_entities`、role-action-object gold、参考答案、forbidden 集、人工 verdict 或任何由其派生的标签作为在线信号。

### 5.2 无 gold 的计算定义

`evidence-sufficiency-v1` 在 C4 基础 packet 上计算三个分量：

- `A`（answerability）：`supported=1.0`、`low_confidence=0.5`、`no_evidence=0.0`。
- `R`（RAO completeness）：`relation-multihop-intent-v1` 从问题文本产生 required role/action/object 掩码；packet 中每个必需分量只有在结构化 claim 或长度不超过 2 的可见 evidence-backed 路径中出现才算覆盖。`R=covered_required/required`。没有 RAO 请求时记为 unavailable，不补 1 分。
- `E`（query entity retention）：从问题中抽取 NFC 精确实体和类型化时间/数值，集合记为 `Q`；只统计 Top-5 seed 扩展后的最终 packet 中、带 evidence provenance 的结构化 entities。`E=|Q∩P|/|Q|`；`Q` 为空时 unavailable。这里的 `E` 与离线 gold entity coverage 是两个不同指标。

综合分：

```text
S = weighted_mean(observed(A=0.45, R=0.35, E=0.20))
insufficient = (answerability == no_evidence)
            OR (S < 0.70)
            OR (answerability == low_confidence AND R is observed AND R < 2/3)
```

unavailable 分量从分母中移除并按剩余权重归一化。`A` 永远可观测，因此不会出现空分母。比较使用原始浮点值，不先四舍五入；边界 `S==0.70` 不触发。

### 5.3 anti-gaming 与冻结

- 相同实体重复出现只计一次；同一 claim 自报多个别名不增加分数。
- 没有可见 evidence link 的 claim、跨 namespace claim、未来事件和 terminal-invisible claim 不计 `R/E`。
- action 只按冻结的动作类词表或关系枚举匹配，不调用可被 prompt 诱导的生成式 judge。
- 记录每个分量、不可观测原因、触发原因和 gate 版本，允许事后审计但不在线调阈值。
- design/dev 完成一次校准后，权重 `0.45/0.35/0.20`、阈值 `0.70` 和特殊 `R<2/3` 条件整版冻结。任何改动都升级到 `evidence-sufficiency-v2`。

最终推荐：C5 raw fallback 与未来 planner 共用上述 `insufficient`；两者也共用 intent eligibility。不要使用 gold coverage，不要仅凭 `low_confidence` 无条件进入慢路径。

## 6. intent=多跳/关系类的判定与约束

### 6.1 判定定义

`relation-multihop-intent-v1` 只读 query、显式时间参数和现有会话指代上下文，不读召回结果或 gold。正类为满足至少一项：

- 现有 `route_query()` 判为 `relation`；
- 出现冻结的所有权、隶属、推荐/执行、报道/主体、负责人/所在地等关系句式；
- 存在两个连续桥接需求，例如“项目负责人常驻哪里”“奖学金获得者的导师是谁”；
- 需要跨事件集合运算或完整枚举，例如“全部/完整列出/分别/一共有多少”；
- 显式要求当前值且同一 slot 可能有历史更新。

当前 `route_query()` 仅凭“关系/关联/依赖/属于”等少量关键词，必须先作为 baseline 测量，不能假设其已满足慢路径路由要求。候选 v1 router 可以在 design/dev 上补确定性句式，但一旦进入 C 系列跑批，其规则与 hash 必须固定。

### 6.2 准确率门禁

dev intent 标签由两名标注者独立判断，分歧仲裁；标签只表示“是否需要关系/多跳能力”，不含答案。至少报告 confusion matrix、precision、recall、FPR、macro-F1 和 Wilson 95% 区间。

启用 C2-C5/`f4` 前必须同时满足：

- 正类 recall ≥ 0.90；
- 正类 precision ≥ 0.90；
- 负类 false-positive rate ≤ 0.05；
- 六个 hard 类别各自 recall ≥ 0.80；
- recall/precision 的 Wilson 95% 下界 ≥ 0.82，FPR 上界 ≤ 0.10。

若当前 router 不通过，只能在 design/dev 修 router 并重新冻结；不得查看 sealed holdout 来补关键词。

### 6.3 慢路径触发率预估

不调用 planner 也能离线重放 router 与充分性计算。对 dev 全量和一份合规、去标识化的真实查询样本（若无授权则只用 dev）计算：

```text
intent_rate       = count(intent=true) / N
insufficient_rate = count(insufficient=true) / N
trigger_rate      = count(intent=true AND insufficient=true) / N
```

按 query 类型、answerability 和时间窗口分层报告点估计与 Wilson 95% 区间。容量规划使用 trigger rate 区间上界，并额外模拟 5%/15%/30% 三档；不得用 sealed 24 题估算生产触发率。

## 7. 指标与逐题配对

每题保存三次重复的原始结果，并同时计算：

- 旧口径：official anchors/accepted rubrics 的 `answer_correct`；
- 新口径：macro-case `entity_coverage_at_5`；no-answer 不进覆盖率分母；
- hard relation：答案正确数、RAO 路径完整数、六类别分项；
- 安全：forbidden entity/assertion 命中、no-answer 被强答、modality violation、leakage violation；
- 成本：召回 p50/p95、packet tokens、扩展节点数、raw 片段数、planner 调用/超时/token；
- 路由：intent confusion matrix 和真实触发率。

逐题 verdict 取三次重复的多数；若三次各不相同，按“错误/违例优先”的保守顺序裁决。连续指标报告三次均值、标准差和每题三次值，不用最好一次。

## 8. 三次重复与随机交错

所有 design/dev arms 各运行 3 次；sealed 阶段只运行 C0 与唯一候选臂，各 3 次。禁止先跑完整 C0 再跑完整候选臂。

相对 seed 规则：

```text
case_seed = first_64_bits(SHA256(preregistration_id || corpus_sha256 || case_id || repeat_index))
arm_order_key = SHA256(case_seed || arm_id)
```

对每个 `(case_id, repeat_index)` 按 `arm_order_key` 排序交错执行。这样 arm 顺序可复现，又不会把模型时段漂移系统性分配给某一臂。相同 case/repeat 的 reader seed、temperature、输入缓存和时钟完全相同；不同 repeat 只通过上述相对规则改变随机 seed。

若 provider 不接受 seed，记录 `seed_unsupported`，仍保持交错顺序并使用三次重复；不得宣称严格可重复。

## 9. 预注册冻结清单

跑批 manifest 缺少任一必填项即失败，不得“先跑后补”：

1. 协议版本、Git commit、`uv.lock` SHA-256、Python/SQLite/OS 版本。
2. design/dev/sealed 语料 SHA-256、case ID 清单与类别分布。
3. 40-case manifest 当前 SHA-256：`706389f1f45a4eb8056ec8565c915507199a86f75088797c12dc258de2981b87`。
4. sealed payload SHA-256：`1e4be5bbc93cfefd31d1d78a0c7b96cddccbe5ac8a3f71f90e77f8471b24f0a1`。
5. extraction cache：每个 DB/manifest 的 hash、统一有序 Merkle/root hash、提取配置指纹；各臂只读同一份缓存。
6. extractor/embedder/reranker/reader/planner 的 provider、model、revision、endpoint 类别和采样参数。
7. extraction、QA、query entity、intent、planner prompt 与 JSON schema 的逐字节 SHA-256。
8. Top-5 seed 相对规则、同分规则、relation 白名单/权重/深度/预算、最终 limit 10。
9. packet 2,000-token 预算、C4 800-token 路径预留、C5 800-token raw 子预算及 tokenizer 版本。
10. `evidence-sufficiency-v1` 全部权重/阈值和 `relation-multihop-intent-v1` 规则 hash。
11. `preregistration_id`、三次重复相对 seed 规则、arm 随机交错顺序生成规则。
12. `question_at/as_of/known_as_of`、timezone、NFC 版本与 scorer 版本。

design/dev、cache、模型和 prompt 的具体 hash 要等后续实现产物生成；在 Hermes/用户确认和 hash 填满前，本协议明确禁止跑实验。

## 10. 预注册通过门禁

先在 dev 上运行所有 arms，按以下硬门禁筛选；通过者多于一个时，依次按 hard relation 净增、entity coverage、p95 延迟、触发率做确定性排序，只保留一个。最终 sealed 比较沿用同一门禁。

### 10.1 质量与逐题零回退

- hard relation 多数 verdict 相对 C0 **净增至少 2 题**；净增=`候选独有正确-C0 独有正确`。
- C0 多数 verdict 正确的题，候选不得出现任何 `correct→incorrect`；即 paired regression count 必须为 0，而不是只要求总分不降。
- macro `entity_coverage_at_5` 不低于 C0；hard relation 子集至少提高 0.05。
- no-answer 正确数不低于 C0，且任何 forbidden entity/assertion 命中均直接失败。

### 10.2 modality 与泄漏零容忍

以下任一计数必须为 0，否则无条件失败：

- text-only 题使用 image/audio 或未授权模态证据；
- 使用其他 namespace、问题时间之后、known-as-of 之后或不在冻结 corpus 的证据；
- runner/召回/planner 输入包含 gold、参考答案、forbidden 集或 scorer verdict；
- sealed 题面出现在 design/dev 日志、prompt cache、调参报告或代码 fixture。

### 10.3 成本上限

- 所有臂 packet ≤ 2,000 tokens、claim ≤ 10；任何单题越界即失败。
- C1-C5 不得新增召回 LLM 调用；C5 raw ≤ 6 片段且 ≤ 800 tokens。
- C1-C5 的 recall p50 ≤ C0 的 1.15 倍；p95 ≤ `max(C0+150 ms, C0×1.25)`。
- `f4` trigger rate 点估计 ≤ 20%；每次最多 1 调用，输入 ≤ 1,200、输出 ≤ 256 tokens，平均额外 planner tokens ≤ 292/query，p95 E2E ≤ C0+2.5 秒。
- 超时/错误必须回退 C4 并计入成本和失败率；不得静默删样本。planner 失败率 > 2% 时 `f4` 失败。

样本只有 24 条，最终决策以预注册绝对门禁和逐题 paired table 为准；McNemar、bootstrap 区间仅作说明，不用 p-value 覆盖硬门禁。

## 11. 执行与报告顺序

1. 后续实现任务创建 design/dev 数据、runner、信号 trace 和 arms；不打开 sealed payload。
2. 填满预注册 manifest，验证所有 hash 与 intent 门禁。
3. 在 design/dev 做三次随机交错；按预注册顺序选择唯一候选臂。
4. 将候选 arm ID、所有参数、代码 commit 和未打开 sealed 的审计记录交 Hermes/用户确认。
5. 获明确授权后，由独立最终执行者解封，只运行 C0 与候选臂三次交错。
6. 先封存原始输出和 hash，再由 scorer 进程加载 gold；生成逐题 paired table、六类分项、成本和所有硬违例。
7. 无论通过或失败均保留完整报告；失败不得转成下一轮调参素材后继续声称是同一 holdout 验证。

本批到第 2 步之前即停止：只交付协议和数据契约，不实施、不跑付费 benchmark。
