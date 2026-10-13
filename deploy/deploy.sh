#!/bin/sh
# Deploy (or update) lesswrong-bot on a server over SSH. Idempotent.
#
#   deploy/deploy.sh [--restart] [ssh-host]      # default host: hetzner
#
# If only the preview code (previews.py, threads.py) or docs changed, the
# running bot is sent SIGHUP and reloads that code in place without leaving
# IRC. Anything else (bot.py, run.sh, the unit file, a fresh install) gets a
# full restart. --restart forces one, e.g. after editing /etc/lesswrong-bot.env.
#
# Assumes root SSH access to an apt-based (Debian/Ubuntu) server, and that the
# commits you want to deploy are already pushed to GitHub (the server pulls
# from https://github.com/shawwn/lesswrong-bot).
set -eu

FORCE_RESTART=0
if [ "${1:-}" = "--restart" ]; then
    FORCE_RESTART=1
    shift
fi
HOST=${1:-hetzner}
HERE=$(cd "$(dirname "$0")" && pwd)
# Headless browser for rendering single-page apps (see browser.py). Only
# installed if /etc/lesswrong-bot.env sets CHROME. Versions:
# https://googlechromelabs.github.io/chrome-for-testing/
CHROME_VERSION=155.0.8059.39

echo "==> copying unit file and config example to $HOST"
scp -q "$HERE/lesswrong-bot.service" "$HOST:/tmp/lesswrong-bot.service"
scp -q "$HERE/lesswrong-bot.env.example" "$HOST:/tmp/lesswrong-bot.env.example"

echo "==> provisioning and updating on $HOST"
ssh "$HOST" "FORCE_RESTART=$FORCE_RESTART CHROME_VERSION=$CHROME_VERSION sh -s" <<'REMOTE'
set -eu
# Never let package installs restart other services (needrestart: list only).
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
REPO=https://github.com/shawwn/lesswrong-bot.git
DIR=/opt/lesswrong-bot
USER=deploy
UNIT=/etc/systemd/system/lesswrong-bot.service
restart=$FORCE_RESTART

missing=""
for cmd in git tmux python3; do
    command -v "$cmd" >/dev/null || missing="$missing $cmd"
done
if [ -n "$missing" ]; then
    apt-get update -q && apt-get install -yq $missing
fi

id "$USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$USER"

if cmp -s /tmp/lesswrong-bot.service "$UNIT"; then
    rm -f /tmp/lesswrong-bot.service
else
    mv /tmp/lesswrong-bot.service "$UNIT"
    echo "unit file changed"
    restart=1
fi

if [ -d "$DIR/.git" ]; then
    old=$(sudo -u "$USER" git -C "$DIR" rev-parse HEAD)
    sudo -u "$USER" git -C "$DIR" pull -q --ff-only
    changed=$(sudo -u "$USER" git -C "$DIR" diff --name-only "$old" HEAD)
else
    install -d -o "$USER" -g "$USER" "$DIR"
    sudo -u "$USER" git clone -q "$REPO" "$DIR"
    changed="(fresh clone)"
    restart=1
fi
echo "deployed: $(sudo -u "$USER" git -C "$DIR" log --oneline -1)"
[ -n "$changed" ] && echo "changed: $(echo $changed)"

# Files the running bot can pick up via SIGHUP (or doesn't use at runtime).
reloadable='^(browser\.py|previews\.py|threads\.py|.*\.md|docs/.*|deploy/deploy\.sh|deploy/lesswrong-bot\.env\.example|\.gitignore|pyproject\.toml)$'
if [ -n "$changed" ] && echo "$changed" | grep -Eqv "$reloadable"; then
    restart=1
fi

# Per-server config. Only installed if missing, so local edits and secrets survive.
if [ ! -e /etc/lesswrong-bot.env ]; then
    install -m 600 /tmp/lesswrong-bot.env.example /etc/lesswrong-bot.env
    echo "installed /etc/lesswrong-bot.env from the example; review it"
    restart=1
