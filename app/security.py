"""Optional HTTP Basic auth + same-origin checks for state-changing requests.

The UI drives the Docker daemon, so a forged cross-site POST (or WebSocket) from a malicious page
in the same browser would be as bad as shell access. Every non-GET request and every WebSocket
must therefore come from this UI's own origin.
"""
from __future__ import annotations

import base64
import secrets
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from . import config


def _header(scope: Scope, name: bytes) -> str:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return ""


def _auth_ok(scope: Scope) -> bool:
    if not config.AUTH_PASSWORD:
        return True
    value = _header(scope, b"authorization")
    if not value.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(value[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return (secrets.compare_digest(user, config.AUTH_USER or "admin")
            and secrets.compare_digest(pw, config.AUTH_PASSWORD))


def _same_origin(scope: Scope) -> bool:
    host = _header(scope, b"host")
    origin = _header(scope, b"origin") or _header(scope, b"referer")
    if not origin:
        # Non-browser clients (curl) send no Origin; browsers always do for POST/WebSocket.
        return scope["type"] != "websocket"
    return urlsplit(origin).netloc == host


class GuardMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if not _auth_ok(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401})
                return
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"www-authenticate", b'Basic realm="Frappe Docker Manager"'),
                                    (b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Authentication required"})
            return
        unsafe = scope["type"] == "websocket" or scope.get("method") not in ("GET", "HEAD", "OPTIONS")
        if unsafe and not _same_origin(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403})
                return
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Cross-origin request blocked"})
            return
        await self.app(scope, receive, send)
