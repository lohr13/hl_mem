# SQLite 连接生命周期与退出阶段诊断

- 日期：2026-08-31
- 状态：Core 1.0 后续专项；不阻塞 Phase 1

## 结论

成熟实现普遍把连接正确性放在运行生命周期内保证：显式所有权、作用域关闭、应用停机释放，以及长时间占用告警。垃圾回收或解释器最终析构产生的警告适合作为诊断兜底，不适合作为跨平台发布门禁。

依据：

- Python 3.13 起，未显式 `close()` 的 `sqlite3.Connection` 在删除时会发出 `ResourceWarning`；同时原生连接的事务 Context Manager 不会关闭连接，必须显式关闭或使用 `contextlib.closing()`。[Python sqlite3 文档](https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager)
- Python 的 `atexit` 只覆盖正常终止；未由 Python 处理的信号、解释器内部致命错误和 `os._exit()` 都会绕过它，因此退出钩子不能成为资源正确性的唯一保证。[Python atexit 文档](https://docs.python.org/3/library/atexit.html)
- pytest 会把测试期间捕获的析构异常转成 `PytestUnraisableExceptionWarning`，适合测试生命周期内的回归检测。[pytest API 文档](https://docs.pytest.org/en/stable/reference/reference.html#pytest.PytestUnraisableExceptionWarning)
- SQLAlchemy 推荐用连接 Context Manager 自动归还连接，并用 `Engine.dispose()` 处理池生命周期；HikariCP 则按连接离池时间记录“可能泄漏”日志。两者都把所有权和运行期观测放在最终析构之前。[SQLAlchemy 连接文档](https://docs.sqlalchemy.org/en/21/core/connections.html#basic-usage)、[HikariCP 配置文档](https://github.com/brettwooldridge/HikariCP#frequently-used)

以上“业内主流”是对这些官方实现共同模式的归纳，不是任何单一文档的原句。

## 后续专项做

- 所有生产连接继续经 `Database` 或受控工厂创建，并记录 owner、创建时间和关闭状态。
- API、MCP、Worker 停止时断言各自持有的连接归零。
- 提供轻量指标和告警：活动连接数、最长持有时间、锁错误、WAL 异常增长和关闭失败。
- 监控只报警，不自动关闭仍可能在使用的连接。
- 保留 pytest 的 `ResourceWarning`、unraisable warning 和确定性生命周期测试。
- 只有存在简单、跨平台且低误报的实现时，才增加解释器最终析构诊断；它仍是诊断项，不替代所有权规则。

## 后续专项不做

- 不用解析子进程 stderr 的复杂监督器阻塞发布。
- 不 monkeypatch 任意第三方 `sqlite3.connect()` 作为生产机制。
- 不通过 GC 枚举或全局强引用改变对象生命周期。
- 不自动关闭来源不明或仍在使用的连接。
- 不宣称能够捕获绕过受控工厂、仅在解释器销毁时暴露的所有泄漏。

## Phase 1 保留的保证

- 受支持的生产连接路径具有明确 owner 和关闭入口。
- API、MCP、Worker 与 `Database` 的关闭行为有确定性测试。
- pytest 生命周期内的 `ResourceWarning` 被提升为错误，unraisable warning 不得静默通过。
- 解释器最终析构阶段的极端漏检被明确记录，不伪装成已经解决。
