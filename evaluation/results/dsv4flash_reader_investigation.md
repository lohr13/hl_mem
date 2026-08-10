# deepseek-v4-flash reader 失败原因调研

日期：2026-08-10
范围：LongMemEval holdout50 中“证据已经进入 reader prompt，但 reader 仍答错”的样本。本文不修改评测数据集，也不把未经验证的模型内部实现当作事实。

## 结论摘要

1. **确证：三个严格成对样本均受 thinking 开关显著影响；高可信假设：它是整批回退的关键配置因素。** 在保持模型、endpoint、固定证据、system/user prompt、温度和 2048-token 上限不变的三组对照中，关闭 thinking 的金额、日期差、旧状态答案全部错误，开启后全部正确。扩展到 8 个原失败样本时，开启 thinking 后有 7 个得到正确最终答案；但其余 5 个没有本轮同预算关闭-thinking对照，因此这里只能作为强支持，不能写成严格因果确证。唯一未产出答案的婚礼计数样本以 `finish_reason=length` 用尽 2048 completion tokens。
2. **确证：原配置的 `max_tokens=512` 没有截断关闭 thinking 时的短错误答案。** 三个成对样本在 512 和 2048 下答案完全相同，completion 仅 1--3 tokens，均以 `stop` 结束。因而“512 截掉了原答案”不是这些失败的解释。但开启 thinking 后 token 需求显著增加，2048 在一个真实样本上仍不够。
3. **确证：`json_object` 不是原 reader 失败原因。** 当前 runner 的 reader 调用是纯文本，未传 `response_format`；只有 judge 使用 JSON mode。额外的一次 JSON 对照仍把 `$495` 答成 `$345`，说明格式约束也没有修复该样本。
4. **高可信假设：问题不是基础加减法完全不会，而是长证据中的聚合、去重、时间/状态选择在关闭 thinking 时不可靠。** 两个 75--80 prompt-token 的合成任务在关闭 thinking 时都答对；而真实 prompt 为约 2.9k--4.4k tokens，关闭 thinking 常直接输出 1--3 token 的错误答案，开启 thinking 后产生数百到七千余个 reasoning 字符并大多恢复。
5. **无法确证：所谓“轻量 MoE 架构边界”是根因。** 本次没有找到可核验、且能与当前百炼服务中这个具体模型版本一一对应的公开架构证据；黑盒行为也不能反推出 MoE 路由或参数规模。只能描述观测到的任务边界，不能把内部机制写成事实。

综合判断：qwen3.7-plus 在同证据下恢复 8/9，以及本次开启 thinking 后观测到 7/8 正确，共同强力支持主要问题位于 **reader 模型/推理配置层**，而不是证据是否存在。因只有三例是本轮严格成对对照，这一整批归因仍标为高可信假设。代码侧仍有相邻 turn 丢证等独立问题，已另行修复；两者不应混为一谈。

## 方法与调用控制

- 输入直接复用 `qwen_reader_recheck_9cases.json` 中持久化的 fixed-evidence reader system/user prompt；不重新 ingest、不重新 recall、不改变证据顺序。
- endpoint：百炼 OpenAI-compatible；模型：`deepseek-v4-flash`；温度：`0.1`。
- 对照变量只有 `enable_thinking`、`max_tokens`、一次 `response_format=json_object` 和一次极短 few-shot。
- 完成并记录 20 次 HTTP 调用，全部 HTTP 200。此前有一次 shell 进程在产生任何结果前被本地超时终止；保守地把它视作最多可能发出 1 次请求，因此总上界为 21，低于 30 次约束。
- 不保存或展示 API key、完整 HTTP 请求、thinking 内容；只记录最终答案、token 数量、reasoning 内容字符数和 `finish_reason`。
- 这是小样本、单次采样实验。它能证明本次请求中的开关效应，不能证明所有未来调用都确定复现。

## 真实样本对照结果

`baseline` 指原 reader 配置：thinking 关闭、`max_tokens=512`。表中原 baseline 答案来自既有诊断结果；本次重新调用的三个核心样本与原错误一致。

