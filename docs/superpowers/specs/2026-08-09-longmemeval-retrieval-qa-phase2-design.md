# LongMemEval 检索/QA 侧优化（阶段 2）设计

## 目标与边界

本阶段只修改 LongMemEval benchmark runner 的 reader 上下文、reader 提示词和 reader/judge HTTP 重试。保持 `RecallService`、生产 claim/evidence schema、提取器、MemDaily 与 PerLTQA 不变；不调整索引默认值、episodic、reranker 权重或 judge 模型与题型规则。

本地不运行 benchmark、canary 或 pytest。行为测试随代码提交，由 GitHub CI 首次执行；本地只运行 Black、isort、Ruff、mypy 与无执行副作用的 Python 编译检查。

## Query-aware turn window

现有 evidence link 只保存 claim 到 event 的关联，没有持久化提取时的 `evidence_quote` 或字符 span。为避免触碰提取侧，runner 从 event 的 `content_json.messages` 恢复 turns，并按关联到该 event 的 retrieved claim 文本、claim value 和当前问题执行确定性匹配。

匹配分数结合规范化子串、词/CJK bigram 覆盖率和字符串相似度。claim value 权重最高，claim 文本其次，问题用于消歧。每个 event 选择最高分 turn，reader evidence 以匹配 turn 为首，随后带上有明确索引和角色的前、后相邻 turn；这样匹配原句不会因前置长对话被 head truncation 淘汰。event 缺少结构化 messages 时退化为旧的 session 文本。

runner 新增 `--reader-context-mode {windowed,head}`，默认 `windowed`。`head` 复用原来的 event 从头截断行为。模式写入结果报告，resume 和 shard merge 拒绝混用模式，从而保证 A/B 可归因。

## 预算与上下文结构

继续使用 `QA_CONTEXT_TOKEN_BUDGET=6000`、`QA_EVIDENCE_EVENT_TOKEN_LIMIT=1200` 和 claim 字段上限。claim 元数据先进入 prompt；event 按 retrieved rank/evidence 顺序去重加入。windowed evidence 先缩小到最多三个 turns，再经过单 event 和总预算拟合；预算不足时停止追加后续 event，避免上下文膨胀与 lost-in-the-middle。

每条 windowed event 附带 `window` 诊断元数据，包括匹配 turn、相邻 turn 索引、总 turn 数及匹配分数。匹配 turn 内容排在 event content 首部，相邻 turns 带 `previous`/`next` 标签，因此即使需要二次字符截断也优先保留 answer-bearing turn。

## Reader 证据合成

reader system prompt 要求在内部执行 Chain-of-Note 风格的四步合成：逐条提取候选答案和精确 relation；标注计划/意图/试镜等非完成状态；按发生、有效和记录时间消解更新；比较候选并选择与问题 relation 完全一致的答案。

提示词明确禁止把“为剧试镜”等同于“参加该剧”、把地点/耗时/距离互换、把计划等同于已执行。多条证据可以进行确定性共指、算术、比较和日期推理，但缺少实体、金额、地点、日期或数量时不得猜测。最终只输出简洁答案；证据确实不足时保留 unavailable 分支。现有通用、temporal、knowledge-update、preference 和 abstention judge 分支保持原样。

## 超时重试

现有 `_qa_call_with_retry` 的 429/5xx 与 `Retry-After` 行为不变，并把直接或异常链中的 `httpx.ReadTimeout`、`httpx.ConnectTimeout` 纳入同一个有限重试循环。默认总尝试次数仍为 3，即最多 2 次指数退避，其他 4xx、解析错误和非指定 transport 异常立即抛出。

## 测试与兼容性

单元测试覆盖：后部命中 turn 及相邻窗口、head A/B、无 messages 回退、总/单 event 预算、CLI/报告/resume/merge 模式身份、ReadTimeout/ConnectTimeout 重试、非重试异常，以及 reader prompt 的 relation/state 合成与 abstention 要求。所有新增 runner 参数均有默认值，既有内部调用保持可用；生产 API 和返回结构不变。
