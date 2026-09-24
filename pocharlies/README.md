# pocharlies fork of Peekaboo

`pocharlies` (the default branch) is **the latest upstream release plus this directory**.
`main` stays a plain mirror of `openclaw/Peekaboo`. Nothing upstream is modified, so an
upstream merge never conflicts with what lives here.

We never build Peekaboo: the Mac runs the official signed CLI of the release in
`UPSTREAM_VERSION` (checksum and team ID verified on every deploy).

## How it runs on the Mac

```
MCP client ──ssh mac nc 127.0.0.1 8812──> exec_host.py ──stdio──> peekaboo_proxy.py ──stdio──> peekaboo mcp --no-remote
                                            (child of the                  │
                                             desktop-mcp daemon)           └──TCP 8811──> desktop-mcp daemon
                                                                               (applescript, notice)
```

Screen Recording and Accessibility belong to the process macOS holds responsible, and an
SSH session never has them. The [desktop-mcp](https://github.com/pocharlies-org/desktop-mcp)
daemon's app bundle does, and it starts `exec_host.py` as its child (a line in
`~/.config/desktop-mcp/children`), so Peekaboo runs in-process with the bundle's grants.
The upstream way would be the Peekaboo.app Bridge (`peekaboo mcp --bridge-socket`), but in
4.5.0 the MCP server fails to start against it ("Bridge operation session is
unavailable") while the plain CLI works — revisit when that is fixed upstream.

## What this directory adds

| File | What for |
|---|---|
| `peekaboo_proxy.py` | MCP proxy in front of `peekaboo mcp`: adds `applescript`, `set_control_context` and `overlay` from the [desktop-mcp](https://github.com/pocharlies-org/desktop-mcp) daemon and lights the on-screen notice ("this Mac is being driven by session X for Y") on every Peekaboo call that looks at or touches the desktop |
| `tests/` | proxy tests against a fake `peekaboo mcp` and a fake daemon (CI) |
| `check_contract.py` | what the proxy assumes of upstream, read from the Swift sources: tool names, `--allow-foreground`, `--no-remote`, stdio (CI) |
| `exec_host.py` | the MCP endpoint on the Mac (127.0.0.1:8812): one proxy + `peekaboo mcp --no-remote` per connection |
| `smoke_mac.py` | live test on the Mac through the real binary: `staged` before deploying, `host` after (through 8812) |
| `deploy_mac.sh` | stage a release (checksum, signature), staged smoke, install, host smoke, stamp |
| `UPSTREAM_VERSION` | the upstream release tag this branch is |

## Daily update

A timer on the x86 (`peekaboo-fork-update`, in `x86-host-runtime-pocharlies`) checks
upstream's latest release every day. When there is a new one: merge the tag into a branch
off `pocharlies`, local tests + contract, PR, wait for this repo's CI, `deploy_mac.sh
--stage-only` on the Mac, merge, `deploy_mac.sh`, and one line per attempt in
`/data/update-watch/attempts.jsonl` (system `peekaboo`, via `fork-rebase`). If the Mac is
asleep, the PR waits and the next run picks it up; a deploy that did not happen is caught
up by comparing the Mac's `DEPLOYED` stamp with `pocharlies`.

## The MCP client

A stdio server whose command is `ssh mac /usr/bin/nc 127.0.0.1 8812`. The grants that
matter are the desktop-mcp daemon's (System Settings → Privacy & Security → Screen
Recording and Accessibility, for its app); Peekaboo.app is not needed.
