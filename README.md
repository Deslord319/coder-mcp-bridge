# Coder MCP Bridge

[English](README.en.md) | 简体中文

面向 Codex 等 MCP 客户端的事件驱动多编程代理调度桥。Coder MCP Bridge 将 ZCode、OpenCode 与 Pi 映射为一致的 `agent-*` 工具，使上层调度器可以统一启动、观察、引导、恢复和关闭代理任务，同时保留各后端的原生会话与推理能力。

当前开发版本：`0.5.0-dev`。

名称约定：项目与插件名为 `coder-mcp-bridge`，MCP server id 为 `vibe_bridge`，对外工具统一使用 `agent-*`；早期的 `zcode` / `zcode-reply` 工具已不再公开。

## 方法论与使用指南

Coder MCP Bridge 是规约驱动长程 Agent 开发中的执行控制面。完整方法涵盖 PRD、UX Flow、RFC、Story、职责分离、外置 backpressure 与独立验收：

- [规约驱动的长程 Agent 开发（中文）](docs/HARNESS-CONTEXT-ENGINEERING.zh-CN.md)
- [Specification-Driven Development for Long-Running Agents (English)](docs/HARNESS-CONTEXT-ENGINEERING.en.md)

文档第 4 章提供按使用顺序组织的快速指南，以及可直接使用的三条短提示词。

## 核心能力

- **统一控制面**：一次配置后，通过相同 MCP 工具调度 ZCode、OpenCode 或 Pi。
- **真实并发**：Bridge 默认不限制全局并发；MCP 调度器决定任务拆分和并发数。
- **资源互斥**：跨进程 SQLite lease 协调 worktree、模拟器和构建目录等共享资源。
- **事件驱动等待**：使用 revision 和最长 60 秒的事件等待替代固定时长轮询。
- **会话生命周期**：支持恢复、引导、中断、分支、压缩与显式关闭，能力按后端报告。
- **权限投影**：把 MCP 工作区和资源声明转换为各代理可执行的权限策略。
- **运行可观测性**：统一暴露 reasoning、工具事件、Token、上下文和终态。

## 工作方式

```text
MCP Client / Orchestrator
        │  agent-*
        ▼
Coder MCP Bridge
        ├── ZCode app-server
        ├── OpenCode HTTP + SSE
        └── Pi JSONL RPC
```

上层 MCP 调度器（例如 Codex）负责全局并发数、任务颗粒度与交付边界。Bridge 不替代上层调度，而是确保独立任务可以并发、冲突资源被正确串行化，并把不同代理的事件转换为稳定的 MCP 状态。

设置正数 `AGENT_MCP_MAX_CONCURRENCY` 可增加每个后端的操作员安全上限；默认值 `0` 表示不由 Bridge 限制并发。

## 支持的后端

| 能力 | ZCode | OpenCode | Pi |
|---|:---:|:---:|:---:|
| Prompt 运行 | ✓ | ✓ | ✓ |
| Durable goal | ✓ | — | — |
| Reasoning / usage 事件 | ✓ | ✓ | ✓ |
| Guide / interrupt / cancel | ✓ | ✓ | ✓ |
| 会话恢复 | ✓ | ✓ | ✓ |
| Branch / compact | ✓ | ✓ | ✓ |
| 后台任务投影 | ✓ | — | — |
| Tool allowlist | ✓ | 明确拒绝 | ✓ |
| Tool denylist | ✓ | ✓ | ✓ |
| 权限通道 | Reverse request | HTTP event | 强制策略扩展 |

使用 `agent-config {"action":"list"}` 获取本机实际安装状态和精确能力。调用方不应假定三个后端的原生能力完全一致。

## 性能与费用基准

### 测试方法

测试日期为 2026-08-06。三个代理均通过真实 MCP JSON-RPC over stdio 调用 `deepseek-v4-flash`，执行同一任务：使用结构化 SVG 绘制一只正在骑自行车、双脚踩在脚踏板上的鹈鹕。结果通过 XML、`viewBox`、必需分组 ID 和输出文件数量检查，并进行人工视觉评分。

为反映当前正确配置，下面的 Pi 数据采用其**原生 `deepseek/deepseek-v4-flash` provider 专项重测**，不是第一次错误自定义 provider 的结果。ZCode、OpenCode 与 Pi 重测并非同一轮同时起跑，因此该表适合比较单端运行表现，不应解读为严格的同步竞速实验。

### 当前推荐配置结果

