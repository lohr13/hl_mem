# 第二轮架构评审问题核验

评估日期：2026-07-24

评估范围：当前工作树中的实现、生产调用方、测试、项目配置与相关历史文档。

评估方式：只读静态核验；未修改源码，未运行会写数据库的测试。本文是本次评估唯一新增文件。

## 结论摘要

六项意见中，A、B、D、F 都包含真实事实，但严重程度或表述需要校正；C 的代码事实属实，但“不对称就是错误”的结论偏重；E 将领域词汇、自然语言识别词和展示文本混为一谈，不能笼统认定为架构问题。

当前最值得安排的是：

1. 将 `StoreClaimResult` 收敛为普通不可变数据对象，并一次性迁移少量字符串兼容调用；
2. 修正 `IngestService.__init__` 的类型并移除实际未使用的 `embedder` 构造参数；
3. 删除无调用且已明确弃用的 `_link_event_atomically`；
4. 增加一个最小 CI，至少自动执行离线单元测试。

Database 上下文管理器和中文领域词汇均不构成当前高风险问题。前者可做低成本 API 语义清理，后者只有在产品确定支持多语言时才值得进行数据契约级迁移。

## 问题 A：`StoreClaimResult` 继承 `str`

### 1. 是否属实

**属实，但这是有意的兼容设计，不是偶然写法。**

- `src/hl_mem/application/ingest.py:46-59` 的 docstring 明确写着“兼容 claim ID 字符串并暴露写入或拒绝原因”。
- Git blame 显示该类型随 importance 写入门槛一同引入；`claim_id is None` 时用 `skipped:<reason>` 作为字符串值，说明目标是给原有“返回 claim ID 字符串”的接口增加 `status/reason`，同时避免立刻迁移旧调用方。
- 生产调用方 `src/hl_mem/workers/worker.py:320-332` 已经使用 `result.status` 和 `result.reason`，不依赖字符串行为。
- 当前发现的字符串行为依赖主要在测试：
  - `tests/unit/test_pipeline.py:20-31` 直接比较两个返回值，并将结果作为 SQL 参数；
  - `tests/unit/test_pipeline.py:55-68` 将返回值作为 claim ID 查询；
  - `tests/unit/test_hybrid_priors.py:107-111` 将返回值作为 claim ID 查询；
  - `tests/unit/test_concurrency.py:84-101` 比较并发返回值。
  `sqlite3` 能绑定该对象正是因为它是 `str` 子类。
- `store_extracted()` 是应用层静态方法，API/MCP 不直接暴露其返回值。实际生产迁移面比“全项目接口破坏”小。

改为 dataclass 的直接影响集中在 `StoreClaimResult` 定义、`store_extracted()` 的调用方以及上述测试。生产代码中 Worker 无需改变字段访问；测试和潜在内部调用应改用 `.claim_id`。仓库内可见改动属于小范围，但若存在仓库外直接导入这个内部方法的使用者，则会有兼容性破坏。

### 2. 合理性评分

**4/5。** 评审者建议收敛为 dataclass 是合理的。`str` 子类携带可变附加属性形成双重语义：它既像 ID，又像结果对象；`skipped:*` 还是一个并非真实 claim ID 的哨兵字符串，容易被误传给 SQL 或日志。

### 3. 当前场景下的实际风险

**低到中。** 当前生产调用已经按结构化结果使用，主要风险是后续开发者继续把返回值当 ID，忽略 skipped 状态；类型检查器也很难阻止这种误用。该设计暂未造成已知运行错误。

### 4. 处置建议

**应该计划做，适合近期小版本一次性收敛；不是紧急修复。**

### 5. 如果做，具体方案

1. 改为 `@dataclass(frozen=True, slots=True)`，字段为 `claim_id: str | None`、`status`、`reason`。
2. 最好进一步把 `status`、`reason` 收敛为 `Literal` 或 Enum，至少让状态分支可静态检查。
3. 所有 SQL 参数和 ID 比较显式使用 `.claim_id`；skipped 分支先判断状态或 `claim_id is None`。
4. 增加测试，证明 skipped 结果不能被当作 claim ID 使用，并覆盖 inserted/duplicate/skipped 三类结果。
5. 若担心外部兼容，先提供一个明确弃用的 `legacy_claim_id`/转换方法，而不是继续继承 `str`；在下一个破坏性版本移除兼容层。

