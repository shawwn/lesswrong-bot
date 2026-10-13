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
| Config         | `run.sh` defaults, overridden by `/etc/lesswrong-bot.env` (from `deploy/lesswrong-bot.env.example`) |
| Thread pages   | written to `/srv/the.shawwn.net/t/`, served by the existing nginx site as `https://the.shawwn.net/t/<id>.html` |
| tmux           | own server: `-L lesswrong-bot`, session `lesswrong-bot` |
| IRC            | `sbot` on irc.libera.chat, channels `#lesswrong` and `#lw-test`, no NickServ account |
| Source of code | https://github.com/shawwn/lesswrong-bot (the server pulls from here) |

## Deploy or update

From a local checkout:

    # 1. Push first. The server deploys whatever is on GitHub's master.
    GH_TOKEN=$(gh auth token --user shawwn) \
      git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push

    # 2. Deploy.
    deploy/deploy.sh            # defaults to the `hetzner` SSH host
    deploy/deploy.sh otherhost  # any root-SSH Debian/Ubuntu server
    deploy/deploy.sh --restart  # force a full restart (e.g. after editing the env file)

`deploy.sh` is idempotent, so it works both on a fresh server and for updates.
It:

1. installs the unit file to `/etc/systemd/system/` if it changed
2. apt-installs `git`, `tmux`, and `python3` if any are missing
3. creates the `deploy` user if it doesn't exist
4. clones the repo into `/opt/lesswrong-bot`, or `git pull --ff-only` if
   already cloned
5. installs `/etc/lesswrong-bot.env` from `deploy/lesswrong-bot.env.example`
   if it doesn't exist yet. If the file sets `THREAD_DIR`, it creates that
   directory, owned by `deploy`.
6. picks between a live reload and a restart (see below), then prints the
   service state, the bot's cgroup, and the last log lines

A healthy restart ends with:

    active
    bot pid 12345, cgroup 0::/system.slice/lesswrong-bot.service
    ... INFO registered as sbot
    ... INFO joined #lesswrong
    ... INFO joined #lw-test

A healthy live reload ends with `... INFO reloaded threads, previews`.

### Live reload vs restart

A restart makes the bot quit and rejoin, which shows up in the channels, so
`deploy.sh` avoids one when it can. On `SIGHUP`, the bot reloads `threads.py`
and `previews.py` in place without disconnecting (`Bot.reload` in `bot.py`).
It compiles both files first, and if either has a syntax error it logs
`not reloading, ... doesn't compile` and keeps running the old code.

`deploy.sh` sends `SIGHUP` when every changed file is preview code, docs, or
deploy tooling. It restarts when anything else changed: `bot.py`, `run.sh`,
the unit file, or a fresh install. Changes to `/etc/lesswrong-bot.env` aren't
tracked in git, so after editing it, use `deploy.sh --restart` or
`systemctl restart lesswrong-bot`. To reload by hand on the server:
`pkill -HUP -u deploy -f '^python3 bot.py$'`.

Use `#lw-test` (the bot joins it too) to try changes without spamming
`#lesswrong`.

The cgroup must be `lesswrong-bot.service`. See the shared tmux server pitfall
below.

The Python code has no dependencies beyond the standard library, so there's no
virtualenv or pip step.

## Configuration

`run.sh` sets the defaults (`IRC_NICK=sbot`). Per-server settings and secrets
go in `/etc/lesswrong-bot.env` on the server. It's loaded through
`EnvironmentFile=` and isn't in git. `deploy.sh` creates it from
`deploy/lesswrong-bot.env.example` the first time, and never overwrites it
afterwards. To change a setting, edit the file on the server, then run
`systemctl restart lesswrong-bot`. See README.md for all the variables.

### Thread pages

For every linked tweet, the bot fetches the author's self-thread (FxTwitter
`/2/thread/<id>`) and the first page of replies to the linked post
(`/2/conversation/<id>`, about 40 replies, shown as a nested tree under it).
If there's a multi-post thread or any replies, it writes a static HTML page to
`THREAD_DIR/<linked-id>.html`. It then posts
`No-login version of <nick>'s link: THREAD_URL/<linked-id>.html` instead of
the embed link. The page is named after the linked post because the
replies shown depend on which post was linked. The page is re-rendered every
time the tweet is linked, so it picks up new replies. Media is loaded straight
from Twitter's CDN; nothing but the HTML is stored. If building the page fails
for any reason, the bot logs it and falls back to the embed link.

The bot doesn't serve these pages itself; any static web server works. On
hetzner, the existing `the.shawwn.net` nginx site (`root /srv/the.shawwn.net`,
`autoindex on`) already serves the directory, so it needed no nginx changes.
On a new server, either point an existing static site at `THREAD_DIR`, or
leave `THREAD_DIR`/`THREAD_URL` unset to turn the feature off.

## Operating

All of these run as root on the server:

    sudo -u deploy tmux -L lesswrong-bot a     # watch the live log; Ctrl-b d to detach
    pkill -HUP -u deploy -f '^python3 bot.py$' # live-reload preview code, stays connected
    systemctl restart lesswrong-bot            # restart (quits and rejoins IRC)
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
