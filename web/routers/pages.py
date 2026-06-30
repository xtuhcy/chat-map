"""HTML page routes.

GET /     — chat UI shell (left iframe + right chat panel)
GET /map  — the AMap page, rendered with secrets injected from server
            config. Sends `Cache-Control: no-store, private` so that
            rotating the AMap key takes effect on the next reload.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from web.config import get_config

router = APIRouter()
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _sign_user_token(cfg, raw: str) -> str:
    """HMAC-sign a user token with the server's secret.

    Browser sends `{raw}.{hmac}` as its user_token — the server can
    later verify it came from us (and was issued recently) by
    re-computing the HMAC. We don't actually verify it on the
    /ws/chat endpoint in v1, but the structure is in place.
    """
    sig = hmac.new(
        cfg.user_token_secret.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]
    return f"{raw}.{sig}"


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """The chat UI shell.

    Note: this response MUST NOT include any secrets. The only thing
    the shell needs is the path to the iframe and the static asset
    base — all keys are loaded on demand via /api/session and /map.
    """
    return templates.TemplateResponse(
        request,
        "index.html",
        {},
    )


@router.get("/map", response_class=HTMLResponse)
async def map_page(request: Request) -> HTMLResponse:
    """Render the AMap page with secrets injected at request time.

    The response is uncacheable so key rotation takes effect on the
    next reload, and so the page doesn't end up in shared caches.
    """
    cfg = get_config()

    # Build a one-shot user_token for this browser session. The
    # iframe (which runs BrowserUseClient.js) needs a token to
    # identify itself to BrowserUseServer; the chat panel will get
    # the same token via /api/session. We sign it with the server
    # secret so it can't be forged by other origins.
    raw = hashlib.sha256(
        f"{time.time()}-{request.client.host if request.client else 'anon'}".encode(),
    ).hexdigest()[:24]
    signed = _sign_user_token(cfg, raw)

    # Cache-bust the static JS asset: append the file's mtime as a query
    # string so the browser fetches the new copy whenever the JS file is
    # edited (without forcing a manual hard-refresh).
    js_path = _STATIC_DIR / "vendor" / "BrowserUseClient.js"
    js_version = int(js_path.stat().st_mtime) if js_path.exists() else 0

    response = templates.TemplateResponse(
        request,
        "map.html.j2",
        {
            "amap_key": cfg.amap_key,
            "amap_security_code": cfg.amap_security_code,
            "user_token": signed,
            # The iframe should connect back through the FastAPI WS
            # proxy, NOT directly to BrowserUseServer. The proxy URL
            # is derived from the same origin as the page itself.
            "ws_scheme": "wss" if request.url.scheme == "https" else "ws",
            "ws_host": request.url.netloc,
            "js_version": js_version,
        },
    )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return response
