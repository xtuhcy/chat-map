"""Session / user_token endpoint: GET /api/session

Returns a per-browser user_token and the page_url that the chat
agent should bind to. The browser caches this in localStorage; the
chat panel sends it as the first frame of /ws/chat ("hello"); the
map iframe reads it from localStorage and passes it to
BrowserUseClient.js.

`Cache-Control: no-store` so the user_token isn't shared-cached.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from web.config import get_config

router = APIRouter()


def _sign_user_token(cfg, raw: str) -> str:
    sig = hmac.new(
        cfg.user_token_secret.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]
    return f"{raw}.{sig}"


@router.get("/api/session")
async def session(request: Request) -> JSONResponse:
    cfg = get_config()

    # Deterministic per remote address + coarse time bucket — keeps
    # the same token stable across page reloads for the same user.
    # If you want a fresh token per tab, swap this for a cookie check.
    host = request.client.host if request.client else "anon"
    bucket = int(time.time() // (60 * 60 * 24))  # 24h bucket
    raw = hashlib.sha256(f"{host}-{bucket}".encode()).hexdigest()[:24]
    signed = _sign_user_token(cfg, raw)

    # page_url the agent will use for tool routing. The browser
    # already knows its own /map URL, so we let the JS layer fill
    # this in (we just provide the origin).
    body = {
        "user_token": signed,
        # The page_url the chat agent binds to. We point at our own
        # /map endpoint by default — the agent only uses it as an
        # opaque routing key on the MCP server side, so the literal
        # string doesn't have to be reachable.
        "page_url": f"{request.url.scheme}://{request.url.netloc}/map",
    }
    response = JSONResponse(body)
    response.headers["Cache-Control"] = "no-store, private"
    return response
