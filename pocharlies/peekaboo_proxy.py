#!/usr/bin/env python3
"""peekaboo-proxy — Peekaboo's MCP tools plus desktop-mcp's AppleScript and on-screen notice.

Peekaboo (https://github.com/openclaw/Peekaboo) covers screen, accessibility and input
far better than desktop-mcp's own tools, but it has no AppleScript/JXA and no banner
telling whoever sits at the Mac which session is driving it. This proxy adds both
without touching Peekaboo:

  MCP client ──stdio──> peekaboo_proxy.py ──stdio──> `peekaboo mcp <args>`
                              │
                              └──TCP 127.0.0.1:$DESKTOP_MCP_PORT──> desktop-mcp daemon
                                   (applescript, set_control_context, overlay)

- tools/list returns Peekaboo's tools plus the daemon's definitions for `applescript`,
  `set_control_context` and `overlay`.
- Every Peekaboo tool call that looks at or touches the desktop lights the notice first,
  and keeps it lit while the call is still running.
- Desktop calls require a session and purpose declared with `set_control_context`
  on this connection. The proxy does not infer either field.

Run it on the Mac, where both Peekaboo and the daemon live. Everything after `--` goes
to `peekaboo mcp`. Remote clients reach it through exec_host.py (127.0.0.1:8812), which
runs it as a child of the desktop-mcp daemon so Peekaboo inherits the daemon's Screen
Recording and Accessibility grants:

  peekaboo_proxy.py -- --allow-foreground --no-remote

If the notice cannot be shown, the proxy refuses new desktop calls. Stdlib only, Python 3.9+.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

PORT = int(os.environ.get("DESKTOP_MCP_PORT", "8811"))
NOTICE_SECONDS = float(os.environ.get("DESKTOP_MCP_OVERLAY_IDLE", "15"))
# A call with no answer after this long stops renewing the notice: a lost response must
# not keep the banner on for as long as the session lives.
NOTICE_MAX_CALL = float(os.environ.get("DESKTOP_MCP_NOTICE_MAX_CALL", "900"))
DAEMON_TOOLS = ("applescript", "set_control_context", "overlay")
# Peekaboo tools that neither look at nor touch the desktop.
QUIET_TOOLS = ("permissions", "sleep", "analyze")
CONTEXT_REQUIRED = (
    "Call set_control_context with your actual session ID or name and a short, specific "
    "purpose before accessing the desktop. Do this on every new connection and whenever "
    "the task changes. Both fields are shown on the Mac: do not use placeholders or secrets.")
INSTRUCTIONS = CONTEXT_REQUIRED + " This also applies to AppleScript and showing the overlay."
PLACEHOLDERS = {"sin identificar", "sin identificar (peekaboo)", "unidentified",
                "objetivo sin declarar", "undeclared purpose"}


def log(msg):
    sys.stderr.write("[peekaboo-proxy] %s\n" % msg)
    sys.stderr.flush()


def find_peekaboo():
    env = os.environ.get("PEEKABOO_BIN")
    if env:
        return env
    for p in ("/opt/homebrew/bin/peekaboo", "/usr/local/bin/peekaboo"):
        if os.access(p, os.X_OK):
            return p
    return shutil.which("peekaboo") or "peekaboo"


class Daemon(object):
    """One persistent JSON-RPC connection to the desktop-mcp daemon, reconnected on demand."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sock = None
        self.rfile = None
        self.next_id = 0

    def _connect(self):
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        s.settimeout(None)
        self.sock, self.rfile = s, s.makefile("rb")
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "peekaboo-proxy", "version": "1"}})

    def _rpc(self, method, params, timeout=30):
        self.next_id += 1
        rid = self.next_id
        self.sock.settimeout(timeout)
        self.sock.sendall((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                       "params": params}) + "\n").encode())
        while True:
            line = self.rfile.readline()
            if not line:
                raise ConnectionError("daemon closed the connection")
            msg = json.loads(line)
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "daemon error"))
                return msg.get("result")

    def request(self, method, params, timeout=30):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self._connect()
                    return self._rpc(method, params, timeout)
                except (OSError, ConnectionError, ValueError) as e:
                    self.close()
                    if attempt:
                        raise ConnectionError("desktop-mcp daemon on 127.0.0.1:%d: %s"
                                              % (PORT, e))

    def call(self, name, args, timeout=30):
        return self.request("tools/call", {"name": name, "arguments": args}, timeout)

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = self.rfile = None


