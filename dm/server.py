"""Local HTTP server: static UI + JSON API, locked down to this machine and this window.

Security model
* binds 127.0.0.1 on a random port; never listens on the network;
* Host header must be 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding);
* every /api call needs the per-launch random token in the X-DM-Token header.
  Browsers cannot add custom headers cross-origin without a CORS preflight,
  which this server never grants, so other websites cannot drive the API;
* Origin / Sec-Fetch-Site must be same-origin when present;
* strict Content-Security-Policy: scripts only from this origin, no inline JS,
  no eval, no outbound connections; nosniff, no-referrer, frame-ancestors none;
* POST bodies must be JSON objects under MAX_BODY_BYTES;
* static files are served only from web/, from a whitelist of extensions.
"""

import hmac
import json
import logging
import re
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import config

log = logging.getLogger("dm.server")

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",
}
_STATIC_PATH = re.compile(r"^/[A-Za-z0-9_\-./]{1,200}$")

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
       "img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "error"):
        super().__init__(message)
        self.status = status
        self.code = code


class Request:
    def __init__(self, handler: "Handler", path: str, query: dict, body: dict | None, params: dict):
        self.handler = handler
        self.path = path
        self.query = query
        self.body = body or {}
        self.params = params

    def q(self, name: str, default=None):
        v = self.query.get(name)
        return v[0] if v else default

    def q_int(self, name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
        try:
            v = int(self.q(name, default))
        except (TypeError, ValueError):
            raise ApiError(400, f"Paramètre « {name} » invalide.")
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def q_float(self, name: str, default: float) -> float:
        try:
            return float(self.q(name, default))
        except (TypeError, ValueError):
            raise ApiError(400, f"Paramètre « {name} » invalide.")


class Router:
    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, object]] = []

    def route(self, method: str, pattern: str):
        """Register a handler. `pattern` may contain {name} segments."""
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[A-Za-z0-9_-]+)", pattern) + "$")

        def deco(fn):
            self.routes.append((method, regex, fn))
            return fn
        return deco

    def match(self, method: str, path: str):
        allowed = False
        for m, regex, fn in self.routes:
            mt = regex.match(path)
            if mt:
                if m == method:
                    return fn, mt.groupdict()
                allowed = True
        return (None, "method") if allowed else (None, None)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64

    def __init__(self, router: Router):
        self.router = router
        self.token = secrets.token_urlsafe(32)
        self.last_ping = time.monotonic()
        super().__init__(("127.0.0.1", 0), Handler)
        self.port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"
    server_version = "DiskManager"
    sys_version = ""

    def log_message(self, fmt, *args):  # pythonw has no console; keep the log quiet
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ responses
    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=(), payment=()")
        self.send_header("Cache-Control", "no-store")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str, code: str = "error") -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    # ------------------------------------------------------------------ checks
    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            return False
        site = self.headers.get("Sec-Fetch-Site")
        return site in (None, "same-origin", "none")

    def _token_ok(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-DM-Token", ""), self.server.token)

    def _read_json(self) -> dict:
        ctype = self.headers.get("Content-Type", "")
        if not ctype.lower().startswith("application/json"):
            raise ApiError(415, "Content-Type application/json attendu.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Content-Length invalide.")
        if length < 0 or length > config.MAX_BODY_BYTES:
            raise ApiError(413, "Requête trop volumineuse.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8", errors="strict") or "{}")
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "JSON invalide.")
        if not isinstance(data, dict):
            raise ApiError(400, "Objet JSON attendu.")
        return data

    # ------------------------------------------------------------------ dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _reject_method(self):
        self._error(405, "Méthode non autorisée.")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _reject_method

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                return self._error(403, "Hôte refusé.", "bad_host")
            parts = urlsplit(self.path)
            path = parts.path
            if path.startswith("/api/"):
                return self._api(method, path, parse_qs(parts.query, max_num_fields=50))
            if method != "GET":
                return self._error(405, "Méthode non autorisée.")
            return self._static(path)
        except (ConnectionError, TimeoutError):
            pass
        except Exception:
            log.error("unhandled error on %s\n%s", self.path, traceback.format_exc())
            try:
                self._error(500, "Erreur interne.")
            except Exception:
                pass

    def _api(self, method: str, path: str, query: dict) -> None:
        if not self._same_origin():
            return self._error(403, "Origine refusée.", "bad_origin")
        if not self._token_ok():
            return self._error(401, "Jeton de session invalide. Relancez l'application.", "bad_token")
        fn, params = self.server.router.match(method, path)
        if fn is None:
            if params == "method":
                return self._error(405, "Méthode non autorisée.")
            return self._error(404, "Route inconnue.", "not_found")
        try:
            body = self._read_json() if method == "POST" else None
            result = fn(Request(self, path, query, body, params))
            self._json(200, result if result is not None else {"ok": True})
        except ApiError as e:
            self._error(e.status, str(e), e.code)

    def _static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        if not _STATIC_PATH.match(path) or ".." in path or "//" in path:
            return self._error(404, "Introuvable.", "not_found")
        web = config.WEB_DIR.resolve()
        target = (web / path.lstrip("/")).resolve()
        if not target.is_relative_to(web) or not target.is_file():
            return self._error(404, "Introuvable.", "not_found")
        ctype = STATIC_TYPES.get(target.suffix.lower())
        if ctype is None:
            return self._error(404, "Introuvable.", "not_found")
        self._send(200, target.read_bytes(), ctype)


def serve_forever_in_thread(server: AppServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True, name="http")
    t.start()
    return t
