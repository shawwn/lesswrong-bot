#!/bin/sh
# Deploy (or update) lesswrong-bot on a server over SSH. Idempotent.
#
#   deploy/deploy.sh [ssh-host]      # default: hetzner
#
# Assumes root SSH access to an apt-based (Debian/Ubuntu) server, and that the
# commits you want to deploy are already pushed to GitHub (the server pulls
# from https://github.com/shawwn/lesswrong-bot).
set -eu

HOST=${1:-hetzner}
HERE=$(cd "$(dirname "$0")" && pwd)

echo "==> installing unit file on $HOST"
scp -q "$HERE/lesswrong-bot.service" "$HOST:/etc/systemd/system/lesswrong-bot.service"

echo "==> provisioning and (re)starting on $HOST"
ssh "$HOST" sh -s <<'REMOTE'
set -eu
REPO=https://github.com/shawwn/lesswrong-bot.git
DIR=/opt/lesswrong-bot
USER=deploy

missing=""
for cmd in git tmux python3; do
    command -v "$cmd" >/dev/null || missing="$missing $cmd"
done
if [ -n "$missing" ]; then
    apt-get update -q && apt-get install -yq $missing
fi

id "$USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$USER"

if [ -d "$DIR/.git" ]; then
    sudo -u "$USER" git -C "$DIR" pull -q --ff-only
else
    install -d -o "$USER" -g "$USER" "$DIR"
    sudo -u "$USER" git clone -q "$REPO" "$DIR"
fi
echo "deployed: $(sudo -u "$USER" git -C "$DIR" log --oneline -1)"

systemctl daemon-reload
systemctl enable -q lesswrong-bot.service
systemctl restart lesswrong-bot.service

sleep 15
systemctl is-active lesswrong-bot.service
pid=$(pgrep -u "$USER" -f '^python3 bot.py$' || true)
[ -n "$pid" ] && echo "bot pid $pid, cgroup $(cat /proc/$pid/cgroup)"
sudo -u "$USER" tmux -L lesswrong-bot capture-pane -p -J -S -20 -t lesswrong-bot | grep -v '^$' | tail -5
REMOTE
