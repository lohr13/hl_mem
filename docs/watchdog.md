# 服务监督与健康检查（Deployment Supervision）

HL-Mem 只提供一个跨平台监督接口：`GET /healthz`。仓库内的 `scripts/healthcheck.py` 对该接口执行一次探测；HTTP 状态码为 200 且 JSON 字段 `status` 等于 `ok` 时退出 0，否则退出 1。进程拉起、重启、防抖、日志和告警由 systemd、Windows 服务管理器或容器编排平台负责，不由 HL-Mem 自己监控自己。

探针只使用 Python 标准库，不需要 HL-Mem 或 Hermes 的虚拟环境：

```text
python scripts/healthcheck.py [--url http://127.0.0.1:8200/healthz] [--timeout 5]
```

有控制台时脚本只输出一行，例如 `ok: status=ok` 或 `fail: <原因>`。`pythonw.exe` 没有控制台输出，计划任务仍可通过进程退出码判断结果。

## Linux / systemd

主服务由 systemd 监督进程退出：

```ini
# /etc/systemd/system/hl-mem.service
[Unit]
Description=HL-Mem
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/hl-mem
ExecStart=/opt/hl-mem/.venv/bin/python /opt/hl-mem/start_server.py
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

`Restart=on-failure` 只处理已退出的进程。要发现“进程仍在但 `/healthz` 失败”，可用 oneshot service + timer 定时运行探针，并把失败交给恢复 unit：

```ini
# /etc/systemd/system/hl-mem-health.service
[Unit]
Description=Probe HL-Mem health
OnFailure=hl-mem-recover.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/hl-mem/scripts/healthcheck.py --timeout 5

# /etc/systemd/system/hl-mem-health.timer
[Unit]
Description=Probe HL-Mem every 30 seconds

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
Unit=hl-mem-health.service

[Install]
WantedBy=timers.target

# /etc/systemd/system/hl-mem-recover.service
[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart hl-mem.service
```

启用后执行 `systemctl enable --now hl-mem.service hl-mem-health.timer`。依赖 HL-Mem 的其他 unit 也可在自己的 `[Service]` 段加入启动门禁；它只阻止下游服务在 HL-Mem 不健康时启动，不替代持续探测：

```ini
ExecStartPre=/usr/bin/python3 /opt/hl-mem/scripts/healthcheck.py --timeout 5
```

若部署不包含脚本，定时 unit 可改用 `curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8200/healthz`，但该命令只校验 HTTP 成功，不校验 JSON 的 `status` 字段。

systemd 的 `WatchdogSec=` 需要服务持续发送 `sd_notify(WATCHDOG=1)`，它不会主动请求 HTTP。HL-Mem 当前不发送该通知；只有部署包装器负责通知时才应启用 `WatchdogSec=`，否则使用上述 timer。

## Windows / Task Scheduler

使用系统 Python 安装中的 `pythonw.exe` 可避免计划任务弹出控制台窗口。以下 PowerShell 示例每两分钟运行一次探针；请按实际安装位置修改两个路径：

```powershell
$pythonw = 'C:\Python311\pythonw.exe'
$probe = 'D:\workspace\hl_agent\hl_mem\scripts\healthcheck.py'
$taskCommand = "`"$pythonw`" `"$probe`" --url http://127.0.0.1:8200/healthz --timeout 5"

schtasks.exe /Create `
  /TN "HL-Mem Healthcheck" `
  /SC MINUTE /MO 2 `
  /RU SYSTEM /RL HIGHEST /F `
  /TR $taskCommand
```

查询、立即触发或删除任务：

```powershell
schtasks.exe /Query /TN "HL-Mem Healthcheck" /V /FO LIST
schtasks.exe /Run /TN "HL-Mem Healthcheck"
schtasks.exe /Delete /TN "HL-Mem Healthcheck" /F
```

任务历史中的 Last Run Result 为 `0` 表示健康，`1` 表示探测失败。需要在终端查看原因时，用 `python.exe scripts\healthcheck.py` 手动运行。计划任务只负责探测；自动恢复应把 HL-Mem 注册为 Windows 服务并配置 Service Control Manager 的失败恢复，或由现有运维代理根据退出码执行。不要再通过 Bash 脚本从产品仓库内终止并重启服务。

## 容器

在包含 Python 和探针脚本的镜像中加入一行：

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["python", "/app/scripts/healthcheck.py", "--url", "http://127.0.0.1:8200/healthz", "--timeout", "5"]
```

Docker 会记录 `healthy` / `unhealthy` 状态，但不会仅因容器变为 unhealthy 自动重启；Docker / Compose 的 restart policy 只响应容器退出。基于 unhealthy 的恢复与告警需要由 Swarm、Kubernetes 或外部监控消费，进程退出后的重启策略也应留在部署配置中。
