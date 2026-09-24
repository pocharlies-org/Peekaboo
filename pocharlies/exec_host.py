#!/usr/bin/env python3
"""exec_host — the MCP endpoint on the Mac: one peekaboo_proxy.py per TCP connection.

Listens on 127.0.0.1:$PEEKABOO_EXEC_PORT (default 8812). Every connection gets its own
`peekaboo_proxy.py -- --allow-foreground --no-remote`, with the socket as its stdin and
stdout. Reach it from another machine with `ssh mac /usr/bin/nc 127.0.0.1 8812`.

Why it exists: Screen Recording and Accessibility belong to the process macOS holds
responsible, and an SSH session never has them. The desktop-mcp daemon's app bundle
does, and its children inherit it — so the daemon starts this host (a line in
~/.config/desktop-mcp/children) and Peekaboo runs in-process (`--no-remote`) with the
bundle's grants. The Peekaboo.app Bridge would be the upstream way, but `peekaboo mcp
--bridge-socket` fails to start in 4.5.0 ("Bridge operation session is unavailable").

Paths go through ~/.local/share/peekaboo-fork/current on every connection, so a deploy
that repoints `current` takes effect on the next session without restarting this host.
Stdlib only.
"""
import os
import socket
import socketserver
import subprocess
import sys
import threading

PORT = int(os.environ.get("PEEKABOO_EXEC_PORT", "8812"))
CURRENT = os.path.expanduser(os.environ.get("PEEKABOO_FORK_CURRENT",
                                            "~/.local/share/peekaboo-fork/current"))
PEEKABOO_ARGS = ["--allow-foreground", "--no-remote"]


def log(msg):
    sys.stderr.write("[exec_host] %s\n" % msg)
    sys.stderr.flush()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        env = dict(os.environ, PEEKABOO_BIN=os.path.join(CURRENT, "cli", "peekaboo"))
        cmd = [sys.executable, os.path.join(CURRENT, "peekaboo_proxy.py"), "--"] + PEEKABOO_ARGS
        try:
            child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     env=env, bufsize=0)
        except OSError as e:
            log("cannot start the proxy: %s" % e)
            return
        sock = self.request

        def to_child():
            try:
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    child.stdin.write(data)
                    child.stdin.flush()
            except OSError:
                pass
            finally:
                try:
                    child.stdin.close()
                except OSError:
                    pass

        t = threading.Thread(target=to_child, daemon=True)
        t.start()
        try:
            while True:
                data = child.stdout.read1(65536) if hasattr(child.stdout, "read1") \
                    else os.read(child.stdout.fileno(), 65536)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    srv = Server(("127.0.0.1", PORT), Handler)
    log("listening on 127.0.0.1:%d, proxy from %s" % (PORT, CURRENT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
