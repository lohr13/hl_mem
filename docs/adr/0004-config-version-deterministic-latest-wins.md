# ADR-0004：冻结 config.version 单 slot 确定性 latest-wins 关链协议

- 状态：Accepted
- 日期：2026-08-26
- 决策者：项目发起人、Hermes Agent 与 Codex（独立方案对辩后收敛）

> 本 ADR 是 v0.30.0 状态实验撤回后的冻结终稿。设计过程文档
> `proposal-llm-final-adjudication-20260826.md`、`hermes-plan-v300-restart-20260826.md` 与
> `consensus-v300-restart-20260826.md` 位于仓库外；后续实现、评测和发布若与过程文档冲突，以本 ADR 为准。
> 本 ADR 只冻结协议，文中配置键、CLI、规则、评测装备和运维动作尚未实现。

## 背景

HL-Mem 已具备不可变事件、证据链、双时间、`StateCoordinate`、`superseded_by_id`、revision、治理账本和事务内
CAS，但同一部署主体的多个版本声明仍可能同时留在 current 视图。2026-08-26 的双机体检中，版本号类 active
claim 噪音为本地 `23/595=3.9%`、火山 `80/341=23.5%`。完全维持现状会持续污染 Agent 注入，但错误关链又比
可见噪音危险：仍有效的 claim 一旦错误退出 current 视图，可能在被发现前影响 Agent 决策。

此前 v0.30.0 状态实验在同一份 400-bundle dev 上依次调过 B2、P1、I1、Z1–Z5 和生产接线，虽取得 dev 13/13，
却在独立 held-out-r5 仅通过 3/13，并产生 27 条错误 edge、3 条反例误 supersede。项目随后撤回状态 prompt、
admission 特许、通用 canonicalizer、resolver 等生产行为；撤回记录见 commit
`3a80601877a708e61ee4cf1bb30fa23c6c4e5df7`（`docs: 记录v0.30.0状态实验终验失败与撤回`）。这次重启不得
恢复同一条“扩大提取后再靠 gate 修补”的路线。

LLM 也没有取得破坏性关链资格。E1C 云端 `qwen3.7-plus` 复检在 70 案中 exact `54/70`，有 2 个危险反向
选择；29 个双序案只有 21 个一致，双序一致率 `21/29=72.4138%`。该实验不是本 ADR 的状态任务冻结集，但它是
“模型增加覆盖同时会引入错误确定性和顺序敏感性”的直接项目证据。因此，本轮只允许可重放的确定性关链；LLM
最多作为条件触发的离线 challenger，不进入产品自动 supersede。

仓库当前提交了 ADR-0001/0002；冲突自动收敛的 ADR-0003 设计过程稿在仓库外并已被后续方案引用。本决策使用
ADR-0004，避免两个不同决策共享 ADR-0003 编号。

## 决策

### 1. 唯一目标与授权范围

首版只授权单值 slot `config.version`。唯一坐标语义对象是现有：

```text
StateCoordinate(
    namespace,
    canonical_subject,
    canonical_slot,
    coordinate_qualifiers,
)
```

`conflict_key` 只是该坐标的持久化派生指纹和查询加速键，不是第五个独立真相。候选发现必须先 exact-match
`StateCoordinate`，只读取同坐标的有界 current tips；FTS、向量、编辑距离或模型判断不得扩大候选边界。

主体优先使用 typed canonical entity id。没有 typed proof 时，只允许现有显式版本化 alias 表证明的稳定主体；任意
简称、编辑距离和模型猜测别名均无自动关链资格。

`state.service_health`、`state.process`、`state.deployment`、`state.connectivity`、`state.job` 以及其他所有 slot
均不在首版授权范围。它们的时间尺度、值域和终态不同，必须逐 slot 另立 ADR、冻结语料和独立发布证据，不能因
`config.version` 通过而继承授权。