### 6. 如果不做，理由

只有在 `store_extracted()` 被视为稳定公开 API、且已知仓库外调用广泛依赖字符串返回时，才有理由暂时保留。即使保留，也应补充弃用期和边界测试，不能长期维持双重契约。

## 问题 B：`IngestService.__init__` 类型松散

### 1. 是否属实

**属实，而且问题比“类型写成 Any”更具体。**

- `src/hl_mem/application/ingest.py:95-97` 将 `connection`、`embedder` 都标为 `Any`。
- 项目已有 `EmbedderProtocol`（`src/hl_mem/protocols.py:8-16`），`store_extracted()` 与 `_build_claim_drafts()` 已经使用它，因此 embedder 的 `Any` 没有必要。
- 对 connection，当前没有自定义 Protocol，但实际对象是 `sqlite3.Connection`，仓储构造器也统一接收 `sqlite3.Connection`。这里直接标成 `sqlite3.Connection` 已足够，不必为了抽象而新建 Protocol。
- 更重要的是，`self.embedder` 在构造后没有任何读取。`ingest_event()` 和 `save_explicit_memory()` 只写 Event/Job；真正需要 embedder 的 `store_extracted()` 是静态方法并显式接收 embedder。因此当前构造函数注入了一个未使用依赖。
- 构造函数的简单本身不是缺陷。事务边界由应用服务持有，仓储按方法临时构造，与项目现有分层一致。没有证据表明它缺少必须注入的 repository、settings 或 transaction manager。

### 2. 合理性评分

**4/5。** “init 有问题”方向正确，但应聚焦为“类型可精确化 + embedder 是幽灵依赖”，而不是认为构造函数太简单或需要增加更多依赖。

### 3. 当前场景下的实际风险

**低。** 主要是静态检查失效、读者误判职责，以及调用 API/MCP 时被迫构造并传入无用的 embedder。运行行为目前不受影响。

### 4. 处置建议

**应该现在做或与 A 一并做。** 这是低侵入清理。

### 5. 如果做，具体方案

1. 将构造函数改成只接收 `connection: sqlite3.Connection`。
2. 删除 `self.embedder`。
3. 更新 `api/server.py`、`mcp/server.py` 和相关测试中的 `IngestService(connection, embedder)` 为 `IngestService(connection)`。
4. 保持 `store_extracted(..., embedder: EmbedderProtocol, ...)` 的显式参数不变，因为 claim 写入阶段确实依赖它。
5. 不额外引入 repository factory 或 service container；当前规模下那会增加无收益抽象。

如果产品希望让 service 实例统一负责“事件接收到 claim 持久化”的完整流程，则另一种方案是保留 `EmbedderProtocol` 注入，并把 `store_extracted()` 改为实例方法。但这会扩大职责和改动面，当前没有必要。

### 6. 如果不做，理由

可因变更窗口很小而暂缓，因为没有运行风险；但没有长期保留未使用依赖和 `Any` 的架构理由。

## 问题 C：`Database.__enter__` / `__exit__` 语义不对称

### 1. 是否属实

**代码事实属实，定性为错误则证据不足。**

- `src/hl_mem/storage/database.py:144-148` 的 `__enter__` 返回 `open_worker()` 的专用连接，`__exit__` 调用 `close()`。
- `close()`（第 130-142 行）确实关闭该 `Database` 实例创建的全部连接，包括 worker 连接、从池中借出或已归还的请求连接，并清空池。
- 但 `with Database(...) as connection:` 的常见所有权解释是：上下文拥有整个 Database 管理器，退出时销毁管理器拥有的全部资源。按这个解释，关闭全部连接并非逻辑错误。
- 真正不清晰之处是 `__enter__` 返回了底层 worker connection，而不是 `Database` 自身；调用者看不到“退出会关闭整个管理器”的语义。如果同一个 `Database` 在进入上下文前已建立池连接，退出会一并关闭它们。
- 当前生产和测试没有发现 `with Database(...)` 或 `with database` 的调用；活跃代码使用的是语义明确的 `with database.connect() as connection:`。因此这组魔术方法目前基本是未使用 API。

### 2. 合理性评分

