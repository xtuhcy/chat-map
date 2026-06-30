"""Chat WebSocket: /ws/chat

Drives one `AgentSession` per connection. The browser sends user
turns as JSON frames; the server streams back reply events as JSON
frames (see `event_bridge` for the protocol).

Cancellation is supported: when a new ``user`` frame arrives while
a turn is in flight, the prior turn is cancelled and a new one
starts. A bare ``cancel`` frame cancels without starting a new one.
``ping`` is acknowledged with ``pong`` for liveness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from web.config import get_config
from web.services.agent_session import AgentSession

logger = logging.getLogger("web.chat")
router = APIRouter()


def _allowed_origins() -> list[str]:
    """Parse `WEB_PUBLIC_ORIGIN` as a comma-separated allowlist.

    Supports a wildcard ``*`` (any origin) — convenient for local
    development but should NOT be used in production. The default
    in `web/.env.example` lists both ``localhost`` and ``127.0.0.1``
    variants so dev users don't have to think about which form
    their browser used.
    """
    cfg = get_config()
    raw = (cfg.web_public_origin or "").strip()
    if not raw:
        return []
    if raw == "*":
        return ["*"]
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def _check_origin(websocket: WebSocket) -> bool:
    """CSRF defense: only allow WS upgrades from configured origins.

    An origin passes if it matches ANY of:
      1. The `WEB_PUBLIC_ORIGIN` CSV allowlist (regular browser/web clients).
      2. The `WX_WS_ORIGIN_ALLOW_PATTERN` regex (WeChat Mini Program
         clients — they always send `https://servicewechat.com/...`).
      3. Wildcard `*` in `WEB_PUBLIC_ORIGIN` (dev only).

    Empty `Origin` header (native WS clients like curl) is also allowed
    since it provides no cross-site information.
    """
    cfg = get_config()
    allowed = _allowed_origins()
    if not allowed:
        return True
    if "*" in allowed:
        return True
    origin = (
        websocket.headers.get("origin")
        or websocket.headers.get("sec-websocket-origin")
        or ""
    ).rstrip("/")
    if not origin:
        # Native WS clients (curl, CLI tools) won't send Origin — allow.
        return True
    if origin in allowed:
        return True
    # WeChat Mini Program origin check (separate from browser allowlist).
    pattern = (cfg.wx_ws_origin_allow_pattern or "").strip()
    if pattern:
        import re
        try:
            if re.match(pattern, origin):
                return True
        except re.error:
            logger.warning(
                "ws/chat: invalid wx_ws_origin_allow_pattern=%r, ignoring",
                pattern,
            )
    return False


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket) -> None:
    if not _check_origin(websocket):
        origin = websocket.headers.get("origin", "<none>")
        logger.warning(
            "ws/chat: rejected origin=%r (allowed=%r)",
            origin, _allowed_origins(),
        )
        await websocket.close(code=4003, reason="Forbidden origin")
        return

    await websocket.accept()
    cfg = get_config()

    # We need a user_token and page_url to construct the agent. The
    # chat panel will fetch these from /api/session on first load
    # and pass them as the first WS message ("hello"). Until we get
    # that frame, we wait — if the client never sends it, we close.
    try:
        hello_raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await websocket.close(code=4001, reason="hello timeout")
        return
    try:
        hello = json.loads(hello_raw)
    except json.JSONDecodeError:
        await websocket.close(code=4000, reason="bad hello")
        return
    if hello.get("type") != "hello":
        await websocket.close(code=4000, reason="expected hello")
        return

    user_token = hello.get("user_token") or "anonymous"
    page_url = hello.get("page_url") or f"http://{cfg.web_public_origin.split('://', 1)[-1]}/map"
    # Default to "browser" so the regular web client (which doesn't
    # send this field) keeps working. The wxmp client sends
    # `client_type: "wxmp"` so its MCP server sessions don't collide
    # with browser sessions and routing lands on the right socket.
    client_type = hello.get("client_type") or "browser"

    session = AgentSession(
        cfg=cfg, user_token=user_token, page_url=page_url, client_type=client_type,
    )
    await websocket.send_json({"type": "ready"})

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg: Dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "invalid json"})
                continue

            mtype = msg.get("type")
            if mtype == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if mtype == "cancel":
                await session.cancel()
                await websocket.send_json({"type": "reply_end"})
                continue
            if mtype == "user":
                content = (msg.get("content") or "").strip()
                if not content:
                    await websocket.send_json({
                        "type": "error", "message": "empty content",
                    })
                    continue
                # Stream the turn. If the WS disconnects mid-stream,
                # the underlying task is cancelled automatically.
                try:
                    async for frame in session.submit(content):
                        await websocket.send_json(frame)
                except WebSocketDisconnect:
                    logger.info("WS disconnected mid-turn — cancelling")
                    await session.cancel()
                    return
                continue

            await websocket.send_json({
                "type": "error", "message": f"unknown type: {mtype!r}",
            })
    except WebSocketDisconnect:
        logger.info("Chat WS disconnected")
    finally:
        # Best-effort cancel; the session can be GC'd after this.
        await session.cancel()
