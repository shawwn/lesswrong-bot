"""Link preview generation. Stdlib only.

Each preview is a single line of plain text suitable for an IRC PRIVMSG.
"""

import html
import ipaddress
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

import browser
import threads

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
# github.com/<owner>/<repo>[/issues|pull/<n>]. Owner names that are GitHub's
# own pages (github.com/settings/..., /orgs/..., ...) aren't repos.
GITHUB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?!(?:orgs|settings|features|topics|collections|marketplace|"
    r"sponsors|login|join|about|pricing|explore|trending|search|notifications|new|apps|enterprise|"
    r"security|customer-stories|readme|site|team|codespaces|copilot|issues|pulls|events)/)"
    r"([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?"
    r"(?:/(issues|pull)/(\d+))?/?(?:[?#].*)?$",
    re.IGNORECASE,
)
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


def fetch(url, accept="text/html,application/xhtml+xml,*/*;q=0.8", headers=None):
    """Return (final_url, content_type, charset, body_bytes)."""
    _check_host(url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "en-US,en;q=0.8", **(headers or {})},
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


def fetch_json(url, accept="application/json", headers=None):
    _, _, charset, body, _ = fetch(url, accept=accept, headers=headers)
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


def tweet_steps(url, nick=None, full=True):
    """Preview a tweet as a list of steps (zero-argument functions, each
    returning text), so the bot can post each one as soon as it's ready.

    Full: the tweet text first (one fast request), then the no-login link,
    which may need two more requests to build the thread page. Link-only:
    just the link, after checking the tweet exists."""
    m = TWEET_RE.match(url)
    user, status_id = m.group(1), m.group(2)

    def link():
        # Like Robomot's "Alternate version of <nick>'s link: ...". Twitter's
        # own embed page shows the tweet without a login, unlike x.com.
        url = _thread_link(status_id) or f"https://platform.twitter.com/embed/Tweet.html?id={status_id}"
        whose = f"{nick}'s" if nick else "this"
        return f"No-login version of {whose} link: {url}"

    def text():
        return _tweet_text(user, status_id)

    if full:
        return [text, link]
    return [lambda: (text(), link())[1]]


def _thread_link(status_id):
    """Publish the thread and replies as a page (see threads.py), if there are any."""
    if not threads.enabled():
        return None
    try:
        thread = fetch_json(f"https://api.fxtwitter.com/2/thread/{status_id}")
        try:
            conv = fetch_json(f"https://api.fxtwitter.com/2/conversation/{status_id}")
        except PreviewError:
            conv = None  # still worth publishing the thread alone
        url, _, _ = threads.publish(thread, status_id, conv)
    except Exception:
        # The page is a bonus; on any failure fall back to the embed link.
        logging.getLogger(__name__).exception("thread page failed for %s", status_id)
        return None
    return url


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
# GitHub (REST API). Unauthenticated, the API allows 60 requests/hour per IP;
# a repo preview uses 3. Set GITHUB_TOKEN (any token, no scopes needed) for
# 5000/hour.
# --------------------------------------------------------------------------- #

def _github_api(path, accept="application/vnd.github+json"):
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/{path}"
    if accept.endswith("json"):
        return fetch_json(url, accept=accept, headers=headers)
    _, _, charset, body, _ = fetch(url, accept=accept, headers=headers)
    return body.decode(charset or "utf-8", "replace")


