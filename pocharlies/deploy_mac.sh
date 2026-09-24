#!/usr/bin/env bash
# Deploy one upstream release of Peekaboo, plus this fork's proxy, to the Mac. Runs on the
# machine that drives the Mac (the x86), over SSH.
#
#   deploy_mac.sh [--stage-only] <tag>
#
# 1. stage   download the release's CLI tarball and app zip, verify both against the
#            release's checksums.txt, unpack into ~/.local/share/peekaboo-fork/staging/<tag>
#            together with this checkout's proxy and smoke test
# 2. smoke   smoke_mac.py --mode staged with the staged binary (no TCC needed)
#    --stage-only stops here: that is the pre-merge gate
# 3. install point `current` at the staging dir; replace /Applications/Peekaboo.app only
#            if its version differs (a same-team, same-bundle-id update keeps the TCC grants)
#            and relaunch it as the background Bridge host; make sure the login agent that
#            starts it that way exists
# 4. smoke   smoke_mac.py --mode bridge, as the MCP client runs it. On failure `current`
#            goes back to what it was
# 5. stamp   ~/.local/share/peekaboo-fork/DEPLOYED = "<tag> <fork sha>"
#
# Exit: 0 ok, 1 failed, 2 Mac unreachable (retry later, not a failure of the release).
# Official signed binaries only: the Bridge refuses a CLI not signed by Peekaboo's team,
# so this fork never builds Peekaboo itself.
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
TAG="$1"; VER="${TAG#v}"
S="$HOME/.local/share/peekaboo-fork/staging/$TAG"
U="https://github.com/openclaw/Peekaboo/releases/download/$TAG"
mkdir -p "$S" && cd "$S"
if [ ! -x cli/peekaboo ] || [ ! -d app/Peekaboo.app ]; then
  rm -rf dl cli app && mkdir dl
  curl -fsSL --retry 3 -o dl/checksums.txt "$U/checksums.txt"
  for f in peekaboo-macos-arm64.tar.gz "Peekaboo-$VER.app.zip"; do
    curl -fsSL --retry 3 -o "dl/$f" "$U/$f"
    want=$(awk -v f="$f" '$2==f {print $1}' dl/checksums.txt)
    got=$(shasum -a 256 "dl/$f" | cut -d' ' -f1)
    [ -n "$want" ] && [ "$want" = "$got" ] || { echo "checksum mismatch: $f" >&2; exit 1; }
  done
  mkdir cli app
  tar -xzf dl/peekaboo-macos-arm64.tar.gz -C cli --strip-components 1
  /usr/bin/ditto -x -k "dl/Peekaboo-$VER.app.zip" app
  rm -rf dl
fi
spctl -a app/Peekaboo.app >/dev/null 2>&1 || { echo "app not accepted by Gatekeeper" >&2; exit 1; }
codesign -dv cli/peekaboo 2>&1 | grep -q 'TeamIdentifier=FWJYW4S8P8' \
  || { echo "CLI not signed by Peekaboo's team" >&2; exit 1; }
EOF
STAGE="\$HOME/.local/share/peekaboo-fork/staging/$TAG"
scp -q -o BatchMode=yes "$HERE/peekaboo_proxy.py" "$HERE/smoke_mac.py" \
  "$MAC:.local/share/peekaboo-fork/staging/$TAG/" || die "copy proxy"

# --- 2. staged smoke -----------------------------------------------------------------
mac "cd $STAGE && $PY smoke_mac.py --mode staged --peekaboo $STAGE/cli/peekaboo --label $TAG" \
  || die "staged smoke $TAG"
[ "$STAGE_ONLY" = 1 ] && { echo "STAGED_OK $TAG"; exit 0; }

# --- 3. install ----------------------------------------------------------------------
PREV=$(mac 'readlink "$HOME/.local/share/peekaboo-fork/current" 2>/dev/null || true')
mac bash -s -- "$TAG" <<'EOF' || die "install $TAG"
set -eu
TAG="$1"; VER="${TAG#v}"
D="$HOME/.local/share/peekaboo-fork"
# -n: replace the link itself; a plain mv onto a link to a directory would move INTO it.
ln -sfn "staging/$TAG" "$D/current"
APP=/Applications/Peekaboo.app
have=$(defaults read "$APP/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null || echo none)
if [ "$have" != "$VER" ]; then
  osascript -e 'quit app "Peekaboo"' >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -x Peekaboo >/dev/null || break; sleep 1; done
  pkill -x Peekaboo 2>/dev/null || true
  rm -rf "$APP.new" && /usr/bin/ditto "$D/staging/$TAG/app/Peekaboo.app" "$APP.new"
  rm -rf "$APP" && mv "$APP.new" "$APP"
fi
# The Bridge must survive a reboot: a login agent starts the app as a background host.
LA="$HOME/Library/LaunchAgents/com.pocharlies.peekaboo-bridge.plist"
if [ ! -f "$LA" ]; then
  cat > "$LA" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.pocharlies.peekaboo-bridge</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/open</string><string>-g</string><string>-a</string>
    <string>/Applications/Peekaboo.app</string><string>--args</string>
    <string>--background-bridge-host</string></array>
  <key>RunAtLoad</key><true/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict></plist>
PLIST
  launchctl bootstrap "gui/$(id -u)" "$LA" 2>/dev/null || true
fi
pgrep -x Peekaboo >/dev/null || /usr/bin/open -g -a "$APP" --args --background-bridge-host
for _ in $(seq 1 30); do [ -S "$HOME/Library/Application Support/Peekaboo/bridge.sock" ] && break; sleep 1; done
# Keep the last three stagings.
cd "$D/staging" && ls -1t | tail -n +4 | while read -r old; do
  [ "$D/staging/$old" -ef "$D/current" ] || rm -rf "$old"; done
EOF

# --- 4. bridge smoke -----------------------------------------------------------------
CUR="\$HOME/.local/share/peekaboo-fork/current"
if ! mac "cd $CUR && $PY smoke_mac.py --mode bridge --peekaboo $CUR/cli/peekaboo --label $TAG"; then
  [ -n "$PREV" ] && mac "ln -sfn '$PREV' \$HOME/.local/share/peekaboo-fork/current"
  die "bridge smoke $TAG (current back to ${PREV:-nothing})"
fi

# --- 5. stamp ------------------------------------------------------------------------
mac "echo '$TAG $SHA' > \$HOME/.local/share/peekaboo-fork/DEPLOYED" || die "stamp"
echo "DEPLOYED $TAG $SHA"