本轮不修改提取 prompt，不恢复 B2/P1 覆盖层，不给 operational snapshot 新 admission 特许，只处理已经通过
v0.31.1 既有写入合同的 claim 与本 ADR 定义的可信版本探针。

### 2. 六分支纯判定合同

关系枚举冻结为六类，不新增含混枚举：

```python
TemporalRelation = Literal[
    "duplicate",
    "corroborates",
    "supersedes_existing",
    "historical_predecessor",
    "compatible",
    "needs_review",
]

@dataclass(frozen=True, slots=True)
class TemporalResolution:
    relation: TemporalRelation
    rule_id: str
    coordinate: StateCoordinate
    current_tip_id: str | None
    older_id: str | None
    newer_id: str | None
    event_time_source: str | None
    reason: str
```

六类语义冻结如下：

- `duplicate`：同一规范事实和同一证据已经存在，只做幂等复用；
- `corroborates`：规范版本相同但带来新的有效证据，只合并 evidence，不增长 active revision；
- `supersedes_existing`：较晚、可信的当前版本 observation 关闭较早 current tip；
- `historical_predecessor`：新到达的记录描述更早事实，只把新 claim 接为历史 predecessor，不反向关闭 current tip；
- `compatible`：跨坐标或语境明确不冲突，保持并存；
- `needs_review`：结构相近但无法满足确定性条件，按第 6 节的灰区终态处理。

版本大小不决定时间方向。初版窄解析器只接受 `v?MAJOR.MINOR.PATCH` 和显式维护的等价 alias；预发布、build
metadata、日期版、git SHA、版本范围和自然语言版本均为灰区。版本 atom 只证明两端可规范化且值不相等；合法
downgrade/rollback 必须能成为较晚 current tip。禁止字符串大小比较。

### 3. `supersedes_existing` 的九项必要前置条件

确定性 `supersedes_existing` 必须同时满足以下九项；任一不满足都不得自动关链：

1. slot 是代码初始 allowlist 中的 `config.version`，且 cardinality 为 single；
2. 两端 `StateCoordinate` 完全一致，current tip 唯一；
3. 新 claim 是明确的 `observation`，不是计划、历史、引用、否定或推断；
4. 存在独立 currentness proof：受信版本探针产生并绑定明确主体的结构化 `status_report/tool_result`，或显式
   correction 动作携带旧 claim id，或结构化写入 API 明示 `current=true`；普通对话经 LLM 提取出的
   `assertion_kind=observation` 单独不够；
5. 两端 event time 均可解析、带时区且来源可信，新时间严格晚于旧时间；`recorded_from` 不参与事实方向；
6. 两端版本 atom 均能被窄解析器规范化且不相等；版本数值大小不参与方向；
7. 新来源权威不低于旧 tip；权威度单独不能破事实平局；
8. 旧 tip 不是 disputed/manual/open conflict，链无环，事务内局部 revision/fingerprint 未变化；
9. 第 4 节全部硬否决均未命中。

新 event time 严格早于 current tip 时只能得到 `historical_predecessor`；等时、缺时或无法证明方向时不得选择
`supersedes_existing`。

### 4. 硬否决与软不足

以下八条是硬否决，任何模式、模型、来源权威或运维动作都不能覆盖：

1. namespace、主体、slot 或 coordinate qualifier 不同；
2. 当前 observation 与 plan、quotation、historical report、negation 极性不一致；
3. critical anchors、单位、环境、部署实例或角色不一致；
4. 多角色、多 payload，或复合 value 无法唯一拆成一个原子状态；
5. 缺少独立 currentness proof；LLM 给出的 `assertion_kind=observation` 只是必要条件，不能单独授权关链；
6. existing tip 为 disputed/manual/open conflict，或 current tip 不唯一；
7. 证据缺失、损坏，或来源身份无法验证；
8. alias 不在显式版本化表中。

软不足仅包括：两端同坐标且证据完整，但 event time 缺失/等时、版本格式不在白名单、来源权威倒置或语义关系仍
不确定。软不足只能得到 `needs_review`，不得创建自动真相。

