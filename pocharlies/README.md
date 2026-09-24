# pocharlies fork of Peekaboo

`pocharlies` (the default branch) is **the latest upstream release plus this directory**.
`main` stays a plain mirror of `openclaw/Peekaboo`. Nothing upstream is modified, so an
upstream merge never conflicts with what lives here.

We never build Peekaboo: the Peekaboo.app Bridge only accepts a CLI signed by Peekaboo's
team, so the Mac runs the official signed release that matches `UPSTREAM_VERSION`.

## What this directory adds

| File | What for |
|---|---|
| `peekaboo_proxy.py` | MCP proxy in front of `peekaboo mcp`: adds `applescript`, `set_control_context` and `overlay` from the [desktop-mcp](https://github.com/pocharlies-org/desktop-mcp) daemon and lights the on-screen notice ("this Mac is being driven by session X for Y") on every Peekaboo call that looks at or touches the desktop |
| `tests/` | proxy tests against a fake `peekaboo mcp` and a fake daemon (CI) |
| `check_contract.py` | what the proxy assumes of upstream, read from the Swift sources: tool names, `--allow-foreground`, `--bridge-socket`, stdio (CI) |
| `smoke_mac.py` | live test on the Mac through the real binary: `staged` before deploying, `bridge` after |
| `deploy_mac.sh` | stage a release (checksums, signature), staged smoke, install, bridge smoke, stamp |
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

```sh
ssh mac 'PEEKABOO_BIN=$HOME/.local/share/peekaboo-fork/current/cli/peekaboo \
  /opt/homebrew/bin/python3 $HOME/.local/share/peekaboo-fork/current/peekaboo_proxy.py -- \
  --allow-foreground --bridge-socket "$HOME/Library/Application Support/Peekaboo/bridge.sock"'
```

Peekaboo.app needs Screen Recording and Accessibility (System Settings → Privacy &
Security). The grants belong to the app, never to an SSH session.
