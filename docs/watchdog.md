# Windows watchdog 运维指南

`scripts/hlmem_watchdog.sh` 是独立于 HL-Mem/Hermes Python 环境的外部探针。它每次运行只探测一次 `/healthz`；Windows 计划任务负责每两分钟调用。连续三次失败后，脚本先保存事故包，再终止 8200 监听进程树并拉起服务。单次超时为 5 秒，重启冷却期为 60 秒。

## 前置条件

- Windows 10，已安装 Git for Windows，系统 Bash 位于 `C:\Program Files\Git\bin\bash.exe`。
- `curl`、Git Bash 自带的 `timeout`、`powershell.exe`、`netstat.exe`、`tasklist.exe` 和 `taskkill.exe` 可用。
- 启动脚本位于 `C:\Users\Administrator\bin\start_hlmem.sh`。
- 可选安装 `py-spy` 并放入计划任务的系统 `PATH`；缺失时事故包仍会保存进程快照。

watchdog 启动时会清除 `VIRTUAL_ENV`、`PYTHONPATH` 和 `PYTHONHOME`，不会使用 Hermes 或 HL-Mem venv。启动服务时才由 `start_hlmem.sh` 设置 HL-Mem 自己的环境。

## 注册计划任务

在“以管理员身份运行”的 PowerShell 中执行以下命令。`/RP *` 会提示输入 Administrator 密码；任务使用最高权限，且因为没有 `/IT`，会在非交互会话运行，不弹出窗口。

```powershell
schtasks.exe /Create `
  /TN "HL-Mem Watchdog" `
  /SC MINUTE /MO 2 `
  /RU Administrator /RP * `
  /RL HIGHEST /F `
  /TR '"C:\Program Files\Git\bin\bash.exe" --noprofile --norc "REDACTED_PATH/scripts/hlmem_watchdog.sh"'
```

查询任务、触发一次探测或卸载：

```powershell
schtasks.exe /Query /TN "HL-Mem Watchdog" /V /FO LIST
schtasks.exe /Run /TN "HL-Mem Watchdog"
schtasks.exe /Delete /TN "HL-Mem Watchdog" /F
```

首次注册后执行一次 `/Run`，再检查 `var/watchdog.log` 是否出现 `healthz_probe_succeeded`。也可以绕过计划任务手动验证：

```powershell
& 'C:\Program Files\Git\bin\bash.exe' --noprofile --norc 'REDACTED_PATH/scripts/hlmem_watchdog.sh'
```

## 状态、日志与事故包

- `var/watchdog.state`：连续失败数和最近重启 Unix 时间；健康探测成功会把失败数清零。
- `var/watchdog.log`：探测结果、耗时、错误、事故收集、终止和重启动作。
- `var/hlmem_startup.log`：watchdog 拉起服务后的标准输出和错误输出。
- `var/crash-packages/<UTC timestamp>/`：最近探测日志、8200 监听 PID、进程树、CPU/内存/线程数、DB/WAL/SHM 元数据、服务日志尾部，以及 `py-spy` 栈或降级说明。

原子目录 `var/watchdog.lock` 防止重叠运行并记录 owner PID/创建时间；正常退出会自动删除，异常残留超过 300 秒后自动安全回收。外部诊断命令默认最多运行 10 秒；监听进程终止失败、端口复查失败或启动器立即退出时，脚本保留失败计数且不进入冷却，等待下次计划任务重试。事故包可能包含命令行和日志中的敏感信息，应只允许 Administrator 访问，并按本机保留策略定期清理。

## 可选覆盖

手动运行或定制任务动作时，可设置 `HL_MEM_ROOT`、`HL_MEM_HEALTH_URL`、`HL_MEM_START_SCRIPT`、`HL_MEM_BASH`、`HL_MEM_PORT`、`HL_MEM_WATCHDOG_TIMEOUT_SECONDS`、`HL_MEM_WATCHDOG_FAILURE_THRESHOLD`、`HL_MEM_WATCHDOG_COOLDOWN_SECONDS`、`HL_MEM_WATCHDOG_LOCK_STALE_SECONDS`、`HL_MEM_WATCHDOG_DIAGNOSTIC_TIMEOUT_SECONDS`、`HL_MEM_WATCHDOG_TERMINATION_WAIT_SECONDS`、`HL_MEM_WATCHDOG_START_GRACE_SECONDS` 和 `HL_MEM_STARTUP_LOG`。默认核心策略依次为 5 秒探测、3 次失败、60 秒冷却、300 秒陈旧锁、10 秒诊断上限、10 秒端口终止等待和 1 秒启动存活检查。
