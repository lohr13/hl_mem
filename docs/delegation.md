# Delegation 宿主集成

本文面向在 HL-Mem 之外运行的裁决宿主。宿主负责有界轮询、读取完整案卷、形成裁决并调用写端点；HL-Mem
本身不安装 cron、systemd unit，也不内置 LLM 冲突判官。

> **信任边界：** HL-Mem 是本地、可信、单租户服务。若宿主不与服务同机，请在 API 前配置认证、授权和
> TLS；不要把服务直接暴露到公网。

## 选择冲突 owner 与行动入口

部署时必须明确一个冲突 owner：使用自动 delegation loop 有界轮询并裁决，或指定人工/按需 owner 在收到通知后
处理。未配置 loop 时，`manual_required` 不会自行消失；Hermes conflict notice 受会话级 system prompt 构建和健康
快照缓存约束，可能延迟，并且只在会话首次观察到非零计数或计数变化时通知，同值重建不会重复提示。它是提醒，
不是后台裁决器。

单独安装 Hermes provider 也不等于宿主具备冲突裁决工具。provider 只暴露只读 `hl_mem_recall`；完整的 pair/group
查看与裁决入口是下述 REST 契约。CLI 可以列案，但其 `resolve` 不覆盖 group 案，因此不能作为完整替代。人工模式下，
宿主具备 HTTP 或 shell 能力时可调用 REST；否则必须由能够访问 API 的外部 operator 负责处理。

安装验收必须在受控环境验证完整路径：非零 `manual_required_count` 能够列出对应开放案、读取其 dossier，并由指定
owner 使用该 dossier 的 revision 与 fingerprint 提交一次 CAS 裁决。只确认 notice 能显示或 `/healthz` 有计数不算
完成验收。

## REST 契约

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/v1/conflicts?status=manual_required&limit=20&offset=0` | 分页列出开放案；`limit` 为 1–100 |
| `GET` | `/v1/conflicts/{case_id}` | 读取轻量 review、`revision`、v2 `fingerprint` 与 group candidates |
| `GET` | `/v1/conflicts/{case_id}/dossier` | 读取 pair/group 完整案卷、双时间、证据、tip 与沿革链 |
| `POST` | `/v1/conflicts/{case_id}/resolve` | 按案型提交一次带 CAS 的裁决 |

列表项的 `group_key` 决定案型：`null` 是 pair，非 `null` 是 group。不要根据 candidate 数量或 Claim 文本猜测
案型。案卷固定上限为 1 MiB；超限返回 `413`，不存在返回 `404`。

每次裁决都必须携带刚读取快照的 `expected_revision`，宿主还应携带同一快照的
`expected_fingerprint`。任一过期都会在写入 Claim、case 或审计前返回 `409`。收到 `409` 后应重新读取案卷、
重新裁决；禁止原样盲重试。`fingerprint` 对 tip、supersession 边和裁决相关字段敏感，案卷同时保留 tip 与完整
沿革，因此旧 Claim 的 `valid_to`、`recorded_to` 和证据仍可见。

### Pair 请求

精确 action 词表为 `{keep_left, keep_right, coexist, reject}`：

- `keep_left` / `keep_right`：选择对应 tip 为 winner。
- `coexist`：双方可同时为真；恢复双方活跃并关闭 case。
- `reject`：驳回这一个 pair 冲突判断；恢复双方活跃，不 retract Claim。

```json
{
  "action": "coexist",
  "expected_revision": 3,
  "expected_fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "rationale": "同一事实的不同时点记录，双时间窗口不重叠",
  "resolver": "agent:delegation-host"
}
```

成功响应是 pair 投影：

```json
{
  "case_id": "case-01",
  "generation": 1,
  "revision": 4,
  "status": "resolved",
  "decision": "coexist",
  "winner_id": null,
  "resolved_at": "2026-08-30T02:30:00+00:00",
  "closed_case_ids": ["case-01"]
}
```

### Group 请求

精确 action 词表为 `{select_candidate, reject_candidate}`。`candidate_key` 必须来自当前 review/dossier：

```json
{
  "action": "select_candidate",
  "candidate_key": "candidate-key-from-current-dossier",
  "expected_revision": 7,
  "expected_fingerprint": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
  "rationale": "证据链与有效时间支持该候选",
  "resolver": "agent:delegation-host"
}
```

`reject_candidate` 是破坏性动作：它 retract 该候选的全部成员 Claim，且 case 返回 `manual_required` 以继续裁其余
候选。请求必须额外携带 `"confirm_retraction": true`，否则 `422` fail-closed。此确认不适用于
`select_candidate`，也不适用于任何 pair action。

已进入 `resolved` / `rejected` 的终态 case 不允许改写 rationale。成功请求会在同一事务写入一条 governance
action；使用旧 CAS 重放时返回 `409`，不会产生第二条审计。

## 裁决原则

1. pair 两边可以同时为真时优先 `coexist`，包括等价表述、时间窗不重叠或限定条件不同的记录。
2. 证据不足、坐标不完整或语义仍不确定时不 POST；保留 `manual_required` 比猜测更安全。
3. `409` 后重拉 dossier 并重新判断，不复用旧结论或旧 fingerprint。
4. group 自动化默认只允许 `select_candidate`；不要让无人值守模型生成 `reject_candidate`。
5. `reject_candidate` 仅供明确的人类破坏性操作；展示成员 Claim 和影响数量后，再发送显式确认。
6. 不把 pair 的 `reject` 与 group 的 `reject_candidate` 混为一谈：前者恢复双方，后者批量 retract。

## 有界宿主循环

单轮循环应同时限制分页、案数和总耗时。下面是与语言无关的伪代码：

```text
deadline = now + 45s
offset = 0
handled = 0
while offset < 200 and handled < 20 and now < deadline:
    page = GET /v1/conflicts?status=manual_required&limit=20&offset=offset
    for case in page.cases:
        if handled == 20 or now >= deadline: break
        dossier = GET /v1/conflicts/{case.case_id}/dossier
        kind = "pair" if dossier.group_key is null else "group"
        decision = adjudicate(kind, dossier)
        if decision is uncertain: continue
        if kind == "group" and decision.action != "select_candidate": continue
        POST once with dossier.revision + dossier.fingerprint
        if 409: record stale; continue       # 下一轮重拉，不立即重试
        if POST failed: record failure; continue
        handled += 1
    if offset + page.limit >= page.total: break
    offset += page.limit
