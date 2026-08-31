# HL-Mem 变更记录

## v1.0.0rc1（2026-08-31）

### Core 1.0 Phase 6：可审计发布候选

- Python 3.12、3.13、3.14 的完整测试矩阵执行 80% 覆盖率门槛；公开 32 案零网络召回门禁不再因私有
  fixture 缺失而跳过，空库、历史库和重复 migration 均进入发布证据。
- 新增确定性的发布证据清单、`v0.36.1` 冻结 Benchmark、依赖漏洞扫描、CycloneDX SBOM、CodeQL、完整
  Git 历史密钥扫描和全 SHA 固定的 GitHub Actions。发布门禁只验证，不自动上传 PyPI。
- `1.0.0rc1` 必须连续观察七个 UTC 日期且没有未关闭 P0/P1，才允许提升为稳定版；SQLite 仍只支持
  备份恢复，不提供 schema downgrade。

### Core 1.0 Phase 5：职责边界与评测解耦

- 提取、召回交付、HTTP 路由和 Worker 编排按真实职责拆分，保留现有公共 patch point 和业务语义；复杂度
  预算同步收紧，不进行全仓目录重排。
- 稳定 `hl-mem eval` 继续随 wheel 发布；v0.30 历史研究装备迁入 `benchmarks/archive/`，生产包禁止导入或
  携带研究代码。

### Core 1.0 Phase 4：显式自动化与关系治理

- 语义维护任务同时在入队和执行阶段门控；升级会终止遗留 pending 语义任务并废弃 pending resurrection，
  防止旧队列绕过新默认值。
- LLM dedup、冲突语义审查、重分类、Query Expansion、Resurrection 和关系发现均要求显式开启；关系发现
  只写 Proposal，批准与带 provenance 的正式落边在同一事务完成。
- 确定性 near-copy 审查与 Observation 构建保持低成本路径；所有 Provider 调用受原子预算、审计和失败结算
  治理。

### Core 1.0 Phase 3：受治理的 Provider 扩展与统一调用面

- 新增版本化 `hl_mem.providers` Entry Point、显式 allowlist、Manifest/配置 Schema/版本协商和冲突即失败的冻结
  Registry；内置与第三方 LLM、Embedding、Reranker 走同一稳定契约，Image 契约明确标为 Experimental。
- 四条真实模型调用统一由宿主执行 HTTP、重试、错误归一化、审计、指标与原子用量预留/结算；Embedding 按实际批次、
  Reranker 按文档数、Image 按图片数记账，Fake/关闭路径不产生用量事件。
- 图片输入在插件执行前完成 base64、文件 allow-root、HTTPS 跳转、公网 DNS、流式大小、MIME/扩展名/魔数与哈希
  校验；插件仅接收字节、MIME 和哈希。
- `doctor` 新增插件解析、进程内信任和只读用量账本检查；`/healthz` 新增脱敏 Provider 清单与聚合用量。
- 新增 Provider API 快照、独立外部插件 wheel 安装门禁和四调用路径完整结算门禁。Provider 插件不提供路由、任务、
  migration、存储或安全策略扩展点。

### Core 1.0 Phase 2：配置与启动边界

- 配置升级为显式 `schema_version = 1`；生产启动要求完整的模型服务与独立密钥，Fake 提取、Embedding 和 Reranker
  仅保留给 `Settings.for_test()`，未知键、未来版本和退役键均 fail-closed。
- 新增确定性的 `hl-mem config migrate`：默认只输出脱敏计划；`--apply` 在验证数据库 backup、manifest 与 tombstone
  ledger 身份后保存逐字节旧配置并原子替换。配置 schema 与 SQLite migration 均不提供 downgrade。
- `hl-mem init` 改为服务中立的验证向导；删除 `--offline`，不再生成低质量 Fake 配置。LLM、Embedding 与可选
  Reranker 只有探测成功后才写入 TOML 和 `.env`，失败保持已有文件不变。
- `doctor` 新增稳定检查码、JSON 输出、生产就绪与恢复集验证；配置错误成为结构化失败。集成门禁证明诊断不会修改
  配置、密钥、数据库、tombstone ledger、backup 或 manifest。
- 删除 extraction pre-filter、独立 Tag 候选通道和关系自动落边配置；Tag soft boost 保留，关系发现仅允许
  `off`/`audit`。Query Expansion 与 Resurrection 的默认值改为 `off`。
- 新增 `docs/config-schema.json` 公共配置快照及 CI 门禁；生产配置分为八组类型化所有权，兼容 facade 保持薄层。

### Core 1.0 Phase 1：运行与兼容基础

- 正式支持 Python 3.12–3.14，统一仓库换行规则；测试期 SQLite 连接所有权、`ResourceWarning` 和
  unraisable warning 进入确定性门禁。
- 请求体限制按实际流式字节执行并限制缓冲生命周期，无法再用缺失或虚假 `Content-Length` 绕过；历史
  Procedure/Tool 召回遵守双时间边界。
- 建立绑定的 1.x 兼容、弃用和恢复政策：稳定 REST、MCP、CLI、配置 schema、备份格式和 Provider API 在
  1.x 内兼容；不可逆升级只能通过升级前恢复集回滚。

## v0.36.1（2026-08-30）

### Hermes 冲突提示生命周期

- `ManualConflictNotice` 的 session 计数状态增加 256 条硬上限，并在既有 `on_session_end(messages, **kwargs)`
  生命周期钩子清理对应 session，避免长生命周期 Hermes 进程随会话数无界增长。
- 保持既有提示语义不变：`count=0` 静默、相同 session 的相同计数不重复提示、非零计数文案和 Degraded 路径均未改动。

### 无自动化宿主引导

- 中英双语 delegation 指南新增 conflict owner 选择、无 loop 时的残留语义、Hermes provider 工具边界、人工 REST
  行动入口与端到端安装验收要求。
- 本补丁不新增 migration，不改变 REST/OpenAPI、MCP 或 prompt 文案契约。

## v0.36.0（2026-08-30）

### 内置冲突 L2 退役

- 删除 conflict L2 judge 的 admission、enqueue、模型调用与 apply 生产链；确定性 L0 自动收敛继续保留，未命中案直接进入
  `manual_required`，`l0_only` 与三处人工桥不变。
- migration 057 将遗留非终态 `resolve_conflict_llm` job 标为 `dead`、清除 lease，并重新 dirty 关联开放案；既有
  `succeeded`/`dead` 历史不改，migration 只向前执行。
- 删除 `[maintenance_judge]` 配置面及隐式本地 8090 默认；`conflict.auto_mode` 仅接受 `off`/`l0_only`。

### Delegation 写面与 CAS

- `POST /v1/conflicts/{id}/resolve` 新增 pair 词表 `keep_left`、`keep_right`、`coexist`、`reject`，并保留 group
  `select_candidate`/`reject_candidate` 契约和破坏性确认要求。
- dossier、winner 与 fingerprint v2 统一跟随 supersession tip，同时保留完整 lineage 和 valid/recorded 过程信息；
  revision 与可选 fingerprint 任一 stale 均在 mutation/audit 前返回 `409`。
- 终态 conflict rationale 不可改写；pair/group winner 均返回 tip ID。新增中英双语宿主集成指南，覆盖有界轮询、
  action 分支、CAS 重拉与 Linux cron/systemd 接线边界。

### Breaking changes 与升级要求

- **Breaking：**`[maintenance_judge]` 已删除，旧配置会以未知 section fail-closed；升级前必须从 `hl_mem.toml`
  删除整段，并把旧 `conflict.auto_mode="observe"|"enforce"` 改为 `off` 或 `l0_only`。
- **Breaking：**终态 rationale immutable；宿主不得用终态重放修改理由，应在首次裁决前生成最终 rationale。
- 本版保持 57 个 SQL migration；升级前停止全部写入者，并一致备份 checkpoint 后的主库、WAL 相关状态与
  tombstone sidecar。旧二进制不得重新打开已升级数据库继续写入。

## v0.35.1（2026-08-30）

### 冲突候选撤回安全闭环

- **Breaking：**group 案的 `reject_candidate` 是会把候选全部成员 Claim 置为 `retracted` 的破坏性动作；REST 与
  application service 现在都要求显式传入 `confirm_retraction=true`，缺失或为 `false` 时 fail-closed。该确认只作用于
  `reject_candidate`，不改变 `select_candidate` 或 pair 裁决行为。
- `reject_candidate` 从 `pending`、`auto_resolved` 或 `manual_required` 执行后统一持久化为 `manual_required`，确保数据库、
  响应与治理审计的 after 状态一致。
- group 候选撤回审计新增成员总数、完整稳定 ID 集合的 SHA-256、最多 64 个排序后成员 ID 及截断标志，使已撤回集合可核验。

### delegation 契约补全

- conflict dossier 的 Claim 明细补充 `qualifiers`、`recorded_from` 与 `recorded_to`，与 valid/recorded 双时间裁决上下文对齐。
- 互斥 group 对 `coexist`/pair `reject` 的错误提示不再指向不存在的 slot/qualifier 在线修正端点，改为说明必须选择唯一
  有效候选且当前接口不提供在线坐标修正。
- 无 migration；OpenAPI 同步到 v0.35.1，并公开 `confirm_retraction` 与 dossier 新字段。

## v0.35.0（2026-08-29）

### 冲突裁决 delegation 读面

- 新增 `GET /v1/conflicts/{id}/dossier`，为 pair/group 冲突统一返回完整案卷：Claim 全文、authority、confidence、
  valid/recorded 双时间坐标与 evidence 对应的源事件，agent 无需再跨表拼接裁决上下文。
- 新增 `GET /v1/conflicts` 三态分页列表，覆盖 `pending`、`auto_resolved` 与 `manual_required`，作为 agent 有界轮询与
  分页领取待裁决案件的入口。

### 冲突裁决写面与人工审计

- pair 案与 group 案统一执行 `expected_revision` 乐观锁；pair 案不再接受缺失 revision 的假保护，也会拒绝陈旧
  revision，避免并发裁决覆盖较新的案件状态。
- 人工裁决现在写入 `governance_actions` 审计账本，记录 resolver、decision、rationale、前后 revision 与状态；
  `reject_candidate` 的 rationale 同步持久化，不再在丢弃候选时遗失。
- CLI 新增 `--resolver`，默认 `agent:hermes-local`；REST 输入沿用同一默认 resolver，并把人工裁决身份带入审计链。

### 模块边界与契约收尾

- 将冲突查询、冲突审计与 API 路由分别下沉到 `conflict_queries`、`conflict_audit`、`conflict_routes`，CLI、
  application 与 API 消费方改为直连真实定义源，保持复杂度棘轮不回退。
- 查询扩展独立线路校验与文档合同对齐：仅配置 `QUERY_EXPANSION_API_KEY` 时继承主线路，不再阻断启动；只有显式配置
  query-expansion provider 或 base URL 时，才要求 provider、base URL 与 key 三项齐全。
- 修复配置参考生成器并清理未使用导入，生成文档重新与 Settings 合同保持一致。

### Breaking changes 与升级提示

- **Breaking：**`hl-mem conflicts resolve` 现在必须显式传入 `--expected-revision`；遗漏参数会直接报错，不再以无保护方式
  尝试裁决 pair 案。
- **Breaking：**pair 案的直接应用服务与 REST 调用现在执行真实 CAS；`expected_revision` 缺失或落后于当前案卷 revision
  时会拒绝写入。调用方应先读取列表或 dossier 的最新 revision，再提交裁决。

## v0.34.0（2026-08-29）

### 查询扩展独立线路

- 查询扩展新增独立线路配置：`recall.query_expansion_provider`、`recall.query_expansion_base_url` 与
  `QUERY_EXPANSION_API_KEY` 全部留空时继承主 LLM 线路；任一项配置后必须三项齐全，否则启动时 fail-closed。
- `recall.query_expansion_timeout_seconds` 默认值从 5 秒提高到 15 秒，
  `recall.query_expansion_total_timeout_seconds` 默认值从 6 秒提高到 16 秒，为独立线路保留合理的请求与总预算。

### L1 生产接线移除

- 删除未进入生产决策的 L1 接线与 `conflict.l1_min_time_delta_seconds`、
  `conflict.l1_min_confidence_delta` 两个配置键；L1 算法继续保留为 E1 冻结语料回放装备，不参与生产维护路径。

### 配置参考修复

- 配置参考生成器现在识别 `QUERY_EXPANSION_API_KEY`，并同步 Settings 字段计数契约。

### Breaking changes 与升级提示

- **Breaking：**旧 `hl_mem.toml` 若仍包含 `conflict.l1_min_time_delta_seconds` 或
  `conflict.l1_min_confidence_delta`，新版本会按未知键 fail-closed。升级到 v0.34.0 前必须从 TOML 中移除这两个键。

## v0.33.0（2026-08-29）

### 提取触顶软拆分与实验装备

- 新增 `extraction.soft_split_enabled`，在紧凑提取结果命中 schema 上限时提供二分重提取与去重合并；只有同时启用
  `extraction.delta_repair_enabled`，且首次二分后的子块仍命中上限时，才会懒触发一次残余增量修复。两项开关默认
  均为 `false`，单独启用 delta repair 不增加调用，能力随版提供但不启用。
- 配套 A/B runner、冻结语料导出、四门评分和审计事件。现有轻量模型代际实验未命中触顶条件，因此本版只保留
  懒触发能力，不把实验路径提升为默认生产行为。

### LLM 输出上限保险丝

- 新增可选 `llm.max_tokens`，统一传入 DashScope、Zhipu 和 OpenAI-compatible 请求，防止异常长输出无界占用时间与
  token。默认未设置，保持 provider 既有上限；若响应以 `finish_reason=length` 截断并破坏 JSON，结构化提取会快速失败，
  交由既有重试/降级链路处理。

### latest-wins 完整性与 Claim mutation 审计

- reclassify 现在识别并保护带完整 `status_report_v1` 证据链的 `config.version` 确定性探针，避免 LLM 重分类改写其
  currentness proof 坐标；legacy backfill 的独立连接也注册必要的 SQLite 函数。
- migration 055–056 在数据库边界为每次 Claim UPDATE/DELETE 写入 `claim_mutation_audit_v1`，记录变更字段、来源与
  trace/event/query/job 维度；056 将上下文桥接改为可移植的持久表 + 每连接 TEMP trigger，并在单次 mutation 后清空。
- 空库首次启动改为先完成 migration，再注册 Claim 审计上下文 trigger，避免表尚未创建时初始化失败。当前 schema
  基线为 56 个不可变、仅向前执行的 SQL migration（001–056）。

### A/B provider 配置忠实度

- `var/eval/softsplit_ab_20260827/run_ab.py` 新增 `--respect-llm-config`；显式使用后，非 delta arm 会保留 TOML 中的
  LLM provider/model/base URL，不再被 runner 的历史固定值张冠李戴。默认不带参数时仍保持冻结实验的旧配置，保证
  已有结果可复现。

### llama.cpp thinking 方言

- 新增 `llm.thinking_control="auto"|"chat_template_kwargs"`。默认 `auto` 保持各 provider 既有请求格式；仅对
  OpenAI-compatible 显式选择 `chat_template_kwargs` 时，才把 `enable_thinking` 放入 llama.cpp 使用的嵌套字段，并
  兼容剥离 JSON 前的空 `<think>...</think>` 块。

### 提取密度与 Zhipu 低推理强度

- compact 中英文 prompt 改为“覆盖优先”：先扫描全文，再输出所有有证据、可独立回答的原子事实；高密度长文开放
  12–30 条协议，同时明确禁止为了凑数而重复、碎片化、概括填充或虚构。响应 schema 的 `maxItems` 从 20 提升到 30。
- 新增可选 `llm.reasoning_effort=low|high|max`，只在显式配置时透传给 Zhipu，其他 provider 与未配置部署不变。
- Zhipu coding 线路 `effort=low` 的 20 案终验中，单案延迟从此前 200–900 秒量级降至 P50 **21.7 秒**；
  **15/20** 个密集案产出至少 12 条，抽审虚构 Claim 为 **0**。密度与 gold coverage 的扩展门仍未全部通过，因此
  软拆分/增量修复继续默认关闭。

### Behavior changes