确定性版本规则不使用 embedding，因此 embedding 缺失或模型/维度不同不阻止该规则；这类否决只约束未来实际
依赖 embedding 的路径。

### 5. currentness proof 与 `report-version` 合同

#### 5.1 CLI 与 owner 边界

产品命令合同冻结为：

```text
hl-mem report-version \
  --namespace default \
  --subject <已存在且唯一解析的稳定部署主体>
```

- reported version 只能从当前进程导入的 `hl_mem.__version__` 读取；不提供 `--version VALUE`；
- `--subject` 必须在 namespace 内解析到唯一、active、版本化 typed alias/entity proof；未解析或多解直接失败；
- 首版不做编辑距离、动态 alias、自由文本 owner 推断或模型补全；
- 命令输出 event id、规范化 owner、reported version、producer contract 和 queued/stored 状态，不输出敏感配置。

#### 5.2 固定事件 schema

探针事件使用版本化固定 schema，所需语义字段为：

```json
{
  "schema_version": "status_report_v1",
  "producer_contract": "hl_mem.report-version-v1",
  "package": "hl_mem",
  "runtime_version": "<read from hl_mem.__version__>",
  "namespace": "<resolved namespace>",
  "subject_proof": {
    "canonical_entity_id": "<resolved typed entity id>",
    "alias_version": 1
  },
  "observed_at": "<RFC 3339 timestamp with UTC offset>"
}
```

不支持的 schema/producer version、缺失字段、自由文本替代、owner proof 失效、版本 atom 无效或 event time 无效均
fail closed。未知扩展字段不能取得 currentness 权限；扩展信任边界必须升级 producer contract 并重新冻结。

#### 5.3 确定性 projector

只有固定 producer contract 能进入探针 projector；普通 `status_report` 不获得特权。projector 不调用 LLM、不经过
普通 prompt，直接构造以下规范字段：

```text
canonical_attribute  = config.version
canonical_slot       = config.version
assertion_kind       = observation
value                = 当前包版本
source_event_indices = (0,)
```

来源合同、owner proof、version atom、event time 任一无效即拒绝 currentness proof。不得通过“先让 LLM 抽取，再由
gate 修正”实现探针。

同版本重复探针只增加 evidence。A→B→A 的后来回滚必须按新的可信 event time 形成新的 current occurrence，不能
把后来 A 错并回已经 superseded 的旧 A。

#### 5.4 探针与存量的关系

- 唯一、健康、同坐标旧 tip：探针可以形成 `supersedes_existing`；
- 多 active tip、disputed/manual、断链、owner 不唯一或旧 tip 缺结构证据：只产生 observe 建议，留给存量 replay；
- 版本号下降不是否决条件，可信 event time 决定 downgrade/rollback 的方向；
- 探针只报告本机当前运行的 HL-Mem 版本，不扩展到 service health、deployment、process 或 job。

### 6. 灰区终态与既有 conflict 合同

状态 latest-wins 规则无法证明关系时，不创建新的人工必办案；新旧 claim 保持现有并存可见语义，并记录低基数
observe reason。可选提醒不阻塞写入、不要求产品用户逐条处理。

既有 conflict 管线若因其自身合同已经产生 case，本功能不得偷偷旁路、关闭或自动裁决。`needs_review` 也不把既有
case 冒充为本功能产生的任务。产品中不增加状态灰区 LLM judge，不承诺 Codex/Hermes 代审。

observe 模式只记录版本化 audit suggestion，不改变 claim/case；enforce 模式也只能应用满足本 ADR 的
`duplicate`、`corroborates`、`supersedes_existing` 和 `historical_predecessor`。模式切换不能给灰区增加权限。

### 7. 双 kill switch、事务与补偿

未来实现必须提供两个相互取交集的完整配置键：`state.latest_wins_mode` 与
`state.latest_wins_slots`。TOML 形式为：

