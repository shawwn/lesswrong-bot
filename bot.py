#!/usr/bin/env python3
"""IRC bot that posts link previews for messages from selected nicks.

Configuration is via environment variables (see README.md). Stdlib only.
"""

import asyncio
import base64
import importlib
import logging
import os
import re
import signal
import ssl
import time

import browser
import previews
import threads

log = logging.getLogger("linkbot")

SERVER = os.environ.get("IRC_SERVER", "irc.libera.chat")
PORT = int(os.environ.get("IRC_PORT", "6697"))
USE_TLS = os.environ.get("IRC_TLS", "1") != "0"
NICK = os.environ.get("IRC_NICK", "shawbot")
REALNAME = os.environ.get("IRC_REALNAME", "link preview bot")
SASL_USER = os.environ.get("IRC_SASL_USER", "")
SASL_PASS = os.environ.get("IRC_SASL_PASS", "")
CHANNELS = [c.strip() for c in os.environ.get("IRC_CHANNELS", "#lesswrong").split(",") if c.strip()]
def _nick_set(var, default):
    return {n.strip().lower() for n in os.environ.get(var, default).split(",") if n.strip()}


# Full previews (tweet text, xkcd, page titles, ...) for these nicks. Everyone
# else only gets the no-login link for tweets, since Robomot previews the rest.
WATCH_NICKS = _nick_set("WATCH_NICKS", "shawwwn")
# Never react to these (other bots; Robomot quotes tweet text, which can contain
# tweet links, so answering it could ping-pong).
IGNORE_NICKS = _nick_set("IGNORE_NICKS", "Robomot__,Robomot")
# Bridge bots that relay other people's messages as "<nick> text"; treat those
# as coming from nick.
RELAY_NICKS = _nick_set("RELAY_NICKS", "feepbot")
# Other link-preview bots. For non-WATCH_NICKS links to JavaScript-only pages
# (which these bots preview with the site-wide title), we render the page,
# wait for their replies, and post ours only if none of them got it right.
RIVAL_NICKS = _nick_set("RIVAL_NICKS", "Robomot__,feepbot")
RIVAL_WAIT = 20  # seconds to wait for every rival to reply
# Writing this anywhere in a message gets full previews, as if from WATCH_NICKS.
TRIGGER = os.environ.get("TRIGGER", "%sbot").casefold()

# IRC formatting codes and zero-width characters (relays often put these inside
# nicks so they don't ping the real person).
_FORMATTING_RE = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?|[\x02\x0f\x16\x1d\x1e\x1f\u200b-\u200d\u2060\ufeff]")
_RELAY_RE = re.compile(r"^<([^\s<>]{1,32})> (.*)$", re.DOTALL)


def _normalize(text):
    """Casefolded words only, for loose "does this message mention that" checks."""
    return " ".join(re.findall(r"\w+", text.casefold()))


def unrelay(sender, text):
    """("feepbot", "<gwern> hi") -> ("gwern", "hi"); other messages unchanged."""
    if sender.lower() not in RELAY_NICKS:
        return sender, text
    m = _RELAY_RE.match(_FORMATTING_RE.sub("", text))
    return (m.group(1), m.group(2)) if m else (sender, text)
MAX_URLS_PER_MESSAGE = int(os.environ.get("MAX_URLS_PER_MESSAGE", "3"))
DUPLICATE_WINDOW = 600  # seconds; don't re-preview the same URL in the same channel

# A PRIVMSG line is capped at 512 bytes including our own ":nick!user@host"
# prefix as relayed to others; leave generous room for it.
MAX_PAYLOAD_BYTES = 400
SEND_BURST = 4  # lines sent back-to-back before throttling to 1/second


def parse_line(line):
    """Parse an IRC line into (prefix, command, params)."""
    prefix = ""
    if line.startswith("@"):  # IRCv3 message tags; we don't use them
        _, _, line = line.partition(" ")
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    if " :" in line:
        head, _, trailing = line.partition(" :")
        params = head.split() + [trailing]
    else:
        params = line.split()
    command = params.pop(0).upper() if params else ""
    return prefix, command, params


