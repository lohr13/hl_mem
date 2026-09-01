# HL-Mem 能力成熟度矩阵

> 基线：v1.1.0。默认模式取自 `Settings` 的静态默认值；部署通过 `hl_mem.toml` 显式覆盖。`audit`/`observe` 表示会记录数据但不自动改变核心结果或生命周期。

## 成熟度定义

- **stable**：默认主路径，契约和降级行为受回归测试保护。
- **beta**：已可用且有安全默认值，仍需更多离线或生产观察才能扩大自动化范围。
- **experimental**：显式选择后使用，接口、质量阈值或运行边界仍可能调整。

## 六大特性

| 名称 | 成熟度 | 默认模式 | 外部 API | 写数据库 | 降级行为 | 晋级标准 |
|---|---|---:|---|---|---|---|
| 多查询召回 | beta | `auto` | 是，触发后调用 LLM | 是，仅 LLM span/audit | 超时、预算耗尽或解析失败时只使用原始 query | 固定评测集 Recall@K/MRR 不回退，P95 满足预算，连续两个版本无高优先级故障 |
| 关系候选发现 | beta | `off` | 是，启用后调用 LLM | 是，`audit` 只写 proposal/audit；不写关系边 | API 失败时不生成 proposal，核心 Claim 写入继续；禁用 Job 在 handler 前终止 | proposal precision 达到发布阈值，重复运行/并发审计稳定，人工批准的边保持来源与证据闭环 |
| Benchmark suite | beta | `off`（CLI 按需） | 视模式而定；真实提取/向量评测需显式配置 | 仅写隔离的临时 benchmark DB、缓存与报告，不污染生产库 | 数据集、缓存 fingerprint 或 adapter 错误时明确失败；429/quota 熔断后可在窗口恢复时用原参数 `--resume` 重跑限流 case，不影响服务运行 | LongMemEval-S extract-once/config-compare 与 50 case、190 gold claim 中文集持续版本化，结果可复现，CI/nightly 基线和回归阈值稳定 |
| 图片证据入口 | experimental | `off` | 是，开启后调用视觉 LLM | 是，成功描述后写 Event/Evidence/Claim | 描述失败则拒绝该图片提取并保留具体错误；不伪造文本证据 | 来源接入、SSRF/路径边界、安全与质量评测完成，失败率和延迟达到 SLO |
| Provider Plugin API | stable（三类）；Image 契约 experimental | 仅内置；第三方白名单为空 | 真实 Provider 是 | 仅独立用量账本、audit/span | 缺失、冲突、不兼容或配置错误时启动 fail-closed；不绕过宿主治理 | 稳定 API 快照、clean-wheel 外部插件和四调用路径用量闭环持续全绿 |
| 反馈驱动维护 | beta | `observe` | 否 | 是，写 feedback/usefulness；默认不改变 TTL/decay | 归因或聚合失败不影响 recall 主结果，记录错误并可重建 | usefulness 重建一致，离线证明生命周期收益且无错误延寿/衰减，再考虑默认 `on` |
| Tool/Procedure intent | beta | `keyword` | 否；`auto` 模式可调用 LLM | recall 会更新受控访问/观测数据 | LLM 路由失败回退 keyword；无候选时回退通用召回 | intent precision/recall、procedure 成功率和负向 outcome 处理达到阈值 |

## 核心功能

