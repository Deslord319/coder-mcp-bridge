# Coder MCP Bridge

面向 Codex 的事件驱动多代理 MCP 调度桥，仓库名为 `coder-mcp-bridge`。当前可在同一套 `agent-*` 工具后选择：

- ZCode：原生 app-server，会话、durable goal、后台任务与精确 reasoning/usage 事件。
- OpenCode：本地认证 HTTP server + SSE，会话恢复、引导、分支、压缩与权限事件。
- Pi：每个运行一个严格 LF JSONL RPC 进程，会话恢复、引导、分支、压缩与 reasoning/usage 事件。

后端由 `agent-config` 在一个 MCP 连接内设置一次。`agent-start` 不接受 backend 参数；切换后只影响未来运行，已有 `runId` 始终绑定原后端。

## 调度边界

Codex 决定全局并发数、任务颗粒度和交付边界。Bridge 默认不设置全局并发上限，只负责：

- 让独立 worktree 和独立资源真正并发；
- 使用跨进程 SQLite lease 串行化冲突的 shared/exclusive 资源；
- 将后端事件投影成有 revision 的紧凑状态；
- 提供最长 60 秒的事件等待，避免 `sleep 60/90` 盲等；
- 在 headless 环境自动处理结构化权限请求和用户输入请求。

设置正数 `AGENT_MCP_MAX_CONCURRENCY` 只作为每个后端的操作员安全上限，不取代 Codex 调度。

## MCP 工具

| 工具 | 用途 |
|---|---|
| `agent-config` | `get` / `set` / `reset` / `list` 后端及能力；通常每批任务设置一次 |
| `agent-start` | 非阻塞启动运行，立即返回 `runId` |
| `agent-wait` | 等待 revision 变化或终态；主进度路径 |
| `agent-observe` | 获取有界事件、模型/reasoning、工具、usage、context 与资源状态 |
| `agent-control` | guide、interrupt、cancel；ZCode 另支持 goal/background 控制 |
| `agent-recover` | 列出或接管当前后端的持久会话 |
| `agent-branch` | 从消息、turn 或 checkpoint 分支（具体粒度取决于后端） |
| `agent-context` | 检查或压缩上下文 |
| `agent-close` | 关闭运行时并释放资源；不删除持久会话 |

`guide` / `interrupt` 面向仍在运行的 run。Pi/OpenCode run 进入终态后，若要继续同一会话，应把返回的 `threadId` 传给新的 `agent-start`；这样 Bridge 会重新获取 worktree/resource lease。

典型调用：

```json
{"name":"agent-config","arguments":{"action":"set","backend":"pi"}}
{"name":"agent-start","arguments":{"prompt":"实现并测试登录流程","cwd":"/path/to/worktree","workspaceAccess":"exclusive"}}
{"name":"agent-wait","arguments":{"runId":"run_...","afterRevision":3,"timeoutMs":30000}}
{"name":"agent-close","arguments":{"runId":"run_..."}}
```

可并发发送多个 `agent-start`。Codex 应给独立故事分配独立 worktree，并只为真实冲突资源声明相同 key：

```json
{
  "prompt": "运行 iOS 验收",
  "cwd": "/path/to/story-worktree",
  "workspaceAccess": "exclusive",
  "resources": [
    {"key": "simulator:iphone-16", "mode": "exclusive"},
    {"key": "/path/to/shared-derived-data", "mode": "exclusive"}
  ]
}
```

绝对路径 resource 同时进入结构化文件权限根；只有 `mode: "exclusive"` 的根允许写入，`shared` 根只读。`simulator:*` 这类抽象 key 只参与调度冲突判断。

## 后端能力

| 能力 | ZCode | OpenCode | Pi |
|---|---|---|---|
| prompt | 是 | 是 | 是 |
| durable goal | 是 | 否 | 否 |
| reasoning/usage 事件 | 是 | 是 | 是 |
| guide / interrupt / cancel | 是 | 是 | 是 |
| session recover | 是 | 是 | 是 |
| branch / compact | 是 | 是 | 是 |
| 后台任务投影 | 是 | 否 | 否 |
| tool allowlist | 是 | 不支持，明确报错 | 是 |
| tool denylist | 是 | 是 | 是 |
| 权限通道 | reverse request | HTTP event | 强制加载的 tool-call extension |

`agent-config {"action":"list"}` 会探测本机安装并返回每个后端的精确 capability。调用方不应假定三端所有原生能力完全相同。

## 权限模型

- `workspaceAccess: "shared"`：拒绝结构化写权限。Pi 同时拒绝 shell；OpenCode 对 shell 权限事件也拒绝。
- `workspaceAccess: "exclusive"`：允许原生文件工具在 `cwd` 与声明的绝对路径 resource 中操作。
- Pi 的 `mode: "plan"` 强制为只读工具策略，即使 workspace lease 是 exclusive。
- Pi 由 Bridge 强制加载 `pi_bridge_extension.mjs`，不依赖模型提示词遵守边界。
- OpenCode 的 `external_directory` 请求只有在所有结构化路径均位于声明根内时才允许；缺少路径信息默认拒绝。

