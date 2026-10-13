"""Link preview generation. Stdlib only.

Each preview is a single line of plain text suitable for an IRC PRIVMSG.
"""

import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (compatible; lesswrong-linkbot/1.0; +https://libera.chat)"
TIMEOUT = 10
MAX_BYTES = 1024 * 1024  # never read more than 1 MiB of any response

URL_RE = re.compile(r"https?://[^\s<>\"'\x00-\x1f]+", re.IGNORECASE)
TWEET_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|"
    r"fixupx\.com|xcancel\.com|nitter\.net)/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)",
    re.IGNORECASE,
)
YOUTUBE_RE = re.compile(
    r"^https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch|shorts/|live/)|youtu\.be/)",
    re.IGNORECASE,
)
XKCD_RE = re.compile(r"^https?://(?:www\.|m\.)?xkcd\.com(?:/(\d+))?/?(?:[?#].*)?$", re.IGNORECASE)
WIKIPEDIA_RE = re.compile(r"^https?://([a-z\-]+)\.(?:m\.)?wikipedia\.org/wiki/([^?#]+)", re.IGNORECASE)


class PreviewError(Exception):
    pass


# --------------------------------------------------------------------------- #
# URL extraction
# --------------------------------------------------------------------------- #

def extract_urls(text):
    urls = []
    for m in URL_RE.finditer(text):
        url = m.group(0)
        # Trim trailing punctuation, but keep a closing paren if it is balanced
        # (e.g. Wikipedia links like .../Flint_(rock)).
        while url and url[-1] in ".,;:!?'\"]>}":
            url = url[:-1]
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        if url and url not in urls:
            urls.append(url)
    return urls


# --------------------------------------------------------------------------- #
# Safe HTTP fetching (refuses to talk to private / loopback addresses)
# --------------------------------------------------------------------------- #