```toml
[state]
latest_wins_mode = "observe"       # off | observe | enforce
latest_wins_slots = ["config.version"]
```

- 代码默认和首轮配置均为 `observe`；
- `latest_wins_mode=off` 停止新建议和新动作；`observe` 只审计；`enforce` 只执行确定性过线动作；
- 配置 slot 必须同时属于代码 allowlist；配置不能授权未知 slot；
- 不新增 `state.gray_judge_mode`，不复用 `maintenance_judge` 或 `conflict.auto_mode` 为状态自动关链开关；
- 规则版本必须进入 audit/revision，例如 `state-latest-wins-v1:version-observation-time`；
- apply 在既有 `BEGIN IMMEDIATE` 内重读 exact-coordinate current tip、fingerprint/revision，执行 CAS、无环和单 tip
  断言；
- 紧急停止把 mode 改为 `off` 或 `observe`；已落地误链只通过 existing governance 能力写补偿 revision，并按
  expected-count manifest 恢复，不写猜测性反向 SQL，不删除历史或 evidence。

以上配置键在本 ADR 批次不实现。

### 8. 防过拟合与冻结语料

#### 8.1 三层数据

1. **开放 calibration 300 案**：只用于实现和调试，不作为发布证据；可使用新时间窗脱敏真实结构案和系统性合成
   边界案。
2. **冻结 validation A 400 案**：使用新的 generation-id、variant-salt、断言实例和上下文池；gold 双人复核后
   封 hash。
3. **冻结 validation B 400 案**：与 A 并行构建，但使用另一时间窗/来源池、另一 salt 和不同模板族；不能是对 A
   失败样本的改写。

A/B 必须在代码、规则、阈值、alias 表以及 D 臂 prompt 全部冻结后各评分一次。任一失败，本轮实现判失败；不得
针对 A 修改后拿 B 当补考，也不得重新生成同代样本补考。

r1–r5 全部保持烧毁，只能引用聚合失败史；不得读取其样本内容、类别分布或把它们拿回 calibration。新 validation
A/B 也不得由烧毁集变体生成。

#### 8.2 每份冻结集配额

| 类别 | 数量/集 | 目的 |
|---|---:|---|
| 明确较晚当前版本 | 80 | 正常升级关链 |
| 明确较晚但版本号更低 | 40 | 回滚/降级，防版本大小决定方向 |
| late-arriving historical predecessor | 40 | 防旧事件反关 current tip |
| duplicate/corroborating evidence | 40 | 防同值重复增长 |
| 跨 namespace/主体/qualifier/环境 | 80 | 坐标隔离 |
| 普通对话 observation、plan/quote/history/negation/多角色 | 80 | currentness proof 与反例安全线 |
| 缺时/等时/低权威/disputed/坏链 | 40 | fail closed |

每集至少 40% 来自与另一集时间窗隔离的脱敏真实结构，其余为系统性边界合成。gold 只记录结构字段和预期关系，
不得向 runner 暴露自由解释。

### 9. Arms、发布门禁与 D 臂边界

#### 9.1 Arms

- A：v0.31.1 现状，无状态 latest-wins；
- B：本 ADR 的纯确定性六分支，唯一生产候选；
- C：删除，不保留本地 qwen3.8 判官占位、配置或模型专用 prompt；
- D：只有在第 9.3 节条件满足时才运行的离线 challenger。

#### 9.2 B 臂十项发布门禁

B 必须在 validation A、B 两份冻结集上分别同时满足：

1. 自动 edge precision = 100%；
2. counterexample false supersede = 0；
3. 跨坐标自动动作 = 0；
4. historical predecessor 方向准确率 = 100%；
5. duplicate/corroborate 不增长 active revision；
6. chain 无环、同坐标 current tip ≤1、dangling=0、as-of 可重放均为 100%；
7. 非 `config.version` ledger 与 A 逐字段等价；
8. 相同 manifest 三次 replay 输出完全一致；
9. gold 中满足全部确定性前置条件的 eligible recall ≥95%；
10. real-cohort active stale-version 数相对 A 至少下降 50%；否则即使安全也因收益不足不发布。

