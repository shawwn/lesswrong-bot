#!/usr/bin/env python3
"""IRC bot that posts link previews for messages from selected nicks.

Configuration is via environment variables (see README.md). Stdlib only.
"""

import asyncio
import base64
import logging
import os
import ssl
import time

import previews

log = logging.getLogger("linkbot")

SERVER = os.environ.get("IRC_SERVER", "irc.libera.chat")
PORT = int(os.environ.get("IRC_PORT", "6697"))
USE_TLS = os.environ.get("IRC_TLS", "1") != "0"
NICK = os.environ.get("IRC_NICK", "shawbot")
REALNAME = os.environ.get("IRC_REALNAME", "link preview bot")
SASL_USER = os.environ.get("IRC_SASL_USER", "")
SASL_PASS = os.environ.get("IRC_SASL_PASS", "")
CHANNELS = [c.strip() for c in os.environ.get("IRC_CHANNELS", "#lesswrong").split(",") if c.strip()]
WATCH_NICKS = {n.strip().lower() for n in os.environ.get("WATCH_NICKS", "shawwwn").split(",") if n.strip()}
MAX_URLS_PER_MESSAGE = int(os.environ.get("MAX_URLS_PER_MESSAGE", "3"))
DUPLICATE_WINDOW = 600  # seconds; don't re-preview the same URL in the same channel

# A PRIVMSG line is capped at 512 bytes including our own ":nick!user@host"
# prefix as relayed to others; leave generous room for it.
MAX_PAYLOAD_BYTES = 400


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
        self.send_queue = asyncio.Queue()

    # ------------------------------------------------------------------ io --

    def raw(self, line):
        log.debug(">> %s", line)
        self.writer.write((line + "\r\n").encode("utf-8"))

    async def sender(self):
        """Drain the outgoing PRIVMSG queue at a flood-safe rate."""
        while True:
            line = await self.send_queue.get()
            self.raw(line)
            await self.writer.drain()
            await asyncio.sleep(1.0)

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

    async def run_forever(self):
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
        if sender.lower() not in WATCH_NICKS:
            return
        if not target.startswith(("#", "&")):
            return  # ignore private messages
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
            asyncio.create_task(self.post_preview(target, url, sender))

    async def post_preview(self, target, url, sender):
        try:
            text = await asyncio.to_thread(previews.preview, url, sender)
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
