# ZCode MCP Bridge(ZCode MCP 桥接)

> 把 **ZCode** 作为完整的智能体(agent)暴露给任意 MCP 客户端 —— 在 Claude Code、Codex、Cursor 或 ZCode 自身里,通过两个工具把复杂编码任务委托给一个独立的 ZCode 智能体去执行。

> [!WARNING]
> **当前默认权限模式是 `yolo`。** 如果调用时没有显式传入 `mode` 或 `sandbox`,ZCode 会自动批准工具调用,包括修改文件和执行 shell 命令。处理不可信提示词、重要仓库或生产环境时,请显式使用 `mode: "plan"` 或 `sandbox: "read-only"`;确认方案后再按需切换到 `build` / `workspace-write`。只有在明确接受完全访问风险时才使用 `yolo` / `danger-full-access`。

本实现**对齐 Codex 官方 `mcp-server`**(`codex mcp-server` 会暴露 `codex` / `codex-reply` 两个工具),本项目提供与之等价的:

| Codex mcp-server | ZCode MCP Bridge | 作用 |
|---|---|---|
| `codex` | `zcode` | 运行一个新会话 |
| `codex-reply` | `zcode-reply` | 用会话 ID(threadId)继续已有会话 |

## 工作原理

```
MCP 客户端(Codex / Claude Code / Cursor / ZCode)
        │  MCP 标准输入输出协议(JSON-RPC 2.0)
        ▼
server.py  ── 提供工具:zcode、zcode-reply
        │  启动子进程并设置 ELECTRON_RUN_AS_NODE=1
        ▼
ZCode 安装包内置 CLI 包(glm/zcode.cjs,无界面 headless 模式)
        │  参数 --prompt / --resume <sess_...> --json
        ▼
ZCode 智能体会话(包含全部已启用插件:技能、Bash、MCP 工具等)
```

- **零额外运行时依赖**:直接驱动 ZCode 安装包内置的 CLI 包(通过 Electron 的 node 模式运行),无需另行安装 Node.js 或任何 Python 包。
- **会话连续性**:`zcode-reply` 通过 `--resume <sessionId>` 续接,上下文跨调用保持(依赖 ZCode 的持久化会话)。
- **能力继承**:无界面会话与桌面端共享同一套插件 / 技能 / MCP 配置。例如启用 `ios-simulator` 插件后,通过桥接的会话可以直接使用 `mcp__plugin_ios-simulator_ios-simulator__*` 这 20 个 iOS 工具(已实测可用)。

## 能力与实测结果