- 默认提取行为会使用新的 coverage-first prompt，并允许单 chunk 最多返回 30 条 Claim；短文本仍允许 0–少量，
  AdmissionPolicy、证据校验与去重链路不变。
- 数据库升级后，所有 Claim UPDATE/DELETE 会追加审计行，审计存储量会随访问刷新、维护和删除操作增长；业务 mutation
  与审计写入保持同一 SQLite 事务。
- `extraction.soft_split_enabled`、`extraction.delta_repair_enabled`、`llm.max_tokens` 与 `llm.reasoning_effort` 均不因升级
  自动启用；`llm.thinking_control` 默认为 `auto`。REST/MCP 业务契约 major 不变，OpenAPI 仅同步服务版本，MCP 工具
  schema 无业务字段变化，`/healthz.version` 自动跟随 `hl_mem.__version__`。

### 回滚与配置提示

- 保持或恢复保守行为时，确认 `extraction.soft_split_enabled=false`、`extraction.delta_repair_enabled=false`，并将
  `llm.thinking_control="auto"`；若不需要输出保险丝或 Zhipu 推理强度覆盖，删除 `llm.max_tokens` 与
  `llm.reasoning_effort` 配置项即可。发布准备不会改写任何部署机的 `.env` 或 `hl_mem.toml`。
- 新 prompt 与 30 条上限没有运行时回滚键。若必须整体回退到 v0.32.0，应停止全部写入者并恢复升级前的主库与
  tombstone sidecar 一致备份；不得让旧二进制继续写已经应用 migration 055–056 的数据库。

## v0.32.0（2026-08-26）

### `config.version` 确定性关链

- 按 [ADR-0004](adr/0004-config-version-deterministic-latest-wins.md) 新增 `config.version` 确定性 latest-wins：
  exact coordinate、可信 event time、来源权威与固定 currentness proof 合同全部满足时，才判定 duplicate、
  corroborates、supersedes 或 historical predecessor；灰区保持并存可见，不新建人工必办队列。
- 新增 `state.latest_wins_mode=off|observe|enforce` 与 `state.latest_wins_slots` 双开关。代码默认 `observe`，只记录
  版本化建议而不改 Claim；当前 slot 白名单仅含 `config.version`。
- 新增 `hl-mem report-version --namespace <namespace> --subject <owner>` 确定性版本探针。版本只读当前包
  `hl_mem.__version__`，owner 必须唯一解析；事件与 Claim 直接投影，零 LLM，失败时 fail-closed。

### 冻结评测依据

- validation A/B 是两份独立的 400 案冻结集。B 臂 exact **800/800（100%）**、eligible recall
  **320/320（100%）**、自动 edge precision **320/320（100%）**；危险误关链 **0**、反例误关 **0/400**、
  跨坐标动作 **0/160**、historical predecessor 方向 **80/80**。
- A（off）基线 exact **20%**、eligible recall **0%**。正式评分只运行一次、零补考；冻结 manifest SHA-256
  前缀为 `25be3ca7`，Hermes 已用预注册独立脚本重算一致。

### 历史与回滚

- 本能力是 v0.30.0 状态行为在 commit `3a80601` 撤回后的窄范围重启：只授权单 slot、结构化探针与确定性规则，
  不恢复通用状态 prompt/canonicalizer，也不让 LLM 取得生产关链权限；冻结协议全文以 ADR-0004 为准。
- 紧急停止新建议和新动作只需设置 `state.latest_wins_mode = "off"`。已经落地的误链必须通过补偿 revision 修复，
  不删除历史 Claim 或证据。

## v0.31.1（2026-08-26）

### Hermes 注入链路与配置安全

- Hermes 插件固定从 `<HERMES_HOME>/hl_mem.toml` 和 `<HERMES_HOME>/.env` 加载配置，不再受 gateway/CLI
  进程当前目录影响；调用方显式传入的路径仍保持最高优先级。
- 插件注册失败会以 ERROR 和 traceback 记录异常类型、配置路径、CWD、Hermes Home 与 hl_mem 版本，然后原样
  抛出；日志不包含环境变量值、API key、TOML 内容或消息正文。
- `database.path` 的相对值改为相对配置文件 symlink 的真实目标目录解析。POSIX 拒绝 Windows drive/UNC
  绝对路径，Windows 拒绝 POSIX 绝对路径，避免错误配置静默创建影子数据库。
- Hermes 插件 install/upgrade 在实际写入后明确要求重启所有已导入 hl_mem 的 gateway/CLI 进程，并禁止用
  已导入旧 editable checkout 的进程验证新配置。

本版没有新增 TOML key、schema migration 或 REST/MCP 契约变化。升级前应先确认新旧数据库绝对路径；若不同，
先完成受控数据迁移，再启动写入者。小宇宙等停留在旧版的实例不应接收 v0.31 配置。

## v0.31.0（2026-08-25）

### 新能力

- 新增 typed canonical entity、版本化 alias/entity relation/Claim link 与热路径 subject/target 坐标。类型不同的
  `person`、`agent`、`device`、`environment`、`instrument`、`project`、`topic` 不自动合并；无显式 proof 时继续
  使用隔离的 legacy 坐标。
- 新增治理动作账本、输入 fingerprint、短事务 CAS 和有条件 rollback，供 conflict、dedup 和 plan mutation 共用；
  三个领域仍保留各自的 case/outcome 枚举和状态账本。
- plan fulfillment 支持 complete/cancel/replace/partial，严格匹配 typed target、action、direction、Decimal
  quantity/unit、account 和 valid-time window。关闭只写 `valid_to`，不改 `recorded_to`、Claim status 或
  `superseded_by_id`。
- 价格序列增加 `(axis, canonical_target_entity_id, snapshot_date)` 坐标；qualified code 与唯一 typed alias
  可确定性解析，缺 target、跨市场歧义、币种或单位变化继续 fail-closed。
- 冲突维护增加 L0–L3 分层、policy-version 重扫和可选 loopback `[maintenance_judge]`。本发布只启用 L0；生产
  零常驻 LLM 依赖。`l0_only` 不执行 L1、不创建 L2 job，且会在构造 judge 前跳过升级前残留的 L2 job；随包
  评测装备可供用户在自己的冻结语料上自验后再显式选择 L2。
- dedup 增加 typed governing entity + slot bucket 的跨 subject 候选及 protected-atom apply gate；查询增加实体
  mention/proof/coverage shadow trace；提取后处理增加 grounded `lesson_signal`。三者发布默认均保持非破坏模式。
- `/healthz` 暴露 residual `manual_required` 计数与最老年龄；Hermes 插件 2.1.0 在同一 session 首次或计数变化时
  最多提示一次，不把 pending/open 案冒充人工案，health 失败不注入旧计数。

### 发版决议与诚实披露

- **E1 SEALED×2**：全量自动化未过门禁，L1 全禁用，L2 不作为默认生产路径。仅 L0 sealed 集达到
  **37/37 precision、危险反向 0**，因此 `conflict.auto_mode="l0_only"`。
- **E5 PASS（A 臂）**：143 个场景中 macro-F1、complete/cancel/replace/partial/ambiguous recall、partial 数量守恒
  均为 **1.0**，错误关闭 **0**，因此 `plan.fulfillment_mode="enforce"`。
- **E6 PASS（B 臂）**：120 条、58 个 instrument，exact target precision **1.0**、coverage **0.90**、
  series decision accuracy **1.0**、missing→uncertain **1.0**、跨 target supersede **0**，因此
  `price.target_mode="enforce"`。
- **E2 SEALED_v2**：自动 dedup 证据未过，`dedup.audit_only=true` 保持；已审 equivalent 不在升级时批量应用。
- **E3 SEALED_v2**：新 notability prompt 不替换旧 prompt，`extraction.lesson_signal_mode="observe"` 保持。
- **E4 行为过、证据不足并封存**：冻结查询全部来自 snapshot-derived synthetic，不能证明 production-shaped
  link coverage，`recall.entity_constraint_mode="observe"` 保持，不对发布候选做硬过滤。

### 配置、迁移与回滚

- migration 050–054 依次增加 governance ledger、conflict policy、typed entities、plan fulfillment 和 slot dedup
  metadata；历史 001–049 未修改。REST/MCP major 不变，OpenAPI 仅反映新增 health 字段和发布版本；Hermes
  plugin minor 从 2.0.0 升至 2.1.0，daemon/context/plugin contract major 均不变。
- 冲突自动化回滚：设 `conflict.auto_mode="observe"` 或 `off`，停止新的 L0 mutation；已应用动作只能通过当前行
  仍匹配 after fingerprint 的 governance rollback 恢复。
- plan 回滚：设 `plan.fulfillment_mode="observe"` 或 `off` 停止新关链；已关闭 plan 按 outcome/action CAS 回滚，
  有后续 outcome 时拒绝覆盖并生成审计案。
- 价格回滚：设 `price.target_mode="observe"` 或 `off`；已由 target 触发的状态变化按对应 action CAS 回滚，不以
  清空 target 列代替历史修复。
- dedup、lesson signal 与查询实体约束分别以 `dedup.audit_only=true`、`extraction.lesson_signal_mode="observe"`、
  `recall.entity_constraint_mode="observe"` 保持旧行为；Hermes 提示可设 `hermes.manual_conflict_notice=false` 关闭。

## 未发布：v0.30.0 状态实验收档（2026-08-22）

- 状态坐标闭环在反复使用同一份 400-bundle dev 集完成 B2、P1、I1、Z1–Z5 和生产接线后取得 dev 13/13，
  但该结果没有泛化到独立 held-out-r5。r5 的运行时守卫、全链冒烟、120/120 transport 和评分均无异常，最终仅
  通过 3/13：原子 precision/recall 为 95.0156%/87.6437%，坐标 precision/recall 为
  70.6714%/67.5676%，edge precision/recall 为 81.2500%/69.6429%，stale absolute 为 25.1852%。
- r5 产生 27 条错误 edge，其中 3 条是冻结反例语境下的误 supersede，违反 edge precision=100% 和
  counterexample false supersede=0 两条安全线。自动关链会让仍有效的 claim 退出 current 视图，因此不能把该结果
  作为可接受的召回噪声发布；已撤回 admission 状态放行、状态 prompt/canonicalizer、双时间身份、resolver、历史访问
  刷新变化和 volatility 行为文档，恢复 v0.29.3 的保守状态语义。本次未发布、未打 tag、未部署，也没有 schema、
  migration 或配置变化。
- 评分器与 adapter 修复、版本化 corpus builder、全链零 LLM 冒烟、runtime guard、冻结语料和实验审计记录继续保留，
  用作未来从新假设和新独立验证集重新启动的基础设施。所有已运行 held-out/sealed 集均视为烧毁，不得用于后续调参
  或再次充当发布证据；dev 13/13 只保留为已知分布内证据，不再宣称为生产有效性结论。

## v0.29.3（2026-08-21）

### Temporal 序列坐标

- 为价格/计量类时间序列快照增加保守的序列坐标判定，避免正常快照被误送入人工冲突队列。同一标的、同一度量的
  不同时间坐标会以 `snapshot_advance` 自动收敛：较新快照成为 current tip，旧值退出 current-state 召回；若先写入
  当前值、后回填历史快照，回填值也会直接关闭而不覆盖当前值。不同度量或不同标的以 `distinct_series` 直接共存。
- 同坐标修订、坐标/标的无法可靠证明、隐式目标价替换、来源或单位不兼容、候选顺序混杂等真矛盾或灰区仍为
  `uncertain`，继续进入既有人工管线，fail-closed 边界不变。人工案的 `rationale` 现携带 evaluator 的具体原因，
  例如 `temporal_update_uncertain:snapshot_coordinate_equal`、
  `temporal_update_uncertain:price_replacement_not_explicit` 或
  `temporal_update_uncertain:snapshot_order_mixed`，不再只给出泛化理由。
- 火山 11 案冻结回放由旧版的 **10 个 `uncertain` + 1 个 `not_applicable`**，收敛为
  **7 个 `distinct_series` + 2 个 `snapshot_advance` + 1 个 `uncertain` + 1 个 `not_applicable`**：
  7 个不同度量/标的直接共存，2 个同序列快照推进自动关链，1 个标的无法证明的灰区继续人工，非时间事实保持
  `not_applicable`。另有同坐标延迟修订、隐式目标价替换和同日混度量反例锁定 fail-closed 三分支边界。

### 编排器拆轻与复杂度棘轮

- 在 characterization 等价验证保护下拆轻 Recall/ingest 编排器，外部行为与可 patch 调用面不变：
  `RecallService.recall()` 从 469 行降至 68 行，procedure 分支下沉到 `ProcedureRecallFlow`；
  `IngestService.store_extracted()` 从 353 行降至 163 行，冲突/时间决议下沉到 `_ingest_resolution`。
- 新增 `scripts/check_complexity_budget.py`、`scripts/complexity_budget.json` 与 CI 棘轮门禁，同时约束模块物理行数、
  callable 有效参数数和 callable 长度。新模块默认不超过 600 行，新 callable 默认不超过 10 个参数和 150 行；
  allowlist 只允许持平或下降，PR 通过 base 比较拒绝新增例外或抬高既有预算。

### 配置、兼容与升级

- 本版没有 schema migration、配置键变更或破坏性 REST/MCP API 变化；`daemon_contract` 保持 `1`。
- 升级后的行为变化仅限 temporal 的 `snapshot_advance` / `distinct_series` / `uncertain` 三分支。没有退回旧判定的
  配置开关：该行为变化就是 v0.29.3 的发布目的；若必须回退，只能降级到旧版本。

## v0.29.2（2026-08-20）

### 注入治理默认开启

- `recall.echo_suppression_mode` 的代码默认值由 `off` 翻转为 `enforce`，
  `recall.freshness_annotation_mode` 的代码默认值由 `off` 翻转为 `render`；策略实现、执行顺序和显式配置均未改变。
- 完整行为评测中，结构层 200×4 全部通过、sentinel 9/9、全量 131/131 agent + 131/131 judge、人工盲审
  9/9；误导采信从 7.3% 降至 0%，echo 抑制召回为 1.0 且零误伤，token 代价为 claim 18、packet P95 0.9%。
- 20 条冻结 stable 验收集为 19/20；未通过项是双臂对称的毛刺样本，不属于机制伤害，按已知边界留档，
  不针对单个 case 调优。
- 升级即默认开启，不需要 migration 脚本。需要退回旧行为时，在 `hl_mem.toml` 显式写入：

  ```toml
  recall.echo_suppression_mode = "off"
  recall.freshness_annotation_mode = "off"
  ```

## v0.29.1（2026-08-18）

### 注入治理与离线门禁

- 修复 Hermes `on_pre_compress` / `on_memory_write` 的 session 传播，并增加共享的 delivery purpose、实验 variant、
  policy version、rendering clock、trace/health envelope 与 cache 隔离上下文。
- 新增相互独立的纯策略 `EchoSuppressionPolicy`（`off|observe|enforce`）和
  `FreshnessAnnotationPolicy`（`off|observe|render`），发布默认均为 `off`。统一执行顺序固定为 echo filter →
  reranker → freshness decorate → packing；本版不增加 `verified_at`。
- migration 048 为 dedup pair 增加确定性来源和新 Claim 端点信号；副本工具可把 597 条低于当前 floor 的 pending
  pair 以 `dismissed_below_floor` 终结，apply 必须提交精确 expected-count，且不改 Claim。
- 新增固定 200-point fixture 构造和 echo × freshness 2×2 bundle replay。离线报告验证 observe 不改输出、跨会话/
  historical/proper-noun 切片等价、18-token 上限和重新 packing；线上 observe/canary 质量评估留给 Hermes 部署后执行。

### 有界回收与兼容清理

- expired Claim 在超过 90 天历史保留窗、没有下游 evidence 消费者且没有 open conflict 时才可回收。维护默认
  `observe`，`on` 每轮最多 100 条；副本 CLI 默认只读，apply 需要精确 expected-count，物理删除逐条复用独立
  tombstone `DeletionService`。
- migration 049 在任何 DROP 前验证 047/048 版本证据并扫描数据库内 view/trigger 消费者；有消费者即事务回滚。
  版本门槛通过后移除 legacy `claims_tags_fts` 和三触发器，同时删除 deletion/tag-update 两个兼容 shim，维护写入
  显式同步 tokenized FTS v2。SQLite 无法发现外部直接 SQL，旧二进制回滚窗口必须由发布流程先关闭。