**3/5。** 对“API 容易误解”的指出合理；断言它必然不对称、必须只关闭 worker connection 则不充分。资源管理器退出时关闭其全部所有资源是可接受语义。

### 3. 当前场景下的实际风险

**低。** 当前无活跃调用。未来若有人混用 `Database` 上下文和连接池，可能意外关闭仍在使用的其他连接，尤其在并发请求中表现为难定位的 `Cannot operate on a closed database`。

### 4. 处置建议

**计划做或暂缓均可，优先级低。** 推荐在下一次 Database API 清理时处理，不需要单独发版。

### 5. 如果做，具体方案

首选删除未使用的 `Database.__enter__/__exit__`，强制调用方二选一：

- 请求级连接使用 `with database.connect() as connection:`，退出后回池；
- worker 生命周期显式使用 `open_worker()`，最终由应用生命周期调用 `database.close()`。

如果必须保留 `with Database(...)`，则建议 `__enter__` 返回 `Database` 自身，并把“退出关闭本实例全部连接”写入 docstring；调用者显式调用 `.open_worker()` 或 `.connect()`。不建议让 `__exit__` 只关闭返回的 worker connection，因为这会让 Database 自身和池中连接的所有权更加含混。

### 6. 如果不做，理由

没有调用方，当前 `close()` 与 Database 作为资源所有者的语义也自洽。只要团队约定使用 `connect()` 和应用生命周期 `close()`，实际风险很低。

## 问题 D：死代码——“link event automatically” 弃用

### 1. 是否属实

**部分属实，评审表述把两个函数混在了一起。**

- `_link_event()`（`src/hl_mem/application/ingest.py:478-490`）有五个活跃生产调用，分别处理精确重复、entails、语义重复、并发重复和新 claim 的 evidence link。它不是死代码。
- `_link_event_atomically()`（第 493-503 行）只有定义，没有生产或测试调用。docstring 明确写着“已弃用：在独立事务中关联事件证据”。它是死代码。
- 代码和注释中没有找到字面文本 “link event automatically”。最接近的内容是 `_link_event_atomically`；评审者很可能把 `atomically` 误写成 `automatically`。
- `docs/refactor-phase13-batch1a-task.md:161` 也明确记录：该函数已不再需要，因为 `_link_event` 已在外层单一事务内调用。

### 2. 合理性评分

**4/5。** 若意见是删除 `_link_event_atomically`，完全合理；若意见是 `_link_event` 也是死代码，则是误判。

### 3. 当前场景下的实际风险

**低。** 弃用 wrapper 不会影响运行，但保留“自行开启事务”的旧路径容易诱导未来调用者破坏当前单一 `BEGIN IMMEDIATE` 的事务所有权。

### 4. 处置建议

**应该现在做。** 删除一个无调用私有函数，改动极小且能减少错误范式。

### 5. 如果做，具体方案

1. 删除 `_link_event_atomically()`。
2. 保留 `_link_event()` 及其 `commit=False` 的现有调用。
3. 运行 ingest、并发去重和 evidence link 相关测试，确认单事务路径不变。
4. 历史任务文档无需篡改；它正好解释了删除原因。

### 6. 如果不做，理由

只有正在进行的跨分支迁移仍引用它时才应短暂保留。当前仓库没有这种证据。

## 问题 E：领域逻辑硬编码中文字面量

### 1. 是否属实

**存在大量中文字面量，但“领域逻辑不应硬编码中文”的笼统结论不成立。不同字符串承担不同职责。**

1. `domain/recall.py` 中“如何/怎么/去年/历史”等是自然语言路由词典，是中文查询识别规则，不是展示文本。面向中文查询时，某种语言词表必然存在；问题是覆盖率和可扩展性，而不是它出现在领域模块。
2. `SLOT_REGISTRY` 的 `name`（例如 `preference.ui_theme`）才是稳定、语言中立的机器标识。`description`、`examples`、部分 `aliases` 是提供给中文 LLM prompt 的语义说明；它们接近模型配置/自然语言资源。
3. `SlotDefinition.predicate` 的“偏好/使用/状态”等不是单纯展示文案，而是当前持久化 claim 契约中的 canonical predicate。它们用于：
   - `PREDICATE_ATTRIBUTE_MAP` 和属性推断；
   - `normalize_predicate()` 的中英文 alias 归一化；
   - `ConflictResolver` 的状态变化规则；
   - preference recall boost；
   - LLM extractor 的允许值与默认值；
   - 测试、评测数据和 migration snapshot。
   因此把它们替换成英文不是 UI 翻译，而是数据模型迁移。
