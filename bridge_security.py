"""
bridge_security.py — request guard for gemini_bridge_server.py (audit 2026-11-19)

Closes: S-2 unauthenticated /eval /upload /ask reachable from any web page (text/plain "simple request" CSRF),
        S-4 Host header never checked (DNS-rebinding), S-5 cross-site WebSocket hijack of /ws (prompt theft + forged answers),
        S-6 no CSP / X-Frame-Options / nosniff.

Wire-up (gemini_bridge_server.py, in main()):
    from bridge_security import new_token, make_security_middleware, html_response
    TOKEN = new_token()
    app = web.Application(middlewares=[make_security_middleware(PORT, TOKEN)], client_max_size=32 * 1024 * 1024)
    ...
    async def handle_index(request):  return html_response(INDEX_HTML, TOKEN)      # token is injected server-side
and in the page's JS:   const TOKEN = "__BRIDGE_TOKEN__";   fetch(url, {method:'POST', headers:{'Content-Type':'application/json','X-Bridge-Token':TOKEN}, body})
A custom header forces a CORS preflight, and the server never answers preflights, so other origins cannot send it.
"""
from __future__ import annotations

import hmac
import secrets
from typing import Iterable

from aiohttp import web


def new_token() -> str:
    return secrets.token_urlsafe(32)


def make_security_middleware(
    port: int,
    token: str,
    *,
    ws_origins: Iterable[str] = ("https://gemini.google.com",),
    public_paths: Iterable[str] = ("/",),
):
    hosts = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}
    own_origins = {f"http://{h}" for h in hosts}
    ws_ok = own_origins | set(ws_origins)
    public = set(public_paths)
    tok = token.encode()

    @web.middleware
    async def guard(request: web.Request, handler):
        if request.host.lower() not in hosts:                          # DNS rebinding: Host must be exactly us
            raise web.HTTPForbidden(text="bad host")
        origin = request.headers.get("Origin")
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if origin not in ws_ok:                                    # browsers always send Origin on a WS handshake
                raise web.HTTPForbidden(text="bad origin")
        else:
            sfs = request.headers.get("Sec-Fetch-Site")
            if sfs and sfs not in ("same-origin", "none"):             # blocks <script src>, fetch(), iframes from other sites
                raise web.HTTPForbidden(text="cross-site request")
            if origin and origin not in own_origins:
                raise web.HTTPForbidden(text="bad origin")
            if request.path not in public:
                if not hmac.compare_digest(request.headers.get("X-Bridge-Token", "").encode(), tok):
                    raise web.HTTPForbidden(text="missing or invalid token")
                if request.method not in ("GET", "HEAD") and request.can_read_body and request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType(text="application/json required")
        resp = await handler(request)
        if not isinstance(resp, web.WebSocketResponse):
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Referrer-Policy", "no-referrer")
            if request.path not in public:
                resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    return guard


def html_response(html: str, token: str, *, allow_tailwind_cdn: bool = False) -> web.Response:
    """Serve the control panel with a per-response nonce CSP and the API token injected (never cacheable)."""
    nonce = secrets.token_urlsafe(16)
    html = html.replace("<script>", f'<script nonce="{nonce}">').replace("__BRIDGE_TOKEN__", token)
    script_src = f"'nonce-{nonce}'" + (" https://cdn.tailwindcss.com" if allow_tailwind_cdn else "")
    csp = (f"default-src 'none'; script-src {script_src}; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
           "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return web.Response(text=html, content_type="text/html", headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})
