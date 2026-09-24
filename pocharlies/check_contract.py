#!/usr/bin/env python3
"""Static contract between peekaboo_proxy.py and the upstream source it is merged with.

The proxy relies on a few things of `peekaboo mcp` it cannot check at runtime without a
Mac: the names of Peekaboo's MCP tools (its quiet list must exist, its own tools must not
collide), the `--allow-foreground` and `--no-remote` flags, and stdio as the default
transport. This reads them from the Swift sources, so an upstream merge that breaks one
fails CI before anything reaches the Mac. Run from the repository root.

If a path moved upstream, the check fails saying which one: update it here, don't skip it.
"""
import importlib.util
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = "Core/PeekabooCore/Sources/PeekabooAgentRuntime/MCP/Tools"
MCP_CMD_DIR = "Apps/CLI/Sources/PeekabooCLI/Commands/MCP"
RUNTIME_OPTS_DIR = "Apps/CLI/Sources/PeekabooCLI/Commands/Base"
# Tools the smoke tests and the everyday use depend on.
REQUIRED_TOOLS = {"see", "click", "type", "press", "image", "app", "window", "permissions"}

problems = []


def swift_sources(rel):
    path = os.path.join(ROOT, rel)
    if not os.path.isdir(path):
        problems.append("upstream path gone: %s" % rel)
        return {}
    out = {}
    for base, _dirs, files in os.walk(path):
        for f in files:
            if f.endswith(".swift"):
                p = os.path.join(base, f)
                with open(p, encoding="utf-8") as fh:
                    out[os.path.relpath(p, ROOT)] = fh.read()
    return out


def load_proxy():
    spec = importlib.util.spec_from_file_location(
        "peekaboo_proxy", os.path.join(ROOT, "pocharlies", "peekaboo_proxy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    proxy = load_proxy()
    tools = set()
    for text in swift_sources(TOOLS_DIR).values():
        tools |= set(re.findall(r'public let name = "([a-z_]+)"', text))
    if not tools:
        problems.append("no MCP tool names found under %s" % TOOLS_DIR)
    else:
        for name in sorted(REQUIRED_TOOLS - tools):
            problems.append("required Peekaboo tool gone: %s" % name)
        for name in sorted(set(proxy.QUIET_TOOLS) - tools):
            problems.append("QUIET_TOOLS names a tool Peekaboo no longer has: %s" % name)
        for name in sorted(set(proxy.DAEMON_TOOLS) & tools):
            problems.append("Peekaboo now has its own '%s': it would collide with the "
                            "proxy's" % name)

    mcp = "\n".join(swift_sources(MCP_CMD_DIR).values())
    if '"allow-foreground"' not in mcp:
        problems.append("`peekaboo mcp` lost --allow-foreground (%s)" % MCP_CMD_DIR)
    if not re.search(r'var transport: String = "stdio"', mcp):
        problems.append("`peekaboo mcp` no longer defaults to stdio (%s)" % MCP_CMD_DIR)
    if '"no-remote"' not in "\n".join(swift_sources(RUNTIME_OPTS_DIR).values()):
        problems.append("--no-remote is no longer a runtime option (%s)" % RUNTIME_OPTS_DIR)

    if problems:
        for p in problems:
            print("FAIL  " + p)
        return 1
    print("ok    %d Peekaboo MCP tools; required, quiet and flags present; no collisions"
          % len(tools))
    return 0


if __name__ == "__main__":
    sys.exit(main())