## v0.29.0（2026-08-18）

### 受限 assertion 门控

- Claim 新增正交的 `assertion_kind=unknown|observation|inference` 认识论字段。产品 compact 提取契约要求模型区分
  证据直接报告的 observation 与推导结论 inference；无法可靠判断时必须输出 unknown。冻结的七字段与 RAO 评测
  契约保持独立，legacy 解析继续兼容。
- migration 047 为存量 Claim 回填 `unknown`。该默认值只用于观测和公开 DTO，不参与 supersede、召回过滤、注入或
  排序；只有新写入的显式门控值可被后续高精度时间关链逻辑消费。项目版本升至 `0.29.0`，schema 为 47 个不可变、
  只向前执行的 SQL migration（001–047）。

### 终态 conflict generation/reopen

- 复用 v0.28.9 的组级 case、candidate、generation、revision 与单 open-case 约束。组内已有终态 generation 时，
  同一 active winner 的精确重申只追加 evidence；不同当前值不改写旧案，而是创建 `generation + 1` 的
  `manual_required` 案，旧 generation 保持不可变。
- 本版不引入 issue-platform 能力：不实现候选压缩、冷热分层、分页、冷却、拆案、重分类、延迟处理或 rollup。

### 高精度自动关链

- 在现有 `entails/state_change/contradicts/uncertain` 写入框架内增加 `temporal-v1` 纯函数，只接受新写入且显式为
  `observation` 的 Claim。确定性覆盖仅含两段：同 subject/attribute/qualifiers 的原子 online/offline 状态，以及带
  旧值锚点、价格轴、严格新时间、来源权威、币种和计费单位守卫的显式价格更正；存量 `unknown` 本身不能授权动作。
- `config.path`、`config.network` 与任何带非互斥 operational slot 的 Claim 明确拒绝该扩展；无法证明的同轴价格
  更新进入既有 pair conflict 人工管线，不静默 latest-wins，也不调用 LLM 或建立通用时间边分类器。
- 固定生产历史门禁由 `scripts/run_v029_temporal_replay.py` 在 `var/eval` SQLite 副本上运行，源库强制
  `mode=ro + query_only`。本次回放 14/14 价格 correct，precision/coverage 均为 1.0；Tailscale 三快照得到
  `entails → state_change`；120 条 `config.path` 与 4 条 `config.network` 合法共存样本误接链为 0。

### Current-state 注入验收

- A3 不新增过滤、排序或注入机制。三快照经 A2 形成 supersede 链后，current-state 的 REST 结果候选、packed
  context 与最终 Context Packet 均只包含当前 online tip；显式 historical retrieval bundle 仍可返回旧 offline
  与当前 online 两个版本。
- 现有双时间可见性已经满足验收，回放没有提供调整排序的必要证据，因此 recency 权重保持 `0.08`，无新增配置键。

### 静态兼容诊断

- `/healthz` 发布 daemon contract、Hermes plugin contract 与 Context Packet wire schema 的静态 major 证据；打包的
  Hermes 插件新增同源 `contract.json`，安装和升级继续逐字节验证完整副本。
- `hl-mem doctor` 从 9 项扩展为 12 项，分别报告运行中 daemon、已安装插件和 wire major 的兼容性。daemon 离线时
  只读探测为 WARN；在线但缺少证据或 major 不匹配时 fail-closed。诊断不写入状态，也不做版本动态协商。
- 这些结果作为 v0.29.1 不可逆兼容清理前的部署证据；v0.29.0 不删除旧契约或引入自动升级机制。

## v0.28.10（2026-08-18）

- 修复存量 legacy `claims_tags_fts` 投影在 `claims_tags_au` UPDATE 触发器中抛出 `SQL logic error`、导致
  `topic_tags_json` 更新回滚的问题。slot backfill 与重新提取脚本统一通过 storage 兼容 helper 写入标签；正常库仍直接
  执行原 UPDATE，仅在精确 legacy 错误下于同一事务内定向清理旧投影、临时卸载触发器、重放更新并补写新投影，且
  无论成功或失败都原样恢复触发器。
- 新增正常路径、legacy 恢复、恢复后触发器同步、清理失败回滚及重新提取入口回归；无新增配置、migration 或
  REST/MCP 业务契约变化。

## v0.28.9（2026-08-18）

### 版本化组级冲突

- 冲突持久化由两两 case 升级为 `(namespace, group_key, generation)` 下的单案多候选。检测仍保留新 Claim 对既有
  1:N 候选的完整竞争集合，但写库只 attach canonical candidate，不再展开为 N 个 pair；`left_claim_id` /
  `right_claim_id` 仅保留为兼容代表。Claim、evidence、candidate attach 与 case `revision + 1` 继续位于同一
  `BEGIN IMMEDIATE` 事务。
- 只有互斥 slot 创建组级冲突；非互斥 slot 仅保留 exact evidence 去重语义。组审核 REST 返回 generation、revision
  和完整候选集；`select_candidate` / `reject_candidate` 必须提交 `expected_revision`，陈旧请求以 409 拒绝且零变更。
  候选终态会自动关案，单一活跃候选可确定性收敛；超过默认 8 个候选的案保持人工处理。
- 自动维护改为持久 dirty queue + cursor：只处理当前活跃 generation，在每轮 case 数与时间预算内执行，并按 case
  失败退避、主动让出 writer。稳定 `manual_required` 案在没有候选/权威值/端点变化时不会再扫描或 UPDATE。
  generation 推进、候选压缩与冷热分层只保留扩展点，明确推迟到 v0.29。

### 存量修复与运维保留

- 新增 `hl-mem conflicts repair-invalid-groups`：默认只读预览；`--apply` 必须配合精确 `--expected-count`，在单一
  即时事务内修复旧摄入路径形成的非互斥 open group，并以审计记录收敛。生产执行前必须离线备份并停止 API、
  Worker 与其他写入者；5841 案形状 fixture 修复后仅保留 3 个合法 open case，且无 disputed orphan/dangling。
- Job、LLM span、已裁决 dedup pair、未标注 feedback 与 audit history 按各自窗口、逐表独立事务和批量上限清理；
  pending/running Job、pending dedup pair 及已标注 feedback 不删除。默认窗口为 succeeded Job 30 天、dead/failed
  Job 90 天、LLM span 30 天、已裁决 dedup pair 90 天、未注入/已注入未标注 feedback 7/90 天。
- 摄入期 pending dedup pair 增加默认 10000 条容量上限和可观测跳过计数。migration 046 一次性将低于 0.88
  摄入 floor 的历史 pending pair 标记为 `dismissed_below_floor`。清理后的终态 Job 幂等键可重新使用；超过窗口的
  未标注 feedback receipt 可能不再接受反馈，调用方不应把 receipt 当作永久标识。

### Migration 与兼容性

- migration 045 增加 group candidate、generation/revision、持久 review queue/cursor、唯一 open group 与端点/权威值
  dirty triggers；migration 046 增加运维清理索引及 dedup decision 扩展。项目版本升至 `0.28.9`，schema 为 46 个
  不可变、只向前执行的 SQL migration（001–046）。
- REST 新增两条组审核/裁决端点并同步 OpenAPI 快照；旧 CLI left/right 裁决路径继续兼容。MCP 业务 schema 不变。

## v0.28.8（2026-08-18）

- 修复 `DELETE /v1/memories/{id}` 在存量 legacy tag FTS5 投影无法执行删除命令时稳定返回 500：删除闭包仅对该精确 `SQL logic error` 在同一主库事务内临时卸载 `claims_tags_ad`、清理目标投影、删除 Claim 并原样恢复触发器；失败仍整笔回滚，未知 Claim 保持 404。
- 新增 `hl-mem correct <memory_id> --text "..." [--url URL]`，复用现有纠正端点并输出新 Claim ID、纠正事件 ID 与幂等创建状态；无新增配置键或 migration。

## v0.28.7（2026-08-18）

- Hermes provider 新增只读 `hl_mem_recall` 工具，采用宿主要求的裸 OpenAI function schema，仅接收必填 `query`、默认 5 的可选 `limit` 与可选 `intent`。调用复用现有 receipt-free retrieval bundle 通道和 `PrefetchCache.fetch_now()`：受同一熔断器保护、无重试、默认最长 8 秒，不 materialize exposure，也不改变被动注入链路；结果只保留 Claim，并以 `id | value | relevance` 紧凑 JSON 文本列表返回。
- 工具 description 增加中英双语的“何时用我”引导：部署、升级或运维前先查目标机器历史与已知状态；端口占用、版本不符、配置异常等环境意外出现时查已知事实；需要历史决策依据时主动查询；当前对话已注入记忆足够时不重复调用。
- `system_prompt_block()` 健康分支改为 4 行真实双层使用说明，明确被动注入与只读主动查询均可用及触发时机；degraded 分支改为 5 行真实状态，明确注入和工具可能不可用，禁止把空结果误判为“没有历史”。provider health 新增线程安全累计 `tool_calls`。
- 事件复盘：小宇宙目标机早在 8 月初已有 0.28.4 部署，但被动 prefetch 按用户原话未召回该历史，导致按新机安装、发现旧服务再回头升级的 20 分钟弯路；本版用“被动注入 + 主动工具 + 使用引导”补上可靠性鸿沟。
- 新增 Hermes 工具 schema、双语触发说明、默认 8 秒无重试调用、claim-only 紧凑结果、熔断 fail-open、无 delivery receipt、健康/降级提示与 `tool_calls` 计数回归。未改服务端检索、排序、rerank、dedup、冲突或 `audit_only` 语义；无新配置键、migration 或 REST/MCP 业务 schema 变化。

## v0.28.6（2026-08-18）

- 根治提取高峰期的 SQLite 写锁型召回延迟：REST recall、内部 retrieval bundle/context packet materialize 与 MCP recall 统一使用 `mode=ro`、`query_only=ON` 的独立连接池，在 WAL 下不再与 claims 写事务争抢写锁；启动期完成迁移，召回请求内保持零同步 SQLite 写。
- 新增有界的召回副作用 dispatcher：access count/last accessed、exposure feedback 与安全校验后的自动复活先非阻塞投递，再由单写线程写入现有 `deferred_tasks`；durable enqueue 遇短暂写锁会按配置有界退避重试，关闭时覆盖单项 busy/retry 预算且不会在线程存活时关闭其数据库。worker 高频消费并将业务变更与任务完成置于同一事务，单轮锁异常与主循环隔离；access 以 query ID、exposure 以 delivery feedback ID 集合生成幂等键，失败按既有重试预算收敛；召回 audit 与 query-expansion span 也移出请求线程。
- `/healthz.recall_side_effects` 继续按 access、feedback、audit 三类报告状态，计数明确为进程内 `submitted`、已写 durable task/审计表的 `persisted`、保留期内已完成最终业务写的 `completed`，并保留 `failures`/`last_error` 降级可见性。exposure 改为最终一致可见；紧随召回到达的 feedback receipt 或 injected 标记若尚找不到 exposure，会以幂等依赖任务持久登记，避免 404 时序竞态；终态召回副作用任务按 retention 有界清理。
- Hermes 按需召回上限由 2 秒调整为默认 8 秒，并新增 `hermes.on_demand_recall_timeout_seconds` TOML 配置；纯检索仍以提取写锁高峰下小于 2 秒为目标，8 秒用于 query expansion LLM 的长尾容错。
- 评估后不实现“客户端超时后把结果落入 prefetch cache”：Hermes 下一轮通常使用不同 query，完整缓存键无法复用；当前按需路径是同步 HTTP 调用，没有可注册的 in-flight future。为低概率重复 query 引入后台 future、取消、容量和关闭治理，收益不足以抵消复杂度。
- 新增只读连接拒写、请求内零 SQLite 写、写锁竞争下小于 2 秒、deferred access/exposure/复活幂等与失败重试、入队 busy 重试、worker 异常隔离、终态清理、即时 feedback/injected 依赖、异步 audit/span、8 秒默认值及 TOML 覆盖、两轮 Hermes 最终一致交付回归。未改检索、排序、rerank、dedup、冲突或 `audit_only` 语义；无新 migration，REST/MCP 业务 schema 不变。

## v0.28.5（2026-08-17）

- 修复 Hermes 注入链路的两轮时序缺陷：Hermes 在轮末以当轮文本 A 排队预取、轮初以新 query B 消费，而旧 provider 只接受包含 query hash 的完整缓存键命中，A≠B 时直接返回空。现在缓存 miss 会复用既有熔断器执行一次无重试、最长 2 秒的同步按需 recall；成功结果继续走 materialize、delivery receipt 与 injected 标记的原链路。
- 对齐 `RecallInput.query` 的 2000 字符边界，后台预取和按需召回都在发送前确定性截断，避免插件/daemon 版本漂移触发超长 query 422。daemon 响应增加 `X-HL-Mem-Version`，客户端对 422 记录去除 `input` 后的有界字段级 detail，以及 client/server 双端版本。
- 增强失败可见性：provider health 暴露累计 `prefetch_failures` 与 `injection_successes`；连续 3 次 prefetch 失败起升级 ERROR，成功 recall 会清零连续失败；熔断或超时仍 fail-open 返回空上下文并计数。degraded 或熔断状态下的 `system_prompt_block()` 改为明确降级，不再宣称记忆已经 injected。
- 新增真实 daemon 两轮回归：`queue(A) → prefetch(B≠A)` 必须发生 B 的按需 recall、调用 materialize，并在 lifecycle flush 后把 `retrieval_feedback.injected` 从 0 更新为 1；另覆盖 2000 字符契约、2 秒按需边界、熔断计数、三连败 ERROR/degraded、422 脱敏双版本与 daemon 版本响应头。
- 事件复盘：历史日志中 639 次 bundle 请求有 548 次成功、91 次 422，但成功 bundle 全属于轮末 A 的缓存项，下一轮 B 从未命中，因此 materialize 调用为 0、Hermes 注入导致的 injected 标记为 0；也就是说 548 次成功 bundle 从未进入真正的注入链路。422 仅是约 14% 的次因，主因是缓存生产/消费协议不匹配与失败状态长期不透明。
- 修复全量门禁暴露的 Windows 资源释放缺口：`TokenBudget` 构造与只读统计不再误把 SQLite context manager 当作连接关闭器，改为在 `finally` 中显式 close，避免评测临时目录退出时 `snapshot.budget.db` 因句柄占用触发 WinError 32。
- 本热修不新增配置键或数据库 migration，不改服务端检索、排序、dedup、冲突或 `audit_only` 语义；REST/MCP 业务 schema 与 Hermes hook 公开契约保持不变。

## v0.28.4（2026-08-17）

- 限制摄入期去重候选的持久归档下限：`IngestService` 写入新 Claim 时，仅当与既有语义候选的余弦相似度 ≥ `INGEST_DEDUP_PAIR_SIMILARITY_FLOOR`（0.88）才将灰色地带 pair 记入 `dedup_pairs` 审查队列。此前任何相似度都会被记录，导致低价值（<0.88）候选在每日 `ORDER BY similarity DESC` 审查队列尾部无限累积（实测单日可新增上百条）。
- 行为边界：仅影响写入期 `_insert_pending_dedup_pair` 记档路径；每日跨主体去重 worker、召回折叠（仅认 `judge_reason='deterministic_near_copy_v1'` 的 equivalent）、LLM judge 与 `audit_only` 默认行为均不变。
- 新增测试：相似度恰为 0.88 正常记档、0.87 不记档但 Claim 正常入库。
- 本热修不新增配置键或 migration，REST/MCP 业务 schema 不变。

## v0.28.3（2026-08-16）

- 修复 Hermes 根目录探测对 `HERMES_HOME/hermes-agent` 子目录的错误偏好；用户插件路径统一为 `HERMES_HOME/plugins/hl_mem`，完整源码 checkout 与仅含 `.venv` 的 agent 子目录都不再导致 `doctor` 误报路径错误。
- 修复 v0.28.2 新增的 4 个 CLI 测试在无 `hl_mem.toml` 环境中的封闭性；测试显式注入安全的测试配置，不再依赖仓库工作目录中恰好存在的 gitignored 配置文件。
- 本热修不新增配置键或 migration，REST/MCP 业务 schema 不变。

