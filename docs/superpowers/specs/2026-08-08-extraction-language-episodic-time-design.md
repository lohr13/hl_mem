# 提取侧语言路由、双层记忆与相对时间设计

## 目标与边界

本次只改提取和提取结果的索引表示，不修改召回管线。目标是让英文 evidence 生成英文 subject/value，让一次性但可回答的细节成为有界生命周期的 claim，并让英文相对日期使用事件发生时的对话时间落到 `occurred_start`。`RecallService`、`IngestService` 的公开签名保持兼容，compact LLM 响应继续使用原有 6 字段 schema。

## 语言路由

提取器对每个待提取 chunk 单独检测主要语言。检测只区分中文和英文：比较 Han 字符数与拉丁单词数；无自然语言字符时回退中文，以维持历史行为。中文继续使用更新后的中文 prompt，英文使用独立、自然英文撰写的 prompt。用户消息包装、schema 修复提示也跟随同一语言，避免英文输入仍被中文上下文牵引。

两个 prompt 使用同一 6 字段 JSON 契约，明确要求 subject/value 与 `<extract_from>` 的主要语言一致，专名、数字、单位和时间原样保留。第一人称只在确实指向对话用户时规范为中文“用户”或英文 `user`；其他命名主体保持原样。compact kind 先映射为中性英文 predicate，再交给现有 canonical predicate/attribute 层处理，避免在语言敏感的输出表示中直接注入中文标签。

提取版本指纹同时覆盖中英文 prompt、语言路由常量和后处理规则，任一侧改变都会更新 extractor version。

## Durable / episodic 双层提取

不新增数据库列或 API 字段。两层复用现有 claim 表示：

- durable：身份、偏好、配置、已采用选型、架构和其他稳定事实，保持现有 scope/volatility 规则；
- episodic：有证据的一次性事件及其数字、时间、地点、专名和耗时细节，编码为 `scope=temporal`、`volatility=ephemeral`、`importance=0.3`。

prompt 不再要求跳过所有 low notability；low 改为 episodic 标记。确定性准入仍拒绝空值、秘密、无法定位的 evidence、纯数字孤值和运行/CI/健康快照。被接受的 low candidate 使用独立原因码 `accepted_episodic`，便于审计。compact schema 的总上限仍为每 chunk 10 条，不增加 LLM 调用或输出字段。

episodic claim 走现有低 importance TTL（默认 3 天），但 ephemeral 的 TTL 锚点改为 `recorded_from`；这避免导入历史对话时 claim 在刚写入就因旧 `occurred_at` 立即过期。durable/temporal stable claim 继续以 observed time 为锚点，不改变既有语义。

## 英文与混合相对日期

新增无外部依赖的纯解析模块。它只在提供合法 `occurred_at` 时解析相对日期，绝不使用提取运行时间。解析结果保留事件时间的时区并归零到当地日开始。

支持：

- 中文今天/昨天/明天/前天/后天、上周/下周、`三个月前` 等；
- 英文 today/yesterday/tomorrow、last/next week、`three months ago`、`in two days`；
- `last Friday`、`next Friday`、`this Friday`；
- 同一 evidence 中的中英文混合信号，按文本中最先出现的有效日期选择；
- 既有 ISO/中文绝对日期及双日期区间。

月/年位移按日历计算并夹紧月末，例如 5 月 31 日的 three months ago 为 2 月末。

## 索引表示与迁移

默认 `index.text_mode` 从 `legacy` 改为 `natural`，默认投影版本升为 `v2`。natural 仅拼接 `subject：value`，不把内部中文 predicate、slot 或英文 tags 注入 FTS/embedding 文本。显式配置为 legacy/value_only/answerable 的部署保持原行为。

既有数据库不做启动时远程 embedding 写入。复用安全、可续跑、带完整性检查的 `backfill-index-text` 命令，并把 CLI 的 `--mode` 选择扩展到全部已支持模式。管理员先执行 `--mode natural --dry-run`，再显式执行写入回填；这会同步 index_text、FTS 和 dense embedding。

## 测试与验证

新增/更新单元测试覆盖：逐 chunk 语言 prompt 路由、source-language subject/value 保留、英文 user 规范化、episodic 准入与 scope/TTL、英文和混合相对日期、natural 默认及 CLI backfill 选择。由于本机 Windows 约束，不运行 pytest、benchmark 或 canary；红绿行为用直接 Python unittest/断言探针验证，最终运行 black、isort、ruff、mypy 静态检查。
