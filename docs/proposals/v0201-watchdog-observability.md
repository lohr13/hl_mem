# v0.20.1 watchdog 与可观测性方案

> 后续状态：本方案中的仓库内 Windows watchdog 已被跨平台 `scripts/healthcheck.py` 和部署层监督取代；当前部署方式见 [`docs/watchdog.md`](../watchdog.md)。以下内容保留为历史设计记录。

## 选择

P0 采用仓库内 Bash 脚本，由 Windows 计划任务每两分钟调用系统 Git Bash。相比 Python 脚本，这一方案不依赖项目或 Hermes venv，可在清除 `VIRTUAL_ENV`、`PYTHONPATH` 后直接使用 `curl` 和 Windows 系统诊断命令。脚本通过 `var/watchdog.state` 跨次保存连续失败数和最近重启时间，通过带 owner 信息及陈旧回收的原子目录锁避免计划任务重叠；单次探测超时 5 秒，健康即清零，第三次连续失败且不在 60 秒冷却期时才处理事故。

重启前创建 `var/crash-packages/<timestamp>/`，在逐命令超时保护下保存三次探测记录、端口/PID 树、进程资源快照、DB/WAL/SHM 元数据、服务日志尾部以及可用时的 `py-spy` 线程栈。随后终止 8200 监听进程树、复查端口，再用系统 Bash 后台调用 `~/bin/start_hlmem.sh`；终止或启动失败不会清零失败状态。所有操作追加到 `var/watchdog.log`。计划任务注册命令及安装、验证和卸载方法写入独立运维文档。

P1 在 FastAPI 增加轻量请求日志中间件：进入时记录方法、路径和可选 `X-Request-ID`，退出时在 `finally` 中记录状态码与单调时钟耗时，异常请求记为 500 后继续抛出。`healthz` 改为 `async def`，保留现有响应字段。其组件、向量、召回副作用和 provider 指标均来自进程内快照，不访问数据库或网络，因此继续保留。

## 验证

新增单元测试覆盖 healthz 路由为协程、成功及异常请求的 start/end 日志；通过临时目录和可控系统命令运行真实 watchdog，覆盖失败阈值、恢复清零、事故包、重启和冷却，并做 Bash 语法检查。最后运行任务文档指定的 `py_compile` 与全量 `tests/unit/` pytest 命令。