## v0.28.2（2026-08-16）

- 统一 Hermes 插件部署链：Hermes 根目录探测逻辑随包发布，`doctor` 可区分路径正确且副本一致、安装路径错误与插件副本漂移；新增幂等的 `hl-mem hermes install/upgrade`，一致副本保持 no-op，升级前备份既有插件文件。
- 增加悬空冲突自愈：maintenance worker 在 `auto_resolve_conflicts` 前自动清理终态且双侧 Claim 均缺失的 `conflict_cases`，每轮最多 100 条并写入 `audit_log`；`/healthz` 新增 `conflict_dangling`，分别报告 `terminal_both_missing`、`terminal_one_side` 与 `open_dangling`；新增 `hl-mem conflicts repair-dangling [--apply]`，默认只读 dry-run，显式 `--apply` 才执行安全子集删除。
- 新增 `HLMemProvider.unavailable_reason()`：当 `hermes.enabled=false` 时返回包含配置位置与启用方式的可操作修复提示，已启用时保持空字符串。
- 冲突裁决支持理由贯通：`ResolutionService.resolve(..., rationale=...)` 可接收人工裁决理由并传播到同组关闭 case，CLI `hl-mem conflicts resolve ... --rationale` 同步开放该参数。
- 本补丁不新增配置键或 migration，REST/MCP 业务 schema 不变。

## v0.28.1（2026-08-16）

- 修复 `config.port` 的裸 substring 误判：英文 `port` 现在必须是完整 token，且端口提示词必须同时有 1–65535 的数值或合法 `host:port` 形态；`importance`、`import`、`transport` 与 `importing` 不再进入端口互斥槽，模型直出的无来源 `config.port` 也会确定性降级。
- 修复 `ResolutionService.reject` 只关闭 case 却遗留不可见 disputed Claim 的事务缺口：非互斥 pair 的非终态双方恢复 active，同互斥组在修正 slot/qualifier 前 fail-loud，提交前断言不存在无 open case 的 disputed Claim，违例整笔回滚。
- 事件复盘：2026-08-15 的开发对话因 `port` 子串误判和旧 reject 语义在单日积压 250 个 `manual_required` case；存量 18 条孤儿已恢复为 15 条 active 与 3 条指向 active 后继的 exact-duplicate superseded，当前孤儿与 open case 均为 0。
- 本热修不新增配置键或 migration，REST/MCP 业务 schema 不变。

## v0.28.0（2026-08-16）

### Breaking / Behavior changes

- 显式 forget 与 archived bulk cleanup 现在执行同一物理删除闭包，并在删除前写独立 tombstone sidecar；账本写入失败、身份错配或删除语义不明确时整笔拒绝，不再允许只改 Claim 状态后留下可复活引用。
- backup manifest 升为格式 v2 并绑定 tombstone ledger identity。restore 必须先证明账本身份并重放删除历史，旧 v1 manifest、缺失账本或 ID 错配会 fail-closed；这是防止旧备份复活已删内容所必需的兼容性收紧。
- 默认配置、REST 与 MCP 业务 schema 没有新增破坏性变化。数据库增加 migration 043–044，升级只向前执行；恢复旧备份前应使用 v0.28 重新生成带账本绑定的备份。

### 删除完整性

- 新增独立、版本化的 `TombstoneLedger` SQLite sidecar。记录只包含删除身份集合 hash、闭包范围和账本元数据，不保存敏感正文；幂等重放使用显式查重后 `INSERT`，禁止 `INSERT OR IGNORE` 掩盖账本写入异常，账本失败会在主库物理删除前中止。
- 新增窄版 `DeletionService`，供用户 forget 与 archived cleanup 共用单一删除闭包：删除 Claim、专属 evidence、关系两端、冲突/派生/supersede 引用及失去引用的 Event。active/archived/superseded × 共享 Event × 关系两端 × ledger 缺失的 P0 矩阵 15/15 受测试保护；candidate、disputed、expired 和 open-manual 语义统一拒绝并报告，retracted 重放保持幂等。
- migration 043 将主库绑定到 ledger identity，并保存删除应用水位。backup manifest v2 同时封存数据库、账本 checksum 与 identity；restore 在新库对外可见前验证绑定并重放 tombstone，沿用既有原子替换。中途只重放一部分可幂等续跑，缺账本、错配账本和无法证明删除历史的旧备份均拒绝恢复。

### 关系时间与完整性巡检

- migration 044 为 `memory_relations` 增加 `valid_from` / `valid_to`。新边从创建时起有效；Claim 转入 retracted、superseded 或 expired 时由既有终态转移路径同步关闭相关边。关系扩展每跳同时检查边的有效时间及两端 Claim 的 namespace、状态和双时间可见性，存量边以 `created_at` 回填起点且保持开放，迁移后行为兼容。
- integrity audit 增加 evidence link、relation endpoint、derivation 与 supersede 引用的 dangling 分类计数，每类只给最多 5 条有界样本且不自动删除；删除闭包回归要求三入口完成后 dangling 为 0。

### 维护正确性与结构收敛

- 提取 Job 在 `complete` 前复用 `stage` 与 `progress_detail_json` 持久化逐窗口及累计 written claim count，使“Job 最终失败但此前已写入”可诊断，不新增结果总线或数据库列。
- `canonical_slot` required qualifier 只接受显式 evidence，禁止从泛化 subject 伪造 `choice.model.task`。在带 hash 的 v0.27 旧缓存上配对验证，16 个既有误配全部修复、0 个新增误配，另 4 个正确样本保持不变；验收未使用新 prompt 缓存。
- `ExperienceService` 从继承仓储改为组合委托；worker 仅抽出 job handler、registry 与维护调度边界，保留旧 `dispatch_job` 导入兼容，不引入通用 handler 框架。

### 提取关系语义实验终局

- 第一轮 compact RAO prompt 虽把来源有界率从 0 提升到 88.3%，但 exact RAO 仅 8%，普通 anchors 从 92.9% 降到 85.7%，非关系 claim yield 降至 68.8%，关系覆盖仅 5/50；按预注册纪律回滚生产改动。
- 第二轮保持主提取不动，尝试 source-first 独立关系注解；两次 runner 权威 ID/evidence 接线缺陷均作废封存并通过 3-call pilot 门禁后重跑。最终来源边界 precision 100%、接受率 90.8%，但 exact RAO 仅 10%、packet RAO 12%、entity coverage 与基线同为 34.7%，可扩展关系边仍为 0，C0/C4 smoke 全等，未获得 sealed v3 资格。
- 配对诊断显示关系 proposal 从 374 降到 322、实际 applied 从 207 降到 16：让同一模型 pass 同时完成候选配对与精确语义注解产生了明显“任务竞争”。方法论结论是 schema 可写与来源安全不能替代端到端可用性，新增任务必须同时验证原任务产出保持；主菜 A 最终不产品化，prompt/schema/窄表/runner 实验分支均删除。
- 删除 dormant C1–C5/f4 臂及只为它们存在的 runner/测试，并删除已判死的 source-first 实验分支。保留生产 relation expansion、RAO 渲染、answer-entity scorer/gold、sealed 隔离纪律、关系建图覆盖/packet 差异 smoke 及通用 pilot/权威 ID 绑定门禁。

### 文档与发布

- README 明确默认 `sqlite_scan` 适用于约 10 万条 Claim 以内，超过该量级应评估并显式切换 `sqlite_vec`；API 文档显著声明 namespace 不是安全边界、默认本地监听不可直接暴露公网；兼容性文档补充 OpenAPI/MCP 独立快照的共同审查规则。
- 项目版本提升至 `0.28.0`；SQL schema 为 44 个不可变、只向前执行的 migration（001–044），backup manifest 格式为 v2。

## v0.27.1（2026-08-15）

- 修复 Context Packet RAO fallback 可能将未出现在公开文本中的 claim value 投影到 relation 行的信息边界问题，并在缺少本地私有评测语料时跳过相关 CI smoke，消除跨环境失败。

## v0.27.0（2026-08-15）

