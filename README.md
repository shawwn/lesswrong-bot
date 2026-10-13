# lesswrong-bot

IRC bot that joins `#lesswrong` on Libera.Chat, watches for messages from
`shawwwn`, and replies with a one-line preview of any links, in Robomot's style:

    Ravi Gupta (@GuptaRK22, 2026-10-13): "Today we announced an agreement to acquire Bumble. / Over the past six months, …"

It uses only the Python standard library (3.9+).

## Previews

- **Twitter / X** (also fxtwitter, vxtwitter, fixupx, xcancel, nitter): fetched
  through the public FxTwitter API. Shows the author, date, and text with
  newlines joined by ` / `, plus any media count and the quoted tweet.
- **YouTube**: title and channel, from oEmbed.
- **Wikipedia**: the lead summary, from the REST API.
- **Everything else**: OpenGraph / Twitter-card title and description,
  falling back to `<title>`. Non-HTML links show the content type and size.

The bot won't fetch private, loopback, or link-local addresses, and it checks
every redirect hop the same way. It reads at most 1 MiB of any response,
previews at most 3 URLs per message, skips a URL it already previewed in the
same channel within the last 10 minutes, and sends at most one line per second.

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
| `WATCH_NICKS`          | `shawwwn`          | comma-separated, case-insensitive        |
| `MAX_URLS_PER_MESSAGE` | `3`                |                                          |
| `LOG_LEVEL`            | `INFO`             | `DEBUG` logs raw IRC traffic             |

Some Libera channels only allow registered nicks to join or speak. If the bot
logs `cannot join channel`, register its nick with NickServ and set the SASL
variables.

The bot reconnects automatically with exponential backoff. To keep it running,
use a systemd user unit, tmux, or similar.
