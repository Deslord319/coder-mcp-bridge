# Coder MCP Bridge

English | [简体中文](README.md)

An event-driven, multi-agent scheduling bridge for Codex and other MCP clients. Coder MCP Bridge maps ZCode, OpenCode, and Pi onto one consistent set of `agent-*` tools, allowing an orchestrator to start, observe, guide, recover, and close coding-agent runs while preserving each backend's native sessions and reasoning capabilities.

Current development version: `0.5.0-dev`.

Naming: the project and plugin are named `coder-mcp-bridge`, the MCP server id is `vibe_bridge`, and all public tools use the `agent-*` prefix. The early `zcode` / `zcode-reply` tools are no longer exposed.

## Highlights

- **Unified control plane** — configure a backend once, then operate ZCode, OpenCode, or Pi through the same MCP tools.
- **Real concurrency** — the bridge imposes no global limit by default; the MCP orchestrator determines task decomposition and concurrency.
- **Resource coordination** — cross-process SQLite leases serialize conflicting worktrees, simulators, and build directories.
- **Event-driven waiting** — revision-based waits of up to 60 seconds replace fixed-duration polling.
- **Session lifecycle** — recovery, guidance, interruption, branching, compaction, and explicit shutdown, subject to backend capabilities.
- **Permission projection** — MCP workspace and resource declarations become enforceable backend policies.
- **Observability** — reasoning, tool events, token usage, context, and terminal state are exposed consistently.

## Architecture

```text
MCP Client / Orchestrator
        │  agent-*
        ▼
Coder MCP Bridge
        ├── ZCode app-server
        ├── OpenCode HTTP + SSE
        └── Pi JSONL RPC
```

The upstream MCP orchestrator, such as Codex, owns global concurrency, task granularity, and delivery boundaries. The bridge does not replace orchestration: it makes independent work truly concurrent, serializes genuinely conflicting resources, and projects backend-specific events into stable MCP state.

A positive `AGENT_MCP_MAX_CONCURRENCY` sets an optional per-backend operator safety limit. Its default value, `0`, leaves concurrency to the upstream orchestrator.

## Supported Backends

| Capability | ZCode | OpenCode | Pi |
|---|:---:|:---:|:---:|
| Prompt runs | ✓ | ✓ | ✓ |
| Durable goals | ✓ | — | — |
| Reasoning / usage events | ✓ | ✓ | ✓ |
| Guide / interrupt / cancel | ✓ | ✓ | ✓ |
| Session recovery | ✓ | ✓ | ✓ |
| Branch / compact | ✓ | ✓ | ✓ |
| Background-task projection | ✓ | — | — |
| Tool allowlist | ✓ | Explicitly rejected | ✓ |
| Tool denylist | ✓ | ✓ | ✓ |
| Permission channel | Reverse request | HTTP event | Enforced policy extension |

Call `agent-config {"action":"list"}` to inspect installed backends and their exact capabilities. Clients should not assume that all native features are identical.

## Performance and Cost Benchmark

### Methodology

The benchmark was run on 2026-08-06. Each agent called `deepseek-v4-flash` through real MCP JSON-RPC over stdio and received the same task: produce a structured SVG of a pelican riding a bicycle, with both feet on the pedals. Outputs were validated for XML structure, `viewBox`, required group IDs, and output-file count, followed by a manual visual review.

The Pi row below uses a **dedicated rerun with Pi's native `deepseek/deepseek-v4-flash` provider**. It does not use the first, incorrectly configured custom-provider result. The ZCode, OpenCode, and corrected Pi measurements were not started in the same concurrent window, so the table compares individual run performance rather than a strict synchronized race.

### Recommended-configuration results

| Backend | Wall time | Total token traffic | Model requests | Tool calls | Estimated cost | Visual score |
|---|---:|---:|---:|---:|---:|---:|
| OpenCode | 3m 13.691s | 87,750 | 3 | 6 | **$0.00733** | 78/100 |
| Pi (native-provider rerun) | 4m 35.062s | 83,923 | 3 | 2 | **$0.00788** | 85/100 |
| ZCode | 8m 57.497s | 139,717 | 4 | 3 | **$0.02821** | 91/100 |

