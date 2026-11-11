import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


JS_PAGE = b"""<html><head><title>JS page</title></head><body>
<div id="out">loading</div>
<script>
document.cookie = "jsid=42; path=/";
fetch("/echo?source=page").then(function (r) { return r.json(); }).then(function () {
  var p = document.createElement("p");
  p.id = "done";
  p.textContent = "rendered " + (40 + 2);
  document.body.appendChild(p);
});
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status, body=b"", headers=()):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self):
        state = self.server.state
        path = self.path.split("?")[0]
        state["hits"].append((self.command, path))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        if path == "/echo":
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
            self._reply(200, body, [("Content-Type", "application/json")])
        elif path == "/set":
            self._reply(200, b"ok", [("Set-Cookie", "sid=abc123; Path=/; Max-Age=3600")])
        elif path == "/flaky":
            state["flaky"] += 1
            if state["flaky"] < 3:
                self._reply(503, b"busy", [("Retry-After", "0")])
            else:
                self._reply(200, b"finally")
        elif path == "/cf":
            self._reply(403, b"<title>Just a moment...</title>",
                        [("cf-mitigated", "challenge"), ("Server", "cloudflare")])
        elif path == "/cf-clears":
            # Challenges the first visitor, then relents, so a solved session's
            # replay can be told apart from a lucky second guess.
            state["cf"] = state.get("cf", 0) + 1
            if state["cf"] < 2:
                self._reply(403, b"<title>Just a moment...</title>",
                            [("cf-mitigated", "challenge"), ("Server", "cloudflare")])
            else:
                self._reply(200, b"<html>served</html>", [("Content-Type", "text/html")])
        elif path == "/js":
            self._reply(200, JS_PAGE, [("Content-Type", "text/html")])
        elif path == "/robots.txt":
            if state["robots"] is None:
                self._reply(404)
            else:
                self._reply(200, state["robots"].encode(), [("Content-Type", "text/plain")])
        else:
            self._reply(200, b"<html>page</html>", [("Content-Type", "text/html")])

    do_GET = do_POST = do_HEAD = _handle

    def log_message(self, *args):
        pass


class _ProxyHandler(BaseHTTPRequestHandler):
    """Minimal forwarding proxy for plain-HTTP GETs."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        import base64
        import urllib.request

        auth = self.headers.get("Proxy-Authorization")
        user = None
        if auth and auth.lower().startswith("basic "):
            user = base64.b64decode(auth.split(None, 1)[1]).decode()
        self.server.seen.append({"url": self.path, "auth": user})
        skip = {"proxy-authorization", "proxy-connection", "connection", "upgrade",
                "http2-settings", "accept-encoding", "host"}
        forward = {k: v for k, v in self.headers.items() if k.lower() not in skip}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(self.path, headers=forward)) as upstream:
            body = upstream.read()
            self.send_response(upstream.status)
            self.send_header("Content-Type", upstream.headers.get("Content-Type", "text/plain"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def proxy():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
    server.seen = []
    server.url = f"http://127.0.0.1:{server.server_port}"
    server.authed_url = f"http://alice:s3cret@127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


class Site:
    def __init__(self, server):
        self._server = server
        self.url = f"http://127.0.0.1:{server.server_port}"
        self.state = server.state

    def hits(self, path):
        return [h for h in self.state["hits"] if h[1] == path]


@pytest.fixture()
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {"hits": [], "flaky": 0, "robots": None}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Site(server)
    server.shutdown()
    server.server_close()
