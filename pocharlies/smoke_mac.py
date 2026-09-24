#!/usr/bin/env python3
"""Live smoke test of peekaboo_proxy.py on the Mac, through the real `peekaboo mcp`.

  --mode staged   BEFORE deploying: a candidate binary run with --no-remote (an SSH session
                  has no TCC grants, so no capture): the handshake, the merged tool list,
                  AppleScript through the daemon and the on-screen notice.
  --mode host     AFTER deploying: through exec_host (127.0.0.1:8812), exactly as the MCP
                  client reaches it, with the desktop-mcp daemon's grants. Adds Screen
                  Recording + Accessibility granted and, if the screen is unlocked, a real
                  `see`.

Prints one JSON line per check and exits 1 if any failed. Stdlib only.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIRED = {"see", "click", "type", "press", "image", "app", "permissions",
            "applescript", "set_control_context", "overlay"}
failed = []


def report(check, ok, detail=""):
    print(json.dumps({"check": check, "ok": bool(ok), "detail": str(detail)[:300]}), flush=True)
    if not ok:
        failed.append(check)


def screen_locked():
    out = subprocess.run(["ioreg", "-n", "Root", "-d1", "-r"], capture_output=True,
                         text=True).stdout
    # The key only exists while the screen is locked; its presence is the answer.
    return "CGSSessionScreenIsLocked" in out


class Session(object):
    def __init__(self, cmd, env):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        self.responses, self.stderr, self.next_id = {}, [], 0
        threading.Thread(target=self._out, daemon=True).start()
        threading.Thread(target=lambda: [self.stderr.append(l) for l in self.p.stderr],
                         daemon=True).start()

    def _out(self):
        for line in self.p.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self.responses[msg.get("id")] = msg

    def request(self, method, params=None, timeout=60):
        self.next_id += 1
        rid = self.next_id
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                       "params": params or {}}) + "\n")
        self.p.stdin.flush()
        end = time.time() + timeout
        while rid not in self.responses and time.time() < end and self.p.poll() is None:
            time.sleep(0.1)
        return self.responses.get(rid)

    def call(self, name, args=None, timeout=60):
        r = self.request("tools/call", {"name": name, "arguments": args or {}}, timeout)
        return (r or {}).get("result")

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=20)
        except Exception:
            self.p.kill()


def text_of(result):
    return " ".join(c.get("text", "") for c in (result or {}).get("content", [])
                    if c.get("type") == "text")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["staged", "host"], required=True)
    ap.add_argument("--peekaboo", help="staged: the candidate peekaboo binary")
    ap.add_argument("--proxy", default=os.path.join(HERE, "peekaboo_proxy.py"))
    ap.add_argument("--port", default=os.environ.get("PEEKABOO_EXEC_PORT", "8812"))
    ap.add_argument("--label", default="smoke", help="goes into the on-screen notice")
    a = ap.parse_args()

    if a.mode == "staged":
        if not a.peekaboo:
            ap.error("--mode staged needs --peekaboo")
        env = dict(os.environ, PEEKABOO_BIN=a.peekaboo)
        s = Session([sys.executable, a.proxy, "--", "--no-remote"], env)
    else:
        s = Session(["/usr/bin/nc", "127.0.0.1", a.port], dict(os.environ))
    try:
        init = s.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "peekaboo-fork-smoke",
                                                       "version": "1"}}, timeout=90)
        info = ((init or {}).get("result") or {}).get("serverInfo") or {}
        report("initialize", info.get("name") == "peekaboo-mcp",
               info or "".join(s.stderr[-3:]))
        if failed:
            return 1
        s.request("notifications/initialized")
        tools = {t["name"] for t in (s.request("tools/list") or {}).get("result", {}).get("tools", [])}
        report("tools", REQUIRED <= tools, "missing: %s" % sorted(REQUIRED - tools))
        s.call("set_control_context", {"session": "peekaboo-fork " + a.label,
                                       "purpose": "validar Peekaboo (%s)" % a.mode})
        res = s.call("applescript", {"script": 'return "smoke-ok"'})
        report("applescript", "smoke-ok" in text_of(res), text_of(res))

        if a.mode == "host":
            res = s.call("permissions")
            meta = (res or {}).get("_meta") or {}
            ok = meta.get("accessibility") is True and meta.get("screen_recording", True) is True
            report("permissions", ok, json.dumps(meta)[:200] if meta else text_of(res))
            if screen_locked():
                report("see", True, "skipped: screen locked")
            else:
                res = s.call("see", {"app_target": "frontmost"}, timeout=120)
                has_image = any(c.get("type") == "image" for c in (res or {}).get("content", []))
                report("see", res is not None and not res.get("isError") and has_image,
                       text_of(res)[:200])
        else:
            # Any non-quiet Peekaboo call must light the notice, even one that fails.
            res = s.call("app", {"action": "list"})
            report("app", res is not None, text_of(res)[:120])

        st = s.call("overlay", {"action": "status"})
        try:
            visible = json.loads(text_of(st)).get("visible")
        except ValueError:
            visible = None
        report("notice", visible is True, text_of(st)[:200])
    finally:
        s.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
