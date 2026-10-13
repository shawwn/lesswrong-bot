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

echo "==> copying unit file and config example to $HOST"
scp -q "$HERE/lesswrong-bot.service" "$HOST:/tmp/lesswrong-bot.service"
scp -q "$HERE/lesswrong-bot.env.example" "$HOST:/tmp/lesswrong-bot.env.example"

echo "==> provisioning and updating on $HOST"
ssh "$HOST" "FORCE_RESTART=$FORCE_RESTART sh -s" <<'REMOTE'
set -eu
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
reloadable='^(previews\.py|threads\.py|.*\.md|docs/.*|deploy/deploy\.sh|deploy/lesswrong-bot\.env\.example|\.gitignore|pyproject\.toml)$'
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
