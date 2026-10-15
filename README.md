# lesswrong-bot

IRC bot that joins `#lesswrong` on Libera.Chat. When `shawwwn` posts a link,
it replies with a preview in Robomot's style:

    Alternate version of shawwwn’s link: https://xc-redirect.alwaysdata.net/GuptaRK22/status/2110024474938790330
    Ravi Gupta (@GuptaRK22, 2026-10-13): "Today we announced an agreement to acquire Bumble. / Over the past six months, …"

When anyone else posts a tweet link, it posts nothing, since Robomot already
previews the tweet. For anyone else's other
links, it stays silent unless the page only shows its content via
JavaScript (so other preview bots see just the site-wide title). In that case
it renders the page, waits for Robomot and feepbot to reply (up to 20s), and
posts its preview only if neither of them showed the page's real headline. It
never replies to Robomot itself.

It uses only the Python standard library (3.9+). Optionally, it uses headless
Chrome to preview pages that only show their content via JavaScript.

## Previews

- **Twitter / X** (also fxtwitter, vxtwitter, fixupx, xcancel, nitter): fetched
  through the public FxTwitter API. Like Robomot, it first posts an "Alternate
  version" link via `xc-redirect.alwaysdata.net` (Robomot's redirector to a
  working Nitter/xcancel mirror), then the author, date, and text with
  newlines joined by ` / `, the quoted tweet, and direct links to any photos,
  videos, or GIFs. If the links don't fit on the same line, they go on the
  next one. Both lines go out together as soon as the tweet is fetched
  (about 0.3s). For every tweet link (anyone's), the bot also builds a
  no-login page with the whole thread and replies (see `THREAD_DIR` below),
  in the background so it never delays the preview. That page's link isn't
  posted.
- **GitHub** repos: description, the README's first paragraph, stars, forks,
  top languages, license, and last push, from the GitHub API. Issues and PRs:
  title, kind, author, state, date, comment count, and the start of the text.
  Other GitHub pages (files, profiles) get the generic preview.
- **xkcd**: number, title, date, the comic's image URL (so IRCCloud shows it
  inline), and the alt text.
- **YouTube**: title and channel, from oEmbed.
- **Wikipedia**: the lead summary, from the REST API.
- **Everything else**: OpenGraph / Twitter-card title and description,
  falling back to `<title>`. Non-HTML links (images, PDFs, ...) get no reply.

The bot won't fetch private, loopback, or link-local addresses, and it checks
every redirect hop the same way. It reads at most 1 MiB of any response,
previews at most 3 URLs per message, skips a URL it already previewed in the
same channel within the last 10 minutes, and sends at most 4 lines at once,
then one line per second.

## Running

    IRC_NICK=shawbot python3 bot.py

To try out previews without connecting to IRC:

    python3 previews.py https://x.com/GuptaRK22/status/2110024474938790330

## Configuration (environment variables)

| Variable               | Default            |                                          |
|------------------------|--------------------|------------------------------------------|
| `IRC_SERVER`           | `irc.libera.chat`  |                                          |
| `IRC_PORT`             | `6697`             |                                          |
| `IRC_TLS`              | `1`                | `0` to disable                           |
| `IRC_NICK`             | `shawbot`          | `_` is appended if the nick is taken     |
| `IRC_SASL_USER`        |                    | NickServ account, for SASL PLAIN login   |
| `IRC_SASL_PASS`        |                    |                                          |
| `IRC_CHANNELS`         | `#lesswrong`       | comma-separated                          |
| `WATCH_NICKS`          | `shawwwn`          | get full previews of every link; everyone else gets nothing for tweets and other links (see above). Comma-separated, case-insensitive |
| `IGNORE_NICKS`         | `Robomot__,Robomot` | never replied to (other bots)           |
| `RELAY_NICKS`          | `feepbot`          | bridge bots whose `<nick> text` messages count as from `nick` |
| `RIVAL_NICKS`          | `Robomot__,feepbot` | other preview bots to wait for and compare against (see above) |
| `TRIGGER`              | `%sbot`            | anyone who writes this in a message gets full previews, as if they were in `WATCH_NICKS` |
| `MAX_URLS_PER_MESSAGE` | `3`                |                                          |
| `THREAD_DIR`           |                    | where to write thread/reply HTML pages   |
| `THREAD_URL`           |                    | public URL that serves `THREAD_DIR`      |
| `CHROME`               |                    | headless Chrome binary, for rendering JavaScript-only pages (see docs/deploy.md) |
| `GITHUB_TOKEN`         |                    | optional; raises the GitHub API limit from 60 to 5000 requests/hour (a repo preview uses 3). Any token works, no scopes needed |
| `LOG_LEVEL`            | `INFO`             | `DEBUG` logs raw IRC traffic             |

Some Libera channels only allow registered nicks to join or speak. If the bot
logs `cannot join channel`, register its nick with NickServ and set the SASL
variables.

The bot reconnects automatically with exponential backoff.

## Deploying

    deploy/deploy.sh [ssh-host]

This sets the bot up as a systemd-managed tmux session on a Debian/Ubuntu
server. See [docs/deploy.md](docs/deploy.md).
