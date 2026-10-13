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

_URL_RE = re.compile(r"https?://[^\s<>\"']+")

CSS = """
body { margin: 0; background: #15202b; color: #e7e9ea;
       font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 600px; margin: 0 auto; padding: 16px; }
header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
header img { width: 48px; height: 48px; border-radius: 50%; }
header .name { font-weight: 700; }
a { color: #1d9bf0; text-decoration: none; }
a:hover { text-decoration: underline; }
.muted, .muted a { color: #8b98a5; }
article { border-top: 1px solid #38444d; padding: 14px 0; }
article.target { background: #1e2732; margin: 0 -16px; padding: 14px 16px; border-left: 3px solid #1d9bf0; }
.text { white-space: pre-wrap; word-wrap: break-word; }
.media img, .media video { display: block; max-width: 100%; border-radius: 12px; margin-top: 10px; }
blockquote { margin: 10px 0 0; padding: 10px 12px; border: 1px solid #38444d; border-radius: 12px; }
.meta { margin-top: 8px; font-size: 14px; }
footer { border-top: 1px solid #38444d; padding-top: 12px; font-size: 13px; }
"""


def enabled():
    return bool(THREAD_DIR and THREAD_URL)


def _e(text):
    return html.escape(text or "", quote=True)


def _linkify(text):
    out, pos = [], 0
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?)")
        out.append(_e(text[pos : m.start()]))
        out.append(f'<a href="{_e(url)}" rel="nofollow noopener">{_e(url)}</a>')
        pos = m.start() + len(url)
    out.append(_e(text[pos:]))
    return "".join(out)


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
        f'<div class="muted"><b>{_e(a.get("name"))}</b> @{_e(a.get("screen_name"))} · '
        f'<a href="{_e(_safe_url(q.get("url")))}">{_e(_date(q))}</a></div>'
        f'<div class="text">{_linkify(q.get("text") or "")}</div>{_media(q)}'
        "</blockquote>"
    )


def _article(status, target_id):
    if status.get("type") == "tombstone" or "id" not in status:
        return f'<article class="muted">{_e(status.get("message") or "This post is unavailable")}</article>'
    sid = _e(status["id"])
    cls = ' class="target"' if status["id"] == target_id else ""
    return (
        f'<article id="{sid}"{cls}>'
        f'<div class="text">{_linkify(status.get("text") or "")}</div>'
        f"{_media(status)}{_quote(status)}"
        f'<div class="meta muted"><a href="{_e(_safe_url(status.get("url")))}">{_e(_date(status))}</a>'
        f' · {status.get("likes", 0):,} likes · {status.get("reposts", 0):,} reposts</div>'
        "</article>"
    )


def render(data, target_id):
    """Return (html, number_of_posts, first_post_id) for a /2/thread response."""
    posts = data.get("thread") or [data["status"]]
    real = [p for p in posts if p.get("type") != "tombstone" and "id" in p]
    first = real[0] if real else data["status"]
    author = first.get("author") or data.get("author") or {}
    name, handle = author.get("name") or "?", author.get("screen_name") or "?"
    title = f"Thread by {name} (@{handle}), {len(real)} posts"
    desc = " ".join((first.get("text") or "").split())[:300]
    avatar = _safe_url(author.get("avatar_url"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(desc)}">
{f'<meta property="og:image" content="{_e(avatar)}">' if avatar else ""}
<style>{CSS}</style>
</head>
<body><main>
<header>
{f'<img src="{_e(avatar)}" alt="">' if avatar else ""}
<div><div class="name">{_e(name)}</div><div class="muted">@{_e(handle)} · {len(real)} posts</div></div>
</header>
{"".join(_article(p, target_id) for p in posts)}
<footer class="muted">Copied from <a href="{_e(_safe_url(first.get("url")))}">x.com</a>
via <a href="https://github.com/FxEmbed/FxEmbed">FxTwitter</a> at {now}.
Made by <a href="https://github.com/shawwn/lesswrong-bot">lesswrong-bot</a>.</footer>
</main></body>
</html>
"""
    return page, len(real), first.get("id") or target_id


def publish(data, target_id):
    """Write the thread page and return its URL (anchored at target_id), or None
    if the tweet isn't part of a multi-post thread."""
    page, count, first_id = render(data, target_id)
    if count < 2:
        return None, count
    if not re.fullmatch(r"\d+", str(first_id)):
        first_id = target_id  # never build a path from unexpected API data
    os.makedirs(THREAD_DIR, exist_ok=True)
    name = f"{first_id}.html"
    fd, tmp = tempfile.mkstemp(dir=THREAD_DIR, prefix=".tmp-", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(page)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(THREAD_DIR, name))
    except BaseException:
        os.unlink(tmp)
        raise
    return f"{THREAD_URL}/{name}#{target_id}", count