覆盖不足不能授权放宽安全条件，所有阈值在生成冻结集前写入 manifest。

#### 9.3 D 臂触发和无补考资格

仅当 B 在两份冻结集上全部安全门过线、但 eligible recall `<80%` 时才运行 D，用于量化未来另立 ADR 时 LLM 可能
增加的覆盖上限。D 固定使用 coding plan 线 `qwen3.7-plus`、`enable_thinking=false`、结构化 JSON、并发 `≤8`，
模型、prompt、schema 和运行参数 hash 写入 manifest。

- B recall `<80%`：B 不发布；若安全线全过，可运行 D 但只写 future-ADR 证据；
- B recall `80%–<95%`：B 不发布，D 不运行；
- B recall `≥95%` 且其余门全过：B 获得发布资格，D 不运行。

D 无补考、替代或修改 B 发布结论的资格；D 即使全过，也不能在本轮取得生产 supersede 权限。

### 10. Hermes 独立验收预注册

Hermes 的验收脚本必须在 Codex 首次评分前冻结 SHA-256。脚本只能读取 corpus manifest、gold JSON 和 arm 产物
JSON，不得 import：

- `state_latest_wins`；
- 生产 projector；
- `state_experiment_scoring`；
- Codex runner helper。

Hermes 必须独立重算而非复述报告中的以下承重字段：

1. corpus、gold、arm、规则、阈值和脚本自身的 manifest hash；
2. 各 arm 总数、成功/失败数以及 relation/tier/reason 分布；
3. eligible numerator/denominator 与 recall；
4. 自动 edge precision 的 numerator/denominator；
5. counterexample false supersede、危险反向和跨坐标动作计数；
6. historical predecessor 方向计数；
7. duplicate/corroborate active revision 增量；
8. chain cycle、current tip cardinality、dangling 和 as-of replay 计数；
9. 非 `config.version` ledger digest 与逐字段等价结果；
10. 三次 replay digest 和 real-cohort stale-version 降幅。

case-level 抽验样本必须由 manifest hash 稳定派生，不能看到结果后挑样本。独立报告必须输出自己的
numerator/denominator、异常类型、抽样算法版本和脚本 hash。Codex/Hermes 任一字段不一致即验收失败，不得现场修改
adapter 或评分器把两边“对齐”。

### 11. 生产 shadow 与 enforce 门

冻结验证通过后，仍须满足以下六条生产 shadow 门：

1. 规则、alias、probe schema、阈值和二进制冻结后连续运行至少 48 小时；任一变化重新计时；
2. 所有目标部署均以 observe 模式运行，并至少成功写入一次真实 `report-version` 探针；
3. 每台部署完成同坐标存量只读 replay，目标范围 100% 扫完；跨部署合计至少产生 30 条 eligible edge。不足 30
   条即 rollout 门失败并保持 observe，不延长等待来假装增加证据；
4. 自动检查必须全部为零：跨 coordinate 动作、非 allowlist slot 动作、hard veto 绕过、方向反转、环、dangling、
   同坐标多 tip 模拟结果、CAS/fingerprint 失配；
5. 同一 manifest 三次 replay 字节级一致，probe 重放幂等，非 `config.version` ledger 与 A 逐字段等价；
6. 若真实 correction/拒绝信号形成可归因 confirmed mislink，任何一条都由部署运维层切回 observe/off；该信号是
   额外安全信号，不是能识别所有静默语义错误的 oracle。

六条全部满足后，只做一次 `observe → enforce` 语义切换，不实现 10%/50%/100% 产品内百分比 canary。多机仍按
既有部署 SOP 顺序发布，但不要求用户逐级确认。