```

不要对 POST 配置 HTTP 客户端的自动重试，尤其不能重试 `reject_candidate`。GET 可以在总预算内做少量带退避的瞬时
错误重试；失败不得变成无界循环。

## Linux 宿主示例

将实际 runner 安装为 `/opt/hl-mem-delegation/run-once`。runner 每次只执行一轮，并对所有 curl 使用
`--fail --max-time`；例如只读拉取：

```bash
curl --fail --silent --show-error --max-time 5 \
  'http://127.0.0.1:8200/v1/conflicts?status=manual_required&limit=20&offset=0'
```

POST 应由 runner 生成一次性 JSON 文件或从标准输入发送，且禁用重试：

```bash
curl --fail --silent --show-error --max-time 8 \
  -H 'Content-Type: application/json' \
  --data-binary @/run/hl-mem-delegation/decision.json \
  'http://127.0.0.1:8200/v1/conflicts/case-01/resolve'
```

systemd oneshot 保证同一时刻只有一轮；进程输出进入 journald：

```ini
# /etc/systemd/system/hl-mem-delegation.service
[Unit]
Description=HL-Mem bounded delegation pass
After=network-online.target hl-mem.service

[Service]
Type=oneshot
User=hlmem
Group=hlmem
RuntimeDirectory=hl-mem-delegation
RuntimeDirectoryMode=0750
ExecStart=/usr/bin/flock -n /run/hl-mem-delegation/host.lock /opt/hl-mem-delegation/run-once
TimeoutStartSec=60
StandardOutput=journal
StandardError=journal
```

```ini
# /etc/systemd/system/hl-mem-delegation.timer
[Unit]
Description=Run HL-Mem delegation every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30s
Persistent=false

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hl-mem-delegation.timer
journalctl -u hl-mem-delegation.service --since today
```

不使用 timer 时，可用 cron 调同一个有界 runner；`flock -n` 会跳过重叠轮次。部署时应预先确认 `hlmem` 用户可在
所选 lock 目录创建文件：

```cron
*/5 * * * * /usr/bin/flock -n /run/lock/hl-mem-delegation.lock /opt/hl-mem-delegation/run-once 2>&1 | /usr/bin/systemd-cat -t hl-mem-delegation
```

宿主日志至少记录 `case_id`、案型、输入 revision/fingerprint、action、HTTP 状态与耗时；不要记录密钥或完整敏感
Claim 文本。告警应针对连续轮次失败、`409` 激增、`413` 或 backlog 年龄增长，而不是在失败后快速重放写请求。
