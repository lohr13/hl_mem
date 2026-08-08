# T4 / B1 / B2 设计说明

## 目标与边界

本批只完成三个评估前置项：补全基于事件时间的英文时间语义、统一 persona subject，并隔离语言路由与 MemDaily 摄入缓存。保持 `RecallService`、`IngestService` 的公开签名和返回结构兼容，不改提取 prompt、索引默认值、episodic、检索排序或 judge。

## T4：时间语义补全

`relative_time.py` 继续作为不依赖运行时当前时间的确定性后处理器。所有相对表达只接受事件的 `occurred_at` 作为基准；基准缺失或无效时，相对日期和缺年份的绝对日期均不推断。

解析结果沿用 `(occurred_start, occurred_end)`，日期级和周级精度使用半开区间 `[start, end)`：日期的结束为下一天零点，周的结束为下一周周一零点。这样零点表示边界，而不是虚构事件恰好发生在午夜。含显式时分秒的绝对时间仍作为时间点，仅填 `occurred_start`。

支持范围包括：

- `last/this/next week` 和对应中文周表达，按事件时区中的周一至下周一计算；
- `May 20, 2023`、`February 15th`、`3/15/2023`，缺年份时使用事件年份；
- `from ... to ...`、`between ... and ...` 双相对或绝对范围，范围起点取左表达的开始，终点取右表达的结束；
- 月份和年份偏移沿用“目标月最后有效日”钳制，覆盖闰年、2 月和月末；
- 保留事件的固定 UTC offset，跨日、跨月、跨年时不转换为机器本地时区。

多日期 evidence 只有在存在显式范围连接词时才合成区间；否则按文本顺序选择首个可解析表达，防止把同一句中的背景日期误当作结束时间。无效日期被忽略；不回退到提取运行时间。

## B1：subject canonicalization

内置别名新增一组严格 persona 映射：`我`、`本人`、`I`、`me`、`myself`、`user`、`the user`、`用户`、`当前用户` 等统一为 `user`。别名键继续先做 Unicode NFKC、空白折叠和大小写归一，因此覆盖大小写及全半角差异。未列入别名的姓名、产品名和项目名不新增任何模糊规则。

compact 和 legacy 提取结果都在构造 `ExtractedClaim` 前经过别名归一；应用写入边界继续执行一次幂等归一，保护 FakeExtractor 和第三方 extractor。

实体身份仍由数据库中的 `(namespace_key, subject_entity_id)` 组成。`user` 只是 namespace 内的局部 persona 标签；去重、事实哈希查找、冲突键与关系扩展均继续以 namespace 约束。不会把 namespace 编进 subject 字符串，以免改变公开返回值。测试将用两个 namespace 的同一事实验证二者不会去重或冲突合并。

新增一次性 Python data migration，只处理明确 persona 别名。迁移保留 claim ID、状态和 evidence，更新 `subject_entity_id`、`fact_hash`、v3 `conflict_key`，并只替换 `index_text` 的 subject 前缀以保留当前索引模式；FTS 更新由既有 trigger 完成。迁移不尝试合并历史重复 claim，也不对人名或产品名做推断。

## B2：缓存隔离

新增显式 `LANGUAGE_ROUTER_VERSION`，并纳入 `_postprocess_rules_fingerprint()`，确保路由算法修改时通过版本提升改变 `PROMPT_HASH` 和 `LLM_EXTRACTOR_VERSION`。现有依赖提取器版本的调用保持不变。

MemDaily 每个 case DB 增加相邻 manifest，记录 case 输入指纹、当前 extractor model/version，以及影响摄入结果的 embedding/index 配置。真实摄入时把实际 extractor version 写入 claim。

`--skip-ingest` 改为“仅复用有效缓存”：manifest 必须匹配当前身份，且 DB 中已有 claim 的 `extractor_version` 必须全部等于当前版本。manifest 缺失、损坏、身份失配或 DB 版本失配时，安全删除该 case 的 DB/WAL/SHM/manifest 后重新摄入；不会复用旧缓存。空 claim DB 由 manifest 判定。

## 验证

新增或更新表驱动单元测试，覆盖周区间、英文月份、双相对范围、闰年/月末、时区边界、无效基准、多日期消歧、persona 的 Unicode/大小写归一、namespace 隔离、旧库迁移、路由版本指纹和 MemDaily 缓存失配回退。

按用户约束不运行 pytest、benchmark 或 canary。本地只运行 `black --check`、`isort --check-only`、`ruff check` 和 `mypy src/hl_mem/`；pytest 留给 CI。
