"""Render a page in headless Chrome and return its DOM after JavaScript runs.

Used for single-page apps whose HTML has no real title or description. The
browser is CHROME (e.g. chrome-headless-shell); rendering is disabled if
it's unset.

All browser traffic goes through a small local proxy that refuses private,
loopback, and link-local destinations. Otherwise a page's JavaScript could
fetch internal services (e.g. localhost:8080) and put the response in its
title, which the bot would then post to IRC. Stdlib only.
"""

import ipaddress
import os
import selectors
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import urllib.parse

CHROME = os.environ.get("CHROME", "")
RENDER_TIMEOUT = 25  # seconds, whole browser run
VIRTUAL_TIME_BUDGET_MS = 5000  # how long the page's JavaScript gets
MAX_DOM_BYTES = 4 * 1024 * 1024
MAX_CONCURRENT = 2

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


class RenderError(Exception):
    pass


def enabled():
    return bool(CHROME) and os.access(CHROME, os.X_OK)


# --------------------------------------------------------------------------- #
# Filtering proxy
# --------------------------------------------------------------------------- #

def _public_addr(host, port):
    """Resolve host and return a public (address, port) to connect to, or None
    if any of its addresses isn't public. Connecting to the address we checked
    (not re-resolving) avoids DNS rebinding."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return None
    if not infos:
        return None
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            return None
    return infos[0][4][:2]


class _ProxyHandler(socketserver.StreamRequestHandler):
    timeout = 20

    def _deny(self, why):
        self.wfile.write(f"HTTP/1.1 403 Forbidden\r\nContent-Length: {len(why)}\r\n\r\n{why}".encode())

    def handle(self):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self.request.recv(65536)
                if not chunk or len(head) > 65536:
                    return
                head += chunk
            header, _, rest = head.partition(b"\r\n\r\n")
            lines = header.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)

            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                host, port = host.strip("[]"), int(port)
                first = b""
            else:
                u = urllib.parse.urlsplit(target)
                if u.scheme != "http" or not u.hostname:
                    return self._deny("bad request")
                host, port = u.hostname, u.port or 80
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                kept = [l for l in lines[1:] if not l.lower().startswith(("proxy-", "connection:"))]
                first = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""]).encode("latin-1") + rest

            addr = _public_addr(host, port)
            if not addr:
                return self._deny(f"refusing non-public destination {host}")
            upstream = socket.create_connection(addr, timeout=10)
        except (OSError, ValueError):
            return

        with upstream:
            if method == "CONNECT":
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                upstream.sendall(first)
            self._pipe(self.request, upstream)

    @staticmethod
    def _pipe(a, b):
        sel = selectors.DefaultSelector()
        sel.register(a, selectors.EVENT_READ, b)
        sel.register(b, selectors.EVENT_READ, a)
        try:
            while True:
                events = sel.select(timeout=20)
                if not events:
                    return
                for key, _ in events:
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    key.data.sendall(data)
        except OSError:
            return
        finally:
            sel.close()


class _Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render(url):
    """Return the rendered DOM of url as HTML text, or raise RenderError."""
    if not enabled():
        raise RenderError("no browser configured (CHROME)")
    if not _slots.acquire(timeout=RENDER_TIMEOUT):
        raise RenderError("too many renders in progress")
    proxy = _Proxy(("127.0.0.1", 0), _ProxyHandler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="lwbot-chrome-") as profile:
            cmd = [
                CHROME,
                "--headless",
                "--dump-dom",
                f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
                f"--user-data-dir={profile}",
                f"--proxy-server=http://127.0.0.1:{proxy.server_address[1]}",
                "--proxy-bypass-list=<-loopback>",  # send even localhost through the proxy
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--disable-quic",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--no-first-run",
                "--mute-audio",
                "--blink-settings=imagesEnabled=false",
                url,
            ]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True
            )
            try:
                out, _ = proc.communicate(timeout=RENDER_TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise RenderError(f"render timed out after {RENDER_TIMEOUT}s")
            if proc.returncode != 0:
                raise RenderError(f"browser exited with {proc.returncode}")
            return out[:MAX_DOM_BYTES].decode("utf-8", "replace")
    finally:
        proxy.shutdown()
        proxy.server_close()
        _slots.release()