enforce 后首 24 小时由部署侧每 2 小时检查结构不变量和可归因 correction。自动停用通过部署运维链修改配置并重启；
本轮不在产品内新增 cron、动态配置服务或自动真值生产者。

### 12. 存量治理双 AI 复核

以下是**本部署的运维合同，不是产品能力**：

1. 新写路径通过后才处理存量；Codex 为每个部署生成只读 manifest、expected count、before/after fingerprint 和
   补偿计划；
2. Hermes 从数据库只读快照独立核对 hash、计数、坐标、链方向和由 manifest hash 派生的稳定抽样；
3. 只有双方逐 action id 的交集进入 apply；任何分歧自动移出 apply 集，不以多数表决放宽规则；
4. 用户只接收 hash/计数不一致、抽样坏链或 apply CAS 失败等异常汇总，不接收灰区逐条任务；
5. 在线备份后按 manifest hash + expected count 有界 apply，逐机验证 chain、dangling、current-tip、as-of 和 recall
   injection；
6. 该流程不写入开源产品文档为默认承诺，产品代码不得依赖 Codex/Hermes 存在。

灰区不批量送 LLM，不为降低噪音比例扩大 alias 或时间推断。

### 13. 行数账本与停止线

#### 13.1 生产代码

| 单元 | 预计净增 | 硬上限 | 说明 |
|---|---:|---:|---|
| `state_latest_wins.py`：version atom、六分支、hard veto | 145–190 | 200 | 不复制 StateCoordinate，不扩 `auto_resolve_conflicts.py` |
| ingest/application 接线、exact-coordinate 查询、audit/CAS | 70–95 | 105 | 灰区不创建新人工队列 |
| settings、allowlist、低基数指标、replay 薄接口 | 45–65 | 70 | off/observe/enforce + slot 双 kill switch |
| `report-version` CLI + 固定 status projector + owner proof | 60–90 | 90 | 包含完整信任边界 |
| **生产合计** | **320–440** | **450** | 任一 active 文件 ≤600 行 |
| 生产测试 | 760–980 | 1,000 | 不以复制 case 堆行数 |

不新增 migration/table，不复制 DTO/L2 client，不触碰已经接近 600 行边界的 conflict worker。若探针要求通用“可信任
任意 status_report”框架、动态 alias 或新鉴权系统，视为设计边界失效并停止，不得以超预算实现。

#### 13.2 评测装备

| 单元 | 预计净增 | 硬上限 | 说明 |
|---|---:|---:|---|
| generation/salt/context-pool 插件与 manifest | 170–240 | 270 | calibration + validation A/B |
| A/B arms、聚合报告、probe/replay adapter | 115–175 | 200 | 删除 C；D adapter 仅条件启用 |
| Hermes 独立验收脚本 | 35–55 | 60 | 不 import scorer/生产判定器 |
| **评测合计** | **320–470** | **500** | 冻结集 hash 与脚本 hash 均进 manifest |
| 评测测试 | 350–500 | 600 | 重点覆盖协议和缝合线 |

任一 active state evaluation 源文件同样不得超过 600 行，不新增 complexity 例外。

### 14. 明确不做