class Bot:
    def __init__(self):
        self.reader = None
        self.writer = None
        self.nick = NICK
        self.seen = {}  # (channel, url) -> timestamp
        self.rival_lines = []  # (monotonic time, channel, nick, text) from RIVAL_NICKS
        self.send_queue = asyncio.Queue()

    # ------------------------------------------------------------------ io --

    def raw(self, line):
        log.debug(">> %s", line)
        self.writer.write((line + "\r\n").encode("utf-8"))

    async def sender(self):
        """Drain the outgoing PRIVMSG queue at a flood-safe rate: a burst of
        up to SEND_BURST lines (so a two-line preview arrives together, like
        Robomot's), then one line per second."""
        tokens, last = SEND_BURST, time.monotonic()
        while True:
            line = await self.send_queue.get()
            now = time.monotonic()
            tokens = min(SEND_BURST, tokens + (now - last))
            last = now
            if tokens < 1:
                await asyncio.sleep(1 - tokens)
                tokens, last = 1, time.monotonic()
            tokens -= 1
            self.raw(line)
            await self.writer.drain()

    def say(self, target, text):
        self.send_queue.put_nowait(f"PRIVMSG {target} :{previews.truncate_bytes(text, MAX_PAYLOAD_BYTES)}")

    # ---------------------------------------------------------- connection --

    async def connect(self):
        ctx = ssl.create_default_context() if USE_TLS else None
        log.info("connecting to %s:%d (tls=%s)", SERVER, PORT, USE_TLS)
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(SERVER, PORT, ssl=ctx), timeout=30
        )
        self.nick = NICK
        if SASL_USER and SASL_PASS:
            self.raw("CAP REQ :sasl")
        self.raw(f"NICK {self.nick}")
        self.raw(f"USER {NICK} 0 * :{REALNAME}")

    async def run_once(self):
        await self.connect()
        sender = asyncio.create_task(self.sender())
        try:
            while True:
                # Libera pings roughly every couple of minutes; treat 5 minutes of
                # silence as a dead connection.
                data = await asyncio.wait_for(self.reader.readline(), timeout=300)
                if not data:
                    raise ConnectionError("server closed connection")
                line = data.decode("utf-8", "replace").rstrip("\r\n")
                log.debug("<< %s", line)
                await self.handle(*parse_line(line))
        finally:
            sender.cancel()
            try:
                self.writer.close()
            except Exception:
                pass

    def reload(self):
        """Reload the preview code in place (on SIGHUP) without reconnecting.
        Changes to bot.py itself still need a restart."""
        modules = [browser, threads, previews]  # previews imports the others
        for mod in modules:
            try:
                with open(mod.__file__, encoding="utf-8") as f:
                    compile(f.read(), mod.__file__, "exec")
            except (OSError, SyntaxError) as e:
                log.error("not reloading, %s doesn't compile: %s", mod.__file__, e)
                return
        for mod in modules:
            try:
                importlib.reload(mod)
            except Exception:
                log.exception("reloading %s failed", mod.__name__)
                return
        log.info("reloaded %s", ", ".join(m.__name__ for m in modules))

    async def run_forever(self):
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, self.reload)
        delay = 5
        while True:
            started = time.monotonic()
            try:
                await self.run_once()
            except (OSError, ConnectionError, asyncio.TimeoutError, ssl.SSLError) as e:
                log.warning("disconnected: %s", e)
            if time.monotonic() - started > 300:
                delay = 5
            log.info("reconnecting in %ds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    # ------------------------------------------------------------ handlers --

    async def handle(self, prefix, command, params):
        if command == "PING":
            self.raw(f"PONG :{params[-1] if params else ''}")

        elif command == "CAP" and len(params) >= 3:
            sub, caps = params[1].upper(), params[-1]
            if sub == "ACK" and "sasl" in caps.split():
                self.raw("AUTHENTICATE PLAIN")
            elif sub == "NAK":
                log.error("server refused SASL capability")
                self.raw("CAP END")

        elif command == "AUTHENTICATE" and params and params[0] == "+":
            token = f"{SASL_USER}\0{SASL_USER}\0{SASL_PASS}".encode("utf-8")
            self.raw("AUTHENTICATE " + base64.b64encode(token).decode("ascii"))

        elif command in ("903", "904", "905", "906", "907"):
            if command == "903":
                log.info("SASL authentication succeeded")
            else:
                log.error("SASL authentication failed (%s): %s", command, params[-1] if params else "")
            self.raw("CAP END")

        elif command == "433":  # nickname in use
            self.nick += "_"
            self.raw(f"NICK {self.nick}")

        elif command == "001":  # welcome
            self.nick = params[0]
            log.info("registered as %s", self.nick)
            for chan in CHANNELS:
                self.raw(f"JOIN {chan}")

        elif command == "JOIN":
            if prefix.split("!")[0].lower() == self.nick.lower():
                log.info("joined %s", params[0] if params else "?")

        elif command in ("471", "473", "474", "475", "477"):
            log.error("cannot join channel: %s", " ".join(params[1:]))

        elif command == "KICK" and len(params) >= 2 and params[1].lower() == self.nick.lower():
            log.warning("kicked from %s; rejoining in 30s", params[0])
            await asyncio.sleep(30)
            self.raw(f"JOIN {params[0]}")

        elif command == "PRIVMSG" and len(params) >= 2:
            self.on_privmsg(prefix.split("!")[0], params[0], params[1])

    def on_privmsg(self, sender, target, text):
        sender, text = unrelay(sender, text)
        if not target.startswith(("#", "&")):
            return  # ignore private messages
        if sender.lower() in RIVAL_NICKS:
            mono = time.monotonic()
            self.rival_lines = [r for r in self.rival_lines if mono - r[0] < 2 * RIVAL_WAIT]
            self.rival_lines.append((mono, target.lower(), sender.lower(), _FORMATTING_RE.sub("", text)))
        if sender.lower() in IGNORE_NICKS or sender.lower() == self.nick.lower():
            return
        # Anyone can ask for full previews of their links by mentioning TRIGGER.
        full = sender.lower() in WATCH_NICKS or TRIGGER in text.casefold()
        if text.startswith("\x01") and not text.startswith("\x01ACTION "):
            return  # CTCP other than /me
        urls = previews.extract_urls(text)[:MAX_URLS_PER_MESSAGE]
        now = time.time()
        self.seen = {k: v for k, v in self.seen.items() if now - v < DUPLICATE_WINDOW}
        for url in urls:
            key = (target.lower(), url)
            if key in self.seen:
                continue
            self.seen[key] = now
            is_tweet = previews.TWEET_RE.match(url)
            if is_tweet:
                # Everyone's tweets get a no-login page, built in parallel so
                # it never delays the preview. Its link isn't posted.
                asyncio.create_task(self.build_tweet_page(url))
            if full:
                asyncio.create_task(self.post_preview(target, url, sender, full))
            elif not is_tweet and sender.lower() not in RIVAL_NICKS:
                asyncio.create_task(self.post_if_better(target, url, time.monotonic()))

    async def post_if_better(self, target, url, since):
        """For someone else's non-tweet link: if it's a JavaScript-only page
        that we can render, post our preview unless one of the other preview
        bots already showed the real headline."""
        try:
            result = await asyncio.to_thread(previews.rendered_preview, url)
        except previews.PreviewError:
            return
        except Exception:
            log.exception("rendered preview crashed for %s", url)
            return
        if not result:
            return
        text, headline = result
        chan = target.lower()

        def replies():
            return [(n, t) for (m, c, n, t) in self.rival_lines if m >= since and c == chan]

        while time.monotonic() < since + RIVAL_WAIT and {n for n, _ in replies()} < RIVAL_NICKS:
            await asyncio.sleep(0.5)
        key = _normalize(headline)
        if any(key in _normalize(t) for _, t in replies()):
            log.info("not posting %s: another bot already showed %r", url, headline)
            return
        log.info("better preview %s -> %s", url, text)
        self.say(target, text)

    async def build_tweet_page(self, url):
        try:
            page = await asyncio.to_thread(previews.build_tweet_page, url)
        except Exception:
            log.exception("building tweet page failed for %s", url)
            return
        if page:
            log.info("built %s -> %s", url, page)

    async def post_preview(self, target, url, sender, full):
        # Post each step as soon as it's ready.
        for step in previews.preview_steps(url, sender, full):
            try:
                text = await asyncio.to_thread(step)
            except previews.PreviewError as e:
                log.info("no preview for %s: %s", url, e)
                return
            except Exception:
                log.exception("preview crashed for %s", url)
                return
            for line in text.splitlines():
                if line.strip():
                    log.info("preview %s -> %s", url, line)
                    self.say(target, line)


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(Bot().run_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