| 名称 | 成熟度 | 默认模式 | 外部 API | 写数据库 | 降级行为 | 晋级标准 |
|---|---|---:|---|---|---|---|
| Event 幂等摄入与证据链 | stable | `on` | 否 | 是 | 写入或约束失败时事务回滚并返回具体错误 | 保持跨版本事务、幂等、并发和证据完整性回归 |
| 来源与 Session 治理 | beta | `enforce` | 否 | 是，Event 两字段与审计 | `unknown` 保持旧行为；外部内容保留但降权；heartbeat/subagent 在模型调用前停提取；`observe` 不改结果 | 宿主标签、无额外模型调用、来源不洗白、Context 隐私和长期运行回归持续全绿 |
| Claim assertion 门控 | beta | `unknown`（legacy） | 否 | 是 | unknown 只可观测，不授权 supersede 或过滤注入 | 新写入分类精度和时间关链生产回放持续满足门禁 |
| 确定性时间关链 | beta | `temporal-v1` 窄规则 | 否 | 是 | 仅 observation 的原子状态/显式价格更正可自动；非互斥 slot 保持共存；灰区转人工 pair case | 固定 14 条 correct 保持 precision 1.0，合法共存误接链持续为 0 |
| `config.version` latest-wins | beta | `observe` | 否 | 是，仅 audit；`enforce` 才关链 | 仅可信 `report-version` proof 与 exact coordinate 可授权；灰区并存可见且不建人工队列；`off` 停止新建议和动作 | ADR-0004 两份独立 400 案保持 exact 800/800、eligible 320/320、危险误关链和跨坐标动作均为 0 |
| Typed canonical entity | stable | 写入解析 `on` | 否 | 是 | 无 proof、跨类型同名或多 active alias 时保持 nullable legacy 坐标；不做跨类型合并 | agent/device/environment 跨类型误合并持续为 0，alias 版本、rekey collision 和旧 reader 兼容受回归保护 |
| 价格 canonical target | stable | `enforce` | 否 | 是 | 只接受 qualified code 或唯一 typed alias；target/date/币种/单位缺失时保持 `uncertain` | E6 B 臂 120+ price case 达到 target precision 100%、跨 target supersede 0、missing→uncertain 100% |
| Plan fulfillment | stable | `enforce` | 可选本地 judge 关闭 | 是 | 坐标不全、多逻辑组、overfill 或单位变化时 abstain；只关闭 valid time | E5 确定性 A 臂 143/143，四类 recall、macro-F1、数量守恒均 1.0，错误关闭 0 |
| Lesson signal | beta | `observe` | 否 | 是，仅 qualifier/audit | 保留旧 prompt；observe 不提升 importance/scope，临时与敏感规则优先 | 新 prompt 须在冻结集达到 high precision/recall 与诱饵误报门禁且一般提取下降不超过 1pp |
| LLM Claim 提取 | stable | `fake`（部署推荐显式设为 `llm`） | 是 | 是 | retry 后失败则 Job 失败，可重试；原始 Event 保留；恰好命中 20 条上限时告警但不伪造余项 | compact/legacy schema 共用 AdmissionPolicy；双语复合事实、关系与枚举原子性保持一致；解析、后处理投影、证据与调用可观测性受回归保护 |
| Extraction entailment verification | beta | `off`（可配置 `audit`/`enforce`） | 启用后是 | 是，仅 audit | verifier 失败时 fail-open，保留原始提取结果并记录错误 | 冻结评测集质量稳定、额外延迟与 token 成本达到 SLO 后再考虑真正 enforce |
| Embedding | stable | `fake`（部署推荐显式设为 `real`） | real 模式是 | 是 | compatible/native 调用失败按 retry 策略报错；不写伪向量 | native 默认不传 `text_type`；query/document 可显式启用，sparse/instruct 实验变体默认关闭；维度、provider、重建和失败恢复持续受保护 |
| FTS + Dense + RRF 混合召回 | stable | `on` | Dense 查询本身否 | 是，受控更新访问统计 | 某个可选通道无候选时使用其余通道；核心错误明确失败 | 保持中文/英文、时间、作用域和排序回归 |
| 查询实体约束 | beta | `enforce` | 否 | 否，仅 trace | 仅唯一 typed entity 且 link coverage 完整时在候选截断前限定范围；历史 alias、歧义、多实体、链接不完整或存储异常均宽搜；`observe`/`off` 可显式回滚 | 24 案确定性回归与 Core 召回门禁持续全绿，并用后续真实流量验证跨实体错误率后再晋级 stable |
| 同会话 echo 抑制 | stable | `enforce` | 否 | 否，仅输出 trace/指标 | 缺少同会话证据时不抑制；可显式设为 `observe` 或 `off` | 保持同会话召回完整、跨会话与专名切片零误伤 |
| 风险门控 freshness 提示 | stable | `render` | 否 | 否，仅输出 trace/指标 | 不满足风险门控时不渲染；可显式设为 `observe` 或 `off` | 保持提示有界、重新 packing 和低风险内容零扰动 |
| SQLite 向量后端 | stable | `sqlite_scan` | 否 | `sqlite_vec` 写派生索引 | scan 使用两阶段精确回表；sqlite-vec dirty 时查询回退 scan，启动自动修复投影；缺少显式选择的 extra 时配置报错 | 两后端召回与时间可见性语义持续一致，规模化延迟、投影修复和扩展兼容性受回归保护 |
| Tag soft boost | stable | `on` | 否 | 否 | 无 tag 命中即零 boost | 离线排名不回退并保持可解释权重 |
| 证据关系图 | beta | 确定性边 `on`；LLM 发现 `off` | 发现阶段可选 LLM | 是 | 正式边必须是 `deterministic`、`manual` 或 `approved_proposal`；存量边只读标记 `legacy`；普通召回不做无界遍历 | 来源、双时间、失效、幂等批准与一至两跳收益门禁持续全绿 |
| Reranker | beta | `off`（可配置 fake/on/real） | real 模式是 | 否 | retry/超时后保留融合前排序 | 相关性净增益、P95 和 API 故障率达到 SLO |
| 双时间与作用域过滤 | stable | `on` | 否 | 是 | 不降级；非法时间/作用域明确失败 | 保持历史查询、可见性与并发回归 |
| TTL / decay / archive | stable | `auto` | 否 | 是 | 单 Job 失败可重试，CAS/事务避免部分更新 | 保持扫描完整性、双时间和访问 bonus 回归 |
| Near-copy / semantic dedup | beta | 确定性 near-copy review `on`；LLM dedup `off`；`audit_only=true` | 仅显式启用 judge 时是 | near-copy 只写 pair/review；召回折叠不写库 | typed entity、protected atom、slot/quantity/phase 任一守卫失败即保留独立 Claim；禁用 LLM Job 不构造 Provider | auto floor arm 须达到 precision 100%、Wilson 下界 ≥96%、硬守卫违规 0、回滚 100%，且 recall 无显著退化 |
| 冲突处理 | stable | 确定性 `l0_only`；LLM consolidation `off` | 是，delegation REST；可选 LLM audit | 是 | L0 只执行 37/37 sealed 精确规则；LLM consolidation 只写 `audit_only:<kind>`，不改变 Claim/案卷；灰区留人工处理 | CAS/ledger/rollback 不变量持续全绿，人工裁决接口保持 fail-closed，语义审计保持零状态突变 |
| 删除完整性 | stable | `on` | 否 | 是，主库 + 独立 tombstone sidecar | forget/cleanup/restore 共用删除闭包；账本失败、状态歧义、manifest/ledger 错配时 fail-closed，不静默降级 | P0 状态/共享 Event/关系两端矩阵、幂等 replay、恢复中断续跑和三入口 dangling=0 持续全绿 |
| Episode / Trace | stable | `on` | 否 | 是 | 不影响 Claim 主通道；非法状态转换明确失败 | 保持 API、状态机、reward 与 usefulness 回归 |
| Policy / Procedure 归纳 | beta | 自动发布 `off` | 否 | 显式启用后写 Policy | 默认不从 Episode 自动发布派生策略；禁用 Job 在 handler 前终止 | 多 Episode 支撑、成功率、退役与审计指标达到阈值 |
| Observation / Mental Model 维护 | beta | 确定性 Observation `on`；自动 Mental Model `off` | 否 | 是，仅 Observation | 构建失败保留现有派生记忆并标记维护失败；没有隐藏的 Mental Model 生成器 | 水位幂等、证据覆盖、刷新质量和 stale 恢复达到阈值 |
| MCP Server（stdio） | beta | 按需启动 | 否 | 依工具而定 | 委托 application 服务；预期业务错误返回 `isError=true`，内部异常保留协议级错误 | 工具契约、事务边界和 REST 行为持续一致，Codex/Claude/Cursor 兼容与长时间运行指标达到 SLO |
| Hermes Provider | beta | `off`（`hermes.enabled=false`）；人工冲突提醒 `on` | 是，调用本地 HL-Mem HTTP | 间接写入 | timeout/circuit breaker 后不阻断 Agent 主任务；health 失败不注入过期计数；同 session 仅首次/计数变化提醒 | 兼容矩阵、重连、超时、提醒 no-spam 和长时间运行指标达到 SLO |
| Audit / LLM spans | stable | `on` | 否 | 是 | 关键审计写入失败时明确报错；非关键 span 不改变业务结果 | 保持字段稳定、敏感信息脱敏和可查询性 |
| SQLite 备份与 migration | stable | `on` | 否 | 是 | migration 失败事务回滚；manifest v2/ledger 校验或 replay 失败不替换现有目标 | 空库升级、ledger identity、tombstone replay、历史快照升级与恢复演练持续受门禁保护 |
