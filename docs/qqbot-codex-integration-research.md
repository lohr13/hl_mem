# NoneBot2 + QQ 机器人集成 Codex 调研报告

> 调研日期：2026-08-01  
> 结论适用范围：自托管 QQ 个人号机器人、NoneBot2、OneBot V11、OpenAI Codex 本地 Agent 运行时  
> 状态说明：QQ 协议端和 Codex 都在快速演进，部署前应重新检查本文“版本与活跃度快照”中的项目状态。

## 1. 执行摘要

推荐首选方案：

```text
QQ 客户端
  -> NapCatQQ（OneBot V11，反向 WebSocket）
  -> NoneBot2 + FastAPI Driver + nonebot-adapter-onebot
  -> nonebot_plugin_codex（鉴权、去重、会话队列、流式聚合）
  -> openai-codex Python SDK / AsyncCodex
  -> 本地 Codex app-server（SDK 管理，默认 stdio JSONL）
  -> OpenAI 服务
```

核心选择如下：

- QQ 协议端选 **NapCatQQ**。它仍在活跃维护，支持 OneBot V11、反向 WebSocket 和 Docker；NoneBot 官方 OneBot 适配器也推荐 V11 使用反向 WebSocket。
- Codex 接入选 **`openai-codex` Python SDK 的 `AsyncCodex`**。它与 NoneBot2 的异步模型匹配，并提供 thread/turn、恢复、流式事件、取消、sandbox 和结构化错误。
- 不建议在业务插件中直接实现 Codex app-server JSON-RPC；Python SDK 已封装该协议。app-server WebSocket 当前仍被官方标记为实验性且不受支持，默认 stdio 更稳妥。
- `codex exec --json` 适合原型、故障降级和运维脚本，不适合作为高并发长期 Bot 的主接口。
- OpenAI Responses API 的 HTTP/SSE 或 WebSocket 是“直接调用模型 API”的另一条路线，不等同于完整 Codex Agent 运行时。仅需要普通问答时它更简单；需要访问仓库、执行工具、维护 Codex thread 时应使用 Codex SDK。
- 默认安全策略应为：仅白名单私聊、`read_only` sandbox、固定工作目录、每会话单飞、全局并发限制、不向 QQ 发送原始 reasoning/命令输出。任何写文件或执行命令能力都必须在隔离环境和审批机制完善后再开放。

本次检索没有发现一个成熟、活跃、可直接安装的“NoneBot2 + 官方 Codex SDK”插件。最接近的参考分为两类：NoneBot2 的通用 LLM 插件，以及 OneBot 11 + ACP Agent 的 QQ Bot 脚手架。可以复用其架构模式，但 Codex 网关仍建议单独实现。

## 2. 概念与边界

“Codex API”至少可能指四种不同接口，必须区分：

| 接口 | 传输 | 是否为完整 Codex Agent | 适用场景 |
|---|---|---:|---|
| `openai-codex` Python/TypeScript SDK | SDK 管理本地 Codex 运行时；Python SDK 控制 app-server JSON-RPC | 是 | 应用内嵌 Codex、thread/turn、工具与仓库任务 |
| Codex app-server | 默认 stdio JSONL；另有 Unix socket；WebSocket 为实验性、不受支持 | 是 | 构建深度自定义客户端；需要直接处理审批、事件和线程生命周期 |
| `codex exec` CLI | 子进程 stdout/stderr；`--json` 为 JSONL | 是 | CI、脚本、原型和降级路径 |
| OpenAI Responses API | HTTPS；`stream=true` 使用 SSE；也支持持久 WebSocket | 否，除非调用方自行实现 Agent 工具循环 | 普通对话、远程模型调用、自定义 Agent |

因此，本报告的主方案不是“NoneBot 直接请求一个名为 Codex 的 HTTP 接口”，而是让 NoneBot 插件调用官方 Codex SDK，由 SDK 驱动本地 Codex Agent。若需求只是 QQ 上的普通 AI 聊天，应重新评估是否真的需要 Codex；直接 Responses API 会更轻量，也更容易做无状态扩缩容。

## 3. NoneBot2 框架调研

### 3.1 架构设计

NoneBot2 是 Python 异步、事件驱动的机器人框架。主要边界为：

- **Driver**：负责网络收发和生命周期。FastAPI Driver 是服务端型驱动器，适合接收 OneBot 实现的反向 WebSocket；客户端型能力可通过 httpx/websockets mixin 组合。
- **Adapter**：协议桥梁。它把 OneBot JSON 转换为 NoneBot 的 `Bot`、`Event`、`Message`/`MessageSegment`，并把插件的 API 调用转换回 OneBot action。
- **Matcher**：事件响应器。按事件类型、Permission、Rule、priority 和 block 选择并执行处理器。
- **Plugin**：业务能力的加载与隔离单元。插件可以注册 Matcher、配置模型、启动/关闭钩子、依赖注入函数和插件元数据。
- **依赖注入**：handler、Rule、Permission 和 hook 可按类型/参数注入 Bot、Event、State、命令参数等对象。

Codex 插件不应把网络协议、会话持久化、SDK 控制和消息格式化全部写在 `__init__.py` 中；应保持 Matcher 薄、服务层独立、存储可替换。

### 3.2 消息处理流程

典型 OneBot V11 消息路径：

