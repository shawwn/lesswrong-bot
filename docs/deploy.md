# Deploying lesswrong-bot

The bot runs in a tmux session started by a systemd service, as the
unprivileged `deploy` user, from a git checkout in `/opt/lesswrong-bot`.
systemd starts it at boot. The tmux session lets you attach and see the live
log. A shell loop restarts the bot if it exits.

| Thing          | Value                                                  |
|----------------|--------------------------------------------------------|
| Server         | `hetzner` (SSH alias for `root@88.198.62.84`, Ubuntu 24.04) |
| Code           | `/opt/lesswrong-bot`, owned by `deploy`                |
| Unit           | `/etc/systemd/system/lesswrong-bot.service` (source: `deploy/lesswrong-bot.service`) |
| Config         | `run.sh` defaults, overridden by `/etc/lesswrong-bot.env` (optional) |
| tmux           | own server: `-L lesswrong-bot`, session `lesswrong-bot` |
| IRC            | `sbot` on irc.libera.chat, channel `#lesswrong`, no NickServ account |
| Source of code | https://github.com/shawwn/lesswrong-bot (the server pulls from here) |

## Deploy or update

From a local checkout:

    # 1. Push first. The server deploys whatever is on GitHub's master.
    GH_TOKEN=$(gh auth token --user shawwn) \
      git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push

    # 2. Deploy.
    deploy/deploy.sh            # defaults to the `hetzner` SSH host
    deploy/deploy.sh otherhost  # any root-SSH Debian/Ubuntu server

`deploy.sh` is idempotent, so it works both on a fresh server and for updates.
It:

1. copies the unit file to `/etc/systemd/system/`
2. apt-installs `git`, `tmux`, and `python3` if any are missing
3. creates the `deploy` user if it doesn't exist
4. clones the repo into `/opt/lesswrong-bot`, or `git pull --ff-only` if
   already cloned
5. enables and restarts the service, waits 15s, then prints the service state,
   the bot's cgroup, and the last log lines

A healthy deploy ends with:

    active
    bot pid 12345, cgroup 0::/system.slice/lesswrong-bot.service
    ... INFO registered as sbot
    ... INFO joined #lesswrong

The cgroup must be `lesswrong-bot.service`. See the shared tmux server pitfall
below.

The Python code has no dependencies beyond the standard library, so there's no
virtualenv or pip step.

## Configuration

`run.sh` sets the defaults (`IRC_NICK=sbot`). To override them, or to add
secrets, create `/etc/lesswrong-bot.env` on the server. It's loaded through
`EnvironmentFile=` and isn't in git:

    IRC_NICK=sbot
    IRC_SASL_USER=sbot
    IRC_SASL_PASS=...

Then run `chmod 600 /etc/lesswrong-bot.env && systemctl restart lesswrong-bot`.
See README.md for all the variables.

## Operating

All of these run as root on the server:

    sudo -u deploy tmux -L lesswrong-bot a     # watch the live log; Ctrl-b d to detach
    systemctl restart lesswrong-bot            # restart
    systemctl stop lesswrong-bot               # stop
    systemctl status lesswrong-bot

Pressing Ctrl-C inside the tmux pane restarts the bot after 10s through the
loop, without going through systemd.

## Pitfalls we hit

- **A plain `tmux new -d` over SSH dies.** On hetzner, root has
  `Linger=no`. A tmux started from an SSH login runs in that login's systemd
  user scope, and it's killed about 10 minutes after the last SSH session
  ends. That's why the bot runs from a system service instead.
- **Shared tmux server.** sharc (`sharc.service`) also runs tmux as `deploy`.
  Without `-L lesswrong-bot`, the bot's `tmux new-session` connected to sharc's
  existing tmux server. The bot then lived in sharc's cgroup, and restarting
  sharc would have killed it. Always use `-L lesswrong-bot` with every tmux
  command for this bot.
- **"dubious ownership" from git.** The checkout is owned by `deploy`, so
  running git as root fails. Use `sudo -u deploy git -C /opt/lesswrong-bot ...`.
- **Pushing as the wrong GitHub account.** The local `gh` has two accounts,
  and the active one is `shawnacs`. The repo belongs to `shawwn`, so push with
  the `GH_TOKEN=$(gh auth token --user shawwn)` command above.
- **Don't run as root.** The bot downloads and parses HTML from any link
  someone posts, which is why it runs as `deploy`. It also refuses to fetch
  private or loopback addresses (`previews._check_host`).