class Proxy(object):
    def __init__(self, peekaboo_args):
        # Two connections: a long AppleScript must not hold up the notice of other calls.
        self.daemon = Daemon()
        self.tool_daemon = Daemon()
        self.out_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.pending_list = set()
        self.pending_init = set()
        self.inflight = {}           # request id -> time it was sent
        self.context = None          # session and purpose declared by this client
        self.daemon_tools = None
        cmd = [find_peekaboo(), "mcp"] + peekaboo_args
        log("starting: %s" % " ".join(cmd))
        self.child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      bufsize=0)

    # -- output -------------------------------------------------------------
    def send(self, msg):
        with self.out_lock:
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def to_child(self, raw):
        try:
            self.child.stdin.write(raw + b"\n")
            self.child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    # -- notice -------------------------------------------------------------
    def require_context(self):
        with self.state_lock:
            if self.context is None:
                raise ValueError(CONTEXT_REQUIRED)
            return self.context

    def notice(self, args=None):
        ctx = self.require_context()
        # The daemon keeps one global context; restore ours before showing the notice.
        result = self.daemon.call("set_control_context", ctx)
        if result.get("isError"):
            raise RuntimeError(result["content"][0]["text"])
        result = self.daemon.call("overlay", args or {"action": "show", "seconds": NOTICE_SECONDS})
        text = result["content"][0]["text"]
        if result.get("isError") or not json.loads(text).get("visible"):
            raise RuntimeError("Could not show the on-screen notice: " + text)
        return result

    def heartbeat(self):
        while True:
            time.sleep(max(1.0, NOTICE_SECONDS / 2))
            now = time.time()
            with self.state_lock:
                busy = any(now - t < NOTICE_MAX_CALL for t in self.inflight.values())
            if busy:
                try:
                    self.notice()
                except Exception as e:
                    log("could not renew the on-screen notice: %s" % e)

    # -- daemon tools -------------------------------------------------------
    def load_daemon_tools(self):
        if self.daemon_tools is None:
            try:
                tools = self.daemon.request("tools/list", {}).get("tools", [])
                self.daemon_tools = [t for t in tools if t.get("name") in DAEMON_TOOLS]
            except Exception as e:
                log("daemon tools unavailable: %s" % e)
                return []
        return self.daemon_tools

    def call_daemon_tool(self, rid, name, args):
        try:
            timeout = float(args.get("timeout") or 60) + 10 if name == "applescript" else 30
            result = self.tool_daemon.call(name, args, timeout)
        except Exception as e:
            result = {"isError": True, "content": [{"type": "text", "text": str(e)}]}
        finally:
            with self.state_lock:
                self.inflight.pop(rid, None)
        self.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def prepare_tool_call(self, rid, name, args):
        if name == "set_control_context":
            context = {}
            for key, limit in (("session", 80), ("purpose", 120)):
                value = args.get(key)
                value = value.strip()[:limit].rstrip() if isinstance(value, str) else ""
                if not value or value.casefold() in PLACEHOLDERS:
                    raise ValueError(CONTEXT_REQUIRED)
                context[key] = value
            with self.state_lock:
                self.context = context
            return {"content": [{"type": "text", "text": "Control context set for this connection."}]}
        if name == "overlay":
            action = (args.get("action") or "status").lower()
            if action == "show":
                return self.notice(args)
            if action == "hide":
                self.require_context()
        elif name not in QUIET_TOOLS:
            self.notice()
            with self.state_lock:
                self.inflight[rid] = time.time()

    # -- pumps --------------------------------------------------------------
    def from_client(self):
        for raw in sys.stdin.buffer:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                self.to_child(raw)
                continue
            method, rid = msg.get("method"), msg.get("id")
            params = msg.get("params") or {}
            if method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                try:
                    result = self.prepare_tool_call(rid, name, args)
                except Exception as e:
                    result = {"isError": True, "content": [{"type": "text", "text": str(e)}]}
                if result is not None:
                    self.send({"jsonrpc": "2.0", "id": rid, "result": result})
                    continue
                if name in DAEMON_TOOLS:
                    threading.Thread(target=self.call_daemon_tool, daemon=True,
                                     args=(rid, name, args)).start()
                    continue
            elif method == "initialize":
                self.pending_init.add(rid)
            elif method == "tools/list":
                self.pending_list.add(rid)
            self.to_child(raw)
        try:
            self.child.stdin.close()
        except OSError:
            pass

    def from_child(self):
        for raw in self.child.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                log("non-JSON line from peekaboo dropped: %r" % raw[:200])
                continue
            rid = msg.get("id")
            result = msg.get("result")
            if rid is not None and "method" not in msg:
                with self.state_lock:
                    self.inflight.pop(rid, None)
                if rid in self.pending_list:
                    self.pending_list.discard(rid)
                    if isinstance(result, dict):
                        tools = result.get("tools", [])
                        if not result.get("nextCursor"):
                            tools = tools + self.load_daemon_tools()
                        result["tools"] = [
                            dict(tool, description=(tool.get("description") or "") + "\n\n" + CONTEXT_REQUIRED)
                            if tool["name"] not in QUIET_TOOLS else tool
                            for tool in tools
                        ]
                elif rid in self.pending_init:
                    self.pending_init.discard(rid)
                    if isinstance(result, dict):
                        prev = result.get("instructions")
                        result["instructions"] = (prev + "\n\n" if prev else "") + INSTRUCTIONS
            self.send(msg)

    def run(self):
        threading.Thread(target=self.heartbeat, daemon=True).start()
        threading.Thread(target=self.from_client, daemon=True).start()
        self.from_child()
        return self.child.wait()


def main():
    argv = sys.argv[1:]
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    code = Proxy(argv).run()
    sys.stdout.flush()
    # os._exit: the stdin pump is a daemon thread blocked on read, and a normal
    # interpreter shutdown aborts on its buffered-reader lock.
    os._exit(code if code is not None else 1)


if __name__ == "__main__":
    main()