shell 命令在 exclusive 模式下仍是 advisory 边界：任意 shell 字符串无法由 Bridge 做完整、可靠的路径语义证明。需要真正隔离时，应把后端运行在容器、VM 或 OS sandbox 中。

## 安装与发现

Bridge 只使用 Python 标准库。至少安装一个后端：

- ZCode：默认发现 `/Applications/ZCode.app`、`~/Applications/ZCode.app`、`/opt/ZCode` 或 `~/.local/opt/ZCode`；也可设置 `ZCODE_APP_PATH`，或同时设置 `ZCODE_BINARY` 与 `ZCODE_CLI_BUNDLE`。
- OpenCode：PATH 中的 `opencode`，或 `OPENCODE_BINARY=/absolute/path/to/opencode`。
- Pi：PATH 中的 `pi`，或 `PI_BINARY=/absolute/path/to/pi`。Pi 可执行文件所需 Node.js 也必须在 PATH 中。

Pi 应优先使用其自身内置 provider catalog。例如 DeepSeek V4 Flash 的原生模型引用是：

```json
{"providerId":"deepseek","modelId":"deepseek-v4-flash"}
```

同时在 Bridge 进程环境提供 `DEEPSEEK_API_KEY`。不要为同一模型重复创建自定义 Pi provider；原生定义已经包含 DeepSeek thinking 协议、1M context、输出限制、缓存价格和 reasoning replay 配置。

探测全部后端：

```bash
python3 server.py --probe
```

只为 ZCode 首次导入桌面 provider 配置：

```bash
python3 server.py --ensure-config
```

Pi 默认把 Bridge 会话放在 `~/.pi/agent/bridge-sessions`，可用 `PI_BRIDGE_SESSION_DIR` 覆盖。OpenCode 会启动仅监听 `127.0.0.1`、使用随机 Basic Auth 密码的共享本地 server；Bridge 退出时关闭它。

## 注册 MCP

直接启动：

```bash
python3 /absolute/path/to/coder-mcp-bridge/server.py
```

Codex `~/.codex/config.toml`：

```toml
[mcp_servers.coding_agent]
command = "python3"
args = ["/absolute/path/to/coder-mcp-bridge/server.py"]

[mcp_servers.coding_agent.env]
AGENT_MCP_DEFAULT_BACKEND = "zcode"
AGENT_MCP_TIMEOUT = "900"
```

仓库根部 `plugin.json` 的插件名和 MCP server id 已使用通用的 `coder-mcp-bridge` / `coding-agent` 命名。`${ZCODE_PLUGIN_ROOT}` 仍保留为 ZCode 插件加载器提供的根目录变量；它是加载协议的一部分，不代表 Bridge 只能调度 ZCode。

## 凭据与本地产物

- 不要把 API key 写入仓库文件；通过进程环境或各代理的本机凭据存储提供。
- `.env*`、`benchmark-results/`、SQLite lease 数据库和日志默认被 `.gitignore` 排除。
- 提交前建议检查 `git status --short`，并对暂存内容执行密钥扫描。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_MCP_DEFAULT_BACKEND` | `zcode` | MCP 连接启动时选中的后端 |
| `AGENT_MCP_TIMEOUT` | `900` | 整个运行默认超时秒数 |
| `AGENT_MCP_MAX_CONCURRENCY` | `0` | 可选安全上限；0 表示 Codex 决定并发 |
| `AGENT_MCP_LOG` | 空 | Bridge 诊断日志 |
| `AGENT_MCP_LEASE_DB` | 兼容旧路径 | 跨 Bridge 资源 lease SQLite 文件 |
| `PI_BINARY` | PATH 自动发现 | Pi 可执行文件 |
| `PI_BRIDGE_SESSION_DIR` | `~/.pi/agent/bridge-sessions` | Pi 持久会话目录 |
| `OPENCODE_BINARY` | PATH 自动发现 | OpenCode 可执行文件 |

旧的 `ZCODE_MCP_TIMEOUT`、`ZCODE_MCP_MAX_CONCURRENCY`、`ZCODE_MCP_LOG` 与 `ZCODE_MCP_LEASE_DB` 仍作为环境变量别名保留，现有安装无需迁移。

## 测试

不调用模型/provider API 的完整测试：

```bash
python3 -m unittest discover -s tests -v
```

这组测试覆盖 MCP 契约、backend 绑定、并发与资源冲突、事件投影、Pi 严格 JSONL、Pi 权限扩展、OpenCode 权限事件及 Windows stdio。若 PATH 没有 Node.js，Pi 扩展的直接 JavaScript 测试会 skip；Pi 运行时仍会在实际启动时验证扩展能否加载。

`tests/test_mcp.py` 与 `tests/test_stress.py` 是会调用当前所选 provider 的实战测试，应只在明确接受 API 用量时运行。

## 当前版本

`0.5.0-dev` 是 ZCode/OpenCode/Pi 统一控制面的开发版本。`0.4.0` 仍是已发布的 ZCode 里程碑。
