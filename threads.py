"""Render a tweet thread to a standalone HTML page that needs no login to view.

Input is the JSON from FxTwitter's /2/thread/<id> endpoint. Pages are written
to THREAD_DIR and served by an ordinary web server at THREAD_URL. Stdlib only.
"""

import html
import os
import re
import tempfile
from datetime import datetime, timezone

THREAD_DIR = os.environ.get("THREAD_DIR", "")
THREAD_URL = os.environ.get("THREAD_URL", "").rstrip("/")

# URLs, @mentions, and #hashtags (a hashtag needs at least one non-digit, so
# "#1" stays plain text).
_LINK_RE = re.compile(
    r"(?P<url>https?://[^\s<>\"']+)"
    r"|(?<![\w@/])@(?P<user>[A-Za-z0-9_]{1,15})\b"
    r"|(?<![\w&/#])#(?P<tag>\w*[^\W\d]\w*)"
)

CSS = """
body { margin: 0; background: #15202b; color: #e7e9ea;
       font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 600px; margin: 0 auto; padding: 16px; }
a { color: #1d9bf0; text-decoration: none; }
a:hover { text-decoration: underline; }
.muted, .muted a { color: #8b98a5; }
article { display: flex; gap: 12px; border-top: 1px solid #38444d; padding: 14px 0; }
main > article:first-child { border-top: none; }
article .body { flex: 1; min-width: 0; }
.avatar { flex: none; width: 40px; height: 40px; border-radius: 50%; background: #38444d; }
.qavatar { width: 20px; height: 20px; border-radius: 50%; vertical-align: -4px; margin-right: 6px; }
article.target { background: #1e2732; margin: 0 -16px; padding: 14px 16px; border-left: 3px solid #1d9bf0; }
.text { white-space: pre-wrap; word-wrap: break-word; }
.media img, .media video { display: block; max-width: 100%; border-radius: 12px; margin-top: 10px; }
blockquote { margin: 10px 0 0; padding: 10px 12px; border: 1px solid #38444d; border-radius: 12px; }
.meta { margin-top: 8px; font-size: 14px; }
h2 { font-size: 18px; margin: 18px 0 4px; }
.byline { margin-bottom: 2px; font-size: 15px; }
.byline b { color: #e7e9ea; }
a.who:hover { text-decoration: none; }
a.who:hover b { text-decoration: underline; }
.children { margin-left: 14px; padding-left: 12px; border-left: 2px solid #38444d; }
.children article:first-child { border-top: none; }
footer { border-top: 1px solid #38444d; padding-top: 12px; font-size: 13px; }
"""


def enabled():
    return bool(THREAD_DIR and THREAD_URL)


def _e(text):
    return html.escape(text or "", quote=True)


def _profile_url(handle):
    return f"https://x.com/{handle}"


def _linkify(text):
    out, pos = [], 0
    for m in _LINK_RE.finditer(text):
        if m.group("url"):
            url = m.group("url").rstrip(".,;:!?")
            while url.endswith(")") and url.count(")") > url.count("("):
                url = url[:-1]
            label = url
        elif m.group("user"):
            url, label = _profile_url(m.group("user")), "@" + m.group("user")
        else:
            url, label = f"https://x.com/hashtag/{m.group('tag')}", "#" + m.group("tag")
        out.append(_e(text[pos : m.start()]))
        out.append(f'<a href="{_e(url)}" rel="nofollow noopener">{_e(label)}</a>')
        pos = m.start() + len(label)
    out.append(_e(text[pos:]))
    return "".join(out)


def _who(author, avatar_cls=None):
    """Name and @handle, linked to the author's profile."""
    handle = (author or {}).get("screen_name") or ""
    avatar = _avatar(author, avatar_cls) if avatar_cls else ""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return f"{avatar}<b>{_e(author.get('name'))}</b>"
    return (
        f'<a class="who" href="{_e(_profile_url(handle))}">{avatar}'
        f'<b>{_e(author.get("name"))}</b> <span class="muted">@{_e(handle)}</span></a>'
    )


def _visible_text(status):
    """Tweet text as x.com shows it: without the leading @mentions that only
    mark who a reply is to (outside raw_text.display_text_range)."""
    text = status.get("text") or ""
    raw = status.get("raw_text") or {}
    try:
        start = int((raw.get("display_text_range") or [0])[0])
    except (TypeError, ValueError, IndexError):
        return text
    prefix = (raw.get("text") or "")[:start]
    if start and re.fullmatch(r"(?:@\w{1,15}\s+)+", prefix) and text.startswith(prefix):
        return text[len(prefix):]
    return text


def _date(status):
    ts = status.get("created_timestamp")
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _safe_url(url):
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


def _media(status):
    parts = []
    for m in (status.get("media") or {}).get("all") or []:
        url = _safe_url(m.get("url"))
        if not url:
            continue
        if m.get("type") == "photo":
            parts.append(f'<a href="{_e(url)}"><img src="{_e(url)}" loading="lazy" alt=""></a>')
        elif m.get("type") == "gif":
            parts.append(f'<video src="{_e(url)}" autoplay loop muted playsinline></video>')
        else:
            poster = _safe_url(m.get("thumbnail_url"))
            poster_attr = f' poster="{_e(poster)}"' if poster else ""
            parts.append(f'<video src="{_e(url)}"{poster_attr} controls preload="none"></video>')
    return f'<div class="media">{"".join(parts)}</div>' if parts else ""


def _quote(status):
    q = status.get("quote")
    if not q or q.get("type") == "tombstone":
        return ""
    a = q.get("author") or {}
    return (
        "<blockquote>"
        f'<div class="byline muted">{_who(a, "qavatar")} · '
        f'<a href="{_e(_safe_url(q.get("url")))}">{_e(_date(q))}</a></div>'
        f'<div class="text">{_linkify(_visible_text(q))}</div>{_media(q)}'
        "</blockquote>"
    )