| 后端 | 耗时 | 总 Token 流量 | 模型请求 | 工具调用 | 估算费用 | 视觉评分 |
|---|---:|---:|---:|---:|---:|---:|
| OpenCode | 3分13.691秒 | 87,750 | 3 | 6 | **$0.00733** | 78/100 |
| Pi（原生 provider 重测） | 4分35.062秒 | 83,923 | 3 | 2 | **$0.00788** | 85/100 |
| ZCode | 8分57.497秒 | 139,717 | 4 | 3 | **$0.02821** | 91/100 |

结论仅适用于本次 SVG 任务：

- **OpenCode** 用时和成本最低，适合快速、批量的实现任务。
- **Pi 原生 provider** 成本接近 OpenCode，工具调用最少，动作约束和结构可靠性更好。
- **ZCode** 视觉完成度最高，模型请求仍较克制，但深度推理带来更长耗时和更高成本。

不同代理上报 Token 的口径并不完全相同。“总 Token 流量”包含各后端报告的缓存读取；它不能直接等同于未缓存计费 Token，也不应单独作为代理效率结论。

### Pi provider 专项复测

Pi 第一次测试误用了临时自定义 provider，导致上下文、thinking 协议和收敛行为不正确。改用 Pi 0.83.0 内置的 `deepseek/deepseek-v4-flash`、`thinkingLevel: high` 后：

| 指标 | 错误自定义 provider | 原生 provider 重测 | 改善 |
|---|---:|---:|---:|
| 耗时 | 13分43.751秒 | **4分35.062秒** | 66.6% |
| 总 Token 流量 | 2,494,533 | **83,923** | 96.6% |
| 模型请求 | 33 | **3** | 90.9% |
| 工具调用 | 32 | **2** | 93.8% |
| 估算费用 | $0.03495 | **$0.00788** | 77.4% |
| 视觉评分 | 89/100 | **85/100** | 以少量细节换取显著收敛 |

因此，错误自定义 provider 的首测数据仅作为配置问题复盘，不代表 Pi 的正常性能。

### 费用口径

费用按测试时使用的 DeepSeek V4 Flash 单价估算：未缓存输入 `$0.14 / 1M tokens`、缓存输入 `$0.0028 / 1M tokens`、输出 `$0.28 / 1M tokens`；reasoning token 按输出计费。价格可能变化，请以 [DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/) 为准。估算值不等同于 provider 最终账单。

## 安装

Bridge 本身只依赖 Python 标准库。克隆仓库：

```bash
git clone https://github.com/Deslord319/coder-mcp-bridge.git
cd coder-mcp-bridge
python3 server.py --probe
```

至少安装一个后端：

- **ZCode**：默认探测 `/Applications/ZCode.app`、`~/Applications/ZCode.app`、`/opt/ZCode` 和 `~/.local/opt/ZCode`；也可设置 `ZCODE_APP_PATH`，或同时设置 `ZCODE_BINARY` 与 `ZCODE_CLI_BUNDLE`。
- **OpenCode**：确保 `opencode` 位于 `PATH`，或设置 `OPENCODE_BINARY`。
- **Pi**：确保 `pi` 及其 Node.js 运行时位于 `PATH`，或设置 `PI_BINARY`。

### Pi 与 DeepSeek

Pi 应使用其内置 provider catalog：

```json
{"providerId":"deepseek","modelId":"deepseek-v4-flash"}
```

同时在 Bridge 进程环境中设置 `DEEPSEEK_API_KEY`。不要为同一模型重复创建自定义 Pi provider；原生定义包含 DeepSeek thinking 协议、1M context、输出限制、缓存价格和 reasoning replay 配置。

## 注册 MCP

Bridge 使用标准 MCP stdio 传输。以 Codex 为例，在 `~/.codex/config.toml` 中添加：

```toml
[mcp_servers.vibe_bridge]
command = "python3"
args = ["/absolute/path/to/coder-mcp-bridge/server.py"]

[mcp_servers.vibe_bridge.env]
AGENT_MCP_DEFAULT_BACKEND = "zcode"
AGENT_MCP_TIMEOUT = "900"
```

也可以直接运行：

```bash
python3 /absolute/path/to/coder-mcp-bridge/server.py
```

其他 MCP 客户端使用等价的 stdio server 配置即可。仓库根目录的 `plugin.json` 使用项目名 `coder-mcp-bridge` 与 MCP server id `vibe_bridge`。`${ZCODE_PLUGIN_ROOT}` 是 ZCode 插件加载器提供的根目录变量，保留它不代表 Bridge 只能调度 ZCode。

