# Trigram FTS5 提交审查

审查对象：`11f2ec47316b203f6ea97a75a19d59357b1481f2`

结论：**需要修改后再发布**。migration 的主体设计正确，且 514 条量级的性能没有问题；但共享的
`sanitize_fts_query()` 被按 trigram 语义全局改写后，同时影响了仍使用 `unicode61` 的 `events_fts`。
当前清洗不能安全地把任意用户文本转换为 FTS5 字面量查询，合法的标点查询会静默变成 0 recall，
部分未清除操作符还会改变查询语义。另有配置可观测性和回归测试缺口。

## 1. Migration 正确性：5/5

### 检查结果

- `claims_fts` 仍声明 `content='claims'` 和 `content_rowid='rowid'`，保留了 external-content 模式。
- `claims_ai`、`claims_ad` 与 migration 002 的最终文本一致；`claims_au` 与 migration 005
  收窄后的 `AFTER UPDATE OF subject_entity_id, predicate, value_json` 版本一致。
- `claims_tags_fts` 的列、external-content 配置、三个触发器及回填与 migration 018 一致，
  仅增加 `tokenize='trigram'`。
- 两个回填均显式写入 `rowid`，并使用与触发器一致的 `COALESCE`/拼接表达式，正确。
- `Database._migrate()` 对 `migrations/*.sql` 排序并自动执行，`022_fts_trigram.sql` 无需手工注册。
- 实测从 001–021 schema 写入存量 claim 后启动当前 `Database`：022 被登记，claims 原表行数不变，
  `claims_fts` 与 `claims_tags_fts` 均可命中中文，六个触发器存在。

### 数据与并发判断

不会丢失 `claims` 表数据。`Database._migrate()` 将整个 022 放在单一 `BEGIN IMMEDIATE` 事务内，
因此其他连接不会看到“表已删除但尚未回填”的已提交中间状态：已有读事务可继续读旧快照，新写入会在
迁移期间阻塞，提交后一次性看到新 schema/index。真正的生产风险是启动时间、写阻塞、构建新索引所需
的临时磁盘/WAL 空间，而不是半成品索引对外可见。

### 建议

- 增加一条正式的 upgrade migration 测试：构造 021 数据库、写入存量 claims、应用 022，断言原表
  行数、两张 FTS 命中、trigger SQL 和 `schema_migrations`。当前提交只更新默认值测试，没有覆盖
  最关键的生产升级路径。
- 大库部署前记录 migration 耗时与磁盘余量；当前 514 条无需专门做在线双写迁移。

## 2. `sanitize_fts_query` 安全性：2/5

### 发现的问题

1. **高优先级：共享函数破坏 `events_fts` 的 unicode61 查询。**
   `src/hl_mem/storage/events.py` 和 `claims.py` 共用该函数，但 022 只把两张 claims FTS 表切为
   trigram；`events_fts` 仍是 migration 001 的默认 unicode61。旧实现逐 token 双引号转义，
   新实现返回裸表达式，不能作为两种 tokenizer 通用的字面量编码。
2. `_FTS5_OPERATORS` 不完整。FTS5 语法还包含前缀 `*`、首 token `^`、phrase 连接 `+`、
   column-set `{...}`、负 column filter `-` 等；更根本的是，FTS5 bareword 只允许 ASCII
   字母/数字/下划线和非 ASCII 字符，`.`、`/`、`-`、`[`、`]` 等普通用户标点也必须被引用。
   “删除已知操作符”不是完备的 escaping 策略。
3. 实测当前清洗结果：
   - `foo-bar` 触发 `no such column: bar`；
   - `abc.def`、`C++`、`a/b` 触发 FTS5 syntax error；
   - `foo*`、`^foo` 保留并启用 prefix/initial-token 语义；
   - `{x y}:foo` 清洗后仍可触发 column 解析错误；
   - 单独的 `AND` 未被删除（正则要求两侧空白），会产生语法错误。
   repository 会把部分此类错误捕获成空列表，所以 API 多数情况下不崩溃，但合法查询会被静默降为
   0 recall。