1. NapCat 通过反向 WebSocket 把 OneBot 事件推送到 NoneBot 的 `/onebot/v11/ws`。
2. FastAPI Driver 接收帧，OneBot Adapter 校验连接/token，解析并构建 `Bot` 与具体 `Event`。
3. NoneBot 运行 event preprocessors；预处理器可以直接忽略不合规事件。
4. 框架按 priority 从小到大检查 Matcher：事件类型 -> Permission -> Rule。
5. 匹配成功后运行 run preprocessors、handler 链及依赖注入。`block=True` 或动态停止传播会阻止更低优先级 Matcher。
6. 插件通过 `Matcher.send/finish` 或 `Bot.call_api` 回复；Adapter 将消息转换为 OneBot action 并通过同一 WebSocket 返回 NapCat。
7. 框架运行 run/event postprocessors，用于日志、指标和错误观测。

Codex 调用可能持续几十秒到数分钟，handler 中不得使用同步 HTTP、同步 SDK或阻塞式 `subprocess.run`。应使用 `AsyncCodex`、异步队列以及可取消任务。

### 3.3 插件机制建议

插件注册两个入口即可：

- 明确命令：`/codex <prompt>`、`/codex-new`、`/codex-reset`、`/codex-status`、`/codex-cancel`。
- 可选的 `@机器人` 消息入口，仅对白名单群开启。

首版不要使用“随机触发普通群消息”的模式。编码 Agent 拥有文件和工具上下文，误触发的成本和安全风险远高于普通闲聊机器人。

推荐使用：

- `get_plugin_config(Config)` 管理插件配置；
- `PluginMetadata` 声明 `supported_adapters={"~onebot.v11"}`；
- `driver.on_startup` 初始化 `AsyncCodex`、数据库和恢复任务；
- `driver.on_shutdown` 取消 worker、落盘并关闭 Codex 客户端；
- `nonebot-plugin-localstore` 获取插件数据目录，SQLite 文件放在该目录下；
- NoneBug 测试 Matcher 行为，pytest/pytest-asyncio 测试服务和并发。

### 3.4 OneBot 实现依赖

NoneBot2 本身不负责登录 QQ。对 QQ 个人号而言，需要 NapCat、go-cqhttp、Lagrange 等协议实现把 QQ 事件转换为 OneBot/Milky 等开放协议。

NoneBot OneBot Adapter 支持 V11 的反向 WebSocket、HTTP POST 和正向 WebSocket，并在文档中把**反向 WebSocket列为推荐连接方式**。反向 WS 的优点是事件上报和 action 调用共用一条双工连接，配置简单，也不必额外暴露 NapCat 的正向 WS 端口。

### 3.5 部署方式

直接运行适合开发：

```text
NapCat Shell/桌面运行
NoneBot: uv run nb run
Codex: 由 openai-codex SDK 启动其匹配的本地 runtime
```

Docker Compose 适合生产：

- `napcat` 服务：登录 QQ、提供 OneBot V11；持久化 QQ 和 NapCat 配置目录。
- `qqbot` 服务：NoneBot2、插件、`openai-codex` runtime、SQLite；挂载严格限定的工作目录和独立 Codex home volume。
- 可选 `redis`：多实例、分布式锁或外部会话存储；单实例首版不需要。

容器网络中，NapCat 的反向 WS 地址使用 `ws://qqbot:8080/onebot/v11/ws`。不应把 NoneBot OneBot 端口、NapCat WebUI 或 Codex transport 直接暴露到公网；管理入口应通过内网、VPN 或 SSH tunnel。

## 4. Codex 接入方式

### 4.1 Python SDK：主推荐

