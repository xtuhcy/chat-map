"""FastAPI entry point for the chat-map web UI.

Lifespan:
  1. Read config (from web/.env via pydantic-settings).
  2. Start BrowserUseServerMCPController on background task —
     it spawns BrowserUseServer (127.0.0.1:8765) and the MCP server
     (127.0.0.1:8766) on a daemon thread. Both bind loopback only,
     so they aren't reachable from the network.
  3. Poll the MCP port until it's actually accepting connections
     (or timeout) — better than the previous "sleep 2s and pray".
  4. Yield to the app.
  5. On shutdown: cancel the controller task, await its cleanup
     so the daemon thread / sockets are released before exit.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# Make sibling top-level packages (server/, agent/) importable when
# this file is run as `uvicorn web.app:app` from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "server"))
sys.path.insert(0, str(_PROJECT_ROOT / "agent"))

from server.BrowserUseServerMCPController import BrowserUseServerMCPController  # noqa: E402

from web.config import get_config  # noqa: E402
from web.routers import browser_proxy, chat, pages, session, wx_config, wx_login  # noqa: E402

logger = logging.getLogger("web.app")


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    """Block (in a thread) until `host:port` accepts a TCP connection.

    FastMCP's `mcp.run(transport='streamable-http')` returns almost
    immediately because it spins up its own asyncio loop on a daemon
    thread — but the actual socket bind happens a few ms later. We
    poll with a short-connect / short-timeout until it succeeds or
    we hit the wall-clock timeout. Returns True if the port is up.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()

    # 1) Spin up the controller — it manages BrowserUseServer + MCP.
    controller = BrowserUseServerMCPController(
        server_host=cfg.browser_use_host,
        server_port=cfg.browser_use_port,
        mcp_port=cfg.mcp_port,
    )
    controller_task = asyncio.create_task(controller.start(), name="mcp-controller")

    # 2) Wait for MCP port to actually bind. We do this synchronously
    #    via run_in_executor because socket polling is blocking I/O.
    loop = asyncio.get_running_loop()
    mcp_ready = await loop.run_in_executor(
        None, _wait_for_port, cfg.mcp_host, cfg.mcp_port, cfg.mcp_ready_timeout,
    )
    if not mcp_ready:
        logger.warning(
            "MCP port %d not ready within %.1fs — agent calls may fail "
            "until the server is up.",
            cfg.mcp_port, cfg.mcp_ready_timeout,
        )
    else:
        logger.info(
            "MCP server reachable on %s:%d", cfg.mcp_host, cfg.mcp_port,
        )

    # Expose the controller to routers (e.g., for the chat session
    # service that needs to know the MCP URL — which we keep in cfg).
    app.state.cfg = cfg
    app.state.controller = controller
    app.state.controller_task = controller_task

    try:
        yield
    finally:
        logger.info("Shutting down — cancelling MCP controller task.")
        controller_task.cancel()
        try:
            await controller_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="Chat Map",
        version="0.1.0",
        lifespan=lifespan,
    )

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount(
        "/static",
        StaticFiles(directory=str(static_dir), check_dir=False),
        name="static",
    )

    app.include_router(pages.router)
    app.include_router(chat.router)
    app.include_router(browser_proxy.router)
    app.include_router(session.router)
    app.include_router(wx_login.router)
    app.include_router(wx_config.router)

    return app


app = create_app()