4. `AND|OR|NOT|NEAR` 使用 `IGNORECASE`，而 FTS5 的 boolean bareword 是大小写敏感的。
   当前实现会把用户文本中的小写 `and/or/not/near` 也删除，造成不必要的信息损失。
5. `len(cleaned) < 3` 统计的是 Python code point，并未按每个被解析出的 trigram phrase 判断。
   例如长查询中混有一个两字符片段时，整体长度检查通过，但该片段仍不会产生可匹配 trigram。

### 空查询与注入判断

- 在项目当前 SQLite 上，trigram 表执行 `MATCH '""'` **不报错且返回空集**；官方文档也说明少于
  3 个 Unicode 字符的 full-text query 不匹配任何行。因此该 sentinel 本身可用。
- SQL 参数使用 `?` 绑定，没有 SQL 注入风险。存在的是 **FTS 查询语言注入/语义操纵**：
  未清除的 `*`、`^`、`+` 等可改变 MATCH 含义；冒号虽被删除，但当前策略仍不完备。
- 连续三字以上的纯中文能正常工作；简单英文和中英混合也能工作，但带常见标点、技术名词
  （如 `C++`、路径、hyphenated word）的查询不可靠。

参考：[SQLite FTS5 query syntax 与 trigram tokenizer 官方文档](https://www.sqlite.org/fts5.html)。

### 建议修复

- 不要通过“枚举并删除操作符”生成字面量查询。实现一个统一的 FTS5 phrase quoting helper：
  将用户片段放入双引号，并把内部 `"` 写成 `""`；根据产品语义决定是把整个查询作为连续 phrase，
  还是按空白切分后对每段分别 quote 并做隐式 AND。
- claims trigram 与 events unicode61 若需要不同的分词/短查询策略，应拆成明确的 sanitizer，
  或让调用者传入 tokenizer/query mode；不能让全局 helper 假定所有 FTS 表都是 trigram。
- 增加参数化测试，至少覆盖中文、英文、中英混合、空白、1/2 字符、`C++`、`foo-bar`、路径、
  email/版本号、双引号及全部 FTS5 语法字符，并分别验证 claims 与 events 查询。

## 3. 向后兼容性：2/5

### 发现的问题

- 旧 sanitizer 对每个空白 token 做双引号转义，保证每段按字面量交给 tokenizer；新实现删除引号并
  暴露 FTS5 grammar。此前依赖该保护的标点查询和 events 查询发生兼容性回退。
- 旧实现并不是把整个输入包装为一个连续精确 phrase，因此普通 `"likes tea"` 的既有语义主要是
  两个字面 token 的 AND；该简单用例仍通过。但标点、显式双引号及用户本来输入的 FTS 关键字，
  语义会改变。
- tokenizer 改变后 BM25 仍可调用且排序方向不变，但统计单元从词变为重叠 trigram，document
  length、term frequency、稀有度均变化；旧 BM25 分数和候选顺序不可视为兼容。后续 RRF 只依赖
  顺序，因此仍会间接受影响。
- 当前单元测试全部通过（`294 passed, 1 warning`），但这主要说明测试未覆盖回退：
  `test_claim_fts_special_characters_do_not_raise` 只断言最终为空，会把“syntax error 被捕获”为成功；
  没有 events FTS 标点回归、trigram 中文正例、短查询或 BM25 顺序测试。
- `test_fts_tokenizer_reads_environment_override` 把环境变量设为 `trigram`，恰好等于默认值，不能证明
  override 生效。应设置为另一个允许值（例如 `unicode61`）再断言。

### 建议修复

- 修复 sanitizer 后补充上述回归测试，并把特殊字符测试从“不得抛错”提升为“应该命中字面内容”。
- 为中文 substring、英文多词、中英混合、技术标点各加正例；对 BM25 至少加一条相对排序契约测试，
  不要断言具体浮点分数。
- 如果不承诺旧排序完全稳定，应在 release notes 明确 tokenizer 切换会改变 FTS/BM25 候选排序。

## 4. 性能影响：4/5

### 检查结果

用 514 条中英混合合成 claims、相同 external-content 配置做本机微基准：

| tokenizer | FTS 增量页大小 | 查询延迟中位数 | 观察 |
|---|---:|---:|---|
| unicode61 | 57,344 B | 0.089–0.100 ms | 中文连续子串为 0 hit |
| trigram | 253,952 B | 0.102–0.176 ms | 中文/混合查询正常命中 |

该样本中 trigram FTS 页占用约为 unicode61 的 **4.4 倍**，查询仍明显低于 1 ms。微基准不是生产
容量预测，但足以说明 514 claims 下性能完全可接受，同时“索引显著变大”应被纳入容量规划。

### 发现的问题

- 022 同一事务内 drop/recreate/backfill 两张索引，数据量大时会延长服务启动并阻塞写入。
- SQLite 文件中旧 FTS 页删除后成为 freelist，文件物理尺寸不一定自动缩小；迁移峰值空间和迁移后
  文件大小都可能高于仅看新索引的估算。
- 提交没有提供真实 514 claims 库上的 migration 时间、迁移前后页数或 recall/latency 基线。

### 建议

- 当前规模可直接迁移；发布前在生产库副本记录 `page_count`、`freelist_count`、迁移耗时和典型查询
  p50/p95。
- 若未来达到大库规模，再设计维护窗口或影子索引切换；不建议为当前 514 条过度设计。
- 若空间成为问题，可评估 trigram 的 `detail=column`/`detail=none`，但官方限制长于 3 字符的
  full-text query，且会影响 phrase/NEAR/BM25 相关能力，不能在本提交中贸然启用。

## 5. 遗漏检查：2/5

### 发现的问题

- `events_fts` 是最重要的遗漏引用：它继续使用 unicode61，却受全局 sanitizer 修改影响。
- `Settings.snapshot()` 未包含 `fts_tokenizer`；`/healthz` 虽返回 snapshot，当前无法观察该配置。
- `fts_tokenizer` 只改变 Settings 默认值，实际 migration 仍硬编码 `tokenize='trigram'`。
  `HL_MEM_FTS_TOKENIZER=unicode61` 不会改变已建 FTS 表。这一字段目前更像声明/占位，而不是有效的
  runtime 开关，命名和 override 测试容易造成误解。
- `docs/CHANGELOG.md` 未记录 022、中文 recall 修复、索引空间/排序变化和迁移写阻塞。
- 提交还包含大量与所述修复无关的 review 文档和诊断/清理脚本（总计 15 个文件、约 2342 行新增），
  不符合“一次提交只做一件事”，也增加发布审计面。本报告按要求只深入审查四个目标文件，但建议
  发布前拆分提交。

### 建议修复

- 在 `Settings.snapshot()` 增加 `fts_tokenizer`，并更新 healthz 测试。
- 明确配置契约：如果它只是 backend capability/观测字段，文档应说明不能在线切换 SQLite schema；
  如果它应控制建表，则需要设计 migration/schema 一致性校验，不能仅改默认值。
- 更新 `docs/CHANGELOG.md`，记录 migration 022、三字符下限、BM25 排序变化、索引增大和部署注意事项。
- 将无关脚本与审查材料拆到独立提交。

## 发布判定

**不建议按当前提交直接发布。**

发布前至少完成：

1. 修复/拆分 sanitizer，确保 claims trigram 与 events unicode61 都对任意用户字面文本安全；
2. 增加 claims/events 的中文、混合、标点、短查询及 migration upgrade 回归测试；
3. 将 `fts_tokenizer` 加入 healthz snapshot，并澄清环境变量是否真正控制 schema；
4. 更新 CHANGELOG。

完成以上项目后，migration 022 本身可以发布；当前 514 claims 的空间与延迟成本可接受。
