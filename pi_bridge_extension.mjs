/**
 * Headless policy enforced for every Pi process started by the Bridge.
 *
 * The policy is supplied per process through environment variables so one
 * Bridge can safely run concurrent sessions with different workspaces.
 */
import path from "node:path";

function parseRoots() {
  const cwd = path.resolve(process.env.AGENT_BRIDGE_CWD || process.cwd());
  let configured = [];
  try {
    configured = JSON.parse(process.env.AGENT_BRIDGE_ALLOWED_ROOTS || "[]");
  } catch {
    configured = [];
  }
  const roots = [cwd, ...configured]
    .filter((value) => typeof value === "string" && path.isAbsolute(value))
    .map((value) => path.resolve(value));
  return [...new Set(roots)];
}

function parseRootModes(cwd, roots, workspaceAccess) {
  let configured = {};
  try {
    configured = JSON.parse(process.env.AGENT_BRIDGE_ROOT_MODES || "{}");
  } catch {
    configured = {};
  }
  const result = Object.fromEntries(roots.map((root) => [root, "exclusive"]));
  result[cwd] = workspaceAccess;
  for (const [root, mode] of Object.entries(configured)) {
    if (path.isAbsolute(root) && (mode === "shared" || mode === "exclusive")) {
      result[path.resolve(root)] = mode;
    }
  }
  return result;
}

export function matchingRoot(rawPath, cwd, roots) {
  const target = path.resolve(cwd, rawPath || ".");
  const matches = roots.filter((root) => {
    const relative = path.relative(root, target);
    return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
  });
  return matches.sort((left, right) => right.length - left.length)[0];
}

export function pathAllowed(rawPath, cwd, roots) {
  return matchingRoot(rawPath, cwd, roots) !== undefined;
}

export function policyDecision(event, policy = {}) {
  const cwd = policy.cwd || path.resolve(process.env.AGENT_BRIDGE_CWD || process.cwd());
  const roots = policy.roots || parseRoots();
  const workspaceAccess = policy.workspaceAccess || process.env.AGENT_BRIDGE_WORKSPACE_ACCESS || "exclusive";
  const mode = policy.mode || process.env.AGENT_BRIDGE_MODE || "auto";
  const readOnly = workspaceAccess === "shared" || mode === "plan";
  const rootModes = policy.rootModes || parseRootModes(cwd, roots, workspaceAccess);
  const mutationTools = new Set(["edit", "write"]);
  const pathTools = new Set(["read", "edit", "write", "grep", "find", "ls"]);

  if (event.toolName === "bash" && readOnly) {
    return { block: true, reason: "Bridge read-only policy denies shell execution" };
  }
  if (pathTools.has(event.toolName)) {
    const rawPath = typeof event.input?.path === "string" ? event.input.path : ".";
    const root = matchingRoot(rawPath, cwd, roots);
    if (!root) {
      return { block: true, reason: `Path is outside Bridge allowed roots: ${rawPath}` };
    }
    if (mutationTools.has(event.toolName) && (readOnly || rootModes[root] !== "exclusive")) {
      return { block: true, reason: `Bridge read-only root denies ${event.toolName}: ${rawPath}` };
    }
  }
  return undefined;
}

export default function bridgePolicyExtension(pi) {
  pi.on("tool_call", async (event) => policyDecision(event));
}
