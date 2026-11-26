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
                        if p["name"] == "applescript":
                            text = "AS:" + p["arguments"]["script"]
                        elif p["name"] == "overlay":
                            text = json.dumps({"visible": p["arguments"].get("action") == "show"})
                        else:
                            text = "ok"
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
        tools = {t["name"]: t for t in self.c.request("tools/list")["result"]["tools"]}
        self.assertTrue({"see", "click", "permissions"} | DAEMON_TOOLS <= tools.keys())
        self.assertNotIn("screenshot", tools)
        for name in ("see", "click", "applescript", "overlay"):
            self.assertIn("set_control_context", tools[name]["description"])

    def test_desktop_calls_without_context_are_blocked(self):
        for name, args in (("see", {}), ("click", {"on": "x"}),
                           ("applescript", {"script": "return 1"}),
                           ("overlay", {"action": "show"})):
            with self.subTest(tool=name):
                res = self.c.call(name, args)
                self.assertTrue(res["isError"])
                self.assertIn("set_control_context", res["content"][0]["text"])
        self.assertEqual(self.daemon.calls, [])

    def test_blank_missing_and_placeholder_context_is_rejected(self):
        for args in ({"session": "S1"}, {"purpose": "P1"},
                     {"session": " ", "purpose": "P1"}, {"session": "S1", "purpose": "\t"},
                     {"session": 42, "purpose": "P1"},
                     {"session": " SIN IDENTIFICAR (Peekaboo) ", "purpose": "P1"},
                     {"session": "S1", "purpose": "objetivo sin declarar"}):
            with self.subTest(args=args):
                self.assertTrue(self.c.call("set_control_context", args)["isError"])
                self.assertTrue(self.c.call("see")["isError"])
        self.assertEqual(self.daemon.calls, [])

    def test_declared_context_is_the_banner_and_stays_on_its_connection(self):
        res = self.c.call("set_control_context", {"session": " S1 ", "purpose": " P1 "})
        self.assertFalse(res.get("isError"))
        other = Client(self.daemon.port)
        self.addCleanup(other.close)
        other.initialize()
        self.assertTrue(other.call("see")["isError"])
        res = self.c.call("see", {"app_target": "frontmost"})
        self.assertEqual(json.loads(res["content"][0]["text"])["tool"], "see")
        res = self.c.call("applescript", {"script": "return 1"})
        self.assertEqual(res["content"][0]["text"], "AS:return 1")
        self.assertTrue(self.daemon.named("overlay"))
        self.assertEqual(self.daemon.named("set_control_context"),
                         [{"session": "S1", "purpose": "P1"}] * 2)

    def test_quiet_tools_do_not_light_the_notice(self):
        self.daemon.reset()
        self.c.call("permissions")
        self.c.call("sleep", {"duration": 1})
        self.assertEqual(self.daemon.named("overlay"), [])

    def test_notice_kept_lit_while_a_call_is_in_flight(self):
        self.c.call("set_control_context", {"session": "S1", "purpose": "P1"})
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
            c.call("set_control_context", {"session": "S1", "purpose": "P1"})
            self.assertFalse(c.call("see", {"delay": 4.5}, timeout=15).get("isError"))
            # The lighting before the call + at most one heartbeat inside the limit.
            self.assertIn(len(daemon.named("overlay")), (1, 2))
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

    def test_only_quiet_tools_work_while_the_notice_daemon_is_down(self):
        names = {t["name"] for t in self.c.request("tools/list")["result"]["tools"]}
        self.assertIn("see", names)
        self.assertFalse(self.c.call("permissions").get("isError"))
        self.assertFalse(self.c.call("set_control_context", {"session": "S1", "purpose": "P1"}).get("isError"))
        for name in ("see", "applescript"):
            res = self.c.call(name)
            self.assertTrue(res["isError"])
            self.assertIn("desktop-mcp daemon", res["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