- 完整 MCP 握手(`initialize` / `ping` / `tools/list` / `tools/call`),标准输入输出传输
- 新建会话 + 会话续接,支持多轮对话(实测单会话连续续接 5 轮正常)
- 复杂任务可用:文件读写、Bash 工具、多步任务、中文回复
- 并发:通过 `ZCODE_MCP_MAX_CONCURRENCY` 限流,实测 6 路并行正常
- 稳定性:连续 / 并行压测 0 失败(详见[测试](#测试))
- 错误处理:参数校验、超时、CLI 异常均以结构化 `isError` 返回,不会导致服务器崩溃

## 平台支持范围

| 操作系统 | CPU 架构 | 支持状态 | 默认发现路径 | 备注 |
|---|---|---|---|---|
| macOS | Apple Silicon(arm64) | 支持 | `/Applications/ZCode.app`、`~/Applications/ZCode.app` | 使用标准 `.app/Contents/...` 布局 |
| macOS | Intel(x64) | 支持 | `/Applications/ZCode.app`、`~/Applications/ZCode.app` | 与 Apple Silicon 使用相同的应用布局 |
| Linux | ARM64(aarch64) | 支持,已验证 | `/opt/ZCode`、`~/.local/opt/ZCode` | 已在 Ubuntu 24.04 ARM64 + ZCode 3.6.5 验证完整 MCP 链路 |
| Linux | x64 | 支持 | `/opt/ZCode`、`~/.local/opt/ZCode` | 使用官方 `.deb` 安装或将运行时解包至受支持路径 |
| Windows | x64 / ARM64 | 基础兼容,待实机验证 | 尚未实现 | 已处理 MCP UTF-8 stdio、日志编码及 Electron 空输出流;当前需手动指定运行时路径 |

macOS 与 Linux 均直接运行 ZCode 内置 CLI,不启动桌面窗口,不依赖 X11、Wayland 或 Xvfb。Windows 已具备基础进程与 MCP stdio 兼容处理,但尚未实现默认安装路径发现,也没有完成端到端实机验证。桥接本身仅使用 Python 标准库,CPU 架构兼容性取决于所安装的官方 ZCode 包。

如果 ZCode 没有安装在默认位置,可设置 `ZCODE_APP_PATH`;也可以分别用 `ZCODE_BINARY` 和 `ZCODE_CLI_BUNDLE` 指定 Electron 可执行文件与 `zcode.cjs`。Linux AppImage 用户应先解包或挂载 AppImage,再把这些变量指向对应文件。Windows 当前需要同时设置 `ZCODE_APP_PATH`、`ZCODE_BINARY` 和 `ZCODE_CLI_BUNDLE`;完成实际安装路径验证并加入自动发现前,不应视为完整支持。

## 目录结构

```
zcode-mcp-plugin/
├── plugin.json          # ZCode 插件清单(贡献 MCP 服务器)
├── server.py            # MCP 服务器实现(自包含,仅用 Python 标准库)
├── README.md
└── tests/
    ├── test_mcp.py      # 功能测试:握手/工具/新会话/续接/复杂任务/错误/并发/稳定性
    ├── test_stress.py   # 压测:高并发/长稳定循环/多轮对话
    └── test_windows_streams.py # Windows UTF-8 stdio、日志与空输出流回归测试
```

## 环境要求

- **ZCode** 已安装于上述[支持平台](#平台支持范围)的默认路径,或已通过环境变量指定运行时位置
- **`~/.zcode/cli/config.json` 已配置模型提供方(model provider)** —— CLI 需要它才能运行无界面会话。若桌面端 ZCode 已在用某个提供方,可一键导入:

```bash
python3 server.py --ensure-config
```

它会从桌面配置 `~/.zcode/v2/config.json` 复制已启用的提供方 + 模型到 CLI 配置(优先选择**带静态 API 密钥**的提供方;只保留该提供方,已有配置会先备份为 `.bak`)。提供方 ID 会规范化为字母数字形式,因为 CLI 的模型目标解析器会拒绝桌面端的 UUID / `builtin:` 前缀 ID。

> 注意:若提供方来自官方 OAuth 登录(无 API 密钥),`--ensure-config` 会跳过它;当没有可用提供方时,需在 CLI 侧登录或配置(`zcode login`)。

## 快速开始

```bash
# 1. 检查环境
python3 server.py --probe

# 2. 一键配置 CLI 模型提供方(首次)
python3 server.py --ensure-config

# 3. 冒烟测试(可选)
python3 tests/test_mcp.py --fast

# 4. 以 MCP 服务器方式运行(配合任一客户端注册,见下)
python3 /path/to/zcode-mcp-plugin/server.py
```

## 运行方式

### 1. 作为 ZCode 插件(自动贡献 MCP 服务器)

把本目录放进 ZCode 插件根目录,或在插件市场添加本目录,启用 `zcode-mcp-bridge` 后 ZCode 会自动启动 `zcode-agent` MCP 服务器,会话里直接出现 `mcp__zcode-agent__zcode` 与 `mcp__zcode-agent__zcode-reply` 工具。

### 2. 作为独立 MCP 服务器(任意 MCP 客户端)

```
python3 /path/to/zcode-mcp-plugin/server.py
```

#### Codex(`~/.codex/config.toml`)

```toml
[mcp_servers.zcode]
command = "python3"
args = ["/path/to/zcode-mcp-plugin/server.py"]
```

然后用 `codex mcp list` 确认;在 Codex 中即可调用 `zcode` / `zcode-reply`。

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

用同一标准输入输出命令注册:`python3 /path/to/zcode-mcp-plugin/server.py`。

## 使用示例

在任意 MCP 客户端中,让主智能体这样委托任务:

> "用 `zcode` 工具运行一次 ZCode 会话,任务:在当前仓库实现用户认证功能,写完代码后跑一遍测试。结束后用 `zcode-reply` 追问一个后续问题,比如要求补文档。"

主智能体的典型调用序列:

```json
// 1. 新建会话
{ "name": "zcode", "arguments": { "prompt": "实现 xxx,完成后回复 DONE", "cwd": "/path/to/project", "mode": "build" } }

// 2. 拿到返回的会话 ID 后继续
{ "name": "zcode-reply", "arguments": { "threadId": "sess_...", "prompt": "给刚才的实现补一段 README" } }
```

只做分析、不允许修改文件或执行高风险操作时,建议从只读模式开始:

```json
{ "name": "zcode", "arguments": { "prompt": "分析当前项目并给出修改方案", "cwd": "/path/to/project", "sandbox": "read-only" } }
```

## 工具参数

### `zcode` — 运行 ZCode 会话

| 参数 | 类型 | 说明 |
|---|---|---|
| `prompt` *(必填)* | 字符串 | 初始用户提示词 |
| `threadId` | 字符串 | 已有会话 ID(`sess_...`)用于续接;不填则新建会话 |
| `cwd` | 字符串 | 工作目录(映射 `--cwd`) |
| `sandbox` | 字符串 | `read-only`→plan、`workspace-write`→build、`danger-full-access`→yolo |
| `mode` | 字符串 | ZCode 权限模式:build / edit / plan / yolo(默认 yolo) |
| `maxTurns` | 整数 | 最大轮数 ⚠️ 见[已知限制](#已知限制重要) |
| `allowedTools` | 字符串数组 | 工具白名单 ⚠️ 见[已知限制](#已知限制重要) |
| `disallowedTools` | 字符串数组 | 工具黑名单(映射 `--disallowed-tools`,真实生效) |
| `timeout` | 整数 | 超时秒数(默认 900,环境变量 `ZCODE_MCP_TIMEOUT`) |

### `zcode-reply` — 继续会话

参数:`threadId`(必填)+ `prompt`(必填),其余同上。

模型不通过 MCP 调用参数覆盖。桥接器始终使用 `~/.zcode/cli/config.json`
中 `model.main` 指定的默认模型及其 provider 凭据，模型切换应由用户在
ZCode/CLI 配置中完成。

返回结构:`threadId`、`traceId`、`turnId`、`usage`、`projection` + 模型回复正文。

## 已知限制(重要)

1. **`maxTurns` / `allowedTools` 被 ZCode CLI 0.16.1 忽略**。这两个参数出现在 `zcode --help` 里,但 0.16.1 的参数解析器(`parseArgs` 严格模式)并不支持它们,传入会以退出码 1 报 `Unknown option`。本插件为兼容而接受并忽略它们(见 `server.py` 中 `UNSUPPORTED_OPTIONS` 的注释)。`disallowed-tools` 是真实可用的。
2. **一次会话即一个进程**:每次 `zcode` / `zcode-reply` 调用都会启动一个无界面 CLI 进程;`threadId` 通过 `--resume` 保持会话连续性(实测可靠,多轮对话工作正常),但上下文在进程间传递依赖持久化会话,不保留进程内状态。
3. **并发上限**:默认最多 2 个并行 ZCode 会话(信号量控制),超出排队。调大 `ZCODE_MCP_MAX_CONCURRENCY` 前请确认你的模型提供方配额。
4. **提供方配置**:若 CLI 侧提供方需要 CLI 专属登录(如智谱官方 OAuth),`--ensure-config` 只复制静态配置,可能仍需 `zcode login`。

## 故障排查

| 症状 | 原因 / 处理 |
|---|---|
| 服务器启动即报 `ZCode runtime not found` | 未安装 ZCode,或通过 `ZCODE_APP_PATH`、`ZCODE_BINARY`、`ZCODE_CLI_BUNDLE` 指定实际位置 |
| 工具调用返回 `[zcode-error:zcode_exit_error] ... Model config is missing` | CLI 缺少模型提供方。运行 `python3 server.py --ensure-config`,或在 `~/.zcode/cli/config.json` 手动配置 `model.main`(格式为 `provider-id/model-id`) |
| 工具调用返回 `Unknown option '--max-turns'` | 调用方显式传了 `maxTurns` 而本插件尚未忽略它(正常会被忽略)。这是 ZCode CLI 0.16.1 的已知缺陷 |
| 调用超时 | 增大 `timeout` 参数或 `ZCODE_MCP_TIMEOUT`;ZCode 会话默认最长约 15 分钟,复杂任务请给足时间 |
| 插件工具 `mcp__plugin_*` 不可见 | 确认对应插件已在 `~/.zcode/cli/config.json` 的 `enabledPlugins` 中启用(如 `ios-simulator@zcode-plugins-official`) |
| 想查看服务器日志 | 设置 `ZCODE_MCP_LOG=/path/to/log` 后重启,日志会包含每次启动的完整命令与退出码 |
| 怀疑 ZCode 发现路径不对 | `python3 server.py --probe` 打印实际使用的 binary / bundle / 配置路径 |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ZCODE_APP_PATH` | 平台默认路径 | ZCode 应用/运行时根目录 |
| `ZCODE_BINARY` | 自动发现 | Electron 可执行文件路径覆盖 |
| `ZCODE_CLI_BUNDLE` | 自动发现 | CLI 包 `zcode.cjs` 路径覆盖 |
| `ZCODE_MCP_TIMEOUT` | `900` | 单次工具调用超时(秒) |
| `ZCODE_MCP_MAX_CONCURRENCY` | `2` | 并行 ZCode 会话上限 |
| `ZCODE_MCP_LOG` | 空(关闭) | 服务器诊断日志文件路径 |

## 测试

```bash
# 功能测试(握手/工具/新会话/续接/复杂任务/错误处理/并发/稳定性)
python3 tests/test_mcp.py

# 压测(默认 4 并发 + 6 轮稳定 + 3 轮多轮对话;可调参)
python3 tests/test_stress.py --concurrency 6 --stability 5 --turns 5
```

实测结果(DeepSeek deepseek-v4-flash):

- `tests/test_mcp.py`:**22 通过 / 0 失败**(含真实文件创建 + Bash 工具调用任务)
- `tests/test_stress.py`(4 并发):**8 通过 / 0 失败**,4 路并行约 10 秒,稳定循环平均约 5 秒/次
- 6 并发 + 5 轮多轮对话压测:**10 通过 / 0 失败**

## 安全提示

- 服务器把 `prompt` 原样传给 ZCode 无界面 CLI;未指定 `mode` / `sandbox` 时默认使用 `--mode yolo`(自动批准全部工具)。生产环境请显式传 `mode: "plan"` 或 `sandbox: "read-only"`,或通过 `disallowedTools` 收紧工具集。
- 服务器不校验 / 不透传 shell 命令;权限模型完全由 ZCode 的 mode 控制。
- 不要把包含 API 密钥的配置提交到版本库。

## 友情链接

- [Linux.do](https://linux.do/) —— 一个面向开发者和技术爱好者的社区

## 许可证

MIT