def _count(n):
    """63235 -> '63.2k'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def _ago(iso):
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return ""
    days = (datetime.now(timezone.utc) - then).days
    if days < 1:
        return "today"
    for size, unit in ((365, "year"), (30, "month"), (1, "day")):
        if days >= size:
            n = days // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"


class _FirstParagraphParser(HTMLParser):
    """Text of the first <p> with real prose (skips logos and badge rows)."""

    MIN_LENGTH = 60

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self._buf = None

    def handle_starttag(self, tag, attrs):
        if tag == "p" and not self.text:
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "p" and self._buf is not None:
            text = clean("".join(self._buf))
            if len(text) >= self.MIN_LENGTH:
                self.text = text
            self._buf = None

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)


def _github_repo(owner, repo):
    r = _github_api(f"repos/{owner}/{repo}")
    stats = [f"★ {_count(r.get('stargazers_count', 0))}", f"{_count(r.get('forks_count', 0))} forks"]
    try:
        langs = _github_api(f"repos/{owner}/{repo}/languages")
        total = sum(langs.values())
        top = [(name, round(100 * size / total)) for name, size in sorted(langs.items(), key=lambda kv: -kv[1])[:3]] if total else []
        if top:
            stats.append(", ".join(f"{name} {pct}%" for name, pct in top if pct))
    except (PreviewError, AttributeError, TypeError):
        pass
    license_id = (r.get("license") or {}).get("spdx_id")
    if license_id and license_id != "NOASSERTION":
        stats.append(license_id)
    if r.get("pushed_at"):
        stats.append(f"updated {_ago(r['pushed_at'])}")
    if r.get("archived"):
        stats.append("archived")
    if r.get("fork") and (r.get("parent") or {}).get("full_name"):
        stats.append(f"fork of {r['parent']['full_name']}")
    tail = " | " + " · ".join(stats)

    about = clean(r.get("description"))
    try:
        parser = _FirstParagraphParser()
        parser.feed(_github_api(f"repos/{owner}/{repo}/readme", accept="application/vnd.github.html"))
        intro = parser.text
    except PreviewError:
        intro = ""
    if intro and not _same_title(intro, about):
        about = f"{about} — {intro}" if about else intro
    head = f"{r.get('full_name') or f'{owner}/{repo}'}" + (f": {about}" if about else "")
    return truncate_bytes(head, TWEET_BUDGET - len(tail.encode("utf-8"))) + tail


def _github_issue(owner, repo, number):
    i = _github_api(f"repos/{owner}/{repo}/issues/{number}")
    pr = i.get("pull_request")
    if pr:
        kind, state = "PR", "merged" if pr.get("merged_at") else i.get("state", "?")
    else:
        kind = "issue"
        state = "closed as not planned" if i.get("state_reason") == "not_planned" else i.get("state", "?")
    date = (i.get("created_at") or "")[:10]
    comments = i.get("comments", 0)
    who = (i.get("user") or {}).get("login", "?")
    meta = f" ({kind} by @{who}, {state}, {date}, {comments} comment{'s' if comments != 1 else ''})"
    head = f"{owner}/{repo}#{number}: {clean(i.get('title'))}{meta}"
    body = clean(re.sub(r"<!--.*?-->", "", i.get("body") or "", flags=re.S))
    return truncate_bytes(f"{head}: {body}" if body else head, TWEET_BUDGET)


def preview_github(url):
    owner, repo, kind, number = GITHUB_RE.match(url).groups()
    if number:
        return _github_issue(owner, repo, number)
    return _github_repo(owner, repo)


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
    """<title>, <meta>, and canonical link, first occurrence of each. Reads the
    whole document, not just <head>: some sites (e.g. LessWrong) put their
    title and og: tags far down in the <body>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.title = ""
        self._in_title = False
        self._svg = 0  # depth inside <svg>, whose <title>s are icon labels

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "svg":
            self._svg += 1
        elif tag == "meta":
            # Not itemprop: microdata in the body (e.g. comments) has its own
            # "description"s, which aren't the page's.
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
            if a.get("charset"):
                self.meta.setdefault("@charset", a["charset"])
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.meta.setdefault("@canonical", a.get("href", ""))
        elif tag == "title" and not self.title and not self._svg:
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "svg":
            self._svg = max(0, self._svg - 1)

    def handle_data(self, data):
        if self._in_title:
            self.title += data


class _HeadlineParser(HTMLParser):
    """The first <h1>'s text and the first substantial <p> after it."""

    MIN_PARAGRAPH = 60

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.h1 = ""
        self.paragraph = ""
        self._in = None  # "h1" or "p" while collecting
        self._buf = []
        self._skip = 0  # depth inside <script>/<style>

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "h1" and not self.h1 and self._in is None:
            self._in, self._buf = "h1", []
        elif tag == "p" and self.h1 and not self.paragraph and self._in is None:
            self._in, self._buf = "p", []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag == self._in:
            text = clean("".join(self._buf))
            if self._in == "h1":
                self.h1 = text
            elif len(text) >= self.MIN_PARAGRAPH:
                self.paragraph = text
            self._in = None

    def handle_data(self, data):
        if self._in and not self._skip:
            self._buf.append(data)


def _sitewide_metadata(url, meta):
    """True if the page's og:url/canonical is its site's homepage, but url is
    some deeper page, so the title/description describe the site, not the page
    (common for single-page apps)."""
    declared = meta.get("og:url") or meta.get("@canonical")
    if not declared:
        return False
    declared = urllib.parse.urljoin(url, declared)
    path = urllib.parse.urlsplit(url).path.strip("/")
    return bool(path) and urllib.parse.urlsplit(declared).path.strip("/") == ""