4. `ConflictResolver` 中 `{"偏好", "状态"}` 使用的是经过 `normalize_predicate()` 归一化后的 canonical 值，逻辑与当前契约一致；但直接重复字面量可维护性一般，使用 Predicate Enum/常量会更稳。
5. `subject: str = "用户"` 是默认领域实体标识，不是纯展示文本。`normalize_entity_id()` 会对 subject 做归一化，它最终进入 fact hash、conflict key 和持久化字段。改变默认值会影响去重和历史实体连续性。

### 2. 合理性评分

**2/5。** 评审者发现了真实的语言耦合，但将中文本身视为不合理硬编码是误判。对一个明确面向中文、local-first、单用户的记忆系统，中文 query markers、prompt 描述和默认用户实体都合理。真正值得指出的是 canonical predicate 和默认 subject 同时承担“自然语言标签”和“稳定机器标识”，这会增加未来国际化成本。

### 3. 当前场景下的实际风险

**当前低，未来多语言场景中高。**

- 当前中文产品中，规则透明、可测试、无需外部分类器，风险主要是关键词召回规则覆盖不足。
- 若直接接受英文或其他语言，`route_query` 的意图识别会漏判；虽然 predicate 有部分英文 alias，整体 prompt、规则、测试和默认 subject 仍以中文为中心。
- 若未来把 canonical predicate 从中文改成代码值，历史 claim 的 fact hash、conflict key、FTS/embedding 文本、去重和 migration snapshot 都会受到影响，不能做简单替换。

### 4. 处置建议

**当前暂缓全面国际化；计划做低侵入的语义常量收敛。**

现在可做但非必须：

- 为 canonical predicate 定义稳定常量或 `StrEnum`，消除 `conflicts.py`、`staged_pipeline.py`、extractor 中的重复字面量；
- 把 query marker 和 predicate alias 集中到明确的语言资源模块，并保持默认中文；
- 为现有中文行为增加参数化测试。

只有产品路线明确要求多语言时，才启动完整国际化迁移。

### 5. 如果做，具体方案

完整国际化不能只把字符串搬到配置文件，应分阶段：

1. 定义语言中立的机器谓词（如 `preference`、`usage`、`state`）和稳定 subject ID；展示名称与语言资源分离。
2. 保留 `normalize_predicate()` 对历史中文值和英文 alias 的兼容映射，新写入统一使用机器值。
3. 对 `route_query`、`route_recall_intent`、query tags、attribute hints、regex 和 LLM prompt 建立按 locale 选择的资源包。
4. 新增 migration/backfill，处理历史 predicate、默认 subject、fact hash、conflict key 和需要重建的检索数据；migration snapshot 保持不可变。
5. 评估 embedding 文本变更是否要求全量重嵌入；不能让新旧记录长期处在不同文本构造规则下。
6. 扩展中英文提取、冲突、偏好、历史查询和 recall eval 数据集，进行双语回归。

影响范围是**大**：至少涉及 `domain/claims/attributes.py`、`domain/recall.py`、`domain/claims/query_tags.py`、`domain/claims/conflicts.py`、`ingest/llm_extractor.py`、`application/ingest.py`、`recall/staged_pipeline.py`、迁移/回填、测试与评测数据。它是数据契约演进，不是文案整理。

### 6. 如果不做，理由

当前产品定位、默认模型提示和测试语料均以中文为主；语言耦合没有造成当前功能错误。现在做全面国际化会引入高侵入数据迁移、重嵌入成本和回归面，却没有已声明的产品需求。

## 问题 F：缺少 CI / GitHub Actions

### 1. 是否属实

**属实。**

项目根目录没有 `.github/`，`git ls-files` 也未找到 GitHub Actions、GitLab CI、Azure Pipelines 或 Jenkins 配置。`pyproject.toml` 已定义 pytest/coverage 开发依赖，但没有自动执行入口。

### 2. 合理性评分