[GitHub Release](https://github.com/lohr13/hl_mem/releases/tag/v0.27.0) · [PyPI](https://pypi.org/project/hl-mem/0.27.0/)

### Breaking / Behavior changes

- `recall.resurrection_mode` 的静态默认值从 `off` 切换为 `auto`。主召回为空或 answerability 低时，系统会执行有界的 archived-only FTS；候选必须重新通过有效时间、来源完整性、冲突竞争和高词项覆盖门禁，且只允许 `archived → active`。A/B 中得到 2 次正确复活、0 次误伤，端到端 p95 为 12.7ms。
- `decay.model` 的静态默认值从 `legacy_linear` 切换为 `activation_halflife`。日常衰减改为按 scope 半衰期更新独立 activation，confidence 不再因时间流逝而改变，只表达证据、冲突、修正与验证强度。离线三臂回放中 activation 臂对 identity 为 0 误杀并满足 confidence 语义分离；`confidence_halflife` 对必须保留样本的误归档率为 66.7%，因此淘汰。
- 旧 `hl_mem.toml` 未声明以上两键时会采用 v0.27 新默认。要保持 v0.26 行为，必须显式配置：

```toml
[recall]
resurrection_mode = "off"

[decay]
model = "legacy_linear"
```

### 冲突治理

- ingest 废除 `existing[0]` 单代表语义：新 claim 落库前与同一非终态 conflict group 的全部相关成员求一致结论；只有全组一致 entail 或形成明确 state-change 链才自动收敛，混合或不确定结论会把整组转入 disputed 并建立 `manual_required` case。
- 新增应用层组级 `ResolutionService`。互斥 conflict group 的 `coexist` 会硬拒绝并提示先修正 slot/qualifier 使 claim 脱离同一 conflict key；`keep_left/right` 在单事务内原子选出组级赢家、收敛其他非终态成员并一致关闭重叠 open cases，commit 前断言组内 active 不超过 1。
- SQL migration 041 增加触发器级激活保护，覆盖未来所有 INSERT/UPDATE 路径，阻止互斥 slot 的同组第二个 active；触发器只防护新写入，不自动改写存量裁决。migration 042 增加 activation、activation_base 与低水位跟踪字段，保留只向前迁移约束。
- repair、resolve、ingest、audit 的典型排列与幂等重跑纳入生命周期闭环回归；maintenance/resolve 在提交前检查组内 active≤1 和无 dangling 冲突引用，audit 额外报告历史 terminal coexist 与当前互斥组不一致，违例整笔回滚。

### 召回与表示

- Context Packet 对有关系语义的 compact claim 结构化渲染 `relation: role → action → object`，空 RAO 不输出额外行；10 claims / 2,000 tokens 总预算不变。52/52 design/dev case 已验证 RAO 从 Claim 经打包到 reader 输入完整传输，REST 与 MCP 继续复用同一 packet 表示。
- 增加受控归档复活冷路径：只查询 archived，永不复活 retracted、superseded 或 expired；复活前重检 valid time、来源完整性和组内 active 竞争者，高阈值命中后重新 embedding、原子切换状态并写 resurrection audit，复活不会提高 confidence。经 A/B 裁决，默认设为 `auto`。
- 增加 activation 半衰期生命周期模型，命中只刷新 `last_accessed_at`；temporal/permanent/identity 默认半衰期为 45/90/365 天，低 activation 持续越界后才归档。三臂实现仍保留 `legacy_linear` 和实验用 `confidence_halflife`，默认切换为 `activation_halflife`，让 confidence 归位为纯证据强度。

### 评测与实验

- 中文 40-case E2E manifest 升级为 gold schema v3，增加 NFC 精确的 `answer_entities`、role/action/object 链、`forbidden_entities` 与 `forbidden_assertions`；冻结 `answer-entity-packet-v1` scorer，以 Top-5 seed 扩展后的最终 packet 做 macro-case entity coverage，no-answer case 不计实体覆盖，既有 anchors 判分保持并行不变。
- 建立六类均衡的关系链 design/dev 与仓库外 sealed holdout，并冻结 C0-C5 + f4 协议、关系覆盖门禁和同包 smoke。C 系列三轮实验沉淀 5 项生产修复，但 C4 未通过 sealed 产品门禁，保持休眠待重新预注册验证；reader 对照揭示强 reader 对 hard relation 有收益，但不足以支持默认切换，生产 reader 维持现状。
- 六因子权重网格在 112-case 隔离检索与 40-case E2E 上未找到 ≥2pp 的稳定 headroom，当前权重未证明值得在线 bandit，v0.28 bandit 硬门判定不通过。
- 通用 bootstrap CI 扩展为按 persona/trajectory 聚类的 paired cluster bootstrap；固定 seed、2,000 次重采样和 95% CI，A/B 对配对差值重采样，并输出版本化报告 schema。

### 其他生产修复

- 关系发现拒绝模型虚构或未配置的 endpoint；API 失败、空结果和无效结构的降级输出契约冻结，禁止把失败伪装成有效关系边。
- 关系缓存构建后必须通过有边 case 覆盖门禁，并在跑批前验证 C0/C4 packet 非全等；sealed v1 因关系未物化而产生的静默 no-op 不再可进入实验。
- 消除并发启动时 SQL migration 登记的竞态，保证多进程同时升级不会因重复登记破坏启动。
- C 系列和表示验证 runner 增加防重复跑批护栏；sealed、scorer、manifest 或运行身份不匹配时明确拒绝复用产物。

### 发布

- 项目版本提升至 `0.27.0`；SQL schema 为 42 个不可变、只向前执行的 migration（001-042）。除上述明确列出的默认行为变化外，REST/MCP 业务 schema 保持兼容；OpenAPI 服务版本同步为 0.27.0。

## v0.26.0（2026-08-14）

[GitHub Release](https://github.com/lohr13/hl_mem/releases/tag/v0.26.0) · [PyPI](https://pypi.org/project/hl-mem/0.26.0/)

### 提取评测 v2

- 冻结原子事实 gold 契约，显式标注来源 Event、角色—动作—对象锚点、专名集合、speaker、canonical subject、禁止传播项和 modality 负例；新增关系方向、逐 claim entity 字段覆盖、链条自包含与多次采样 majority 指标。
- 新增 24 条公开合成 hard case（entities/prompt 各 12 条）及 40 组平衡 dedup pair；困难负例覆盖共享实体下的值、方向、关系和 modality 差异，并单独报告不安全的 false reuse。
- 预测必须显式携带来源 Event 索引；关系方向 gold 必须按序覆盖全部角色、动作和对象，专名字段仅允许 Unicode NFC 等价，避免宽松归一化掩盖实体改写。
- E2E scorer 升级为 `deterministic-rubric-v2`：official answer-anchor 仍要求全量命中，仅对人工审核的开放描述题增加概念组 AND、同义表达 OR 的 `accepted_rubrics`；新增枚举完整性、简短语义答案与“推荐≠执行”合成负例。
- 修正一个把受邀参与者误标为共同组织者的 PerLTQA gold，替换为同一来源中角色一致的问题；历史 `90%` 明确记录为 v0.25.3 离线重评分数字，代码回归改以同一提取缓存的版本 A/B 等价比较。

### Abstention 语义统一

- `no_evidence` 统一表示没有候选的 hard abstention，benchmark reader 直接返回信息不足且不调用 QA 模型；`low_confidence` 统一表示仍有候选的 soft abstention 元数据，在 observe 语义下继续调用 QA 并随答案保留 soft 标签。REST/Context Packet 使用同一冻结枚举。
- 两个召回评测入口都把 hard 与 soft 的并集计为总体 no-answer 预测，并分别报告 hard/soft precision、recall 与 F1；固定快照 recall 报告 schema 升为 v3，避免旧口径 baseline 被静默比较。

### Active claim 不变量收敛

- 摄入发现互斥 conflict key 下已有多个 active claim 时，不再只处理排序第一条；整组与新 claim 一并转入 disputed，并逐对建立 `manual_required` 审核记录。
- reclassify 在改写 canonical slot/conflict key 前执行原子碰撞守卫，冲突 mutation 明确计为 guarded，不产生新的互斥双 active。
- 新增只读 `audit` 与显式 `repair --dry-run/--apply` 维护工具：精确重复确定性复用并汇总 Event evidence，语义不确定的互斥组全部进入 disputed，冲突对创建或重开为手工审核；既有终态裁决保持原样并显式计数，完整 apply 在单一写事务中执行。

### 未纳入的实验改动

- 不引入 entities hybrid：32 case × 2 arm × 3 次真实提取中，B 臂 entity F1 虽从 0.51 提升到 0.93，但 precision `95.70% < 98%`、recall `90.87% < 95%`、exact-set `82.89% < 90%`，token 增加 `5.38% > 3%`，出现 1 条 control 回退，且只纠正 `1/20` 个 false merge，冻结门禁 9 项失败 6 项。
- 不修改专有名词保真 prompt：24 case × 2 arm × 3 次真实提取中，value exact-surface recall 为 `95.18% < 98%`，hard slice 改善 `0pp < 8pp`，canonical subject 正确率 `69.23% < 100%`，完整通过 `53/72 < 69/72`，token 增加 `3.00% > 2%`；多人引用与昵称并存的 gold—模型规范化偏差保留为评测盲区。
- 不把 abstention `enforce` 设为默认：112 case A/B 中 no-answer F1、precision、recall 分别下降 `3.28/2.12/7.14pp`，answerable gold recall@5 下降 `3.57pp > 1pp`，关键 MemDaily slice 最大下降 `14.29pp`；默认保持 observe，soft 标签只记录不拦截 reader。

### 已知限制

- “高盛债券”和“大宗商品”两条投资关系题仍需要 reader 跨叶子 claim 聚合角色与标的；当前 event-level R@5 会在答案实体未进入 Top-5 时产生假阳性。该问题不以宽松作答 prompt 修补，留给下一版本做 evidence-group context 与 modality 负例约束的受控实验。
- 多人引用与昵称并存样例的 canonical subject 正确率仍受 gold 与模型规范化习惯差异影响；不据此改写生产 subject 规范。

### 发布

- 项目版本提升至 `0.26.0`；SQL migration 数保持 40，REST/MCP 公共业务字段无新增破坏性变更。

## v0.25.3（2026-08-14）

### 提取实体保真

- 双语 LLM 提取 prompt 明确要求逐字保留具体人名、地名、组织名、产品名和项目名；关系角色与摘要只能作为附加信息，不得替代具体实体。
- 跨行或结构化记录必须联合读取姓名、描述和关系字段，并新增中英文命名人物示例。
- PerLTQA adapter 升级为 v2，社会关系记忆不再只传递 `Description`，同时保留 `Supporting Characters` 与 `Relationship`，修复姓名在进入提取器前已丢失的问题。
- 项目版本提升至 `0.25.3`；提取器指纹更新为 `llm-v2+e2d8f433b71c`。

## v0.25.2（2026-08-13）

### Preference 召回简化

- 移除 preference intent 的硬保留 slot 机制（`_preference_first`），该函数现在只执行纯截断；偏好排序完全由上游 `_filter_and_score` 中的 `preference_recency_boost` 分数因子处理。
- 移除 reranker 在 PREFERENCE intent 下对遗漏 preference claim 的追加逻辑；reranker 返回的顺序不再被 preference intent 改写。
- 统一 `RecallConfig.preference_recency_boost` 与 `RecallContext.preference_boost` 的默认值从 1.0 降为 0.12，与 `Settings` 中的 `recall.preference_recency_boost` 一致；preference score boost 现在是轻量辅助因子而非主导排序。
- 新增两个测试保护 reranker-preference 交互不变式：(a) reranker 顺序不被 preference intent 改写，(b) reranker 省略的 preference claim 不被重新追加。

### Benchmark runner 跨 provider 支持

- LongMemEval benchmark runner 新增 `HL_MEM_EVAL_QA_BASE_URL` 和 `HL_MEM_EVAL_QA_API_KEY` 环境变量，允许 QA 阶段使用与提取器不同的 provider 和 endpoint。

## v0.25.1（2026-08-12）

[GitHub Release](https://github.com/lohr13/hl_mem/releases/tag/v0.25.1) · [PyPI](https://pypi.org/project/hl-mem/0.25.1/)

### 偏好召回与 LongMemEval 口径对齐

- LongMemEval reader evidence 不再对 preference 题执行“生产 Top-12 → 按数值 score 二次重排 → 只保留 1 条偏好”的评测专用裁剪；所有题型直接请求并保留 `RecallService` 返回的生产 Top-10 顺序，避免评测层覆盖生产 reranker 与 preference-first 结果。
- 生产自动意图路由新增零调用成本的中英文个性化推荐规则，覆盖 `recommend`、`suggest`、第一人称 `like`、推荐/建议选择、适合我和“想去哪”等高精度表达；历史语义继续优先，显式 `intent` 继续覆盖自动判断，歧义性的 `likely`、`What is it like`、单纯出行计划和过程咨询不触发 preference。
- preference intent 的既有最多 3 个保留位改为优先选择 `subject_entity_id` 确定性归一为 `user` 的偏好，其他主体仅在不足时按原顺序补位；不新增过滤、权重、通道或配置，不改变其他 intent。
- 使用现有 case DB 对 8 条 preference 与 4 条非重叠风险 case 做定向 QA 重放：preference 从冻结 v0.25.0 基线的 7/8 提升为 8/8，`0edc2aef` 恢复通用酒店偏好并判对；`0a995998`、`gpt4_59149c77` 仍错，`a82c026e`、`eac54add` 仍对。该 12 条定向结果不能替代完整 holdout50 基线。

### LongMemEval 全上下文对照

- 新增独立的 `--mode full-context`：按时间顺序把每个 case 的全部原始 session 无截断交给现有 reader，绕过提取、case DB、维护、检索与 reranker，作为 `hl_mem+reader` 的上限对照；默认输出使用 `longmemeval_fullcontext_*` 身份。
- 控制报告固定 `control: full-context`、数据集 SHA-256、渲染协议、模型和预算，检索指标显式标为不适用；同时记录 reader/judge 的 input/output/reasoning token、延迟和按费率快照估算的成本。长上下文 reader timeout 独立放宽为 300 秒，thinking budget 仍为 2048，judge 继续关闭 thinking。

### LongMemEval native RAG 对照

- 新增独立的 `--mode native-rag`：将原始 session 渲染为带时间戳的消息块，使用现有 embedder 做 dense Top-10 检索，再交给与主评测相同的 reader/judge；该路径不提取 Claim，用于衡量结构化记忆相对普通向量 RAG 的价值。
- 对照报告固定 `control: native-rag`、`raw-session-dense-rag-v1`、数据集 SHA-256、Top-K 与模型身份，并分开记录索引/查询 embedding 成本、reader/judge token、延迟和估算费用。

## v0.25.0（2026-08-12）

[GitHub Release](https://github.com/lohr13/hl_mem/releases/tag/v0.25.0) · [PyPI](https://pypi.org/project/hl-mem/0.25.0/)

### 提取架构最终版

- Hermes `sync_turn` 通过新增的原子批量端点一次写入 user/assistant Event；原有单 Event API 保持兼容。
- Worker 对同 namespace/session 的消息执行默认最多 5 Event、最多等待 120 秒的有界微批；显式记忆、非消息及无 session Event 仍实时直达。
- compact extraction 增加 `source_event_indices`，Claim 可链接窗口内一个或多个真实来源 Event；speaker、turn、发生时间与证据映射不再因合并提取丢失。
- LongMemEval 改为排队 Event 后驱动生产 Worker，生产与 benchmark 不再维护两条提取路径。
- 新增批量原子性、窗口边界、会话隔离、speaker/turn prompt、来源校验、多 Event evidence 与 benchmark 对齐回归测试。
- Migration 039 为 Event 增加 nullable `metadata_json`，仅保存 turn 等非正文 locator；该提取架构新增配置仅为 `extraction.batch_max_events` 与 `extraction.batch_max_wait_seconds`。

### 双语提取语义与上限审计

- `b143daf` 对齐中英文原子性语义：英文补齐复合事实正反例，两种语言都明确要求单独保留已发生/已确认的关系动作、一次性事件，以及枚举中每个可独立回答项的数量和单位；仅当原文明示总数时才提取总数。
- 结构化响应恰好达到 20 条 schema 上限时新增 `extract/possible_under_extraction/claim_limit_reached` 审计告警，不增加调用、不放宽 schema，也不改变生产阈值。
- 当前 `PROMPT_HASH` 为 `86c522e45f92`，提取器身份为 `llm-v2+86c522e45f92`；此前的 `fff10cabee53` 缓存按既有 fingerprint 规则失效。固定小样本 A/B 中三个英文关系/枚举样本保持正确，中文关系+枚举组合样本仍为空，因此本变更不能宣称已消除所有关系漏提取。

### 发布前兼容性与可靠性收口

- `auto` 英文 FTS 查询改为 raw 全词 AND 与 stem 全词 AND 两个分支，既能命中 v0.24.0 存量 raw-only 索引，也保持新索引的跨词形召回；Claim、Event 和启动重建路径统一使用当前 Database 的 `recall.fts_language`。
- `/v1/events/batch` 以微秒级内部 `recorded_at` 保留请求数组顺序，避免四条交替 user/assistant Event 被角色排序打乱；整个批次与 extraction job 仍在同一事务中提交。
- JSONL Event 归档纳入 `metadata_json` 的导入、导出和同 ID 冲突判定；Worker 在长任务期间续租全部 job，并在终态更新未持有 lease 时返回 `lease_lost`，不再报告伪成功。
- compact/legacy extraction 的 `source_event_indices` 上限与可配置微批上限统一为 32；结构化输出的 20 claims 上限及生产去重/冲突阈值未改变。
- `extract_event` 仅在 HTTP 429 耗尽普通 job 重试后写入通用 deferred task，由维护循环按 1/4/12 小时重放三次；成功即收敛，非 429 维持原 job 语义，多次 429 后放弃。维护会用持久化的精确 HTTP 429 错误前缀回收升级前已 dead 且仍无 evidence 的 Event，不把 token budget/500 等历史失败混入。pending Event 在重试终态前受 retention 保护。
- 跨 subject `DedupJudge` 明确输出示例并校验全部协议字段；空 decision、未知枚举、非 JSON 或无效 confidence/reason 降级为低置信 `uncertain`，不再让整轮日任务因单个格式错误进入 dead。provider/transport 调用异常仍沿用普通 job 重试。
- Hermes Episode goal 改用本轮 `sync_turn(content)`，空白时确定性回退并在 provider 侧截断到 5000 字符；Trace 只映射最后一条 user 消息之后的工具轨迹，避免 compaction summary 和累计历史触发 422 或污染 Episode。422 日志记录字段长度与移除 `input` 后的有界响应诊断。

### 重复与矛盾双层治理

- 摄入在既有 subject、predicate、canonical slot/attribute、qualifiers 与有效时间守卫之后，新增保守的近复述复用：仅当词法近似、dense cosine 与数字、日期、相对时段、路径、否定词、专名及实体 mention 顺序同时一致时，复用已有 Claim 并追加 evidence；未增加提取 prompt 或 LLM 调用。
- Worker 维护循环每轮最多读取 `dedup.scan_limit` 条现有 pending `dedup_pairs`，用同一确定性规则标记安全 pair 为 `equivalent`；未审候选优先，不能确证的 pair 保持 pending 并按 `reviewed_at` 轮转。该路径不删除、不 supersede、不改写 Claim，也不会把规则确认结果送入旧的物理自动合并路径。
- 召回在既有候选窗口内折叠已确认且再次通过安全门的等价组；对尚未形成 pair 的跨 subject 近复述，也只在同一有界窗口内用相同安全门动态兜底。跨 subject 仅接受文本可证明的 `user ↔ user's <entity>` 投影，protected atoms 的顺序和次数必须相同。两条路径都保留最高分代表项、公开 `equivalent_claim_ids`、在 trace 标记 `equivalent_folded`，并去重汇总组内 evidence。没有新增数据库表、排序通道、权重或阈值；不同数字、限定条件、时间区间和专名继续分开返回。
- 标准 LongMemEval case 在 fresh ingest 或 `--skip-ingest` cache 打开后、召回前执行一次 `deterministic-dedup-conflicts-v1` 轻量维护，只包含确定性 dedup review 与 `auto_resolve_conflicts`，并把统计写入 case 结果。embedding config-compare 保持隔离，不复用生产 embedding 产生的 pair。
- 上述双层治理实现链截至 `1e8e1fd`：中文邻接英文实体、数字和版本也纳入 protected atoms；未新增 LLM pair judge、全库两两扫描或物理删除路径。

### LongMemEval 评测工具链与基线

- benchmark reader 全题型开启 thinking，并为 reasoning 与正文分别设置受控预算；judge 与 extractor 保持关闭 thinking。reader 使用 LongMemEval 官方 Top-10 evidence 口径，并保留重复事实只计一次、count/更新题规则、相邻 turn excerpt 与 assistant ordinal 回退。
- 超量 claims 自适应拆分、内容审查拒绝事件隔离、HTTP 非 2xx 脱敏诊断与 cache/resume 身份已收口；结果文件新增每条候选的 dense 原始分、reranker 原始分、通道来源、最终顺序和完整 `search_trace`。
- 冻结 holdout50 基线为 **40/50（80%）**：`deepseek-v4-flash-0731`、reader thinking、Top-10、自有 judge。temporal gate 排除 2 道问题时点无有效答案的诊断口径为 **40/48（83.3%）**，只用于误差分析，不替代官方 50 题分母。
- 已知限制：内容审查隔离跳过 2 个输入 Event；剩余错误集中在 multi-session 聚合、temporal 计算和少量 single-session 细粒度限定词。benchmark reader 窗口与生产 recall/context packing 是两套契约，不应把 80% 直接解释为生产端到端准确率。
- 上述重复治理尚未重跑完整 holdout50；40/50 仍是冻结且可比较的最近全量基线，不能把局部 aquarium Top-10 变化外推为新的总体分数。
- `eeda8a6d` 临时数据库局部重放中，20-gallon 近复述被折叠后释放一个 Top-10 位置，第二缸的 betta 线索进入窗口，reader/judge 从 16/错误恢复为 17/正确。该单例只验证机制，不构成新基线。

### 升级与发布说明

- v0.24.1/v0.24.2 是仓库内过渡版本，从未建立 Git tag；`1e8e1fd` 与 `b143daf` 均已纳入 v0.25.0，该版本是 v0.24.0 之后的首个正式对外发布。
- 升级会顺序执行 migration 038 的 persona subject Python 数据回写、migration 039 的 nullable Event metadata 列与 migration 040 的 deferred task 队列。大库应先备份、停止其他写入者并预留 migration 038 全表扫描和写锁时间；数据库 migration 仅向前，不支持降级。
- 本版本共 40 个不可变 SQL migration（001-040）；REST 单 Event API、依赖集合及生产阈值 `0.82/0.92/0.95` 保持不变。

## v0.24.2（2026-08-09）

### LongMemEval 全量评测前修复

- 相对时间解析接受规范的逗号分组数字并要求完整 token 边界；超出 `datetime` 年份范围的单个 match 会被跳过，数千年前的历史/叙事年龄不再强制映射为对话相对时间，也不会中断整条 Claim 或整道 case。
- preference intent 最多只预留前三个偏好结果，余下位置恢复全局/reranker 顺序，让相关事实与计划继续进入 Top-K。
- assistant 提取由“通用知识一律跳过”调整为只保留可再次引用的 durable span，包括表格行、编号项、脚本设定、联系人和工具映射；仍拒绝 generic chatter 与无边界的整段回答。
- LongMemEval session 改为逐 turn Event 写入，保留真实 `source_role`/`actor_type`、稳定 session 和 turn/span locator；subject 与 speaker 不再混为一谈。现有事件 schema 已能表达这些字段，无新增 migration；旧 benchmark cache 由 event-model/prompt fingerprint 拒绝复用。
- reader 按题型分流：事实题维持闭卷确定性推理，偏好推荐题可将已知偏好作为约束合成答案；temporal 题先选择问题时点有效的最新基准，再应用星期条件或相对偏移，历史问题不得借用更晚的当前值。

### 证据召回、指标与运行恢复

- assistant/明确引用先前列表、表格或脚本的问题新增窄版原始事件第二路：在 case namespace 内用 OR 语义检索 Top-1 session/assistant turn，与 claim 证据按 Event ID 去重，并进入既有 1,200-token evidence 配额；没有新增全量 turn-vector schema。
- extraction coverage 改为覆盖所有成功 case；claim retrieval 与 session retrieval 各用独立 eligibility 分母，报告同时给出 R@K eligible numerator/denominator，避免 claim 标注缺失污染 session 指标。
- runner 已确认逐 case 原子写入报告、`Retry-After`/退避与熔断行为；`--resume` 现在会保留成功及非 429 结果，但在配额窗口恢复后自动重跑 `http_429`/`quota` case。评测文档补充错峰、单进程/低并发和同参数恢复指引。

### DeepSeek V4 Flash 与 holdout 诊断加固（2026-08-10）

- `bdd8391` 新增百炼 OpenAI-compatible `deepseek-v4-flash` benchmark 配置与任意 QA model override；显式控制 extractor/reader/judge 的 thinking 参数，并把 endpoint、effective provider、extractor payload、QA/query-expansion model 纳入 manifest/resume identity，避免跨模型或跨请求配置复用缓存。
- `43bb3ae` 修复 holdout50 诊断链：超长 conversation turn 可拆为有界 JSON 单元，windowed reader 按 session 与数值 turn index 重建相邻证据；同时加入 temporal gate 歧义标记、脱敏 HTTP 错误诊断和相邻 turn Claim 复述膨胀指标。
- `aaf4440` 将 hard split 加固为可无损还原的 `semantic-turn-fragments-v1`，并把 fragment/reader 协议与 chunk target/overlap 纳入 cache、resume 和 merge 身份；诊断统一做结构化敏感字段脱敏，Claim 密度改按物理行与缓存实值计算，legacy resume 对不可得指标显式留空，temporal gate 按问题时区比较同日边界。
- CI 收口恢复 `benchmark_extraction.sanitize_http_response_body` 兼容导出，并让相邻复述候选仅在相同的非终态 lifecycle status 内比较，避免 candidate/disputed 之间交叉计数。

### 仓库治理

- `.env.dsv4` 与 `/evaluation/datasets/` 纳入忽略规则；本地评测数据和未跟踪 proposal 移出仓库，`evaluation/results/dsv4flash_reader_investigation.md` 停止 Git 跟踪但保留本地副本。`/evaluation/results/` 的既有忽略规则保持不变。

### 兼容与发布

- SQL schema 仍为 38 个不可变 migration（001-038），生产阈值 `0.82/0.92/0.95` 未调整，依赖集合未变化。
- 版本更新为 v0.24.2；新增/更新的 LongMemEval 与时间解析回归由 GitHub Actions 全量测试验证。

## v0.24.1（2026-08-09）

### Migration 038 完整性

- 补全 persona subject 数据迁移：同步规范化 `entities_json` 中的明确 persona，重算 fact/conflict/index 派生身份，并清空受影响 Claim 的 dense/sparse embedding 及模型、维度元数据。
- 受影响 Claim 显式写入 `claim_vector_dirty`，sqlite-vec 投影不会继续保留旧 subject/index 语义；升级后必须先执行 `hlmem backfill-index-text --mode <当前 index.text_mode> --dry-run`，确认范围后再去掉 `--dry-run` 完成 embedding 回填。
- 迁移结束时报告同 namespace 的 active 精确重复组与 conflict-key 冲突组，不自动合并或删除 Claim。
- 数据迁移版本升级为 `038_data_subject_canonicalization_v2`，已运行早期 v1 回填的数据库也会重新失效相关派生数据。

### 摄入、评测与时间语义

- Admission/ExtractedClaim 新增向后兼容的显式 `memory_layer`；episodic fact 从摄入时间计算短 TTL，episodic plan 从 `max(recorded_from, occurred_end, occurred_start)` 计算 grace，TTL 不再依赖 `reason` 文本。
- MemDaily 缓存指纹覆盖提取 provider/结构化模式、chunk/verification、retention/write floor、外部实体别名文件摘要、索引/embedding 与关系发现配置；`--skip-ingest` 删除 stale 库前打印原因。
- PerLTQA 直接写入使用生产 `index_text_mode`，并对最终 index text 而非原始 claim 文本生成 embedding。
- 时间解析完整消费显式 `Z`/UTC offset；无范围的多日期 evidence 必须由 claim value 唯一定位，否则拒绝推断。

### LongMemEval 与共享重试

- LongMemEval runner 拆出 reader context、QA client 与 judge 模块，CLI、参数、报告字段及 runner 兼容导出保持不变。
- windowed reader 在单个 session 中选择最多三个高分、互不重叠的 turn 窗口，并在原有总 token 预算内合并相距较远的证据。
- QA reader/judge 复用增强后的 `http_utils.retry_http`，保留嵌套异常链、HTTP 429/5xx、Read/Connect timeout、指数退避和 `Retry-After` 能力。

### 升级注意

- SQL schema 仍为 38 个不可变 migration（001-038），没有新增 SQL migration。
- ⚠️ 038 Python data migration 会在 `BEGIN IMMEDIATE` 中全表扫描 `claims` 并在处理期间持有写锁。大库应先备份、在维护窗口升级并预留完整扫描时间；升级完成后按上述命令显式回填 embedding。

## v0.24.0（2026-08-07）

### 向量检索与性能

- 默认 `sqlite_scan` 将全行 `SELECT *` 向量扫描改为两阶段执行：先只读取 ID 与向量并批量计算余弦分数，再按候选 ID 回表物化完整 Claim，降低解码与 Python 对象开销。
- 修复两阶段扫描的可见性边界：回表过滤不足 `limit` 时继续读取下一批评分候选，直到满足上限或候选耗尽，保持历史时间、记录时间与 namespace 过滤后的结果完整性。
- 新增可选 `sqlite_vec` 后端与 `hl-mem[sqlite-vec]` extra；默认仍为精确 `sqlite_scan`。后端实现覆盖建表/回填、增删改同步、模型与维度漂移守卫、dirty 检测、受控 scan fallback 和扩展加载失败诊断。
- 抽取公共候选物化器，使 `sqlite_scan` 与 `sqlite_vec` 共用回表、namespace 与双时间可见性规则，减少两条检索路径的语义漂移。

### Query expansion

- 收紧 `auto` 触发：短查询不再单独触发 LLM；指代查询仅在存在可用 session context 时预扩展，避免对“用户名”等短而明确的查询产生无谓调用。
- 保留 low-recall 边界：context/coreference 扩展未产出有效改写时，原始召回仍可在候选不足后触发 low-recall expansion。

### 可靠性、依赖与可观测性

- 服务启动时自动 drain `claim_vector_dirty`，将旁路 SQL 造成的更新/删除同步到 sqlite-vec 派生投影，避免长期停留在精确扫描回退路径。
- MCP Python SDK 从核心依赖移至 `mcp` extra，修复与 `claude-agent-sdk` 的 MCP 版本冲突；dev dependency、lockfile、CI 测试导入和中英文安装文档统一接入 `hl-mem[mcp]`。
- `/healthz` 新增 `vector_backend` 字段，直接报告当前配置的向量后端；doctor 的 migration 识别改用明确的 data-migration 常量，不再误计 Python data migration。

### Migration 与兼容性

- 新增不可变 SQL migration `037_vector_index_control.sql`，将向量后端控制表和 dirty triggers 纳入常规 schema runner；本版本结束时共有 37 个 SQL migration。
- 新增 `sqlite_vec.py` Python data migration，仅在显式选择 sqlite-vec 时构建可重建的派生向量投影；核心 Claim BLOB 继续作为权威数据。
- `sqlite_scan` 仍为默认后端，现有部署无需安装本地扩展；MCP 用户升级后需安装 `hl-mem[mcp]`。REST 主契约保持兼容，`/healthz` 仅增加字段。

## v0.23.1（2026-08-06）

### 仓库治理
- 目录结构精简：benchmarks/ 合并到 evaluation/，评测脚本移入 evaluation/tools/
- docs/ 精简：一次性报告归档，删除无关文件
- scripts/ 从 42→~15 个，删除一次性脚本和敏感数据
- .gitignore 收紧：evaluation/results/cache、docs/tasks、docs/superpowers
- install_to_hermes.py 移入 scripts/
- var/ 和 hl_mem.toml 从 git 跟踪中移除
- Git 历史净化：清除敏感数据（DB 备份、真实路径/用户名、API key）
- sdist 体积从 246MB→1.5MB（排除 .build-venv/.git/evaluation）

### CI 修复
- 恢复脱敏后的 CI 测试 fixture
- 更新 quality-smoke baseline 匹配脱敏数据集

## v0.23.0（2026-08-06）

### 提取质量与准入

- 将约 260 行提取 prompt 精简为约 63 行，LLM 输出收敛为 6 字段 compact schema；旧 14 字段输出继续兼容。
- 新增纯函数 `AdmissionPolicy`，以 notability、证据可定位性、敏感值和操作快照规则统一约束 dry-run 与生产写入。
- compact schema 后处理恢复 choice、qualifiers、时间边界和非 subject 实体，使精简输出仍能投影到完整 Claim schema。
- 修复准入误杀：稳定 preference/architecture 不再被一次性操作词拦截；数字、IP 和端口证据要求精确一致；legacy 输出也进入同一准入链路。
- 当 `should_memorize=false` 但返回非空 claims 时以 claims 为准，避免模型控制字段与实际结果矛盾导致静默丢失。

### Embedding 与 Reranker

- native embedding 的 `text_type` 默认从 `document` 改为未设置；仅在 TOML 显式配置时发送，compatible 模式行为不变。
- `text_type`、sparse 和 instruct 实验能力保留为可选配置，默认关闭；12 题 LongMemEval-S 分层选型中，Q1（native、不传 `text_type`）按二元命中口径取得 1.0，该口径现统一命名为 Hit@1。
- Reranker 默认型号从 `gte-rerank-v2` 迁移到 `qwen3-rerank`；密钥、provider 和 model 仍分别通过 `.env` 与 TOML 配置。

### Benchmark 基础设施

- 新增 LongMemEval-S runner，支持 session 粒度 ingest、extract-once、多 embedding 配置重建、缓存 fingerprint、claim/session 双层指标及可选 QA。
- 新增 50 case、190 条 gold claim 的中文记忆测试集及 embedding 对比 runner，覆盖偏好、身份、技术配置、日常事实、知识更新和噪音。
- claim relevance 默认阈值从 0.65 调整为 0.5；12 题阈值分布分析中 0.40 为探索性最优点，0.5 作为更保守的 runner 默认值。
- 修正检索指标命名：二元命中统一报告 Hit@K，Recall@K 表示相关 Claim 集合覆盖率；固定独立 relevance scorer，避免候选 embedding 同时充当评委。

### 测试与兼容性

- 995 项 unittest 全部通过；新增 AdmissionPolicy 稳定偏好、数字证据精确匹配、compact/legacy 准入一致性及 compact schema 投影回归。
- 无数据库 migration 变更，schema 仍为 migration 036；REST 和持久化 Claim schema 保持兼容。

## v0.22.0（2026-08-05）

### Embedding Model Migration

- Embedder 新增双 API 模式（compatible + native），支持 DashScope native API 的 `text_type` query/document 角色区分。
- 生产 embedding 模型从 text-embedding-v4 迁移到 qwen3.7-text-embedding（native API, 2048 维, text_type 角色化），消融实验验证 Hit@5 +13.3%、MRR +20.7%。
- 新增 `embedding.api_mode` 配置项（`compatible` | `native`），默认 `compatible` 保持向后兼容。
- 全量 re-embedding 脚本 `scripts/reembed_all_claims.py` 支持 `--dry-run`、并发漂移守卫与失败时事务回滚。
- `embedding_model` 列记录每条 Claim 的 embedding 版本，并由完整性检查识别模型或维度漂移，降低旧模型向量混用风险。

### Extraction Verification

- 新增 entailment verifier（`src/hl_mem/ingest/verifier.py`）：提取后批量 LLM 验证 claim 是否被原文支持（entailed/partially_entailed/contradicted/unsupported）。
- 新增 `extraction.verification_mode` 配置项（`off` | `audit` | `enforce`），默认 `off`。
- audit 模式只记录不拦截，enforce 模式验证但不拦截（未来版本才真正拦截 unsupported claim）。
- 触发条件：claim 数量超过阈值（默认 5）或 enforce 模式下每个非空 chunk。
- fail-open 设计：API 失败/限流时不影响提取流程。

### Dedup Safety Gate

- dedup 改为三层判定链：cosine 生成候选 → 确定性安全门 → 异步 LLM 判灰区。
- 确定性安全门检查 subject/slot/qualifier 一致性，阻止已知误合并模式（不同 qualifier 值、不同主体、HTTP_PROXY vs HTTPS_PROXY 等）。
- cosine 不再直接决定合并，只生成候选。
- policy_version 升级到 v2；v1 决策不再自动 apply。
- DedupJudge 补充 canonical_slot/canonical_attribute/qualifiers/valid_from/valid_to 字段，强化等价判定 prompt。

### Evaluation Infrastructure

- 新增 3 套冻结评测数据集：claim-pair（80 对）、recall query（80 条）、extraction/entailment（123 对）。
- 新增 embedding 逐级消融 benchmark runner（6 配置 V0→Q4）。
- 新增 no-answer calibration 脚本。
- 评测数据集验证器将预期的 corpus 漂移降级为 warning，同时保持数据结构、标签与 split 校验严格。

### Known Issues

- 拒答能力（no-answer precision）仍然弱（0.17-0.24），相似度阈值无法有效区分有答案/无答案查询；需要更复杂机制（entailment/no-evidence 判定）。
- relevance gate 保持 observe 模式，不建议切换 enforce。
- 本版本不新增 migration，schema 仍为 migration 036。

## v0.21.2（2026-08-04）

### Conflict Convergence and Operations

- `auto_resolve_conflicts` 扫描 `pending`、`auto_resolved`、`manual_required` 全部未决 case，追踪 supersede 链端点，并在两端汇聚或一端已终态时自动收敛，消除已分类 case 永不回访的盲区。
- `hl-mem conflicts resolve keep_left|keep_right` 在裁决 case 的同时将 loser 置为 `superseded`，写入 `superseded_by_id` 与双时间结束边界，使人工裁决与自动收敛保持一致。
- `hl-mem conflicts list` 补充左右 Claim 的 value、status、authority 和 `recorded_from`，人工审核无需再额外查询 Claim。
- `/healthz` 新增 `conflict_open_count`，统计尚未终结的 `pending`、`auto_resolved`、`manual_required` case，便于监控冲突积压。

- 本批次不新增 migration，schema 仍为 migration 036。

## v0.21.1（2026-08-04）

### Extraction Quality and Safety

- LLM 提取结果在任何原始字段审计与写入前扫描完整结构化 claim，确定性拒绝 recovery codes、`sk-` token、`password=` / `api_key=` 赋值和 16–32 位字母数字混合 token；`secret_rejected` audit 只记录原因分类与数量，不复制敏感原文。
- 提取后处理将包含“建议/考虑/待定/或许/可以考虑/计划中/未执行”等未决信号的 claim confidence 封顶为 `0.55`；“已确认/已批准/已执行/已完成/正式采用”等明确落地信号保持原置信度。
- system prompt 增加三组策略讨论对照：未批准建议拒绝、用户明确确认的政策归入 `config.policy`、代码与实测共同证明的架构归入 `fact.architecture`；LLM extractor 版本更新为 `llm-v2`。
- 对完整 system prompt、响应 JSON Schema 与确定性后处理规则常量计算 canonical SHA-256 前 12 位指纹；新 LLM claim 的 `extractor_version` 记录为 `llm-v2+<hash12>`，提取 audit detail 记录 `extractor_hash`，extraction benchmark manifest 记录 `prompt_hash`。历史 `llm-v2` 值继续兼容，无需 migration。
- canonical attribute registry 新增 `config.policy`、`config.version`、`fact.dependency` 和 `fact.architecture`，并补充“版本/依赖/架构”历史中文别名及内容推断提示，不增加 operational slot 或冲突语义。

### Data Governance and Evaluation

- `scripts/reclassify_predicates.py --custom-unknown` 增加不调用 LLM 的 registry 属性回填模式，支持 dry-run 和原子 apply；该小版本只更新 `canonical_attribute`，不改写历史 predicate 或派生索引。本地存量 5 条 `custom.unknown` 已闭环为 architecture 2、capability 1、dependency 1、version 1。
- 通过正常 `ForgetService` 撤回 6 条明确来自 assistant 单次执行过程、未批准条件建议或 quoted assistant 自述的测试策略噪声，并清除其 dense/sparse embedding。
- gold matcher 复用生产 `normalize_entity_id()` 和 canonical attribute registry：统一 `user`/`用户`、大小写及全半角实体差异，按已注册 attribute family 兼容“配置/使用”和“事实/状态”旧标签，并在保持 value 阈值 `0.62` 不变的前提下规范引号、标点、数字格式和 URL 尾斜杠；value 部分匹配继续输出连续分数。
- qwen3.7-plus 同口径 50-event benchmark 全部完成且无提取错误；复用已有结果文件、未重新调用 API 的 20 条 gold 事件指标如下：

| 指标 | v0.21.0 baseline | v0.21.1 prompt | B（matcher 修复前） | B（matcher 修复后） |
|---|---:|---:|---:|---:|
| Claim precision | 13.3% | 18.8% | 25.0% | 31.2% |
| Claim recall | 5.6% | 8.3% | 11.1% | 13.9% |
| Scope accuracy | 50.0% | 66.7% | 75.0% | 60.0% |
| 漏提取 | 34 | 33 | 32 | 31 |
| 过提取 | 13 | 13 | 12 | 11 |

- matcher 修复后新增匹配的 entity graph claim 内容正确，但预测 scope 为 `permanent`、gold 为 `temporal`；因此 scope accuracy 降至 60.0%，反映的是新暴露的真实 scope 错分，而非匹配质量回退。

- 本批次不新增 migration，schema 仍为 migration 036。

## v0.21.0（2026-08-04）

### Memory Lifecycle and Corrections

- 核心记忆 `identity.*` 与 `memory.explicit` 统一免于自动衰减、归档和 TTL 回填，避免长期身份锚点及显式记忆因低访问量退出召回通道；身份变更继续由冲突与 supersede 链表达。
- historical 召回可见 `archived`、`superseded` 与 `expired` 历史状态；TTL worker 同时关闭已到期的 `active` 和 `disputed` Claim。
- decay 以 UTC 当日零点统一计算并记录 `last_decayed_at`，消除非零点运行造成的逐日衰减漂移；temporal cleanup 不再读取或依赖已弃用的 `volatility` 字段。
- REST、MCP 及 feedback correction 的 `idempotency_key` 改为可选；缺省时由 `CorrectionService` 自动生成 UUID，显式提供时仍保留原有幂等冲突校验。

### MCP Runtime

- 使用官方 MCP Python SDK 2.x 低层 `Server` 接入 stdio transport，新增 `hl-mem-mcp` 与 `python -m hl_mem.mcp` 入口；工具列表直接复用既有 JSON Schema，避免 transport 契约漂移。
- 同步数据库和应用服务调用通过 worker thread 执行；参数校验、资源不存在和生命周期冲突等预期错误返回 MCP `isError=true`，未分类内部异常保留协议级错误语义。
- 新增内存级 MCP Client/Server 协议测试、线程执行测试和 `--config`/`--env-file`/`--db` 入口参数测试，并补充 Codex、Claude Code/Desktop 与 Cursor 的 stdio 连接说明。

### Packaging and First Run

- 项目版本更新为 `0.21.0`，新增 `mcp>=2,<3` 运行依赖和 `hl-mem-mcp` console script；PyPI 安装包继续同时提供 `hlmem` 与 `hl-mem`。
- 新增 tag `v*` 触发的 PyPI Trusted Publishing workflow：先校验 tag 与 `pyproject.toml` 版本一致，再构建 wheel/sdist，通过 `pypi` environment 和 OIDC `id-token: write` 上传；首次创建项目时需先配置 pending publisher。
- sdist 显式排除本地 `.env`、部署配置、备份、运行时 `var/`、任务草稿和本地研究笔记，避免发布归档携带工作区状态。
- 中英文 README 第一屏改为 PyPI 安装、`hlmem init --offline`、server、remember、recall 和证据引用说明；源码安装、在线模型、Hermes 与 systemd 移至进阶章节。

### Configuration Errors

- `Settings.validate()` 中历史 `HL_MEM_*` 错误提示改为实际 TOML 路径；缺少已启用组件密钥时明确列出 `LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`，并提示写入 `.env` 或关闭对应 TOML mode。

### Memory Query and Correction

- 新增 `GET /v1/memories/{memory_id}`，沿用 `MemoryQueryService` 返回 Claim 内容、生命周期与分类字段、`superseded_by_id`、完整 evidence links、来源事件及 conflict 历史。
- 新增 application 层 `CorrectionService` 与 `POST /v1/memories/{memory_id}/correct`：显式纠正只替换内容，不重新经过 extractor；新 Claim 继承原分类与重要性字段，并原子重算 fact hash、index text、embedding 和 TTL，旧 Claim 同步转为 `superseded`，其派生 observation 标记 stale，并建立 correction event 与替代证据链；远程 embedding 窗口使用继承字段快照防止并发分类更新丢失。
- MCP 新增 `memory_get`、`memory_correct`，与 REST 复用同一查询/纠正服务；`memory_feedback` 的显式 correction 也统一走该服务，并与 REST 一致返回 `correction_event_id`，同时保留既有 `id`、`replacement_event_id`、`forgotten`/`idempotent` 兼容字段。

### Reliability

- `Database` 在首次打开与迁移前自动创建数据库父目录，修复 `hlmem init` 后默认 `var/` 尚不存在时首次启动报 `unable to open database file`；该保护同时覆盖 server、worker、MCP 与自定义数据库路径。
- 测试套件在 `tests/conftest.py` 顶层通过 `setdefault` 提供 `test`/`fake`/`off` 默认运行环境，避免集成测试误用真实 extractor、embedding 或 reranker，同时保留显式环境变量的覆盖优先级。
- `repair_conflict_losers` 存量修复命令按 resolved case 同时收敛胜败者终态：遗留的 disputed 胜者恢复为 active，disputed 败者转为 superseded 并补齐替代关联与双时间边界。
- 本批次不新增 migration，schema 仍为 migration 036。

### Daily CLI and Offline First Run

- `hl-mem` 新增 `init`、`server`、`remember`、`recall`、`list`、`forget` 日常命令，并提供等价的 `hlmem` entry point；日常读写默认通过本地 HTTP 服务，保持与真实部署一致的事务、审计和错误语义。
- `hl-mem init --offline` 可生成无需 API key 的安全配置：确定性 fake extraction/embedding、关闭 reranker、图片描述、query expansion、关系发现和跨主体去重，并明确 fake embedding 不代表语义检索。
- 新增 `GET /v1/memories`，通过 application service 与 ClaimRepository 共享查询路径，按 namespace/status 提供稳定的 limit/offset 分页。
- 新增 `recall.dense_enabled`（默认 `true`）；关闭时召回管线完全跳过 query embedding 和 dense 候选收集。offline 配置同时关闭 tag channel，提供诚实的 FTS-only 关键词候选召回。
- 无有效 LLM key 时，Worker 不再调度需要 LLM 的每日冲突归并和跨主体去重任务；确定性的生命周期维护保持运行。

## v0.20.2 (2026-08-02)

### Recall Quality and Diagnostics

- 修复 query expansion 结构化提示词缺少 `queries` 输出键的问题；将默认模式切换为 `auto`，支持为扩展请求单独配置 `glm-4.7`，并将单次/总超时放宽到 5/6 秒。
- slot hint 现在同时匹配 `canonical_slot` 与兼容字段 `canonical_attribute`，并补充显存、VRAM、graphics card、处理器等高价值中英文别名。
- 回滚基于首轮 FTS 命中数和 dense top score 的 `low_recall` 触发升级，恢复候选数不足时才触发的保守规则，等待针对性评测完成后再决定是否扩大触发面。
- dense 检索结果携带真实 cosine `_score`，SearchTrace 的 dense channel scores 不再缺失原始通道分数。

### Evaluation

- 新增 28 条针对性配对评测集，并扩展 `eval_runner` 输出 `pair_id`、dense cosine 与 reranker raw score，支持 query expansion 开关的逐对基线比较。

### Configuration

- 恢复仓库与示例 TOML 的 `recall.relevance_keep_top1 = true`，保护低分但正确的 top-1 结果。
- 将仓库与示例 TOML 的 `recall.default_limit` 从 20 调整为 5、`recall.relevance_reranker_floor` 从 0.4 调整为 0.15；`Settings` 静态代码默认值保持 20 / 0.4，部署覆盖与代码默认在配置文档中明确区分。

### Reliability and Operations

- 移除 Bash watchdog，新增纯标准库 `scripts/healthcheck.py`，并补充 systemd、Windows Task Scheduler 与容器平台的跨平台探测、恢复和告警职责说明。
- 新增 Windows 单次执行的静默 `scripts/hlmem_supervisor.py`：兼容 `pythonw.exe`，提供连续失败阈值、重启冷却、跨次状态、陈旧锁回收、端口进程树终止和无窗口拉起。

### Compatibility

- 无新增数据库 migration，当前 schema 仍为 migration 036；无 REST 或 MCP 契约变更。`query_expansion_mode` 的 `Settings` 默认值从 `off` 变为 `auto`，生产部署需要提供 `LLM_API_KEY`，不希望产生扩展调用的环境应显式配置为 `off`。

## v0.20.1 (2026-08-02)

### Reliability and Observability

- 放宽 `canonical_attribute` 的 LLM 结构化输出 schema：非规范值不再导致提取任务直接失败，而是进入既有领域校验并回退为 `custom.unknown`。
- 将 `/healthz` 改为不依赖数据库的异步端点，仅返回进程内状态，避免线程池耗尽或数据库锁竞争使健康探针饿死。
- 历史记录：v0.20.1 曾新增 P0 Windows watchdog；后续架构改造已移除 `scripts/hlmem_watchdog.sh`，改用纯标准库的 `scripts/healthcheck.py` 提供跨平台探测，并将重启、告警等监督职责交给 systemd、Windows 服务管理器或容器编排平台。
- 新增 P1 HTTP 请求生命周期日志中间件：记录 `request_started` / `request_finished`、方法、路径、状态码、耗时及经过清理的可选 `X-Request-ID`，异常请求也会记录完成事件。
- 修复生产启动的 logging 配置：为 `hl_mem` logger 单独启用 INFO 输出，确保请求日志中间件事件不再被过滤，同时不开启第三方库的 INFO 日志。

### Configuration

- 在实例与示例配置中启用 relevance gate `observe` 模式，仅观测 `current_state` intent；阈值调整为 reranker `0.4`、dense `0.3`、relative drop `0.30`，并关闭 `keep_top1`。

### Compatibility

- 无新增数据库 migration，当前 schema 仍为 migration 036；无 REST 或 MCP 路由变更。`/healthz` 不再返回需要查询数据库的 24 小时 `llm_stats` 聚合，数据库与 LLM span 统计应使用专用只读审计路径。

## v0.20.0 (2026-08-01)

### Tokenized FTS v2 Migration

- 新增确定性中文 lexicalizer：NFKC 归一化、jieba 领域词典与 stopwords、技术标识符整体词和分段词；claims、events 与 claim topic tags 的生产稀疏检索统一切换到预分词 FTS v2，dense、RRF 与 reranker 通道保持不变。
- 新增不可变 migration 036，创建 `claims_fts_v2`、`events_fts_v2`、`claims_tags_fts_v2` 与源行删除清理 trigger；启动时校验 source/v2 rowid 集合，不完整时在单一 `BEGIN IMMEDIATE` 事务中原子重建三个通道。
- claim/event 写入、`index_text` compare-and-set 回填和 v2 文档更新共享事务边界；新增 `backfill_tokenized_fts` 工具支持按 channel 原子重建。
- 旧 trigram/raw FTS 表保留用于回滚窗口，但生产查询不再调用 legacy sanitizer 或 trigram fallback；计划在后续 migration 037 删除旧表。

### Evaluation

| 版本 / 口径 | 正例数 | Hit@1 | Hit@5 | MRR |
|---|---:|---:|---:|---:|
| v0.19.1 原始标签 | 20 | 0.6500 | 0.7500 | 0.6917 |
| v0.19.1 有证据子集 | 17 | 0.7647 | 0.8824 | 0.8137 |
| v0.20.0 tokenized FTS v2 + real dense/reranker | 17 | 0.8235 | 0.9412 | 0.8725 |

- 30-case 数据集经原文核对后将无库内事实的 q13、q16、q18 修正为 no-answer；当前口径为 17 条有证据正例与 13 条 no-answer。
- 发布门禁保持 hybrid Hit@5 >= 0.75；纯 FTS 的 Hit@5 为 0.1765，仅作为语义改写问题的诊断指标。

### Known Issues and Compatibility

- q08“向量模型”仍依赖后续语义别名与 reranker 融合改进；dense 通道仍会为 no-answer 查询补足候选，当前 no-answer precision 为 0，拒答阈值标定不在本版本范围内。
- 绕过 repository 的原始 SQL UPDATE 不会运行 Python lexicalizer，需用 `backfill_tokenized_fts` 修复内容陈旧；原始 SQL INSERT 造成的缺行会在下次启动时自动重建。
- lexicalizer 首次加载 jieba 词典会增加冷启动时间，首次升级还会同步重建三个 v2 channel。
- 无公共 API breaking change。数据库 migration 036 不可变且生产稀疏检索直切 v2；升级前应按既有流程备份数据库并预留首次重建时间。

## v0.19.1 (2026-07-31)

### Bug Fixes

- 修复 Hermes Provider hook 签名不匹配：`on_memory_write(action, target, content, metadata)` 匹配 Hermes 调用约定，`on_session_end(messages)` 接受位置参数，修复参数错位和 `invalidate_session` 从未执行的缺陷。
- `sync_turn()` 和 `on_memory_write()` 成功写入后主动失效 prefetch 缓存，消除同一会话内最多 300 秒的 stale bundle 窗口。
- A/B 测试 runner 增加实验有效性 gate：两 arm 投影 `text_hash` 相同时标记 `result.status = "inconclusive"`，避免从无效实验生成结论。

### Enhancements

- JSONL import 在 `jobs_queued > 0` 时向 stderr 输出 extractor model warning，提高 CLI 透明度。

## v0.19.0 (2026-07-31)

### Context Delivery and Feedback

- 新增严格版本化的 Context Packet v1：召回完成相关性过滤和最终 token 预算打包后，统一输出有序记忆条目、证据、answerability、截断状态及逐条 `feedback_id`。
- Claim 写入、FTS、dense embedding、回填与一致性检查统一消费持久化 `index_text`，避免同一 Claim 在不同检索通道使用不同文本投影。
- feedback exposure 改为在最终 Context Packet 物化时批量创建；Hermes 只在文本实际跨过 Agent host/model 输入边界后异步确认 `injected`，失败可降级并有限重试。
- 新增不可变 migration `035_retrieval_feedback_injected`，将 `retrieval_feedback.used_by_model` 收口为语义明确的 `injected` 字段。

### Operations and Compatibility

- 新增整库 `hl-mem backup` / `hl-mem restore` CLI：备份附带 SHA-256 manifest，恢复前校验 sidecar、大小、哈希和 SQLite integrity，并通过同目录临时库原子替换。
- JSONL import 默认按稳定幂等键重建缺失的 `extract_event` Job，使 Event 归档恢复后可由 Worker 重建 Claims；取证场景可显式跳过。
- REST 显式记忆与 MCP `memory_save` 支持调用方幂等键，重试会返回原始 Event/Claim，不再生成重复记忆。
- 数据集与 quality-smoke baseline hash 统一使用 CRLF/LF/CR 无关的 `sha256-utf8-lf-v1`；未知 schema 或 hash 算法拒绝静默比较。
- 修复 Windows/POSIX 启动脚本：从脚本位置解析仓库根目录、统一调用 `start_server.py`，删除旧 `start_v017.sh` 和失效的 `HL_MEM_*` 覆盖。
- 公共接口以 `namespace` 为唯一现行名称，`tenant_id` 仅保留为已弃用兼容别名；文档明确 namespace 是相关性软分区而非安全边界。

### Evaluation and Release Gates

- 修正评测中 Hit@5 与 Recall@5 的定义和聚合，Hit@5 仅表示是否至少命中一条，Recall@5 按相关集合覆盖率计算。
- 改善实验性 `answerable` 文本投影及其回填/一致性校验；受控 A/B 实验结果为 inconclusive（两 arm 投影文本相同），保留 `legacy` 为保守选择。
- 建立 v0.19 eval gate 基础设施：冻结 dataset/fixture/config、compatibility 与 non-regression gate、关键 slice 门禁、受控单变量 A/B 报告和显式 baseline 警告。
- CI 合成 fixture 的 no-answer 指标保留为已知限制，不作为真实 provider 的生产拒答质量证据。

### Configuration and Maintenance

- 发布版本更新为 `0.19.0`；DashScope 默认 LLM 模型与 provider 对齐为 `qwen3.7-plus`。
- GitHub Actions 中仍使用 Node 20 的 setup/upload action 已升级到 Node 24 运行时版本。
- 清理无入口、硬编码本机路径或生产数据库的一次性调试脚本，并扩充临时日志/pytest 输出忽略规则。

## v0.18.0 (2026-07-30)

### Breaking Changes

- 非敏感运行配置改为单一 TOML 配置源。所有正式入口默认从进程当前工作目录读取 `hl_mem.toml`；文件缺失、未知表或键、类型错误都会直接阻止启动。
- 删除运行环境 profile、`Settings.from_env()`、fake 自动回退以及全部 `HL_MEM_*` 配置变量。未在 TOML 中列出的字段使用 `Settings` 的静态安全默认值。
- `.env` 和进程环境现在只识别 `LLM_API_KEY`、`EMBEDDING_API_KEY`、`RERANKER_API_KEY`、`IMAGE_API_KEY`；图片描述器不再复用 LLM 密钥。
- 提取、Embedding、Reranker、图片描述器的代码默认模式分别为 `fake`、`fake`、`off`、`off`。真实能力必须在 TOML 中显式启用并提供各自密钥。

### Configuration and Startup

- 新增 `config.example.toml` 常用配置模板和由 `Settings` metadata 生成的完整配置参考 `docs/configuration.md`。
- `start_server.py` 在组件创建前只加载、校验一次配置，并向 API 与 Worker 注入同一个不可变 `Settings` 快照。
- 升级步骤：复制 `config.example.toml` 为 `hl_mem.toml`，复制 `.env.example` 为 `.env`，填写已启用组件的密钥，并确保 systemd `WorkingDirectory` 指向这两个文件所在目录。

## v0.17.4 (2026-07-29)

### Bug Fixes
- Fixed an FTS column-name mismatch that caused rebuild failures (migration 034)
- Changed the `scripts/install_to_hermes.py` plugin path from `plugins/memory/` to `plugins/hl_mem/`

### Features
- Added the `hl_mem doctor` diagnostic command with 9 checks
- Added `.env` placeholder validation so production mode rejects fake keys

### Documentation
- Improved the README installation section with a systemd template, three-step verification, and FAQ

## v0.17.3 (2026-07-29)

### Bug Fixes
- **SQLite busy_timeout**: increased from 5s to 30s to prevent "database is locked" errors on slow disk IO
- Added configurable `HL_MEM_DB_BUSY_TIMEOUT_SECONDS` environment variable (default 30)
- Applied to both migration and regular connections

### Tests
- New test verifying busy_timeout configuration is applied correctly

## v0.17.2 (2026-07-29)

### Extraction Prompt Optimization

Rewrote SYSTEM_PROMPT with structured 10-section design based on industry research (Mem0/Letta/Zep/LangMem/Cognee):

- **Four-gate admission**: evidence gate, future utility gate, temporal gate, distinctiveness gate
- **Speech act classification**: asserted/committed admitted; proposed/hypothetical/procedural/phatic rejected
- **Discrete confidence anchors**: 0.98/0.90/0.75/0.55 (no arbitrary decimals)
- **Scope x volatility four-quadrant**: replaces single "one year" criterion
- **3 positive + 3 negative few-shot examples**: covering different confidence levels
- **Tightened predicates**: reviews/suggestions/hypotheses cannot use fact predicate
- **Process noise rejection**: execution status, CI snapshots, duration estimates

### Validation
- 7/7 dry-run extraction tests correct (3 noise rejected, 4 valid extracted)
- Confidence distribution now differentiated (0.98 vs 0.90 vs 0.75)
- 605 tests passed, 0 failed

## v0.17.1 (2026-07-29)

### Code Review Fixes (13 items)

#### P1 fixes
- Multi-channel fallback now enforces dense_floor when dense channel is present
- Session context SQL filters by actor_type before LIMIT
- Backfill CAS includes embedding_model and dimension; version tracks re-embedding
- Observe/enforce relative-drop computation unified (shared function, >= threshold)
- Observe mode writes relevance_reasons instead of filter_reasons (trace semantic fix)
- Eval runner consumes answerability for no-answer detection
- Gate check validates forbidden_hits, http_success_rate, no_answer_recall, slice coverage
- Session context get_recent_events() adds optional user_id parameter (defensive)

#### P2 fixes
- Context budget skips oversized messages instead of breaking
- Context outcome structured in trace (ok/empty/missing_session/read_error/deadline_exhausted)
- Batch cosine streaming via fetchmany (reduces peak memory)
- Vector sort key uses claim_id for deterministic tie-break
- Backfill retry adds backoff and error classification

## v0.17.0 (2026-07-29)

### Recall Pipeline Improvements

#### A: Evaluation Infrastructure
- Expanded recall evaluation dataset from 50 to 80 cases with 7 slice categories
- Added eval_runner.py for Recall@K, MRR, nDCG@5, Top-3 precision, latency metrics
- Added gate_check.py for release quality gating
- Versioned baseline tracking (baseline_v1.json)

#### E: NumPy Batch Cosine
- Replaced pure-Python cosine loop with NumPy batch matrix computation
- 100-500x faster dense vector scanning, same exact results
- Added vector_batch_size setting (default 512) for memory control
- Added numpy>=2.0 as runtime dependency

#### B: Relevance Gate (observe + enforce)
- New HL_MEM_RELEVANCE_GATE_MODE=off|observe|enforce (default off)
- observe: computes per-candidate relevance diagnostics without truncation
- enforce: truncates low-relevance tail for current_state intent (whitelist)
- Per-candidate score_path and reranker_raw_score in API response
- answerability field promoted to API schema

#### C: Answerable Index Text
- New index_text mode "answerable": subject + readable label + value
- Template from SLOT_REGISTRY, deterministic, no LLM
- CLI: python -m hl_mem.cli backfill-index-text --dry-run
- Safe compare-and-set backfill with cursor resume

#### D: Session-Aware Coreference Resolution
- session_id now flows from API through to QueryExpander
- coreference queries use session context for disambiguation
- Fixed: get_recent_events() now filters by namespace (tenant_id)
- Fixed: Settings.from_env() default query_expansion_mode unified to off
- New: HL_MEM_QUERY_CONTEXT_MODE=off|coreference (default off)

### Bug Fixes
- session_id was defined in RecallInput but never passed to RecallService
- get_recent_events() lacked namespace isolation (tenant_id filter)
- Settings.from_env() implicitly defaulted to query_expansion_mode=auto

### Dependencies
- Added numpy>=2.0

## v0.16.1 — 2026-07-28

### Fixed

- 精确化 trace observation 的 error 检测逻辑：改用行首模式匹配（Traceback / `Error:` / `FAILED`）与非零 exit_code 判断，不再因输出中包含 "error" 子串而误标。

### Changed

- 新增 `scripts/reclassify_predicates.py`：用 LLM 对历史 predicate="事实" 的 claims 重新分类，实际数据治理将"事实"占比从 52% 降至 14%。

## v0.16.0 — 2026-07-28

### Added

- 新增召回诊断、`index_text` 三模式 A/B、provider 调用观测与跨模型结构化提取 benchmark。
- 新增 `HL_MEM_LLM_ENABLE_THINKING`、`HL_MEM_INDEX_TEXT_MODE`、结构化输出重试与提取分块配置。
- 新增可审计的 scope 降级、canonical attribute → predicate 投影、subject 守卫与扩展 JSON repair。

### Fixed

- 修复 claim 独立索引文本上线后，仓储兼容写入未生成 `index_text` 导致 FTS 漏召回的问题。
- 将 claim FTS 更新触发器收窄到 `index_text`，避免访问计数、状态和标签更新造成无关索引写放大。
- 更新领域分层重构后的单元测试导入路径。

### Documentation

- 更新 README、配置模板、架构与交接状态；将已完成的 benchmark/research/task 过程文档移入历史归档。

## v0.15.0 — 2026-07-26

- Provider diagnostics, safe answerability shadow gating, and bounded expansion deadlines.
- Query slot hints, regression coverage, sliding-window monitoring, alerts, and health snapshots.
- Calibration, score paths, claim index text, provider-call persistence, daily reports, and controlled A/B evaluation.

本文件记录发布级变更摘要。测试数字是对应版本的发布基线；migration 数是该版本结束时的 SQL migration 总数。

## v0.14.3 — 2026-07-26

- **类型治理**：清零 mypy 错误，移除 baseline 门禁，并对 core/domain 启用 strict。
- **CI 与质量门禁**：migration 使用冻结的 uv 环境；quality smoke 收紧 Recall@5/MRR 容差、增加最大排名约束并报告 p50/p90 延迟。
- **Tests**: validated by CI workflow on tag commit

## v0.14.1 — 2026-07-26

- **治理门禁**：mypy 纳入 uv 锁文件，Ruff 扩展为全仓库检查，主 CI 增加 smoke 与 `v*` tag 触发。
- **质量 smoke v2**：17 个确定性用例覆盖干扰项、同义查询、真实 supersede 生命周期、关系存储/发现与负例，并加入哈希基线、delta 和退化阈值。
- **类型债**：按配置解析、存储行边界和可空分支根因修复指定模块，mypy 基线由 37 降至 7。
- **验证约束**：按发布任务要求未运行 pytest；发布门禁由 Ruff、导入边界、文档一致性、mypy baseline 与 quality smoke 验证。

## v0.14.0 — 2026-07-26

- **类型与 lint**：Ruff 扩展至 F/E4/E7/E9/I；mypy 基线由 68 降至 37，并清零 recall/storage 当前错误。
- **质量趋势 MVP**：新增 10 条确定性 smoke 数据、离线 runner，以及 nightly/manual GitHub Action artifact。
- **契约治理**：新增 PR contract checklist，并要求人工审查 OpenAPI/MCP snapshot 变更。
- **工程配置**：统一 uv dependency group 和 CI 安装方式，以 CI badge 替代 README 硬编码测试数字。
- **验证**：按发布任务约束未运行 pytest；最近冻结测试基线为 445 passed，1 skipped。

## v0.13.4 — 2026-07-26

- **P0 治理**：版本 SSOT 覆盖 `pyproject.toml`，mypy 新错误门禁，CI 固定 lockfile，v0.10 历史数据库升级夹具，
  Policy/Derivation 生命周期守卫，以及扩展后的分层导入边界。
- **P1 公共契约**：新增兼容性政策、OpenAPI/MCP 快照、JSONL 导出格式版本和环境变量稳定性分级。
- **P2 质量趋势**：新增 nightly/manual 趋势基础设施设计文档，未实现运行器或工作流。
- **验证**：按治理任务约束未运行 pytest；最近已验证基线为 445 passed，1 skipped。

## v0.13.3 — 2026-07-26

### Fixed

- 修复 CI dev extra 与 coverage 门禁。
- 收紧 recall fold 语义保护，并为 TTL 扫描增加 180 天候选窗口。

### Changed

- 校正文档、能力矩阵、MCP 工具数和 PostgreSQL 实验性状态。

- **Migrations**：29（无新增）。
- **测试**：445 passed，1 skipped。

## v0.13.0 — 2026-07-26

### Added

- 新增能力成熟度矩阵，以及格式、构建、Python 3.12、空库 migration 和依赖方向 CI 门禁。

### Changed

- 完成工程收敛并修正文档 SSOT。

- **Migrations**：29（无新增）。
- **测试**：443 passed，1 skipped（沿用 v0.12.4 发布基线，本版本按发布约束未重跑 pytest）。

## v0.12.4 — 2026-07-26

### Fixed

- 修复 temporal cleanup/TTL 并发竞态。
- 收敛召回折叠语义和成本。

### Added

- 关系提案按 `run_id` 保留审计历史。
- 新增 TTL/cleanup 扫描索引。

- **Migrations**：29（新增 028、029）。
- **测试**：443 passed，1 skipped。

## v0.12.3 — 2026-07-26

### Added

- 新增默认关闭的 deterministic extraction pre-filter，在 LLM 调用前过滤低价值运行时事件。

### Changed

- 过滤结果保留审计，规则异常时回退正常提取。

- **Migrations**：27（无新增）。
- **测试**：433 passed，1 skipped。

## v0.12.2 — 2026-07-26

### Added

- 召回输出 score，并增加相似度折叠和 temporal 回填维护。

### Changed

- 清理语义重复 Claim，增强提取 prompt。

- **Migrations**：27（无新增）。
- **测试**：411 passed，1 skipped。

## v0.12.1 — 2026-07-26

### Fixed

- 修复 usefulness 类型约束、TTL 双时间和关系并发写入。
- 收敛组件降级、查询扩展并发、召回副作用和 benchmark 时间语义。

- **Migrations**：27（新增 025–027）。
- **测试**：401 passed，1 skipped。

## v0.12.0 — 2026-07-26

### Added

- 交付多查询召回、关系候选发现、Benchmark suite、图片证据入口、反馈驱动维护和 Tool/Procedure intent。

- **Migrations**：24（新增 023、024）。
- **测试**：373 passed，1 skipped。

## v0.11.2 — 2026-07-25

### Fixed

- 补齐 trigram FTS 行为与 migration 022 回归，并完成数据清理。

### Changed

- CI 扩展为全量测试套件。

- **Migrations**：22（无新增）。
- **测试**：342 passed。

## v0.11.1 — 2026-07-24

### Fixed

- 修复空 trigger、异常边界和 reranker retry。

### Changed

- 统一配置 Enum、HTTP retry、状态校验和核心类型/docstring。

- **Migrations**：22（无新增）。
- **测试**：325 passed。

## v0.11.0 — 2026-07-24

### Added

- 新增 LLM spans、Job 进度、中文 FTS 评测、后端协议、dry-run extraction 和 ConsolidationScope。

### Changed

- 将 Claim FTS 切换为 trigram。

- **Migrations**：22（新增 019–022）。
- **测试**：325 passed。

## v0.10.1 — 2026-07-24

### Added

- 增加 MRR/nDCG 和独立 behavioral scenarios。

### Changed

- 冻结排序因子，统一 RecallConfig，并类型化召回上下文。

- **Migrations**：18（无新增）。
- **测试**：292 passed，1 skipped。

## v0.10.0 — 2026-07-24

### Added

- 完成 topic tags soft boost、可选独立 tag channel 和确定性 query-to-tag 解析。

- **Migrations**：18（新增 018）。
- **测试**：292 passed，1 skipped。

## v0.9.1 — 2026-07-24

### Fixed

- 修复 qualifier 降级、TTL UTC 统一和回填 CAS。

- **Migrations**：17（无新增）。
- **测试**：277 passed。

## v0.9.0 — 2026-07-24

### Added

- 交付 slot+tags 分类、跨 subject 审计去重，以及 importance 联动 TTL。

- **Migrations**：17（新增 016、017）。
- **测试**：发布记录未保留精确计数；v0.9.1 基线为 277 passed。

## v0.7.0 — 2026-07-24

### Added

- 完成 canonical attribute、scope 后置规则、TTL policy 和 decay priority。

- **Migrations**：15。
- **测试**：发布记录未保留精确计数。

## v0.3.0 — 2026-07-23

### Added

- 完成冲突检测、事务原子化、fact_hash v2、MCP application 委托与初始架构分层。

- **Migrations**：13。
- **测试**：发布记录未保留精确计数。