## MCP 工具

| 工具 | 说明 |
|---|---|
| `agent-config` | 获取、设置、重置后端，或列出安装状态与能力 |
| `agent-start` | 非阻塞启动运行并立即返回 `runId` |
| `agent-wait` | 等待 revision 变化或终态；推荐的主进度通道 |
| `agent-observe` | 读取有界事件、reasoning、工具、usage、context 和资源状态 |
| `agent-control` | Guide、interrupt、cancel；ZCode 额外支持 goal/background 控制 |
| `agent-recover` | 列出或接管当前后端的持久会话 |
| `agent-branch` | 从消息、turn 或 checkpoint 创建分支，粒度取决于后端 |
| `agent-context` | 检查或压缩上下文 |
| `agent-close` | 关闭运行时并释放资源，不删除持久会话 |

典型调用：

```json
{"name":"agent-config","arguments":{"action":"set","backend":"pi"}}
{"name":"agent-start","arguments":{"prompt":"实现并测试登录流程","cwd":"/path/to/worktree","workspaceAccess":"exclusive"}}
{"name":"agent-wait","arguments":{"runId":"run_...","afterRevision":3,"timeoutMs":30000}}
{"name":"agent-close","arguments":{"runId":"run_..."}}
```

后端配置在一个 MCP 连接内设置一次。`agent-start` 不接受 backend 参数；切换后端只影响未来运行，现有 `runId` 始终绑定原后端。

## 并发与资源

独立任务应使用独立 worktree，并发发送多个 `agent-start`。仅为真实冲突资源声明相同 key：

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

绝对路径 resource 同时成为结构化文件权限根：`exclusive` 允许写入，`shared` 只读。`simulator:*` 等抽象 key 只参与冲突判断。

## 权限与安全

- `workspaceAccess: "shared"` 拒绝结构化写权限；Pi 同时拒绝 shell，OpenCode 对 shell 权限事件也拒绝。
- `workspaceAccess: "exclusive"` 允许原生文件工具访问 `cwd` 和声明的绝对路径 resource。
- Pi 的 `mode: "plan"` 始终使用只读工具策略。
- Pi 强制加载 `pi_bridge_extension.mjs`，不依赖提示词约束文件权限。
- OpenCode 的外部目录请求只有在所有结构化路径均位于声明根内时才允许。
- Shell 命令在 exclusive 模式下仍属于 advisory 边界；需要强隔离时，应使用容器、VM 或 OS sandbox。
- API key 应通过环境或代理本机凭据存储提供，不要写入仓库文件。

`.env*`、`benchmark-results/`、SQLite lease 数据库和日志已被 `.gitignore` 排除。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_MCP_DEFAULT_BACKEND` | `zcode` | MCP 连接启动时选中的后端 |
| `AGENT_MCP_TIMEOUT` | `900` | 默认运行超时，单位秒 |
| `AGENT_MCP_MAX_CONCURRENCY` | `0` | 每后端可选安全上限；0 表示由上层 MCP 调度器决定 |
| `AGENT_MCP_LOG` | 空 | Bridge 诊断日志路径 |
| `AGENT_MCP_LEASE_DB` | 兼容旧路径 | 跨进程资源 lease SQLite 文件 |
| `PI_BINARY` | 自动探测 | Pi 可执行文件 |
| `PI_BRIDGE_SESSION_DIR` | `~/.pi/agent/bridge-sessions` | Pi 持久会话目录 |
| `OPENCODE_BINARY` | 自动探测 | OpenCode 可执行文件 |

旧的 `ZCODE_MCP_TIMEOUT`、`ZCODE_MCP_MAX_CONCURRENCY`、`ZCODE_MCP_LOG` 和 `ZCODE_MCP_LEASE_DB` 仍作为兼容别名保留。

## 测试

运行不调用模型 API 的完整测试：

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖 MCP 契约、后端绑定、并发与资源冲突、事件投影、Pi 严格 JSONL、Pi 权限扩展、OpenCode 权限事件和 Windows UTF-8 stdio。`tests/test_mcp.py` 与 `tests/test_stress.py` 会调用当前 provider，只应在明确接受 API 用量时运行。

## 友情链接

- [Linux.do](https://linux.do/) — 面向开发者和技术爱好者的社区。

## 许可证

本项目采用 [MIT License](LICENSE)。
