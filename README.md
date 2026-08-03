# ZCode MCP Bridge

> 把 **ZCode** 作为完整 agent 暴露给任意 MCP 客户端 —— 在 Claude Code、Codex、Cursor 或 ZCode 自身里,通过两个工具把复杂编码任务委托给一个独立的 ZCode agent。

实现上**对齐 Codex 官方 `mcp-server`**(`codex mcp-server` 暴露 `codex` / `codex-reply` 两个工具),本插件提供等价的:

| Codex mcp-server | ZCode MCP Bridge | 作用 |
|---|---|---|
| `codex` | `zcode` | 运行一个新会话 |
| `codex-reply` | `zcode-reply` | 用 threadId 继续已有会话 |

## 工作原理

```
MCP 客户端 (Codex / Claude Code / Cursor / ZCode)
        │  MCP stdio (JSON-RPC 2.0)
        ▼
server.py  ── tools: zcode, zcode-reply
        │  spawn + ELECTRON_RUN_AS_NODE=1
        ▼
ZCode.app 内置 CLI bundle (glm/zcode.cjs, headless 模式)
        │  --prompt / --resume <sess_...>  --json
        ▼
ZCode agent 会话(含全部已启用插件:skills、Bash、MCP 工具…)
```

- **零额外运行时依赖**:直接驱动 ZCode.app 内置的 CLI bundle(Electron node 模式),不需要安装 Node.js 或任何 Python 包。
- **会话连续性**:`zcode-reply` 通过 `--resume <sessionId>` 续接,上下文跨调用保持(依赖 ZCode 持久化 session)。
- **能力继承**:headless 会话与桌面端共享同一套插件/技能/MCP 配置。例如启用了 `ios-simulator` 插件后,通过 bridge 的会话可以直接使用 `mcp__plugin_ios-simulator_ios-simulator__*` 的 20 个 iOS 工具(已实测)。

## 能力与实测结果