def _is_real(status):
    return status.get("type") != "tombstone" and "id" in status


def _avatar(author, cls="avatar"):
    url = _safe_url((author or {}).get("avatar_url"))
    if not url:
        return f'<div class="{cls}"></div>'
    return f'<img class="{cls}" src="{_e(url)}" loading="lazy" alt="">'


def _profile_link(author, inner):
    handle = (author or {}).get("screen_name") or ""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return inner
    return f'<a href="{_e(_profile_url(handle))}">{inner}</a>'


def _article(status, target_id):
    if not _is_real(status):
        return f'<article class="muted">{_e(status.get("message") or "This post is unavailable")}</article>'
    sid = _e(status["id"])
    cls = ' class="target"' if status["id"] == target_id else ""
    a = status.get("author") or {}
    return (
        f'<article id="{sid}"{cls}>{_profile_link(a, _avatar(a))}<div class="body">'
        f'<div class="byline muted">{_who(a)} · '
        f'<a href="{_e(_safe_url(status.get("url")))}">{_e(_date(status))}</a></div>'
        f'<div class="text">{_linkify(_visible_text(status))}</div>'
        f"{_media(status)}{_quote(status)}"
        f'<div class="meta muted">{status.get("replies", 0):,} replies · '
        f'{status.get("reposts", 0):,} reposts · {status.get("likes", 0):,} likes</div>'
        "</div></article>"
    )


MAX_REPLY_DEPTH = 6


def _parent_id(status):
    r = status.get("replying_to")
    return r.get("status") if isinstance(r, dict) else None


def _replies(conv, target_id, exclude):
    """Render replies from a /2/conversation response as a nested tree.
    Returns (html, number_shown)."""
    replies = [r for r in conv.get("replies") or [] if _is_real(r) and r["id"] not in exclude]
    if not replies:
        return "", 0
    ids = {r["id"] for r in replies}
    children = {}
    for r in replies:
        parent = _parent_id(r)
        children.setdefault(parent if parent in ids else target_id, []).append(r)

    # Order like x.com: the original poster's own replies first, then replies
    # the OP responded to somewhere below, then the rest (in API order).
    op = ((conv.get("status") or {}).get("author") or {}).get("screen_name", "").lower()

    def by_op(r):
        return ((r.get("author") or {}).get("screen_name") or "").lower() == op

    has_op = {}

    def op_below(r):
        if r["id"] not in has_op:
            has_op[r["id"]] = False  # guards against cycles in bad data
            has_op[r["id"]] = any(by_op(c) or op_below(c) for c in children.get(r["id"], []))
        return has_op[r["id"]]

    def rank(r):
        return 0 if by_op(r) else 1 if op_below(r) else 2

    def tree(parent, depth):
        out = []
        for r in sorted(children.get(parent, []), key=rank):
            out.append(_article(r, target_id))
            if r["id"] in children:
                inner = tree(r["id"], depth + 1)
                out.append(inner if depth >= MAX_REPLY_DEPTH else f'<div class="children">{inner}</div>')
        return "".join(out)

    total = (conv.get("status") or {}).get("replies")
    url = _safe_url((conv.get("status") or {}).get("url"))
    more = ""
    if isinstance(total, int) and total > len(replies):
        more = f' <span class="muted">· {len(replies)} of {total:,}, <a href="{_e(url)}">more on x.com</a></span>'
    return f'<section id="replies"><h2>Replies{more}</h2>{tree(target_id, 0)}</section>', len(replies)


def render(data, target_id, conv=None):
    """Render a /2/thread response, plus replies to target_id from a
    /2/conversation response if given.
    Returns (html, number_of_posts, number_of_replies)."""
    posts = data.get("thread") or [data["status"]]
    real = [p for p in posts if _is_real(p)]
    first = real[0] if real else data["status"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    replies_html, n_replies = _replies(conv or {}, target_id, {p["id"] for p in real})
    # The author's thread continuation reads first (as on x.com), then replies.
    body = [_article(p, target_id) for p in posts] + [replies_html]

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- video.twimg.com returns 403 to requests with a foreign Referer. -->
<meta name="referrer" content="no-referrer">
<!-- No title or preview tags: the bot already posts the tweet text, and other link bots would repeat it. -->
<title></title>
<style>{CSS}</style>
</head>
<body><main>
{"".join(body)}
<footer class="muted">Copied from <a href="{_e(_safe_url(first.get("url")))}">x.com</a>
via <a href="https://github.com/FxEmbed/FxEmbed">FxTwitter</a> at {now}.
Made by <a href="https://github.com/shawwn/lesswrong-bot">lesswrong-bot</a>.</footer>
</main></body>
</html>
"""
    return page, len(real), n_replies


def publish(data, target_id, conv=None):
    """Write the page and return (url, posts, replies). url is None if there is
    nothing worth a page: a single post with no replies."""
    page, posts, replies = render(data, target_id, conv)
    if posts < 2 and not replies:
        return None, posts, replies
    # Named after the linked post, since the replies shown depend on it.
    # target_id comes from our own URL regex, so it is always digits.
    if not re.fullmatch(r"\d+", target_id):
        raise ValueError(f"bad status id {target_id!r}")
    os.makedirs(THREAD_DIR, exist_ok=True)
    name = f"{target_id}.html"
    fd, tmp = tempfile.mkstemp(dir=THREAD_DIR, prefix=".tmp-", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(page)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(THREAD_DIR, name))
    except BaseException:
        os.unlink(tmp)
        raise
    return f"{THREAD_URL}/{name}", posts, replies