fi
rm -f /tmp/lesswrong-bot.env.example
THREAD_DIR=$(sed -n 's/^THREAD_DIR=//p' /etc/lesswrong-bot.env | tail -1)
if [ -n "$THREAD_DIR" ]; then
    install -d -o "$USER" -g "$USER" -m 755 "$THREAD_DIR"
fi

# Headless Chrome, if configured.
if grep -q '^CHROME=' /etc/lesswrong-bot.env; then
    CHS=/opt/chrome-headless-shell
    if [ ! -x "$CHS/$CHROME_VERSION/chrome-headless-shell" ]; then
        echo "==> installing chrome-headless-shell $CHROME_VERSION"
        # Libraries it needs beyond a minimal server (see deb.deps in the zip).
        # Ubuntu 24.04 renamed some with a t64 suffix; prefer that if it exists.
        pkgs="unzip ca-certificates"
        for p in libnspr4 libnss3 libatk1.0-0 libatk-bridge2.0-0 libxdamage1 libasound2 libatspi2.0-0; do
            apt-cache show "${p}t64" >/dev/null 2>&1 && p="${p}t64"
            pkgs="$pkgs $p"
        done
        apt-get install -yq --no-install-recommends $pkgs >/dev/null
        tmp=$(mktemp -d)
        curl -sSfo "$tmp/c.zip" "https://storage.googleapis.com/chrome-for-testing-public/$CHROME_VERSION/linux64/chrome-headless-shell-linux64.zip"
        unzip -q "$tmp/c.zip" -d "$tmp"
        install -d "$CHS/$CHROME_VERSION"
        cp -a "$tmp/chrome-headless-shell-linux64/." "$CHS/$CHROME_VERSION/"
        rm -rf "$tmp"
    fi
    ln -sfn "$CHROME_VERSION" "$CHS/current"
    # Ubuntu 23.10+ blocks the unprivileged user namespaces Chrome's sandbox
    # needs. Rather than --no-sandbox, let just this binary use them, as
    # Chromium documents (docs/security/apparmor-userns-restrictions.md).
    if [ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = 1 ]; then
        printf '%s\n' \
            '# Installed by lesswrong-bot deploy.sh: lets chrome-headless-shell create' \
            '# user namespaces for its sandbox.' \
            'abi <abi/4.0>,' \
            'include <tunables/global>' \
            '' \
            "profile chrome-headless-shell $CHS/*/chrome-headless-shell flags=(unconfined) {" \
            '  userns,' \
            '  include if exists <local/chrome-headless-shell>' \
            '}' > /etc/apparmor.d/chrome-headless-shell
        apparmor_parser -r /etc/apparmor.d/chrome-headless-shell
    fi
    # Smoke test, as the bot's user.
    if (cd /tmp && sudo -u "$USER" timeout 30 "$CHS/current/chrome-headless-shell" --dump-dom about:blank >/dev/null 2>&1); then
        echo "chrome-headless-shell $CHROME_VERSION works"
    else
        echo "WARNING: chrome-headless-shell failed to start; rendering will be skipped"
    fi
fi

systemctl daemon-reload
systemctl enable -q lesswrong-bot.service
pid=$(pgrep -u "$USER" -f '^python3 bot.py$' || true)

if [ "$restart" = 1 ] || [ -z "$pid" ]; then
    echo "==> restarting (the bot will quit and rejoin IRC)"
    systemctl restart lesswrong-bot.service
    sleep 15
elif [ -n "$changed" ]; then
    echo "==> live-reloading pid $pid (stays connected)"
    kill -HUP "$pid"
    sleep 2
else
    echo "==> nothing changed"
fi

systemctl is-active lesswrong-bot.service
pid=$(pgrep -u "$USER" -f '^python3 bot.py$' || true)
[ -n "$pid" ] && echo "bot pid $pid, cgroup $(cat /proc/$pid/cgroup)"
sudo -u "$USER" tmux -L lesswrong-bot capture-pane -p -J -S -20 -t lesswrong-bot | grep -v '^$' | tail -5
REMOTE