官方 Python 包为 [`openai-codex`](https://pypi.org/project/openai-codex/)，要求 Python 3.10+；发布包会安装与 SDK 版本匹配的 Codex CLI runtime。调研时 PyPI 最新可见版本为 `0.144.4`（2026-07-17 上传），实际部署应通过 lockfile 固定测试过的版本。

关键 API：

- `AsyncCodex`：适配 NoneBot 的异步事件循环；应用启动时创建，关闭时释放。
- `thread_start(...)`：新会话，可设置 `cwd`、model、sandbox、instructions 等。
- `thread_resume(thread_id, ...)`：从持久化 thread ID 恢复。
- `AsyncThread.run(...)`：等待一轮完成，返回 `TurnResult.final_response`。
- `AsyncThread.turn(...)`：取得 `AsyncTurnHandle`，可 `stream()`、`steer()`、`interrupt()`。
- `AsyncTurnHandle.stream()`：异步产生 `turn/started`、`item/agentMessage/delta`、`item/completed`、`turn/completed` 等通知。
- `thread.compact()`：长会话上下文压缩。
- `Sandbox.read_only/workspace_write/full_access`：线程或单轮权限边界。
- `is_retryable_error`、`ServerBusyError`、`JsonRpcError`：结构化重试与错误分类。

一个 `AsyncCodex` 实例可以路由多个并发 turn 的事件，但同一个 QQ 会话仍应串行处理，避免用户的第二条消息与前一轮工具执行交错。

### 4.2 app-server：仅用于深度定制

Codex app-server 是 Codex 富客户端使用的底层接口，提供线程、轮次、审批、认证、模型列表、文件变化和增量事件。

稳定主路径是：

- 默认 `stdio://`：换行分隔 JSON；
- 消息语义接近 JSON-RPC 2.0，但线上省略 `"jsonrpc":"2.0"`；
- 连接后必须先 `initialize`，再发送 `initialized`；
- 通过 `thread/start` 或 `thread/resume` 建立会话，`turn/start` 开始一轮；
- 通过 `item/agentMessage/delta` 聚合文本，最终以 `item/completed` 和 `turn/completed` 为准。

app-server 也提供 WebSocket listener，但官方文档明确标为**实验性且不受支持**。非本地连接还涉及 TLS、bearer token、队列过载和健康探针。因此 QQ Bot 不应直接把 app-server WebSocket 暴露给 NapCat 或公网。

直接实现 app-server 客户端只有两种合理情形：Python SDK 尚未暴露所需实验方法，或需要完全自定义审批 UI/协议。否则 SDK 能显著降低协议版本漂移成本。

### 4.3 CLI：原型与降级方案

`codex exec` 是非交互模式：

- 普通模式将进度写入 stderr，最终消息写入 stdout；
- `codex exec --json` 将 stdout 变为 JSONL 事件流；
- 事件包括 `thread.started`、`turn.started/completed/failed`、`item.*` 和 `error`；
- `codex exec resume --last` 或 `codex exec resume <SESSION_ID>` 可以继续会话；
- `--ephemeral` 禁止持久化 session rollout；
- 默认只读 sandbox，写入需显式配置。

作为主链路的缺点：每轮管理子进程、进程退出与 JSONL 解析更复杂；高并发会带来启动开销；中断、审批和多 turn 控制弱于 SDK。可保留 `CliCodexGateway` 作为 feature flag 下的 fallback，便于排障和验证 SDK 问题。

### 4.4 直接 Responses API：普通聊天替代方案

Responses API 支持：

- HTTPS `POST /v1/responses`；
- `stream=true` 时通过 SSE 返回类型化增量事件；常用事件为 `response.created`、`response.output_text.delta`、`response.completed` 和 `error`；
- Conversations API 提供持久 conversation ID；也可以使用 `previous_response_id` 链接多轮；
- 持久 WebSocket `wss://api.openai.com/v1/responses` 适合工具调用很多的长工作流。

Responses WebSocket 当前每条连接一次只能运行一个 in-flight response、不支持 multiplexing、连接最长 60 分钟，断线后需用已持久化 response ID 或完整上下文恢复。

这条路线不自带 Codex 的本地 shell、文件修改、sandbox、AGENTS.md 和 thread 行为。选择它意味着 QQ Bot 团队自己实现工具循环和执行隔离。对于“只聊天、不动代码”的需求，这是优点；对于“通过 QQ 远程控制编码 Agent”的需求则是明显的重复建设。

## 5. 会话管理与上下文保持

### 5.1 会话键

推荐默认隔离策略：

| 场景 | session key | 默认行为 |
|---|---|---|
| 私聊 | `onebot:{self_id}:private:{user_id}` | 每个 QQ 用户独立 Codex thread |
| 群聊 | `onebot:{self_id}:group:{group_id}:user:{user_id}` | 同群不同用户也隔离，防止代码上下文和结果串线 |
| 显式共享群会话 | `onebot:{self_id}:group:{group_id}:shared` | 仅管理员开启；所有成员都能看到并影响同一上下文 |

不要仅以 `user_id` 为键：多机器人账号会冲突。也不要默认仅以 `group_id` 为键：群内任意成员都能干预另一人的 Agent turn，并可能看到对方工作目录信息。

### 5.2 持久化模型

建议使用 SQLite，而不是仅保存内存 dict 或 JSON：

```text
codex_sessions
  session_key PK
  thread_id
  scope_type
  owner_user_id
  group_id NULL
  cwd
  sandbox
  status
  last_used_at
  version

codex_turns
  correlation_id PK
  session_key
  onebot_message_id UNIQUE
  codex_turn_id NULL
  status
  started_at
  finished_at NULL
  error_code NULL
```

插件只保存 QQ session key 到 Codex thread ID 的映射；实际 Codex 历史由 Codex runtime 保存。`CODEX_HOME`/session 目录与 SQLite 必须一起持久化，否则数据库里的 thread ID 会指向不存在的本地记录。

恢复策略：

1. 查到 thread ID 后调用 `thread_resume`。
2. 若 thread 已丢失或不可恢复，将映射标记为 stale，创建新 thread，并明确通知用户“旧会话不可恢复，已新建”。
3. `/codex-new` 创建新 thread；旧 thread 可归档而不是立即删除。
4. 定期归档超过 TTL 的映射；不要在普通请求路径永久删除 Codex 历史。
5. 长会话接近上下文上限时优先 `compact()`；压缩失败再提示用户新建会话。

### 5.3 并发策略

- 每个 session key 一个 `asyncio.Queue` + worker 或 `asyncio.Lock`，保证同一 thread 只有一轮在执行。
- 全局 `asyncio.Semaphore` 限制并发 Codex turn 数，例如首版 2-4。
- 同会话繁忙时默认排队一条，队列满则回复“当前任务处理中”；不要静默丢消息。
- `/codex-cancel` 调用当前 `AsyncTurnHandle.interrupt()`。
- 新消息默认不要自动调用 `turn.steer()`；steer 会改变正在执行的任务语义，只应由显式命令触发。
- 以 `self_id + message_id` 做短期入站去重，避免 OneBot 重连/重放导致重复执行工具。
- 多进程或多副本部署时，进程内 Lock 不够，需要 SQLite lease、Redis 分布式锁或按 session 做一致性路由。首版建议单实例。

## 6. 流式响应设计

QQ/OneBot V11 没有一个可依赖的通用“编辑已发送消息”能力。逐 token 发送会刷屏并触发平台频率限制，因此不能把 Codex delta 原样转发。

推荐两种模式：

- **`final`（默认）**：开始时最多发送一次“已开始处理”，后台消费全部事件，收到 `turn/completed` 后发送最终回复。
- **`progress`（白名单可选）**：只转发经过脱敏的阶段信息，如“正在分析仓库”“正在运行测试”；文本 delta 在内存中聚合，按段落、时间窗和最小字符数批量发送。

流式聚合规则：

1. `item/agentMessage/delta` 只进入 buffer，不立即发送。
2. `item/completed` 的 agentMessage 是权威完整文本；用于修正 delta 丢失或重连后的结果。
3. `turn/completed.status` 决定成功、失败或中断。
4. 不转发 raw reasoning、环境变量、命令完整 stdout/stderr、绝对路径或补丁内容；可只发经过允许的摘要。
5. 最终文本按可配置长度切分，优先在段落边界切；超长结果可使用 OneBot 合并转发消息或保存为文件后发送摘要。
6. 每个 turn 设置发送条数上限和最小发送间隔，避免模型输出造成 QQ flood。
7. Bot 断线时继续聚合；连接恢复后只补发最终结果，不重放所有 partial。

图片输入可在后续版本支持：下载 QQ 图片到受控临时目录，验证 MIME/大小，使用 SDK 的 `LocalImageInput`，turn 完成后清理。首版建议只支持文本，避免 SSRF、恶意文件和磁盘占用问题。

## 7. 错误处理与重试

### 7.1 分类

| 类型 | 示例 | 处理方式 |
|---|---|---|
| 瞬时过载 | SDK `ServerBusyError`、app-server `-32001` | 有界指数退避 + jitter，最多 2-3 次 |
| 上游限流/临时服务错误 | 429、部分 5xx、stream disconnect | 尊重 `Retry-After`；SDK已有重试时避免双重重试 |
| 认证/配额 | Unauthorized、billing/quota、UsageLimitExceeded | 不重试；管理员告警，用户收到简短错误 |
| 请求/版本错误 | InvalidParams、MethodNotFound、BadRequest | 不重试；修配置或确保 SDK 使用其 pinned runtime |
| 上下文超限 | ContextWindowExceeded | compact；仍失败则新建会话并提示 |
| sandbox/审批 | SandboxError、等待审批 | 拒绝危险操作或走显式管理员审批流程 |
| 运行超时 | turn 长时间无完成事件 | 先 interrupt，再重启 runtime；状态标记 unknown/failed |
| OneBot 发送失败 | WS 断开、action timeout | 最终结果短期落库；重连后按策略补发一次 |

### 7.2 重试边界

最重要的原则是：**不要盲目重放可能已经执行过副作用的整轮 Agent 任务**。

- thread start/resume 或 turn start 在确认服务端尚未接受前，可以安全有限重试。
- 一旦收到 `turn/started`、命令执行或文件修改事件，断线后的整轮自动重放可能重复副作用；应恢复原 thread、查询状态，或把状态标为不确定并要求用户确认。
- OpenAI 官方 SDK会对符合条件的限流错误自动重试并遵守 `Retry-After`；若应用再套重试，需计算总尝试次数和总等待时间。
- app-server 文档列出的 `ContextWindowExceeded`、`UsageLimitExceeded`、`HttpConnectionFailed`、`ResponseStreamDisconnected` 等应映射为稳定的用户提示和内部 error code。

### 7.3 超时与熔断

- startup timeout：Codex runtime 无法初始化时让插件进入 unavailable，而不是阻塞整个 NoneBot 启动。
- turn hard timeout：按任务类型配置，例如普通问答 2 分钟、编码任务 10 分钟；超时执行 `interrupt()`。
- stream idle timeout：长时间没有事件时探测进程状态，不要只依赖总超时。
- 连续认证/配额错误触发全局熔断，避免群消息持续消耗失败请求。
- 日志中使用 correlation ID 串联 OneBot message、session、thread 和 turn；严禁记录 API key、auth token 和完整私聊内容。

## 8. 参考实现与可复用模式

### 8.1 NoneBot2 LLM 插件

| 仓库 | 状态快照 | 可复用模式 | 不应照搬的部分 |
|---|---|---|---|
| [FuQuan233/nonebot-plugin-llmchat](https://github.com/FuQuan233/nonebot-plugin-llmchat) | 2026-07-31 仍有提交；GPL-3.0 | Matcher 薄入口、服务拆分、群/私聊状态隔离、每会话 Queue+worker、异步流聚合、localstore、启动/关闭落盘、测试结构 | GPL 代码复制需评估许可证；其上下文是 Chat Completions，不是 Codex thread；内置群管理工具不应默认暴露给 Agent |
| [KroMiose/nonebot_plugin_naturel_gpt](https://github.com/KroMiose/nonebot_plugin_naturel_gpt) | 2025-06-24 后无新提交；Apache-2.0 | `group_*`/`private_*` 会话命名、人格/记忆/摘要分层、会话管理命令 | 依赖旧 `openai<=0.28`，存在同步调用包装和复杂全局状态，不适合作为新项目底座 |
| [Alpaca4610/nonebot_plugin_chatgpt_turbo](https://github.com/Alpaca4610/nonebot_plugin_chatgpt_turbo) | 2025-03-04 后无新提交；pyproject 声明 MIT | `event.get_session_id()`、`AsyncOpenAI`、清理上下文命令、OneBot 多模态消息提取 | session 仅内存 dict、无历史上限/持久化/并发锁；源码存在同步 `httpx.get`，会阻塞事件循环 |
| [AkashiCoin/nonebot-plugin-chatgpt-plus](https://github.com/AkashiCoin/nonebot-plugin-chatgpt-plus) | 已归档；MIT | conversation/parent ID 映射、会话保存/切换/回滚命令、处理中消息 | 依赖 ChatGPT 网页私有/第三方 backend API、session token 和网页认证；不应用于正式 Codex 集成 |

其中最值得复用的是 `nonebot-plugin-llmchat` 的**每会话 worker**和模块边界，而不是其模型 API 代码。建议独立重写 Codex gateway、session schema 和安全层。

### 8.2 相邻的 QQ ↔ Agent 实现

- [happysnaker/qq-ai-bot](https://github.com/happysnaker/qq-ai-bot)：OneBot 11 + NapCat/LLOneBot + ACP Agent 的自托管桥接，提供会话持久化、进度回传、入站去重、correlation ID、指标和 Docker。它不是 NoneBot2 插件，也没有替代官方 Codex SDK，但其 session store、progress、dedupe 和部署边界很有参考价值。
- [Open-ACP/OpenACP](https://github.com/Open-ACP/OpenACP)：通过 ACP 把 Codex、Claude Code 等编码 Agent 接到 Telegram/Discord 等渠道，适合参考审批、diff、进度和持久会话设计；不直接支持 QQ/NoneBot。
- [openai/codex](https://github.com/openai/codex)：Codex CLI、SDK 和 app-server 的官方开源实现。Python SDK 的流式示例和错误重试示例应作为实现依据。

没有必要为了“同时支持 Claude”在首版引入 ACP 抽象。若产品路线明确需要 Codex/Claude Code/OpenCode 多 Agent，再把 `CodexGateway` 提炼为通用 `AgentGateway`，并评估 ACP；否则 YAGNI，先对接官方 Codex SDK。

## 9. 推荐插件结构

```text
nonebot_plugin_codex/
├── __init__.py                 # 元数据、Matcher 注册、生命周期钩子
├── config.py                   # Pydantic 配置与校验
├── models.py                   # Session/Turn/Progress DTO
├── matchers/
│   ├── chat.py                 # /codex 与 @bot 入口
│   └── commands.py             # new/reset/status/cancel/approval
├── services/
│   ├── codex_gateway.py        # AsyncCodex 封装；唯一接触 SDK 的模块
│   ├── session_manager.py      # session key、thread start/resume/compact
│   ├── dispatcher.py           # 每会话队列、全局 semaphore、取消
│   ├── stream_buffer.py        # delta 聚合、节流、最终文本修正
│   └── message_formatter.py    # QQ 输入规范化与输出切分/脱敏
├── storage/
│   ├── database.py             # SQLite 初始化和事务
│   ├── sessions.py             # session/thread 映射
│   └── turns.py                # 去重、turn 状态和补发记录
└── security.py                 # allowlist、cwd 映射、工具/审批策略
```

接口边界建议：

```python
class CodexGateway(Protocol):
    async def start_thread(self, *, cwd: Path, sandbox: str) -> str: ...
    async def resume_thread(self, thread_id: str, *, cwd: Path) -> AgentThread: ...
    async def stream_turn(self, thread: AgentThread, prompt: str) -> AsyncIterator[AgentEvent]: ...
    async def interrupt(self, session_key: str) -> None: ...
```

NoneBot Matcher 不应 import app-server 协议类型；所有外部事件先由 gateway 转换成少量稳定的 `AgentEvent`。这样未来切换 CLI fallback 或 ACP 才不会重写消息层。

## 10. 消息转发逻辑

建议的完整路径：

1. Matcher 只接受 `/codex` 或已配置群中的 `@bot`；检查 user/group allowlist。
2. 校验文本长度、消息段类型、工作目录映射和当前 sandbox；不接受用户传入任意绝对路径作为 cwd。
3. 用 `self_id + message_id` 查询 turn 表，重复事件直接忽略或返回已有状态。
4. 生成 session key 和 correlation ID，入每会话队列。
5. worker 取得全局 semaphore，加载 session；有 thread ID 则 resume，否则 start。
6. 调用 `thread.turn(prompt)`，保存 Codex turn ID；消费 `turn.stream()`。
7. stream buffer 聚合 `item/agentMessage/delta`；只发送允许的 progress。
8. 收到 `item/completed` 时更新权威最终文本；收到 `turn/completed` 后提交 turn 状态。
9. message formatter 脱敏、分段或转文件，通过 Matcher/Bot 回复原消息。
10. 异常时按类型落库并给出用户可操作的提示；释放 semaphore 和 session lock。

建议首版只允许预配置的项目别名：

```text
/codex-new hl-mem
/codex 检查最近一次提交是否引入召回回归
```

服务端把 `hl-mem` 映射到固定 `/workspaces/hl-mem`。绝不允许 QQ 文本直接指定 `C:\`、`/`、`~` 或 Docker socket 等宿主路径。

## 11. 配置管理

建议配置项：

```dotenv
CODEX_BOT_ENABLED=true
CODEX_BOT_ALLOWED_USER_IDS=["123456789"]
CODEX_BOT_ALLOWED_GROUP_IDS=[]
CODEX_BOT_ENABLE_GROUP_CHAT=false
CODEX_BOT_GROUP_SESSION_MODE=group_user

CODEX_BOT_DEFAULT_PROJECT=hl-mem
CODEX_BOT_PROJECTS={"hl-mem":"/workspaces/hl-mem"}
CODEX_BOT_SANDBOX=read_only
CODEX_BOT_MAX_CONCURRENCY=2
CODEX_BOT_MAX_QUEUE_PER_SESSION=1
CODEX_BOT_TURN_TIMEOUT_SECONDS=600
CODEX_BOT_STREAM_MODE=final
CODEX_BOT_REPLY_CHUNK_CHARS=1500
CODEX_BOT_SESSION_TTL_DAYS=30

ONEBOT_ACCESS_TOKEN=<通过 secret 注入>
```

原则：

- model 留空时使用 Codex 当前配置，不在插件里硬编码容易过时的 model slug。
- API key、OneBot token 不进入 `.env.example` 的真实值、不写日志、不通过 QQ 命令显示。
- 生产环境使用 Docker/Kubernetes secret 或受权限保护的环境文件；不要把 key 放在群级配置 JSON。
- 配置加载时验证所有 project path 均存在、为绝对路径、位于允许的根目录内。
- 配置热更新只用于 allowlist/展示参数；sandbox、project root、认证变更建议重启生效。

## 12. 技术选型建议

### 12.1 OneBot 实现

| 方案 | 2026-08-01 快照 | 评价 |
|---|---|---|
| **NapCatQQ** | 仓库活跃；调研时最新 release `v4.18.13`（2026-07-19）；提供 OneBot V11 和 Docker | **推荐**。当前 NoneBot + OneBot V11 最直接路线。需关注混合/限制性许可证、QQ 版本适配与账号风控 |
| go-cqhttp | 仓库未归档且有近期提交，但 README 明示因 QQ 加密更新已无力维护；最新正式 release 仍为 `v1.2.0`（2023-10-09） | **不建议新部署**。只保留兼容旧环境的迁移期支持 |
| Lagrange | 当前 `Lagrange.Core` 主线为 V2，README 明示 V1 sunset；对外 Bot 服务主推 `Lagrange.Milky` | **不作为 OneBot V11 首选**。若愿意改用 Milky + `nonebot-adapter-milky`，可以单独评估 |

注意：GitHub 的 archived 标志、最近 commit 和项目 README 的维护声明可能不一致。选择时应以“当前主线协议、可用 release、维护者声明和实际登录测试”综合判断，而不是只看 star 或 pushed_at。

### 12.2 Codex 接口

1. **主选**：`openai-codex` + `AsyncCodex`。
2. **调试/降级**：`codex exec --json`。
3. **不建议首版**：直接 app-server JSON-RPC 或 app-server WebSocket。
4. **普通聊天替代**：OpenAI Responses API HTTP/SSE；工具往返很多时再考虑 Responses WebSocket。

### 12.3 部署

推荐单机 Docker Compose、双容器起步：NapCat 与 qqbot 分离。qqbot 容器内运行 NoneBot 和 SDK 管理的 Codex runtime，原因是 app-server 的稳态 transport 是本地 stdio。

安全配置：

- qqbot 非 root 运行；root filesystem 尽量只读；
- 只挂载指定 workspace volume、插件 data volume 和独立 Codex home volume；
- 禁止挂载宿主 `/`、用户 home、SSH agent、云凭证目录和 `/var/run/docker.sock`；
- API key 通过 secret 注入；agent 子进程不应获得不必要的 OneBot 管理 token；
- NapCat WebUI 不公开，首次扫码通过内网/SSH tunnel；
- OneBot 反向 WS 使用 token，两个容器只在内部网络通信；
- 配置 `restart: unless-stopped`、健康检查、日志轮转和磁盘配额。

更高安全级别可以把 Codex 运行时放进独立 runner 服务，每个项目独立容器/用户；NoneBot 通过内部认证 RPC 调用 runner。这会增加自定义服务和协议维护，不建议作为 MVP。

### 12.4 依赖管理

本仓库已经使用 uv，建议继续使用：

- Python 3.11；
- `nonebot2[fastapi]`；
- `nonebot-adapter-onebot`；
- `openai-codex`；
- `nonebot-plugin-localstore`；
- `aiosqlite` 或项目现有异步 SQLite 封装；
- 测试使用 `nonebug`、`pytest`、`pytest-asyncio`。

`pyproject.toml` 使用兼容范围，提交 `uv.lock` 固定完整依赖图。Codex SDK 与其 runtime 是配套版本，不要单独覆盖 `codex_bin`，除非兼容性测试明确要求。

## 13. 实现步骤

### Phase 0：安全边界与验收口径

1. 明确首版只允许哪些 QQ 用户、哪些项目和哪些操作。
2. 默认关闭群聊，sandbox 固定 `read_only`。
3. 定义成功标准：私聊连续 10 轮不串线、重启可恢复、重复事件不重复执行、取消在限定时间内生效。

### Phase 1：打通 OneBot 与 NoneBot

1. 用 uv 创建/扩展 NoneBot 应用，注册 FastAPI Driver 和 OneBot V11 Adapter。
2. 启动 NapCat，配置反向 WS `/onebot/v11/ws` 和一致的 access token。
3. 实现 `/ping`，验证私聊、群聊 @、断线重连和重复事件。

### Phase 2：实现 Codex Gateway

1. 在 startup 创建 `AsyncCodex`，验证账号/API key 状态。
2. 实现 start/resume/run final-only；固定一个只读 workspace。
3. 将 SDK 错误转换为内部 error code，先不做通用重试。
4. 在 shutdown 关闭客户端；测试 runtime 初始化失败不会拖死 NoneBot。

### Phase 3：会话与并发

1. 建 SQLite session/turn 表和唯一约束。
2. 实现 session key、thread mapping、`/codex-new/reset/status`。
3. 实现每会话 Queue/Lock、全局 Semaphore、message ID 去重。
4. 重启进程验证 thread resume；模拟 thread 丢失并验证 stale fallback。

### Phase 4：流式、取消与长输出

1. 使用 `thread.turn()` + `turn.stream()` 消费官方事件。
2. 聚合 delta，以 `item/completed` 修正最终文本，以 `turn/completed` 收敛状态。
3. 实现 final/progress 两种输出模式、节流、分段和合并转发 fallback。
4. 保存当前 TurnHandle，实现 `/codex-cancel`。

### Phase 5：错误治理与安全加固

1. 仅对 `is_retryable_error` 返回 true 的初始化/无副作用操作做有限退避。
2. 增加 hard/idle timeout、熔断、健康检查、correlation ID 和脱敏日志。
3. 增加 project allowlist、路径 canonicalization、prompt/附件大小限制。
4. 做 prompt injection、符号链接逃逸、危险命令、密钥泄露和重复副作用测试。

### Phase 6：受控写操作（可选）

1. 把每个项目放入独立 workspace/container；先启用 `workspace_write`，仍禁止 full access。
2. 设计 QQ 侧一次性审批 request ID，仅 owner/superuser 可批准。
3. 审批信息只展示命令/文件变更摘要；超时自动拒绝。
4. 做备份、git diff、回滚和审计，再对白名单开放。

### Phase 7：生产部署

1. 构建固定 digest 的 qqbot 镜像和 NapCat 镜像。
2. 挂载 SQLite、Codex home 和 workspace volumes；注入 secrets。
3. 运行 smoke tests：登录、WS、10 轮会话、重启恢复、取消、限流、断网、磁盘满。
4. 先单用户灰度，再扩大 allowlist；群聊和写能力分开灰度。

## 14. 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|---|---:|---:|---|
| QQ 非官方协议变更导致登录失效 | 高 | 高 | NapCat 锁定已验证版本；升级前灰度；保留维护窗口和备用账号，但不要自动规避平台安全机制 |
| QQ 账号冻结/风控 | 中-高 | 高 | 专用账号、低频率、明确触发、禁止群刷屏；遵守腾讯条款和当地法规 |
| QQ 消息等价于远程代码执行入口 | 高 | 极高 | 白名单、私聊优先、read-only、固定 cwd、容器隔离、工具 allowlist、审批、无 Docker socket |
| Prompt injection 诱导泄密/危险工具 | 高 | 极高 | 不把秘密放入 agent 环境；最小权限；输出脱敏；工具参数校验；拒绝任意路径和任意 shell |
| 群会话串线或隐私泄露 | 中 | 高 | 默认 group+user 隔离；共享会话仅管理员开启；禁止原始日志/推理回传 |
| OneBot 重放导致重复 Agent 副作用 | 中 | 高 | message ID 唯一约束、turn 状态机、收到 turn start 后不盲目重跑 |
| 流式消息刷屏/限频 | 高 | 中 | final 默认、聚合和节流、发送条数上限、长文本合并转发/文件 |
| Codex/API 限流、费用失控 | 中 | 高 | 用户配额、全局 semaphore、每日预算/告警、最大 turn 时长、禁止随机群触发 |
| Codex SDK/app-server 协议快速变化 | 中 | 中 | 使用官方 SDK pinned runtime；锁版本；contract tests；避免直接依赖实验 API |
| 本地 session 与 SQLite 映射不一致 | 中 | 中 | 同时持久化 CODEX_HOME 与 DB；启动巡检；stale 状态和显式新建 fallback |
| Docker volume 泄漏宿主数据 | 中 | 极高 | 仅命名 workspace volume；非 root；不挂 home/root/socket；定期权限审计 |
| 参考插件许可证不兼容 | 中 | 中 | 优先复用设计模式；复制 GPL 代码前做许可证评估；保留 NOTICE/归属 |

## 15. 测试与验收建议

最低测试集：

- Matcher：非白名单、非 @ 群消息、空 prompt、过长输入均被拒绝。
- Session：私聊用户、不同群用户、多个 self_id 不串 thread。
- Concurrency：同 session 串行、不同 session 可并行、全局上限有效。
- Persistence：进程重启后 resume；Codex home 丢失时新建且通知。
- Stream：delta 丢失/重复/乱序防御；`item/completed` 修正；失败/中断收敛。
- Dedupe：相同 OneBot message ID 只启动一次 turn。
- Cancel：繁忙、完成后取消、重复取消均为幂等行为。
- Retry：只重试 ServerBusy/明确瞬时错误；InvalidParams/Auth/Quota 不重试。
- Security：路径穿越、符号链接、命令注入、日志密钥、恶意图片 URL、prompt injection。
- Deployment：NapCat/NoneBot 任一方重启、WS 重连、OpenAI 断网、磁盘只读、数据库锁冲突。

生产验收门槛建议：

- 100 次文本 turn 无跨会话回复；
- 模拟 20 次重复事件无重复 Codex turn；
- 重启后既有 session 恢复率 100%，丢失 thread 时有清晰 fallback；
- 取消 p95 在 5 秒内收到中断确认；
- 日志扫描不包含 API key、OneBot token、auth.json 内容和 raw reasoning；
- 容器内无法访问未挂载的宿主目录和 Docker socket。

## 16. 资料与仓库地址

### NoneBot / OneBot / QQ

- [NoneBot2 官方文档](https://nonebot.dev/docs/)
- [NoneBot2 GitHub](https://github.com/nonebot/nonebot2)
- [NoneBot Adapter 架构](https://nonebot.dev/docs/advanced/adapter)
- [NoneBot 事件处理流程 API](https://nonebot.dev/docs/api/message)
- [NoneBot Matcher 进阶](https://nonebot.dev/docs/advanced/matcher)
- [NoneBot 部署](https://nonebot.dev/docs/best-practice/deployment)
- [NoneBot OneBot Adapter GitHub](https://github.com/nonebot/adapter-onebot)
- [OneBot V11 连接配置](https://onebot.adapters.nonebot.dev/docs/guide/setup/)
- [NapCatQQ GitHub](https://github.com/NapNeko/NapCatQQ)
- [NapCat Docker](https://github.com/NapNeko/NapCat-Docker)
- [NapCat 接入 NoneBot](https://napneko.github.io/use/integration)
- [NapCat OneBot 网络说明](https://napneko.github.io/onebot/network)
- [go-cqhttp GitHub](https://github.com/Mrs4s/go-cqhttp)
- [go-cqhttp 维护说明/迁移讨论](https://github.com/Mrs4s/go-cqhttp/issues/2471)
- [Lagrange.Core GitHub](https://github.com/LagrangeDev/Lagrange.Core)
- [Milky 生态（含 Lagrange.Milky 与 NoneBot Adapter）](https://milky.ntqqrev.org/awesome)

### Codex / OpenAI

- [Codex SDK 官方文档](https://learn.chatgpt.com/docs/codex-sdk)
- [Codex Python SDK API Reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md)
- [Codex Python SDK 流式示例](https://github.com/openai/codex/tree/main/sdk/python/examples/03_turn_stream_events)
- [Codex Python SDK 错误与重试示例](https://github.com/openai/codex/tree/main/sdk/python/examples/10_error_handling_and_retry)
- [Codex app-server 官方文档](https://learn.chatgpt.com/docs/app-server)
- [Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)
- [OpenAI Codex GitHub](https://github.com/openai/codex)
- [Responses API 会话状态](https://developers.openai.com/api/docs/guides/conversation-state)
- [Responses API SSE 流式响应](https://developers.openai.com/api/docs/guides/streaming-responses)
- [Responses API WebSocket Mode](https://developers.openai.com/api/docs/guides/websocket-mode)
- [OpenAI API Rate Limits 与重试](https://developers.openai.com/api/docs/guides/rate-limits)

### 参考实现

- [FuQuan233/nonebot-plugin-llmchat](https://github.com/FuQuan233/nonebot-plugin-llmchat)
- [KroMiose/nonebot_plugin_naturel_gpt](https://github.com/KroMiose/nonebot_plugin_naturel_gpt)
- [Alpaca4610/nonebot_plugin_chatgpt_turbo](https://github.com/Alpaca4610/nonebot_plugin_chatgpt_turbo)
- [AkashiCoin/nonebot-plugin-chatgpt-plus](https://github.com/AkashiCoin/nonebot-plugin-chatgpt-plus)
- [happysnaker/qq-ai-bot](https://github.com/happysnaker/qq-ai-bot)
- [Open-ACP/OpenACP](https://github.com/Open-ACP/OpenACP)

## 17. 最终推荐

首版采用 **NapCatQQ + OneBot V11 反向 WebSocket + NoneBot2/FastAPI + `openai-codex`/`AsyncCodex` + SQLite**。部署为单机 Docker Compose，NapCat 与 qqbot 分容器，Codex runtime 与 NoneBot 同处受限 qqbot 容器并通过 SDK 的本地 stdio transport 通信。

上线顺序必须是：白名单私聊 + 只读 workspace -> 持久会话和取消 -> 流式聚合 -> 隔离容器内的受控写入 -> 最后才考虑群共享会话和 QQ 审批。不要用 go-cqhttp 新建部署，不要依赖 ChatGPT 网页私有接口，不要公开 app-server WebSocket，也不要让群消息直接获得宿主机 shell/文件权限。