- 完整 MCP 握手(`initialize` / `ping` / `tools/list` / `tools/call`),stdio 传输
- 新会话 + 会话续接,支持多轮对话(实测单会话 5 轮续接正常)
- 复杂任务可用:文件读写、Bash 工具、多步任务、中文回复
- 并发:通过 `ZCODE_MCP_MAX_CONCURRENCY` 限流,实测 6 路并行正常
- 稳定性:连续/并行压测 0 失败(见 [测试](#测试))
- 错误处理:参数校验、超时、CLI 异常均以结构化 `isError` 返回,不崩服务器

## 目录结构

```
zcode-mcp-plugin/
├── plugin.json          # ZCode 插件清单(贡献 MCP server)
├── server.py            # MCP server 实现(自包含,仅标准库)
├── README.md
└── tests/
    ├── test_mcp.py      # 功能测试:握手/工具/新会话/续接/复杂任务/错误/并发/稳定性
    └── test_stress.py   # 压测:高并发/长稳定循环/多轮对话
```

## 环境要求

- **ZCode.app** 已安装(默认 `/Applications/ZCode.app`,可用 `ZCODE_APP_PATH` 覆盖)
- **`~/.zcode/cli/config.json` 配置了 model provider** —— CLI 需要它才能跑 headless 会话。若桌面端 ZCode 已在用某个 provider,可一键导入:

```bash
python3 server.py --ensure-config
```

它会从桌面配置 `~/.zcode/v2/config.json` 复制启用的 provider + 模型到 CLI 配置(优先选择**带静态 apiKey** 的 provider;仅保留该 provider,已有配置会先备份为 `.bak`)。provider id 会规范化为字母数字形式,因为 CLI 的 model-target 解析器会拒绝桌面端的 UUID / `builtin:` 前缀 id。

> 注意:若 provider 来自官方 OAuth 登录(无 apiKey),`--ensure-config` 会跳过它;无可用 provider 时需在 CLI 侧登录/配置(`zcode login`)。

## 快速开始

```bash
# 1. 检查环境
python3 server.py --probe

# 2. 一键配置 CLI model provider(首次)
python3 server.py --ensure-config

# 3. 冒烟测试(可选)
python3 tests/test_mcp.py --fast

# 4. 以 MCP server 方式运行(配合任一客户端注册,见下)
python3 /path/to/zcode-mcp-plugin/server.py
```

## 运行方式

### 1. 作为 ZCode 插件(自动贡献 MCP server)

把本目录放进 ZCode 插件根目录,或在插件市场添加本目录,启用 `zcode-mcp-bridge` 后 ZCode 会自动启动 `zcode-agent` MCP server,会话里直接出现 `mcp__zcode-agent__zcode` 与 `mcp__zcode-agent__zcode-reply` 工具。

### 2. 作为独立 MCP server(任意 MCP 客户端)

```
python3 /path/to/zcode-mcp-plugin/server.py
```

#### Codex(`~/.codex/config.toml`)

```toml
[mcp_servers.zcode]
command = "python3"
args = ["/path/to/zcode-mcp-plugin/server.py"]
```

然后 `codex mcp list` 确认;在 Codex 中即可调用 `zcode` / `zcode-reply`。

#### Claude Code(`~/.claude.json` 或项目 `.mcp.json`)

```json
{
  "mcpServers": {
    "zcode": {
      "command": "python3",
      "args": ["/path/to/zcode-mcp-plugin/server.py"]
    }
  }
}
```

#### Cursor / 其他 MCP 客户端

用同一 stdio 命令注册:`python3 /path/to/zcode-mcp-plugin/server.py`。

## 使用示例

在任意 MCP 客户端中,让主 agent 这样委托任务:

> "用 `zcode` 工具运行一次 ZCode 会话,任务:在当前仓库实现用户认证功能,写完代码后跑一遍测试。结束后用 `zcode-reply` 追问一个后续问题,比如要求补文档。"

主 agent 的典型调用序列:

```json
// 1. 新建会话
{ "name": "zcode", "arguments": { "prompt": "实现 xxx,完成后回复 DONE", "cwd": "/path/to/project", "mode": "build" } }

// 2. 拿到返回的 threadId 后继续
{ "name": "zcode-reply", "arguments": { "threadId": "sess_...", "prompt": "给刚才的实现补一段 README" } }
```

## 工具参数

### `zcode` — 运行 ZCode 会话

| 参数 | 类型 | 说明 |
|---|---|---|
| `prompt` *(必填)* | string | 初始用户提示词 |
| `threadId` | string | 已有会话 id(`sess_...`)用于续接;不填则新建会话 |
| `model` | string | 模型覆盖(如 `deepseek/deepseek-v4-flash`),经 `ZCODE_MODEL` 环境变量传递 |
| `cwd` | string | 工作目录(映射 `--cwd`) |
| `sandbox` | string | `read-only`→plan、`workspace-write`→build、`danger-full-access`→yolo |
| `mode` | string | ZCode 权限模式:build / edit / plan / yolo(默认 yolo) |
| `maxTurns` | int | 最大轮数 ⚠️ 见 [已知限制](#已知限制重要) |
| `allowedTools` | string[] | 工具白名单 ⚠️ 见 [已知限制](#已知限制重要) |
| `disallowedTools` | string[] | 工具黑名单(映射 `--disallowed-tools`,真实生效) |
| `timeout` | int | 超时秒数(默认 900,环境变量 `ZCODE_MCP_TIMEOUT`) |

### `zcode-reply` — 继续会话

参数:`threadId`(必填)+ `prompt`(必填),其余同上。

返回结构:`threadId`、`traceId`、`turnId`、`usage`、`projection` + 模型回复正文。

## 已知限制(重要)

1. **`maxTurns` / `allowedTools` 被 ZCode CLI 0.16.1 忽略**。这两个参数出现在 `zcode --help` 里,但 0.16.1 的参数解析器(`parseArgs` strict)并不支持它们,传了会以退出码 1 报 `Unknown option`。本插件为兼容而接受并忽略它们(见 `server.py` 中 `UNSUPPORTED_OPTIONS` 注释)。`disallowed-tools` 是真实可用的。
2. **单会话即单进程**:每次 `zcode` / `zcode-reply` 调用启动一个 headless CLI 进程;`threadId` 通过 `--resume` 保持会话连续性(实测可靠,多轮对话工作正常),但上下文在进程间传递靠持久化 session,不保留进程内状态。
3. **并发上限**:默认最多 2 个并行 ZCode 会话(信号量),超出排队。调大 `ZCODE_MCP_MAX_CONCURRENCY` 前请确认你的模型 provider 配额。
4. **provider 配置**:若 CLI 侧 provider 需要 CLI 专属登录(如智谱官方 OAuth),`--ensure-config` 只复制静态配置,可能仍需 `zcode login`。

## 故障排查

| 症状 | 原因 / 处理 |
|---|---|
| 服务器启动即报 `ZCode.app not found` | 未安装 ZCode.app,或设置 `ZCODE_APP_PATH=/path/to/ZCode.app` |
| 工具调用返回 `[zcode-error:zcode_exit_error] ... Model config is missing` | CLI 缺少 model provider。运行 `python3 server.py --ensure-config`,或手动在 `~/.zcode/cli/config.json` 配置 `model.main`(如 `deepseek/deepseek-v4-flash`) |
| 工具调用返回 `Unknown option '--max-turns'` | 说明调用方显式传了 `maxTurns` 而本插件尚未忽略它(正常会被忽略)。检查是否直接用了较新的 `server.py`;这是 ZCode CLI 0.16.1 的已知缺陷 |
| 调用超时 | 增大 `timeout` 参数或 `ZCODE_MCP_TIMEOUT`;ZCode 会话默认最长约 15 分钟,复杂任务请给足时间 |
| 插件工具 `mcp__plugin_*` 不可见 | 确认对应插件在 `~/.zcode/cli/config.json` 的 `enabledPlugins` 中启用(如 `ios-simulator@zcode-plugins-official`) |
| 想查看服务器日志 | 设置 `ZCODE_MCP_LOG=/path/to/log` 后重启,日志含每次 spawn 的完整命令与退出码 |
| 怀疑 ZCode 发现路径不对 | `python3 server.py --probe` 打印实际使用的 binary / bundle / 配置路径 |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ZCODE_APP_PATH` | `/Applications/ZCode.app` | ZCode.app 路径 |
| `ZCODE_CLI_BUNDLE` | `<app>/Contents/Resources/glm/zcode.cjs` | CLI bundle 路径覆盖 |
| `ZCODE_MCP_TIMEOUT` | `900` | 单次工具调用超时(秒) |
| `ZCODE_MCP_MAX_CONCURRENCY` | `2` | 并行 ZCode 会话上限 |
| `ZCODE_MCP_LOG` | 空(关闭) | 服务器诊断日志文件路径 |
| `ZCODE_MODEL` | — | 每次调用的模型覆盖(工具参数 `model` 优先) |

## 测试

```bash
# 功能测试(握手/工具/新会话/续接/复杂任务/错误处理/并发/稳定性)
python3 tests/test_mcp.py

# 压测(默认 4 并发 + 6 轮稳定 + 3 轮多轮对话;可调参)
python3 tests/test_stress.py --concurrency 6 --stability 5 --turns 5
```

实测结果(DeepSeek deepseek-v4-flash):

- `tests/test_mcp.py`:**22 通过 / 0 失败**(含真实文件创建 + Bash 工具调用任务)
- `tests/test_stress.py`(4 并发):**8 通过 / 0 失败**,4 路并行约 10s,稳定循环平均 ~5s/次
- 6 并发 + 5 轮多轮对话压测:**10 通过 / 0 失败**

## 安全提示

- 服务器把 `prompt` 原样传给 ZCode headless CLI,默认 `--mode yolo`(自动批准全部工具)。生产环境请显式传 `mode: "plan"` 或 `sandbox: "read-only"`,或通过 `disallowedTools` 收紧工具集。
- 服务器不校验/不透传 shell 命令;权限模型完全由 ZCode 的 mode 控制。
- 不要提交包含 apiKey 的配置到版本库。

## License

MIT