For this SVG task only:

- **OpenCode** had the lowest latency and cost, making it attractive for fast or high-volume implementation work.
- **Pi with its native provider** cost nearly the same as OpenCode, used the fewest tool calls, and followed structural and pose constraints more reliably.
- **ZCode** achieved the strongest visual finish with a restrained request count, at the cost of longer deep-reasoning time and higher spend.

Token accounting differs between agents. “Total token traffic” includes cache reads reported by each backend; it is not equivalent to uncached billable input and should not be used as the sole efficiency metric.

### Dedicated Pi provider rerun

Pi's first benchmark accidentally used a temporary custom provider, producing incorrect context, thinking-protocol, and convergence behavior. Switching Pi 0.83.0 to its built-in `deepseek/deepseek-v4-flash` model with `thinkingLevel: high` produced:

| Metric | Incorrect custom provider | Native-provider rerun | Improvement |
|---|---:|---:|---:|
| Wall time | 13m 43.751s | **4m 35.062s** | 66.6% |
| Total token traffic | 2,494,533 | **83,923** | 96.6% |
| Model requests | 33 | **3** | 90.9% |
| Tool calls | 32 | **2** | 93.8% |
| Estimated cost | $0.03495 | **$0.00788** | 77.4% |
| Visual score | 89/100 | **85/100** | Far faster convergence with a small detail trade-off |

The custom-provider result is retained only as a configuration postmortem. It does not represent normal Pi performance.

### Cost basis

