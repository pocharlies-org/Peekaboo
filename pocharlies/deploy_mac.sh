#!/usr/bin/env bash
# Deploy one upstream release of Peekaboo, plus this fork's proxy, to the Mac. Runs on the
# machine that drives the Mac (the x86), over SSH.
#
#   deploy_mac.sh [--stage-only] <tag>
#
# 1. stage   download the release's CLI tarball, verify it against the release's
#            checksums.txt and its signature, unpack it into
#            ~/.local/share/peekaboo-fork/staging/<tag> with this checkout's pocharlies/*.py
# 2. smoke   smoke_mac.py --mode staged with the staged binary (no grants needed)
#    --stage-only stops here: that is the pre-merge gate
# 3. install point `current` at the staging dir; make sure the desktop-mcp daemon starts
#            exec_host.py (a line in ~/.config/desktop-mcp/children, so it inherits the
#            daemon's Screen Recording and Accessibility) and restart exec_host so a new
#            version of it is the one serving
# 4. smoke   smoke_mac.py --mode host, through 127.0.0.1:8812 as the MCP client reaches it.
#            On failure `current` goes back to what it was
# 5. stamp   ~/.local/share/peekaboo-fork/DEPLOYED = "<tag> <fork sha>"
#
# Exit: 0 ok, 1 failed, 2 Mac unreachable (retry later, not a failure of the release).
# Official signed binaries only: this fork never builds Peekaboo itself.
set -u
STAGE_ONLY=0
[ "${1:-}" = "--stage-only" ] && { STAGE_ONLY=1; shift; }
TAG="${1:?usage: deploy_mac.sh [--stage-only] <tag>}"
MAC="${PEEKABOO_MAC_HOST:-mac}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SHA="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
PY=/opt/homebrew/bin/python3

mac() { ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 "$MAC" "$@"; }
die() { echo "FAIL: $*" >&2; exit 1; }

mac true 2>/dev/null || { echo "OFFLINE: $MAC unreachable" >&2; exit 2; }

# --- 1. stage ------------------------------------------------------------------------
mac bash -s -- "$TAG" <<'EOF' || die "stage $TAG"
set -eu
TAG="$1"
S="$HOME/.local/share/peekaboo-fork/staging/$TAG"
U="https://github.com/openclaw/Peekaboo/releases/download/$TAG"
mkdir -p "$S" && cd "$S"
if [ ! -x cli/peekaboo ]; then
  rm -rf dl cli && mkdir dl
  curl -fsSL --retry 3 -o dl/checksums.txt "$U/checksums.txt"
  f=peekaboo-macos-arm64.tar.gz
  curl -fsSL --retry 3 -o "dl/$f" "$U/$f"
  want=$(awk -v f="$f" '$2==f {print $1}' dl/checksums.txt)
  got=$(shasum -a 256 "dl/$f" | cut -d' ' -f1)
  [ -n "$want" ] && [ "$want" = "$got" ] || { echo "checksum mismatch: $f" >&2; exit 1; }
  mkdir cli && tar -xzf "dl/$f" -C cli --strip-components 1
  rm -rf dl
fi
codesign -dv cli/peekaboo 2>&1 | grep -q 'TeamIdentifier=FWJYW4S8P8' \
  || { echo "CLI not signed by Peekaboo's team" >&2; exit 1; }
EOF
scp -q -o BatchMode=yes "$HERE/peekaboo_proxy.py" "$HERE/smoke_mac.py" "$HERE/exec_host.py" \
  "$MAC:.local/share/peekaboo-fork/staging/$TAG/" || die "copy scripts"
STAGE="\$HOME/.local/share/peekaboo-fork/staging/$TAG"

# --- 2. staged smoke -----------------------------------------------------------------
mac "cd $STAGE && $PY smoke_mac.py --mode staged --peekaboo $STAGE/cli/peekaboo --label $TAG" \
  || die "staged smoke $TAG"
[ "$STAGE_ONLY" = 1 ] && { echo "STAGED_OK $TAG"; exit 0; }

# --- 3. install ----------------------------------------------------------------------
PREV=$(mac 'readlink "$HOME/.local/share/peekaboo-fork/current" 2>/dev/null || true')
mac bash -s -- "$TAG" "$PY" <<'EOF' || die "install $TAG"
set -eu
TAG="$1"; PY="$2"
D="$HOME/.local/share/peekaboo-fork"
up() { /usr/bin/nc -z 127.0.0.1 "$1" >/dev/null 2>&1; }
# -n: replace the link itself; a plain mv onto a link to a directory would move INTO it.
ln -sfn "staging/$TAG" "$D/current"
LINE="$PY $D/current/exec_host.py"
CH="$HOME/.config/desktop-mcp/children"
mkdir -p "$(dirname "$CH")"
if ! grep -qxF "$LINE" "$CH" 2>/dev/null; then
  echo "$LINE" >> "$CH"
  # The daemon reads the file when it starts: restart it (its launchd agent brings it
  # back through `open`, which keeps the app bundle's grants).
  pkill -f 'mac-desktop-mcp/server.py' || pkill -f 'desktop-mcp/server.py' || true
  for _ in $(seq 1 60); do up 8811 && break; sleep 2; done
else
  # exec_host re-reads `current` per connection; restarting it only matters when
  # exec_host.py itself changed, and it is cheap: the daemon starts it again in 5 s.
  pkill -f "$D/current/exec_host.py" || true
  sleep 1
fi
for _ in $(seq 1 30); do up 8812 && break; sleep 1; done
up 8812 || { echo "exec_host is not listening on 8812" >&2; exit 1; }
# Keep the last three stagings.
cd "$D/staging" && ls -1t | tail -n +4 | while read -r old; do
  [ "$D/staging/$old" -ef "$D/current" ] || rm -rf "$old"; done
EOF

# --- 4. host smoke -------------------------------------------------------------------
CUR="\$HOME/.local/share/peekaboo-fork/current"
if ! mac "cd $CUR && $PY smoke_mac.py --mode host --label $TAG"; then
  [ -n "$PREV" ] && mac "ln -sfn '$PREV' \$HOME/.local/share/peekaboo-fork/current"
  die "host smoke $TAG (current back to ${PREV:-nothing})"
fi

# --- 5. stamp ------------------------------------------------------------------------
mac "echo '$TAG $SHA' > \$HOME/.local/share/peekaboo-fork/DEPLOYED" || die "stamp"
echo "DEPLOYED $TAG $SHA"