1. 不把灰区 LLM 判官接入产品写入、维护或自动 supersede。
2. 不保留本地 qwen3.8 C 臂、占位配置或模型专用 prompt。
3. 不无条件运行 D；D 不具有补考或替代 B 的资格。
4. 不把灰区默认送人工队列，不要求产品用户逐条裁决；只并存可见和可选提醒。
5. 不把 Codex/Hermes 运维裁决写成通用产品能力或产品文档承诺。
6. 不恢复 B2/P1 prompt、admission snapshot 特许或 v0.30 通用状态 canonicalizer。
7. 不从普通聊天 observation、assistant status narration 或自由文本 tool output 推导 currentness proof。
8. 不允许 `report-version` 接受调用者提供的任意版本值，不允许 owner 未解析时回退到模糊 subject。
9. 不按版本号大小决定时间方向；合法 downgrade/rollback 必须能成为较晚 current tip。
10. 不跨 namespace/subject/slot/qualifier 使用 FTS、向量、编辑距离或动态 alias 扩大候选。
11. 不把 `recorded_from` 冒充 event time，不用 confidence/recency/authority 单独破平局。
12. 不因 embedding 缺失阻止完全不使用 embedding 的确定性版本规则；使用 embedding 的未来路径才受其否决。
13. 不新增第二套 lifecycle/case/revision/rollback 数据模型，不复制现有 governance/L2 client。
14. 不把百分比 canary、动态配置服务或产品内 cron 当成本轮新能力。
15. 不以“补偿可逆”降低 precision=100%、反例误关=0 的门禁。
16. 不复用或改写 r1–r5 烧毁语料；也不在 A 失败后调规则再拿 B 当补考。
17. 不因真实版本噪音仍高就扩大到 health/process/deployment/connectivity/job；每个 slot 另立证据。

## 选择原因

1. **错误确定性优先防御。** 3 条反例误 supersede 和 E1C 的 2 个危险反向证明，模型覆盖增益不足以承担关闭
   current claim 的破坏性权限。
2. **结构化探针补足收益入口。** 普通聊天 observation 不能授权 currentness；可信、零 LLM 的版本自报让窄规则在
   不放宽证据标准的前提下处理真实旧版本 tip。
3. **冻结集与执行独立。** 两份并行冻结集、一次评分和 Hermes 独立重算针对 v0.30.0 的 dev 反复调优与评分缝合
   缺陷。
4. **复杂度有界。** 复用现有坐标、双时间、事务、revision 和治理账本，不创建第二套生命周期或产品 LLM 系统。
5. **灰区可见而不强判。** 并存噪音是可观察问题；错误关链是静默破坏。不能证明时保留多值比制造单一真相安全。

## 后果

正面后果：

- 可证明的 `config.version` 序列能形成有证据、可重放、可补偿的历史链；
- 普通聊天、跨环境和不可信时间不会因版本字样相似而互相关闭；
- 产品保持零 LLM 自动关链依赖，灰区不增加用户必办负担；
- 评测、shadow、存量 apply 和代码规模都有预注册停止线。

负面后果：

- 首版不会清除全部版本文本噪音，只处理满足 currentness proof 的同坐标序列；
- 版本探针需要每个部署提供稳定、唯一的 typed owner；缺 owner 时收益为零但不会放宽；
- 两份 400 案冻结集、独立验收和 48h shadow 增加发布前工作量；
- 达不到 eligible recall 或 real-cohort 降幅时，即使规则安全也不发布。

## 重新评估条件

满足以下任一条件时必须新增 ADR，不得原地放宽本协议：

- 计划开放 `config.version` 之外的状态 slot；
- 计划让 LLM、向量相似度、模糊 alias 或普通聊天 observation 取得自动关链权限；
- 计划改变 currentness proof producer、版本 atom 白名单、九项前置条件或八条硬否决；
- 计划降低任一安全门、覆盖门、shadow 覆盖量或独立验收要求；
- 实现必须新增 lifecycle/case/revision 数据模型、migration 或超过本 ADR 行数硬上限；
- 两份冻结集、生产 shadow 或 real cohort 任一显示危险误链。

## 参考

- 仓库内：[v0.30.0 状态实验撤回记录](../CHANGELOG.md#未发布v0300-状态实验收档2026-08-22)；
- 仓库 commit：`3a80601877a708e61ee4cf1bb30fa23c6c4e5df7`；
- 仓库外设计过程：`proposal-llm-final-adjudication-20260826.md`；
- 仓库外独立方案：`hermes-plan-v300-restart-20260826.md`；
- 仓库外收敛终稿：`consensus-v300-restart-20260826.md`；
- 仓库外 E1C 云端复检：`evaluations/v030/cloud_verify/E1C/report.json`。
