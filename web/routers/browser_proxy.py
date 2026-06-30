"""WebSocket proxy: /ws/browser → BrowserUseServer (loopback :8765).

The browser (the BrowserUseClient.js inside the map iframe) connects
to ``ws://<host>/ws/browser``. We forward every frame in both
directions to the real BrowserUseServer WebSocket on
``127.0.0.1:8765``. This keeps BrowserUseServer unexposed to the
network and gives us a single public port to deploy.

If either side disconnects, both ends are closed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from web.config import get_config
from web.routers.chat import _allowed_origins

logger = logging.getLogger("web.browser_proxy")
router = APIRouter()


def _check_origin(websocket: WebSocket) -> bool:
    # Re-use the chat.py implementation — both endpoints need the
    # same origin logic (browser allowlist + WeChat pattern match).
    from web.routers.chat import _check_origin as _chat_check_origin
    return _chat_check_origin(websocket)


async def _pump(
    src,
    dst,
    name: str,
    closer: asyncio.Event,
) -> None:
    """Forward frames from `src` to `dst` until either side closes.

    `name` is just for log readability. `closer` is set when the
    opposite direction's pump finishes (or fails), signalling us to
    bail — we don't want a hung read on one side to keep the
    connection open after the other side has gone.
    """
    try:
        async for frame in src:
            await dst.send(frame)
    except websockets.ConnectionClosed:
        pass
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("proxy pump %s error: %s", name, e)
    finally:
        closer.set()


@router.websocket("/ws/browser")
async def browser_proxy(websocket: WebSocket) -> None:
    if not _check_origin(websocket):
        origin = websocket.headers.get("origin", "<none>")
        logger.warning(
            "ws/browser: rejected origin=%r (allowed=%r)",
            origin, _allowed_origins(),
        )
        await websocket.close(code=4003, reason="Forbidden origin")
        return

    cfg = get_config()
    target_uri = f"ws://{cfg.browser_use_host}:{cfg.browser_use_port}"

    await websocket.accept()
    upstream: Optional[websockets.WebSocketClientProtocol] = None
    try:
        upstream = await websockets.connect(target_uri, open_timeout=5.0)
    except Exception as e:  # noqa: BLE001
        logger.error("upstream connect to %s failed: %s", target_uri, e)
        await websocket.close(code=4002, reason="upstream unavailable")
        return

    # Each direction runs in its own task. The `_done` event is set
    # by whichever direction finishes first; we then cancel the
    # other side to keep things tidy.
    done = asyncio.Event()

    async def to_upstream() -> None:
        try:
            while not done.is_set():
                # FastAPI's WebSocket.receive() can yield text/binary/
                # disconnect — we forward as text since
                # BrowserUseClient.js uses JSON.stringify.
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "text" in msg:
                    await upstream.send(msg["text"])
                elif "bytes" in msg:
                    await upstream.send(msg["bytes"])
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("to_upstream error: %s", e)
        finally:
            done.set()

    async def from_upstream() -> None:
        try:
            async for frame in upstream:
                if isinstance(frame, (bytes, bytearray)):
                    await websocket.send_bytes(bytes(frame))
                else:
                    await websocket.send_text(frame)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("from_upstream error: %s", e)
        finally:
            done.set()

    t_in = asyncio.create_task(to_upstream(), name="proxy-to-upstream")
    t_out = asyncio.create_task(from_upstream(), name="proxy-from-upstream")

    # Wait for either side to finish, then cancel the other.
    try:
        await done.wait()
    finally:
        for t in (t_in, t_out):
            if not t.done():
                t.cancel()
        # Give the cancels a moment to propagate.
        await asyncio.gather(t_in, t_out, return_exceptions=True)
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
        logger.info("browser proxy closed")