| case | gold | baseline / 关闭 thinking | 关闭 thinking，2048 | 开启 thinking，2048 | 结论 |
|---|---|---|---|---|---|
| `2b8f3739` 金额 | `$495` | `$345`，stop，2 tokens | `$345`，stop，2 tokens | `$495`，stop，612 tokens | thinking 恢复；提高上限无效 |
| `4f54b7c9` 物品 | `5` | `4` | 未重复调用 | `5`，stop，520 tokens | thinking 恢复 |
| `eeda8a6d` 鱼 | `17` | `16` | 未重复调用 | `17`，stop，1919 tokens | thinking 恢复，且已接近 2048 |
| `gpt4_2f8be40d` 婚礼 | `3` | `2` | 未重复调用 | 空答案，`length`，2049 tokens | thinking 有明显预算风险；本次未证明可恢复 |
| `gpt4_8279ba02` 日期差 | `10` 天 | `3`，stop，1 token | `3`，stop，1 token | `10`，stop，253 tokens | thinking 恢复；提高上限无效 |
| `gpt4_59149c77` 日期差 | `7` 天 | `4` | 未重复调用 | `7`，stop，1015 tokens | thinking 恢复 |
| `gpt4_2c50253f` weekday offset | `6:45 AM` | `7:00 AM` | 未重复调用 | `6:45 AM`，stop，172 tokens | thinking 恢复 |
| `c7dc5443` 状态更新 | `5-2` | `3-2`，stop，3 tokens | `3-2`，stop，3 tokens | `5-2`，stop，593 tokens | thinking 恢复；提高上限无效 |

三个完整成对样本的 prompt tokens 分别为 3595、4421、4035。开启 thinking 后，返回中 `reasoning_content` 的字符数分别为 2338、777、2039；本文没有保存其内容。其余成功恢复样本也观察到 511--7242 个 reasoning 字符。

### JSON mode 与 few-shot

- `2b8f3739` 在关闭 thinking、2048 tokens、JSON mode 下返回 `{"answer":"$345"}`，仍然错误。
- 同一 case 加入“已完成金额求和、重复销售只计一次”的简短异题 few-shot 后仍返回 `$345`。
- 这只能否定“一个很短的通用 few-shot 足以修好该例”，不能否定更精细、覆盖数据形态的 few-shot 可能有效。

此外，当前 runner 的 `_run_qa()` 调用明确使用 `enable_thinking=False` 且没有 `json_object=True`；judge 才使用 JSON object。因此 JSON mode 对历史 reader 错误没有直接因果路径。

## 合成能力探针

为区分“基础算术完全不会”和“长上下文聚合不稳定”，使用两个不含真实用户数据的短任务：

| 任务 | 关闭 thinking | 开启 thinking | gold |
|---|---|---|---|
| 三笔已完成收入求和，含一次 assistant 重复和一笔未来计划 | `$495` | `$495` | `$495` |
| 从最新 `7:00 AM` 基线应用 Thursday 提前 15 分钟 | `6:45 AM` | `6:45 AM` | `6:45 AM` |

因此可以确认该服务并非在关闭 thinking 时连这些基本运算都无法完成。更符合证据的描述是：**在数千 token、含重复和干扰候选的真实 reader prompt 中，关闭 thinking 的可靠性不足。** 这是行为结论，不是模型架构结论。

## 对问题假设的分级

### A. `enable_thinking=false` 是否削弱推理？

**确证（针对三个严格成对的固定 prompt）：开关效应很大。** 三例在关闭 thinking、2048 tokens 时仍输出原错误值，开启 thinking、保持其余条件不变后全部正确。

**高可信假设（针对整批）：`enable_thinking=false` 是关键回退因素。** 8 个原失败样本开启后观测到 7 个正确，但另外 5 个只与既有 baseline 结果比较，且所有配置都只有一次温度 0.1 采样；因此不能把 7/8 直接升级为整批严格因果确证。

**无法确证的部分：** 无法仅凭黑盒响应证明模型内部在关闭时“完全不推理”或“退化为纯生成”。可以确认的是输出行为和服务返回的 reasoning 通道发生了显著变化。

