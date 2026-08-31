# MCP stdio 使用说明

HL-Mem 通过 `hl-mem-mcp` 提供 MCP Python SDK 2.x 的低层 `Server` 和 stdio transport。MCP 客户端负责启动这个子进程；不要同时手工运行同一个 stdio 命令。它直接使用 `hl_mem.toml` 指定的 SQLite 数据库，不依赖 HTTP API 进程。

## 安装与参数

```bash
python -m pip install "hl-mem[mcp]"
hlmem init
hl-mem-mcp --help
```

可用启动参数：

- `--config <path>`：TOML 路径；默认是子进程工作目录下的 `hl_mem.toml`。
- `--env-file <path>`：密钥文件路径；默认是子进程工作目录下的 `.env`。
- `--db <path>`：只覆盖 `database.path`，便于为某个客户端使用独立数据库。
- `--version`：输出 MCP runtime 版本。

客户端的工作目录可能与终端不同，建议配置绝对路径。Windows JSON 中可使用正斜杠路径，例如 `D:/memory/hl_mem.toml`。

## Codex

Codex CLI、IDE extension 和 ChatGPT desktop app 在同一 Codex host 上共享 MCP 配置。用 CLI 添加 stdio server：

```bash
codex mcp add hl-mem -- hl-mem-mcp \
  --config /absolute/path/hl_mem.toml \
  --env-file /absolute/path/.env
codex mcp list
```

也可以编辑 `~/.codex/config.toml`；可信项目可使用项目内 `.codex/config.toml`：

```toml
[mcp_servers.hl_mem]
command = "hl-mem-mcp"
args = [
  "--config", "/absolute/path/hl_mem.toml",
  "--env-file", "/absolute/path/.env",
]
startup_timeout_sec = 20
tool_timeout_sec = 60
```

重启对应 Codex 客户端后，用 `/mcp` 检查连接。参见 [Codex MCP 官方说明](https://learn.chatgpt.com/docs/extend/mcp)。

## Claude Code 与 Claude Desktop

Claude Code 可直接注册本地 stdio server：

```bash
claude mcp add hl-mem --scope user -- hl-mem-mcp \
  --config /absolute/path/hl_mem.toml \
  --env-file /absolute/path/.env
claude mcp get hl-mem
```

若希望项目成员共享配置，使用 `--scope project` 生成项目根目录的 `.mcp.json`。Claude Desktop 使用相同的 `mcpServers` 结构；配置文件位于 macOS 的 `~/Library/Application Support/Claude/claude_desktop_config.json` 或 Windows 的 `%APPDATA%/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "hl-mem": {
      "command": "hl-mem-mcp",
      "args": [
        "--config", "/absolute/path/hl_mem.toml",
        "--env-file", "/absolute/path/.env"
      ]
    }
  }
}
```

保存后重启 Claude Desktop；Claude Code 中可使用 `/mcp` 查看状态。参见 [Claude Code MCP 官方说明](https://docs.anthropic.com/en/docs/claude-code/mcp)。

## Cursor

在项目中创建 `.cursor/mcp.json`，或在 `~/.cursor/mcp.json` 配置全局 server：

```json
{
  "mcpServers": {
    "hl-mem": {
      "command": "hl-mem-mcp",
      "args": [
        "--config", "/absolute/path/hl_mem.toml",
        "--env-file", "/absolute/path/.env"
      ]
    }
  }
}
```

保存后在 Cursor 的 MCP 设置中确认 `hl-mem` 与工具已启用。参见 [Cursor MCP 官方说明](https://docs.cursor.com/context/model-context-protocol)。

## 工具与结果

runtime 直接复用 `get_tool_schemas()` 的 JSON Schema，避免 transport 与现有 MCP 契约各自演进。

| 工具 | 用途 |
|---|---|
| `memory_save` | 保存显式记忆 |
| `memory_recall` | 召回记忆和证据 |
| `memory_get` | 读取完整 Claim 详情 |
| `memory_correct` | 保留分类并替换 Claim 内容 |
| `memory_forget` | 经 tombstone 账本与统一闭包物理删除记忆 |
| `memory_explain` | 返回事件或 Claim 的证据链 |
| `memory_feedback` | 提交召回效用反馈和可选纠正 |

成功调用同时返回 JSON 文本内容和 `structuredContent`。输入校验、资源不存在、生命周期冲突等预期业务错误返回 `isError: true`，便于模型读取错误并修正参数；未分类的内部异常仍由 SDK 作为协议级内部错误处理。同步的数据库与业务调用在线程中执行，不阻塞 MCP 事件循环。

`memory_forget` 与 REST forget 共用 `DeletionService`：账本先写，随后删除 Claim 的专属 evidence、关系/冲突/派生
引用和无引用 Event。candidate/disputed/expired/open-manual 等歧义状态以及账本失败会 fail-closed，并以业务错误
返回；MCP 不保留一条更宽松的“只撤回状态”旁路。

stdio 的 stdout 只用于 MCP 帧。诊断输出应写入 stderr；若客户端报告连接关闭，先在终端运行 `hl-mem-mcp --config <绝对路径> --env-file <绝对路径>`，检查配置或密钥错误，然后再由客户端重启进程。