**4/5。** 对 solo、本地单机部署项目，CI 不是运行系统的必要组件；但该项目已有 284 个测试、21 个 migration、多个事务与并发路径，且仓库托管地址是 GitHub。CI 的收益已经明显高于维护成本。

### 3. 当前场景下的实际风险

**中。** 风险不是服务在线可用性，而是提交/合并时漏跑测试、Windows 本地环境掩盖跨平台问题、lockfile 或 migration 破坏未被及时发现。solo 项目同样会受到上下文切换和本机环境漂移影响。

本地单机部署降低了部署流水线、容器发布和多环境矩阵的必要性，但不降低自动回归测试的价值。

### 4. 处置建议

**应该现在做最小 CI。** 暂不需要 CD、真实 API 测试、GPU 测试或复杂矩阵。

### 5. 如果做，具体方案

最小可行 GitHub Actions：

1. 触发：`push` 到主分支和 `pull_request`。
2. 环境：先用 `windows-latest` + Python 3.11，与实际 Windows/SQLite 运行环境对齐；若希望验证可移植性，再增 `ubuntu-latest`，不要一开始扩张矩阵。
3. 使用官方 Python/uv 安装方式和 `uv sync --locked --dev`，确保 lockfile 可复现。
4. 运行项目已声明的离线单元测试命令：`.venv/Scripts/python.exe -m pytest tests/unit/ -q --tb=short`；在跨平台写法中可用 `uv run pytest tests/unit/ -q --tb=short`。
5. 设置 `HL_MEM_ENV=test`、fake embedder/reranker/extractor，禁止依赖真实 API key 和网络模型调用。
6. 增加一个最轻量的包/import 检查（例如构建 wheel 或导入 `hl_mem`）。格式化、lint、coverage gate 可在规则稳定后再加，避免首个 CI 同时改变质量政策。
7. 使用并发取消旧运行和依赖缓存即可；不在 CI 中启动生产服务、不读 `.env`、不上传本地数据库。

### 6. 如果不做，理由

只有仓库完全不进行远程协作、所有变更均由单一开发者严格执行本地测试、且不把 GitHub 分支作为质量门禁时，才可暂缓。即便如此，随着测试和 migration 数量增加，手工纪律的可靠性会持续下降。

## 汇总表

| 问题 | 代码事实 | 评分 | 当前风险 | 建议时机 | 判定 |
|---|---|---:|---|---|---|
| A `StoreClaimResult(str)` | 属实；为旧 claim ID 返回契约做兼容，测试仍依赖字符串，生产 Worker 已用结构化字段 | 4/5 | 低到中 | 计划近期做 | **真实设计债务，不是无意错误** |
| B `IngestService.__init__` | 属实；已有 `EmbedderProtocol`，connection 可用 `sqlite3.Connection`，且构造器 embedder 完全未使用 | 4/5 | 低 | 现在或与 A 一起做 | **新确认的真实问题：幽灵依赖 + Any** |
| C Database 上下文不对称 | `enter` 返回 worker、`exit` 关闭实例全部连接属实；但资源所有权语义可自洽，当前无调用 | 3/5 | 低 | 计划做/暂缓 | **部分合理，定性为错误偏重** |
| D link event 死代码 | `_link_event` 活跃；只有 `_link_event_atomically` 无调用且明确弃用；不存在 “automatically” 原文 | 4/5 | 低 | 现在做 | **部分真实：wrapper 是死代码；整体说法误判** |
| E 中文字面量 | 存在且广泛使用；既有语言资源，也有持久化领域契约，不能统一视为展示文案 | 2/5 | 当前低，国际化时高 | 常量收敛可计划；全面 i18n 暂缓 | **当前问题基本误判，但揭示未来语言耦合成本** |
| F 无 CI | 属实；无任何 CI 配置 | 4/5 | 中 | 现在做最小 CI | **真实工程保障缺口，非部署阻塞项** |

## 最终优先级

1. **低成本立即清理**：B 的类型/未使用依赖、D 的弃用 wrapper。
2. **近期契约收敛**：A 改普通结果对象并迁移字符串调用。
3. **工程保障**：F 增加只跑离线单测的最小 CI。
4. **随 Database API 整理处理**：C 删除未使用魔术方法或明确所有权语义。
5. **由产品需求触发**：E 先集中常量和语言资源；没有多语言目标时不做数据契约级国际化。
