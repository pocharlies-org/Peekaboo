#!/usr/bin/env python3
"""Stand-in for `peekaboo mcp`: a minimal MCP stdio server for the proxy tests.

tools/call answers with the tool name and its arguments; an integer `delay` argument
makes the call take that many seconds, and `exit` makes the server exit with that code.
Every argv it was started with is echoed in the initialize instructions so the tests
can check what the proxy passed after `--`.
"""
import json
import sys
import threading
import time

TOOLS = ["see", "click", "type", "press", "image", "permissions", "sleep", "analyze"]
out_lock = threading.Lock()


def send(msg):
    with out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def call(rid, params):
    args = params.get("arguments") or {}
    if args.get("delay"):
        time.sleep(float(args["delay"]))
    send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
        {"type": "text", "text": json.dumps({"tool": params.get("name"), "args": args})}]}})


for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-peekaboo", "version": "0"},
            "instructions": "fake argv=" + json.dumps(sys.argv[1:])}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": n, "description": n, "inputSchema": {"type": "object"}} for n in TOOLS]}})
    elif method == "tools/call":
        if "exit" in (params.get("arguments") or {}):
            sys.exit(int(params["arguments"]["exit"]))
        threading.Thread(target=call, args=(rid, params), daemon=True).start()
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