Costs use the DeepSeek V4 Flash rates applicable during the benchmark: `$0.14 / 1M tokens` for uncached input, `$0.0028 / 1M tokens` for cached input, and `$0.28 / 1M tokens` for output. Reasoning tokens are billed as output. Pricing can change; consult [DeepSeek's official pricing page](https://api-docs.deepseek.com/quick_start/pricing/). Estimates are not provider invoices.

## Installation

The bridge itself uses only the Python standard library:

```bash
git clone https://github.com/Deslord319/coder-mcp-bridge.git
cd coder-mcp-bridge
python3 server.py --probe
```

Install at least one backend:

- **ZCode** — discovered in `/Applications/ZCode.app`, `~/Applications/ZCode.app`, `/opt/ZCode`, or `~/.local/opt/ZCode`; alternatively set `ZCODE_APP_PATH`, or both `ZCODE_BINARY` and `ZCODE_CLI_BUNDLE`.
- **OpenCode** — place `opencode` on `PATH`, or set `OPENCODE_BINARY`.
- **Pi** — place `pi` and its Node.js runtime on `PATH`, or set `PI_BINARY`.

### Pi and DeepSeek

Use Pi's built-in provider catalog:

```json
{"providerId":"deepseek","modelId":"deepseek-v4-flash"}
```

Provide `DEEPSEEK_API_KEY` in the bridge process environment. Do not duplicate the same model as a custom Pi provider: the native definition includes the DeepSeek thinking protocol, 1M context, output limits, cache pricing, and reasoning-replay configuration.

## MCP Registration

The bridge uses standard MCP stdio transport. For Codex, add it to `~/.codex/config.toml`:

```toml
[mcp_servers.vibe_bridge]
command = "python3"
args = ["/absolute/path/to/coder-mcp-bridge/server.py"]

[mcp_servers.vibe_bridge.env]
AGENT_MCP_DEFAULT_BACKEND = "zcode"
AGENT_MCP_TIMEOUT = "900"
```

It can also be started directly:

```bash
python3 /absolute/path/to/coder-mcp-bridge/server.py
```

Other MCP clients can use the equivalent stdio server configuration. The root `plugin.json` uses `coder-mcp-bridge` as the project name and `vibe_bridge` as the MCP server id. `${ZCODE_PLUGIN_ROOT}` remains because it is supplied by the ZCode plugin loader; it does not limit the bridge to ZCode.

## MCP Tools

| Tool | Purpose |
|---|---|
| `agent-config` | Get, set, or reset the backend; list installation state and capabilities |
| `agent-start` | Start a non-blocking run and immediately return a `runId` |
| `agent-wait` | Wait for a revision change or terminal state; the recommended progress path |
| `agent-observe` | Read bounded events, reasoning, tools, usage, context, and resource state |
| `agent-control` | Guide, interrupt, or cancel; ZCode also supports goal/background control |
| `agent-recover` | List or adopt persistent sessions for the selected backend |
| `agent-branch` | Branch from a message, turn, or checkpoint, depending on backend support |
| `agent-context` | Inspect or compact context |
| `agent-close` | Shut down runtime state and release resources without deleting the session |

Typical sequence:

```json
{"name":"agent-config","arguments":{"action":"set","backend":"pi"}}
{"name":"agent-start","arguments":{"prompt":"Implement and test the login flow","cwd":"/path/to/worktree","workspaceAccess":"exclusive"}}
{"name":"agent-wait","arguments":{"runId":"run_...","afterRevision":3,"timeoutMs":30000}}
{"name":"agent-close","arguments":{"runId":"run_..."}}
```

Backend selection is configured once per MCP connection. `agent-start` has no backend parameter. A configuration change affects only future runs; every existing `runId` remains bound to its original backend.

## Concurrency and Resources

Give independent tasks separate worktrees and issue multiple `agent-start` calls concurrently. Declare the same resource key only for genuine conflicts:

```json
{
  "prompt": "Run iOS acceptance tests",
  "cwd": "/path/to/story-worktree",
  "workspaceAccess": "exclusive",
  "resources": [
    {"key": "simulator:iphone-16", "mode": "exclusive"},
    {"key": "/path/to/shared-derived-data", "mode": "exclusive"}
  ]
}
```

Absolute-path resources also become structured file-permission roots: `exclusive` permits writes, while `shared` is read-only. Abstract keys such as `simulator:*` participate only in conflict coordination.

## Permissions and Security

- `workspaceAccess: "shared"` rejects structured writes. Pi also rejects shell access, and OpenCode rejects shell permission events.
- `workspaceAccess: "exclusive"` allows native file tools inside `cwd` and declared absolute-path resources.
- Pi's `mode: "plan"` always enforces a read-only tool policy.
- Pi is launched with the mandatory `pi_bridge_extension.mjs`; file boundaries do not depend on prompt compliance.
- OpenCode external-directory requests are allowed only when every structured path is inside a declared root.
- Shell execution remains an advisory boundary in exclusive mode. Use a container, VM, or OS sandbox when strong isolation is required.
- Supply API keys through the environment or each agent's local credential store; never commit them to the repository.

`.env*`, `benchmark-results/`, SQLite lease databases, and logs are excluded by `.gitignore`.

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `AGENT_MCP_DEFAULT_BACKEND` | `zcode` | Backend selected when an MCP connection starts |
| `AGENT_MCP_TIMEOUT` | `900` | Default run timeout in seconds |
| `AGENT_MCP_MAX_CONCURRENCY` | `0` | Optional per-backend safety limit; 0 delegates to the upstream MCP orchestrator |
| `AGENT_MCP_LOG` | empty | Bridge diagnostic log path |
| `AGENT_MCP_LEASE_DB` | legacy-compatible path | Cross-process resource-lease SQLite database |
| `PI_BINARY` | auto-detected | Pi executable |
| `PI_BRIDGE_SESSION_DIR` | `~/.pi/agent/bridge-sessions` | Pi persistent-session directory |
| `OPENCODE_BINARY` | auto-detected | OpenCode executable |

`ZCODE_MCP_TIMEOUT`, `ZCODE_MCP_MAX_CONCURRENCY`, `ZCODE_MCP_LOG`, and `ZCODE_MCP_LEASE_DB` remain supported as compatibility aliases.

## Testing

Run the complete suite without calling a model API:

```bash
python3 -m unittest discover -s tests -v
```

The suite covers the MCP contract, backend binding, concurrency and resource conflicts, event projection, strict Pi JSONL, Pi permission enforcement, OpenCode permission events, and Windows UTF-8 stdio. `tests/test_mcp.py` and `tests/test_stress.py` call the selected provider and should only be run when API usage is explicitly accepted.

## Friends

- [Linux.do](https://linux.do/) — a community for developers and technology enthusiasts.

## License

Licensed under the [MIT License](LICENSE).
