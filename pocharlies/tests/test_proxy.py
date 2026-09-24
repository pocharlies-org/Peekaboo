"""Tests for peekaboo_proxy.py against a fake `peekaboo mcp` and a fake desktop-mcp daemon.

Run from the repository root:
    PYTHONPYCACHEPREFIX=/tmp/py-cache python3 -m unittest discover -s pocharlies/tests -v
"""
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(os.path.dirname(HERE), "peekaboo_proxy.py")
FAKE = os.path.join(HERE, "fake_peekaboo.py")
DAEMON_TOOLS = {"applescript", "set_control_context", "overlay"}


class FakeDaemon(object):
    """desktop-mcp daemon stand-in: newline JSON-RPC over TCP, records every tool call."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        daemon = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    req = json.loads(line)
                    m, p = req.get("method"), req.get("params") or {}
                    if m == "initialize":
                        res = {"serverInfo": {"name": "fake-daemon"}}
                    elif m == "tools/list":
                        res = {"tools": [{"name": n, "description": n,
                                          "inputSchema": {"type": "object"}}
                                         for n in sorted(DAEMON_TOOLS) + ["screenshot"]]}
                    elif m == "tools/call":
                        with daemon.lock:
                            daemon.calls.append((p["name"], p.get("arguments") or {}))
                        text = "AS:" + p["arguments"]["script"] if p["name"] == "applescript" else "ok"
                        res = {"content": [{"type": "text", "text": text}]}
                    else:
                        res = {}
                    self.wfile.write((json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                                                  "result": res}) + "\n").encode())

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def named(self, name):
        with self.lock:
            return [a for n, a in self.calls if n == name]

    def reset(self):
        with self.lock:
            self.calls = []

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Client(object):
    def __init__(self, port, extra_args=("--flag-x", "val y"), notice_seconds="1",
                 max_call="900"):
        os.chmod(FAKE, 0o755)
        env = dict(os.environ, DESKTOP_MCP_PORT=str(port), PEEKABOO_BIN=FAKE,
                   DESKTOP_MCP_OVERLAY_IDLE=notice_seconds,
                   DESKTOP_MCP_NOTICE_MAX_CALL=max_call)
        self.p = subprocess.Popen([sys.executable, PROXY, "--"] + list(extra_args),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
        self.responses = {}
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            msg = json.loads(line)
            self.responses[msg.get("id")] = msg

    def request(self, method, params=None, timeout=10):
        self.next_id += 1
        rid = self.next_id
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                       "params": params or {}}) + "\n")
        self.p.stdin.flush()
        end = time.time() + timeout
        while rid not in self.responses:
            if time.time() > end:
                raise AssertionError("no response to %s" % method)
            time.sleep(0.02)
        return self.responses[rid]

    def call(self, name, args=None, timeout=10):
        return self.request("tools/call", {"name": name, "arguments": args or {}}, timeout)["result"]

    def initialize(self, name="test-client"):
        return self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": name, "version": "0"}})

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.p.stdout.close()


class ProxyWithDaemon(unittest.TestCase):
    def setUp(self):
        self.daemon = FakeDaemon()
        self.c = Client(self.daemon.port)
        self.init = self.c.initialize()

    def tearDown(self):
        self.c.close()
        self.daemon.close()

    def test_initialize_passes_args_and_appends_instructions(self):
        instr = self.init["result"]["instructions"]
        self.assertIn('fake argv=["mcp", "--flag-x", "val y"]', instr)
        self.assertIn("set_control_context", instr)

    def test_tools_are_peekaboo_plus_daemon_extras_only(self):
        names = {t["name"] for t in self.c.request("tools/list")["result"]["tools"]}
        self.assertTrue({"see", "click", "permissions"} <= names)
        self.assertTrue(DAEMON_TOOLS <= names)
        self.assertNotIn("screenshot", names)

    def test_applescript_goes_to_the_daemon(self):
        res = self.c.call("applescript", {"script": "return 1"})
        self.assertEqual(res["content"][0]["text"], "AS:return 1")
        self.assertEqual(self.daemon.named("applescript"), [{"script": "return 1"}])

    def test_desktop_call_lights_notice_named_after_client(self):
        res = self.c.call("see", {"app_target": "frontmost"})
        self.assertEqual(json.loads(res["content"][0]["text"])["tool"], "see")
        self.assertTrue(self.daemon.named("overlay"))
        ctx = self.daemon.named("set_control_context")[-1]
        self.assertEqual(ctx["session"], "test-client (peekaboo)")

    def test_declared_context_is_the_banner(self):
        self.c.call("set_control_context", {"session": "S1", "purpose": "P1"})
        self.daemon.reset()
        self.c.call("click", {"on": "x"})
        self.assertEqual(self.daemon.named("set_control_context")[-1],
                         {"session": "S1", "purpose": "P1"})

    def test_quiet_tools_do_not_light_the_notice(self):
        self.daemon.reset()
        self.c.call("permissions")
        self.c.call("sleep", {"duration": 1})
        self.assertEqual(self.daemon.named("overlay"), [])

    def test_notice_kept_lit_while_a_call_is_in_flight(self):
        self.daemon.reset()
        self.c.call("see", {"delay": 2.5}, timeout=15)
        self.assertGreaterEqual(len(self.daemon.named("overlay")), 2)

    def test_child_exit_code_is_the_proxy_exit_code(self):
        self.c.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                                         "params": {"name": "permissions",
                                                    "arguments": {"exit": 3}}}) + "\n")
        self.c.p.stdin.flush()
        self.assertEqual(self.c.p.wait(timeout=10), 3)


class NoticeGivesUpOnAHungCall(unittest.TestCase):
    def test_a_call_past_the_limit_stops_renewing_the_notice(self):
        daemon = FakeDaemon()
        c = Client(daemon.port, max_call="1.5")
        try:
            c.initialize()
            daemon.reset()
            c.call("see", {"delay": 4.5}, timeout=15)
            # the lighting before the call + at most one heartbeat inside the limit
            self.assertLessEqual(len(daemon.named("overlay")), 2)
        finally:
            c.close()
            daemon.close()


class ProxyWithoutDaemon(unittest.TestCase):
    def setUp(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()  # nothing listens here any more
        self.c = Client(self.port)
        self.c.initialize()

    def tearDown(self):
        self.c.close()

    def test_peekaboo_keeps_working(self):
        res = self.c.call("see")
        self.assertEqual(json.loads(res["content"][0]["text"])["tool"], "see")
        names = {t["name"] for t in self.c.request("tools/list")["result"]["tools"]}
        self.assertIn("see", names)

    def test_applescript_reports_the_daemon_is_down(self):
        res = self.c.call("applescript", {"script": "return 1"})
        self.assertTrue(res.get("isError"))
        self.assertIn("desktop-mcp daemon", res["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