def _rendered_preview(url, generic_title, site):
    """Render url in headless Chrome (see browser.py) and preview what the
    page shows, as (text, headline). None if rendering isn't available or
    doesn't help."""
    if not browser.enabled():
        return None
    try:
        dom = browser.render(url)
    except browser.RenderError:
        return None
    meta_parser, headline = _MetaParser(), _HeadlineParser()
    for parser in (meta_parser, headline):
        try:
            parser.feed(dom)
        except Exception:
            pass
    meta = meta_parser.meta
    title = clean(meta.get("og:title") or meta.get("twitter:title") or meta_parser.title)
    if title and not _sitewide_metadata(url, meta) and title != generic_title:
        # The app filled in page-specific metadata once its JavaScript ran.
        # (The <h1> is only a fallback: on ordinary pages it's often a vote
        # count, breadcrumb, or the site's name.)
        desc = clean(meta.get("og:description") or meta.get("twitter:description") or meta.get("description"))
    elif headline.h1:
        title, desc = headline.h1, headline.paragraph
    else:
        return None
    headline = title
    if site and site.lower() not in title.lower():
        title = f"{title} ({site})"
    return (f"{title}: {desc}" if desc else title), headline


def _decode(body, charset):
    if not charset:
        m = re.search(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_\-]+)""", body[:4096], re.IGNORECASE)
        charset = m.group(1).decode("ascii") if m else "utf-8"
    try:
        return body.decode(charset, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def _static_metadata(url):
    """Fetch url and return (final_url, title, desc, site, needs_render).
    Raises PreviewError for non-HTML (images, PDFs, ...): a bare
    "[image/png, 770.9 KB]" isn't worth a message, and IRCCloud shows images
    inline anyway."""
    final_url, ctype, charset, body, _ = fetch(url)
    if "html" not in ctype:
        raise PreviewError(f"not HTML ({ctype})")

    parser = _MetaParser()
    try:
        parser.feed(_decode(body, charset))
    except Exception:
        pass
    meta = parser.meta

    title = clean(meta.get("og:title") or meta.get("twitter:title") or parser.title)
    desc = clean(meta.get("og:description") or meta.get("twitter:description") or meta.get("description"))
    site = clean(meta.get("og:site_name"))
    needs_render = not (title or desc) or _sitewide_metadata(final_url, meta)
    return final_url, title, desc, site, needs_render


def rendered_preview(url):
    """For pages whose HTML only has site-wide (or no) metadata, which is what
    other preview bots will show: render the page and return (text, headline)
    of what it really says. None for ordinary pages, or if rendering fails.
    Used to decide whether to speak up for people other than WATCH_NICKS."""
    if TWEET_RE.match(url):
        return None
    final_url, title, _, site, needs_render = _static_metadata(url)
    if not needs_render:
        return None  # ordinary page: the other bots see the same metadata we do
    rendered = _rendered_preview(final_url, title, site)
    if not rendered or _same_title(rendered[1], title):
        return None  # rendering found nothing the other bots won't show
    return rendered


def _same_title(a, b):
    """Loosely the same: equal, or one contains the other, ignoring case and
    punctuation ("Flint" vs "Flint - Wikipedia")."""
    a, b = (" ".join(re.findall(r"\w+", s.casefold())) for s in (a or "", b or ""))
    return a == b or bool(a and b and (a in b or b in a))


def preview_generic(url):
    final_url, title, desc, site, needs_render = _static_metadata(url)

    # Only render pages whose HTML has no metadata, or only site-wide metadata.
    # Elsewhere rendering is slower and often worse (bot-check pages).
    if needs_render:
        rendered = _rendered_preview(final_url, title, site)
        if rendered:
            return rendered[0]

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

def preview_steps(url, nick=None, full=True):
    """Return a list of steps (zero-argument functions returning text, or
    raising PreviewError) that together make the preview for url. Run them in
    order and post each result; stop at the first error.

    nick is who posted the link, for wording like "<nick>'s link". With
    full=False, only tweets get anything: just the no-login link line."""
    if TWEET_RE.match(url):
        return tweet_steps(url, nick, full)
    return [lambda: _preview_other(url, full)]


def preview(url, nick=None, full=True):
    """The whole preview for url as one string (lines joined), or raise
    PreviewError."""
    return "\n".join(step() for step in preview_steps(url, nick, full))


def _preview_other(url, full):
    if not full:
        raise PreviewError("only tweets get link-only previews")
    if YOUTUBE_RE.match(url):
        return preview_youtube(url)
    if GITHUB_RE.match(url):
        try:
            return preview_github(url)
        except PreviewError:
            pass  # private repo, rate limit, ...: fall back to the page itself
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