### B. `json_object` 是否限制了 reader 推理？

**确证：不是这次回退的原因。** 失败发生时 reader 根本没有启用 JSON mode。单次格式对照也没有改善答案。

百炼当前模型列表把 `deepseek-v4-flash` 标为支持 thinking，但把结构化输出标为不支持；另一方面，本次兼容接口仍接受了一次 `json_object` 请求并返回合法 JSON。这个差异可能来自兼容层行为或文档/部署版本差异，**无法据此宣称该模型稳定支持 JSON mode**。官方错误码文档还说明 JSON mode 与 thinking 不能同时开启。因此不建议把二者组合成 reader 方案。[百炼文本生成模型列表](https://help.aliyun.com/zh/model-studio/text-generation-model)；[百炼错误码说明](https://help.aliyun.com/en/model-studio/error-code)

### C. 模型本身的算术/多步聚合能力边界？

**高可信假设：存在长上下文聚合可靠性边界。** 证据是短合成任务全对、真实长证据关闭 thinking 多类失败、开启 thinking 大多恢复。

**无法确证：** 不能把边界归因到“轻量 MoE”、具体参数量、训练配方或路由机制。当前 DeepSeek 官方 API 文档也不能证明百炼此模型名背后的具体版本/内部结构与某个公开模型完全相同。

### D. `max_tokens=512` 是否截断？

**确证：没有截断原关闭-thinking错误答案。** 三个重跑样本的 `finish_reason=stop`，completion 仅 1--3 tokens；把上限调到 2048 不改变答案。

**确证：开启 thinking 后，输出预算成为新约束。** 婚礼样本在 2048 下 `finish_reason=length` 且没有最终答案，鱼样本也用了 1919 completion tokens。官方 Chat Completion 文档把 `max_tokens` 定义为最大生成 token 数，并把 `length` 定义为达到最大 token 数；这里与实测一致。[DeepSeek Chat Completion API](https://api-docs.deepseek.com/api/create-chat-completion/)

**无法确证：** 本次没有继续提高婚礼样本的上限，因此不能声称 4096 或 8192 一定恢复。若启用 thinking，应单独评测输出上限，而不能沿用 512。

### E. prompt / few-shot / 其他

- **确证：** 当前 system prompt 已明确要求私下逐条记录、去重、简单算术、日期计算和最新状态选择；不能把失败简单归结为“完全没有任务指令”。
- **确证：** 一个极短通用 few-shot 没有修复金额例。
- **假设：** “只返回最终答案、不要展示分析”与关闭 thinking 的组合，可能鼓励模型直接选择一个候选而没有足够的中间计算；thinking 对照与此相容，但无法从黑盒响应确证内部过程。
- **假设：** 更聚焦的证据预聚合、显式列出候选 ID/状态，或针对性 few-shot 可能降低负担；本次未调用验证，不能写成已证实方案。

## 建议

1. 正式门禁优先继续使用经同证据复核为 8/9 的 qwen3.7-plus reader；这是当前证据最充分、改动最小的选择。
2. 若必须使用 deepseek-v4-flash reader，先启用 thinking，再对 completion budget 做小规模扫描。2048 已被一个样本耗尽，不能直接作为安全上限；具体上限需另测，本文不臆测一个保证值。
3. reader 保持纯文本最终答案即可。不要为了“增强推理”强行改成 JSON object；它不是原问题，且服务文档对该模型的结构化输出支持并不肯定。
4. 在改门禁配置前，至少对这 9 个固定证据样本做 3 次重复，分别报告准确率、`length` 率、completion tokens 和成本。当前一次采样足以定位方向，不足以估计稳定性。
5. 不因这次调研修改生产 dedup 阈值或数据集。claim 膨胀的生产影响尚未建立因果证据，应保持为独立观察指标。

## 证据等级说明

- **确证：** 可由当前代码、持久化 prompt、HTTP 响应字段或严格单变量对照直接复核。
- **假设：** 与观测相容，但本实验没有隔离或公开资料不足。
- **无法确证：** 现有黑盒接口与小样本不能回答；本文明确保留未知，不推断模型内部机制。
