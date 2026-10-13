# lesswrong-bot

IRC bot for #lesswrong on Libera.Chat. When `shawwwn` posts links, it replies
with Robomot-style one-line previews (tweets, xkcd, YouTube, Wikipedia,
generic OpenGraph). It uses only the Python standard library.

- `bot.py`: the asyncio IRC client (TLS, SASL, reconnect, flood-limited
  sending). Configured through environment variables listed in README.md.
- `previews.py`: `preview(url)` returns one or more lines, or raises
  `PreviewError`. To test a link without IRC: `python3 previews.py <url>`.
- `threads.py`: renders a tweet's whole thread and its replies to a static
  HTML page (enabled by `THREAD_DIR`/`THREAD_URL`).
- `run.sh`: the entry point used in production.
- `deploy/`: the systemd unit and `deploy.sh`. **Read `docs/deploy.md` before
  deploying.** It covers the server, the layout, and the pitfalls we've already
  hit.

## Workflow

- Pushing: the repo is `shawwn/lesswrong-bot`, but the active `gh` account is
  `shawnacs`. Push with
  `GH_TOKEN=$(gh auth token --user shawwn) git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push`.
- Deploying: push first, then run `deploy/deploy.sh [host]` (defaults to
  `hetzner`). The server pulls from GitHub.
- End-to-end test without Libera: point the bot at a fake local server with
  `IRC_SERVER=127.0.0.1 IRC_PORT=... IRC_TLS=0`.
- IRC lines are limited to 512 bytes. Previews are kept under about 400 bytes
  (`TWEET_BUDGET` / `MAX_PAYLOAD_BYTES`). When truncating, keep image and media
  URLs whole so IRCCloud can still expand them.
