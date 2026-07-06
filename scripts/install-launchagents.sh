#!/bin/bash
# install-launchagents.sh — Install (or reinstall) the tv-tracker server
# LaunchAgent into ~/Library/LaunchAgents/, substituting the template's
# placeholder repo path and Tailscale IP.
#
# The server binds to the machine's Tailscale IP so the tracker is
# reachable from Tailscale-authenticated devices (phone) and NOT from the
# home LAN — the tailnet is the access control; there is no app auth.
#
# Idempotent: safe to re-run (boots out a loaded agent before reinstalling).
# BlockBlock will prompt once for the new plist — expected.
#
# Usage:
#   scripts/install-launchagents.sh              # install
#   scripts/install-launchagents.sh --uninstall  # remove

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
TEMPLATE_DIR="$REPO_ROOT/launchagents"
INSTALL_DIR="$HOME/Library/LaunchAgents"
PLACEHOLDER="/Users/YOUR_USER/bin/tv-tracker"
BIND_PLACEHOLDER="YOUR_TAILSCALE_IP"
LABEL="net.midwood.tv-tracker.server"

resolve_tailscale_ip() {
    if ! command -v tailscale >/dev/null 2>&1; then
        echo "ERROR: tailscale CLI not found on PATH." >&2
        echo "       Install Tailscale and sign in before running this script." >&2
        exit 1
    fi
    local ip
    ip=$(tailscale ip -4 2>/dev/null | head -n 1 || true)
    if [[ -z "$ip" ]]; then
        echo "ERROR: 'tailscale ip -4' returned nothing. Is tailscaled running and signed in?" >&2
        exit 1
    fi
    echo "$ip"
}

uid=$(id -u)

unload_if_loaded() {
    if launchctl print "gui/$uid/$LABEL" >/dev/null 2>&1; then
        launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
        echo "  booted out: $LABEL"
    fi
}

if [[ "${1:-}" == "--uninstall" ]]; then
    unload_if_loaded
    target="$INSTALL_DIR/$LABEL.plist"
    if [[ -f "$target" ]]; then
        rm "$target"
        echo "  removed: $target"
    fi
    exit 0
fi

mkdir -p "$INSTALL_DIR"
mkdir -p "$REPO_ROOT/baselines/logs"

TAILSCALE_IP=$(resolve_tailscale_ip)
echo "  using tailscale IP: $TAILSCALE_IP"

template="$TEMPLATE_DIR/$LABEL.plist"
target="$INSTALL_DIR/$LABEL.plist"
if [[ ! -f "$template" ]]; then
    echo "missing template: $template" >&2
    exit 1
fi
unload_if_loaded
sed -e "s|$PLACEHOLDER|$REPO_ROOT|g" \
    -e "s|$BIND_PLACEHOLDER|$TAILSCALE_IP|g" \
    "$template" > "$target"
plutil -lint "$target" >/dev/null
launchctl bootstrap "gui/$uid" "$target"
echo "  installed: $LABEL"

echo
echo "Done. Verify with:"
echo "  launchctl print gui/$uid/$LABEL | head"
echo "  curl http://$TAILSCALE_IP:8431/healthz"