def _check_host(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PreviewError(f"refusing URL {url!r}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise PreviewError(f"DNS failure for {parts.hostname}: {e}") from e
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not addr.is_global:
            raise PreviewError(f"refusing non-public address {addr} for {parts.hostname}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirectHandler)


def fetch(url, accept="text/html,application/xhtml+xml,*/*;q=0.8"):
    """Return (final_url, content_type, charset, body_bytes)."""
    _check_host(url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "en-US,en;q=0.8"},
    )
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            ctype = resp.headers.get_content_type()
            charset = resp.headers.get_content_charset()
            length = resp.headers.get("Content-Length")
            if ctype.startswith(("text/", "application/json")) or "html" in ctype or "xml" in ctype:
                body = resp.read(MAX_BYTES)
            else:
                body = b""
            return resp.geturl(), ctype, charset, body, length
    except urllib.error.HTTPError as e:
        raise PreviewError(f"HTTP {e.code} for {url}") from e
    except (urllib.error.URLError, OSError) as e:
        raise PreviewError(f"fetch failed for {url}: {e}") from e


def fetch_json(url):
    _, _, charset, body, _ = fetch(url, accept="application/json")
    try:
        return json.loads(body.decode(charset or "utf-8", "replace"))
    except ValueError as e:
        raise PreviewError(f"bad JSON from {url}") from e


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

def clean(text):
    """Collapse whitespace and strip control characters."""
    text = html.unescape(text or "")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def slashify(text):
    """Join lines with ' / ', the way Robomot renders multi-line tweets."""
    lines = [clean(line) for line in (text or "").splitlines()]
    return " / ".join(line for line in lines if line)


def truncate_bytes(text, limit):
    """Truncate text to at most limit UTF-8 bytes, ending with an ellipsis."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    cut = data[: max(limit - 3, 0)].decode("utf-8", "ignore")
    return cut.rstrip() + "…"


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# --------------------------------------------------------------------------- #
# Twitter / X  (via the public FxTwitter API)
# --------------------------------------------------------------------------- #

TWEET_BUDGET = 400  # bytes per IRC line
MIN_TWEET_TEXT = 150  # below this, media links move to their own line


def _media_links(t):
    """Direct URLs to a tweet's photos, videos, and GIFs."""
    media = t.get("media") or {}
    return [m["url"] for m in media.get("all") or [] if m.get("url")]


def _format_tweet(t, budget):
    author = t.get("author") or {}
    name = clean(author.get("name")) or "?"
    handle = author.get("screen_name") or "?"
    ts = t.get("created_timestamp")
    date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "?"
    text = slashify(t.get("text") or "")
    suffix = " [poll]" if t.get("poll") else ""

    head = f"{name} (@{handle}, {date}): "
    text = truncate_bytes(text, budget - len((head + '""' + suffix).encode("utf-8")))
    return f'{head}"{text}"{suffix}'


def _pack_lines(words, limit=TWEET_BUDGET):
    lines = []
    for w in words:
        if lines and len((lines[-1] + " " + w).encode("utf-8")) <= limit:
            lines[-1] += " " + w
        else:
            lines.append(w)
    return lines


def preview_tweet(url):
    m = TWEET_RE.match(url)
    user, status_id = m.group(1), m.group(2)
    # Twitter's own embed page renders the tweet (with media) without a login,
    # unlike x.com. Nitter/xcancel instances come and go; this doesn't.
    text = _tweet_text(user, status_id)
    return f"No-login link: https://platform.twitter.com/embed/Tweet.html?id={status_id}\n{text}"


def _tweet_text(user, status_id):
    data = fetch_json(f"https://api.fxtwitter.com/{user}/status/{status_id}")
    tweet = data.get("tweet")
    if not tweet:
        raise PreviewError(f"fxtwitter: {data.get('message') or 'no tweet'}")
    quote = tweet.get("quote")
    links = _media_links(tweet) + (_media_links(quote) if quote else [])

    # Append media links to the tweet line if that still leaves room for a
    # reasonable amount of text; otherwise put them on following lines.
    link_str = "".join(" " + link for link in links)
    budget = TWEET_BUDGET - len(link_str.encode("utf-8"))
    if budget >= MIN_TWEET_TEXT:
        extra_lines = []
    else:
        budget, link_str = TWEET_BUDGET, ""
        extra_lines = _pack_lines(links)

    if quote:
        sep = " | Quoting "
        budget -= len(sep)
        line = _format_tweet(tweet, budget * 4 // 7) + sep + _format_tweet(quote, budget * 3 // 7)
    else:
        line = _format_tweet(tweet, budget)
    return "\n".join([line + link_str] + extra_lines)


# --------------------------------------------------------------------------- #
# YouTube (oEmbed)
# --------------------------------------------------------------------------- #

def preview_youtube(url):
    data = fetch_json("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe=""))
    title = clean(data.get("title"))
    author = clean(data.get("author_name"))
    if not title:
        raise PreviewError("youtube: no title")
    return f"YouTube: {title}" + (f" (by {author})" if author else "")


# --------------------------------------------------------------------------- #
# xkcd (JSON API). The bare image URL is included so IRCCloud inlines it.
# --------------------------------------------------------------------------- #

def preview_xkcd(url):
    num = XKCD_RE.match(url).group(1)
    data = fetch_json(f"https://xkcd.com/{num}/info.0.json" if num else "https://xkcd.com/info.0.json")
    title = clean(data.get("safe_title") or data.get("title"))
    try:
        date = f"{int(data['year']):04d}-{int(data['month']):02d}-{int(data['day']):02d}"
    except (KeyError, ValueError):
        date = "?"
    line = f"xkcd #{data.get('num')}: {title} ({date})"
    img = data.get("img") or ""
    # Interactive comics have no real image (img points at a directory).
    if re.search(r"\.(?:png|jpe?g|gif|webp)$", img, re.IGNORECASE):
        line += " " + img
    alt = clean(data.get("alt"))
    if alt:
        head = line + ' | Alt: "'
        line = head + truncate_bytes(alt, TWEET_BUDGET - len(head.encode("utf-8")) - 1) + '"'
    return line


# --------------------------------------------------------------------------- #
# Wikipedia (REST summary API)
# --------------------------------------------------------------------------- #

def preview_wikipedia(url):
    m = WIKIPEDIA_RE.match(url)
    lang, title = m.group(1).lower(), m.group(2)
    data = fetch_json(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}")
    extract = clean(data.get("extract"))
    if not extract:
        raise PreviewError("wikipedia: no extract")
    return f"{clean(data.get('title'))}: {extract}"


# --------------------------------------------------------------------------- #
# Generic HTML pages (OpenGraph / Twitter card / <title>)
# --------------------------------------------------------------------------- #

class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.title = ""
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
            if a.get("charset"):
                self.meta.setdefault("@charset", a["charset"])
        elif tag == "title" and not self.title:
            self._in_title = True
        elif tag == "body":
            self.done = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _decode(body, charset):
    if not charset:
        m = re.search(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_\-]+)""", body[:4096], re.IGNORECASE)
        charset = m.group(1).decode("ascii") if m else "utf-8"
    try:
        return body.decode(charset, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def preview_generic(url):
    final_url, ctype, charset, body, length = fetch(url)
    if "html" not in ctype:
        size = f", {human_size(length)}" if length and length.isdigit() else ""
        return f"[{ctype}{size}]"

    parser = _MetaParser()
    try:
        parser.feed(_decode(body, charset))
    except Exception:
        pass
    meta = parser.meta

    title = clean(meta.get("og:title") or meta.get("twitter:title") or parser.title)
    desc = clean(meta.get("og:description") or meta.get("twitter:description") or meta.get("description"))
    site = clean(meta.get("og:site_name"))

    if not title and not desc:
        raise PreviewError("no title or description")
    if desc and title and desc.startswith(title):
        title = ""
    if site and title and site.lower() not in title.lower():
        title = f"{title} ({site})"
    if title and desc:
        return f"{title}: {desc}"
    return title or desc


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

def preview(url):
    """Return a preview for url (one or more lines), or raise PreviewError."""
    if TWEET_RE.match(url):
        return preview_tweet(url)
    if YOUTUBE_RE.match(url):
        return preview_youtube(url)
    if XKCD_RE.match(url):
        try:
            return preview_xkcd(url)
        except PreviewError:
            pass
    if WIKIPEDIA_RE.match(url):
        try:
            return preview_wikipedia(url)
        except PreviewError:
            pass
    return preview_generic(url)


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        try:
            print(preview(arg))
        except PreviewError as e:
            print(f"error: {e}")
