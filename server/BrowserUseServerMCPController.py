#!/usr/bin/env python3
"""
BrowserUseServerMCPController2.py

MCP-call-meta variant of BrowserUseServerMCPController.py.

`page_url` is no longer a tool argument nor an HTTP header — it is read
from the MCP protocol's native `_meta` field on `CallToolRequestParams`,
i.e. `ctx.request_context.meta`. The client side (`RemoteBrowserUseAgent2`)
forwards it via `UserMsg.metadata[MCP_CALL_META_KEY]` and the agentscope
framework automatically promotes it to `session.call_tool(meta=...)`.

`user_token` remains an HTTP header (`X-User-Token`) — it is
per-instance, not per-request, so it has no business in the meta field.

Usage:
    python BrowserUseServerMCPController2.py [--server-port PORT] [--mcp-port PORT]
"""

import asyncio
import argparse
import logging
import sys
import threading
from typing import Optional
from mcp.server.fastmcp import FastMCP, Context
from agentscope.tool import MCP_CALL_META_KEY
from server.BrowserUseServer import BrowserUseServer, ActionResult, BrowserState

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("BrowserUseMCPMeta")


# Static per-instance header — client embeds it once at construction.
HEADER_USER_TOKEN = "X-User-Token"


def _generate_session_id(page_url: str, user_token: str, client_type: str = "browser") -> str:
    """Generate a session ID based on the page_url, user_token, and
    client_type, using UUID v5.

    Matches BrowserUseClient.js _generateSessionId() algorithm for the
    default `client_type="browser"`:
      1. Strip fragment from URL
      2. Combine URL with user_token as `<url>|<user_token>`
      3. Generate UUID v5 using SHA-1 with string-encoded namespace
      4. Return as formatted UUID string (with dashes)

    For non-browser `client_type` (currently only "wxmp"), we prefix
    the combined string with `client_type|" so wxmp sessions and
    browser sessions with the same (url, user_token) never collide.
    The WxmpMapClient mirrors this format in its init: it computes
    `UUIDv5("wxmp|" + pageUrl + "|" + userToken)`.
    """
    import hashlib

    namespace = "6ba7b811-9dad-11d1-80b4-00c04fd430c8"
    clean_url = page_url.split("#")[0]
    # Only prefix the client_type when it's NOT the default — keeps
    # browser session_ids byte-for-byte identical to the pre-wxmp
    # implementation, so existing browser sessions aren't invalidated.
    if client_type and client_type != "browser":
        combined = f"{client_type}|{clean_url}|{user_token}"
    else:
        combined = f"{clean_url}|{user_token}"
    # SHA-1 hash of namespace string + combined
    data = namespace.encode() + combined.encode()
    sha1_hash = hashlib.sha1(data).digest()
    # Take first 16 bytes and set version/variant
    uuid_bytes = bytearray(sha1_hash[:16])
    uuid_bytes[6] = (uuid_bytes[6] & 0x0F) | 0x50  # Version 5
    uuid_bytes[8] = (uuid_bytes[8] & 0x3F) | 0x80  # Variant
    # Format as UUID with dashes (same as JavaScript)
    uuid_hex = uuid_bytes.hex()
    return f"{uuid_hex[0:8]}-{uuid_hex[8:12]}-{uuid_hex[12:16]}-{uuid_hex[16:20]}-{uuid_hex[20:32]}"


def _request_context(ctx: Context) -> tuple[str, str]:
    """Pull (page_url, user_token) from the MCP request.

    `page_url` is read from the inner field of the MCP request's
    `CallToolRequestParams._meta` (surfaced as `ctx.request_context.meta`
    by FastMCP). The client side stores it under
    `UserMsg.metadata[MCP_CALL_META_KEY]`; the agentscope framework
    unwraps that outer key and forwards the inner dict as
    `session.call_tool(meta=...)`. So the server reads the inner field
    directly — not the outer `MCP_CALL_META_KEY` attribute (that one
    is only meaningful on the client side, in `UserMsg.metadata`).

    `user_token` is read from (in order of priority):
    1. HTTP header `X-User-Token` (per-instance, set at client construction)
    2. `params._meta.user_token` field (per-request, via MCP _meta field)

    Raises ValueError if `page_url` is missing — that's a hard
    requirement. Raises ValueError if `user_token` is missing from both
    sources.
    """
    # `ctx.request_context.meta` is a Pydantic `RequestParams.Meta`
    # with `extra="allow"`, so `page_url` is a regular attribute
    # populated from the JSON-RPC `_meta` field — i.e. the inner
    # dict forwarded by the client via `UserMsg.metadata[MCP_CALL_META_KEY]`.
    meta = ctx.request_context.meta
    page_url = getattr(meta, "page_url", None) if meta else None
    if not page_url:
        raise ValueError(
            f"Missing required `page_url` in MCP request `params._meta`. "
            f"The MCP client must set it via "
            f"`UserMsg.metadata[{MCP_CALL_META_KEY!r}]['page_url']`."
        )

    # `user_token` can be provided via:
    # 1. HTTP header `X-User-Token` (per-instance, set at client construction)
    # 2. `meta.user_token` field (per-request, via MCP _meta field)
    # FastMCP exposes the Starlette Request as `ctx.request_context.request`.
    request = ctx.request_context.request
    user_token = request.headers.get(HEADER_USER_TOKEN)
    if not user_token:
        # Fallback to meta field if header is not present
        user_token = getattr(meta, HEADER_USER_TOKEN, None) if meta else None
    if not user_token:
        raise ValueError(
            f"Missing required `user_token` in MCP request. "
            f"It must be provided via HTTP header `{HEADER_USER_TOKEN}` "
            f"or via `params._meta.user_token`."
        )
    # `client_type` is optional — defaults to "browser" for the
    # original web client. The wxmp agent sets it to "wxmp" so the
    # MCP server can (a) compute a per-client session id and
    # (b) route `map_*` calls back to the right client registration.
    client_type = getattr(meta, "client_type", None) if meta else None
    client_type = client_type or "browser"
    print(
        f"Received MCP request for page_url={page_url} "
        f"client_type={client_type} user_token={user_token}"
    )
    return page_url, user_token, client_type


def _client_type_from_meta(ctx: Context) -> str:
    """Pull `client_type` from the MCP request meta.

    Defaults to `"browser"` for backwards compatibility with the
    original BrowserUseClient.js, which does not send a `client_type`
    field in its `UserMsg.metadata`. The wxmp client always sets
    `"wxmp"` via the same metadata path.
    """
    meta = ctx.request_context.meta
    ct = getattr(meta, "client_type", None) if meta else None
    return ct or "browser"


class BrowserUseServerMCPController:
    """
    MCP-call-meta variant of BrowserUseServerMCPController.

    `page_url` is read from `ctx.request_context.meta` (the
    `CallToolRequestParams._meta` field) on each call. `user_token`
    is read from the static `X-User-Token` HTTP header. The agent
    (`RemoteBrowserUseAgent2`) is responsible for setting the meta
    on every `UserMsg` it sends.
    """

    def __init__(
        self,
        server_host: str = "localhost",
        server_port: int = 8765,
        mcp_port: int = 8766,
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.mcp_port = mcp_port
        self.browser_server: Optional[BrowserUseServer] = None
        self.mcp: Optional[FastMCP] = None

    async def start(self):
        """Start both BrowserUseServer and MCP server"""
        print("=" * 60)
        print("BrowserUseServerMCPController (MCP-call-meta)")
        print("=" * 60)
        print()

        # Start BrowserUseServer in background
        self.browser_server = BrowserUseServer(self.server_host, self.server_port)
        server_task = asyncio.create_task(self.browser_server.start())
        await asyncio.sleep(0.5)  # Wait for server to start

        print(f"BrowserUseServer running on ws://{self.server_host}:{self.server_port}")
        print(f"MCP Server running on http://localhost:{self.mcp_port}/mcp")
        print()
        print("Available MCP tools:")
        print("  get_browser_state, get_dom_tree")
        print("  click_element, input_text, select_option")
        print("  scroll, scroll_horizontally, execute_javascript")
        print("  Map (map.html): map_run_search, map_search_and_zoom, map_get_state,")
        print("                  map_set_center, map_set_zoom, map_zoom_in/out,")
        print("                  map_add_marker, map_add_marker_with_info, map_clear_markers,")
        print("                  map_locate, map_search_nearby")
        print()
        print(f"Required on every tool call: `page_url` in `params._meta`")
        print(
            f"  (client sets it via `UserMsg.metadata[{MCP_CALL_META_KEY!r}]['page_url']`)"
        )
        print(f"Optional header (default: test-user-token): {HEADER_USER_TOKEN}")
        print()
        print("Press Ctrl+C to stop...")
        print()

        # Initialize MCP server
        self._setup_mcp()

        # Run MCP server in a separate thread (it manages its own event loop)
        mcp_thread = threading.Thread(
            target=self.mcp.run, kwargs={"transport": "streamable-http"}, daemon=True
        )
        mcp_thread.start()
        logger.info("MCP server thread started")

        # Wait for interrupt signal
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            print("\nStopping servers...")
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass
            print("Server stopped.")

    def _setup_mcp(self):
        """Setup MCP server with all tools"""
        self.mcp = FastMCP(
            "BrowserUseServerMCPMeta", json_response=True, port=self.mcp_port
        )
        logger.info("MCP server initialized")

        # ===== Browser State =====

        @self.mcp.tool()
        async def get_browser_state(ctx: Context) -> dict:
            """
            Get full browser state including URL, title, and page content.

            Note: `page_url` is read from `CallToolRequestParams._meta`
            (surfaced as `ctx.request_context.meta`); `user_token` is
            read from the static `X-User-Token` HTTP header. Neither is
            a tool argument.

            Not supported on WeChat Mini Program clients (no DOM).
            """
            trace_id = ctx.request_id if ctx else "N/A"
            if _client_type_from_meta(ctx) == "wxmp":
                return {"success": False, "message": "unsupported on wxmp client"}
            page_url, user_token, client_type = _request_context(ctx)
            ctx.info(f"Getting browser state for URL: {page_url}") if ctx else None
            session_id = _generate_session_id(page_url, user_token, client_type)
            logger.info(
                f"Tool called: get_browser_state(session_id={session_id}), trace_id={trace_id}"
            )
            state: BrowserState = await self.browser_server.get_browser_state(
                session_id
            )
            result = {
                "url": state.url,
                "title": state.title,
                "header": state.header,
                "content": state.content,
                "footer": state.footer,
            }
            logger.info(f"get_browser_state returned URL: {state.url}")
            return result

        @self.mcp.tool()
        async def get_dom_tree(ctx: Context) -> str:
            """
            Get the DOM tree structure of a page as HTML.

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.

            Not supported on WeChat Mini Program clients (no DOM).
            """
            trace_id = ctx.request_id if ctx else "N/A"
            if _client_type_from_meta(ctx) == "wxmp":
                return "<ERROR: unsupported on wxmp client>"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"Getting DOM tree for: {page_url}") if ctx else None
            logger.info(
                f"Tool called: get_dom_tree(session_id={session_id}), trace_id={trace_id}"
            )
            result = await self.browser_server.update_tree(session_id)
            logger.info(f"get_dom_tree returned tree of length: {len(result)}")
            return result

        # ===== Element Actions =====

        @self.mcp.tool()
        async def click_element(index: int, ctx: Context) -> dict:
            """
            Click an element on the page by its highlight index.

            Args:
                index: The highlight index of the element to click (from DOM tree)

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.

            Not supported on WeChat Mini Program clients (no DOM).
            """
            trace_id = ctx.request_id if ctx else "N/A"
            if _client_type_from_meta(ctx) == "wxmp":
                return {"success": False, "message": "unsupported on wxmp client"}
            page_url, user_token, client_type = _request_context(ctx)
            ctx.info(f"Clicking element {index} on: {page_url}") if ctx else None
            session_id = _generate_session_id(page_url, user_token, client_type)
            logger.info(
                f"Tool called: click_element(session_id={session_id}, index={index}), trace_id={trace_id}"
            )
            result: ActionResult = await self.browser_server.click_element(
                session_id, index
            )
            logger.info(
                f"click_element result: success={result.success}, message={result.message}"
            )
            return result.to_dict()

        @self.mcp.tool()
        async def input_text(index: int, text: str, ctx: Context) -> dict:
            """
            Input text into an element (input field, textarea, etc.) by highlight index.

            Args:
                index: The highlight index of the element to input text into
                text: The text string to input

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.

            Not supported on WeChat Mini Program clients (no DOM).
            """
            trace_id = ctx.request_id if ctx else "N/A"
            if _client_type_from_meta(ctx) == "wxmp":
                return {"success": False, "message": "unsupported on wxmp client"}
            page_url, user_token, client_type = _request_context(ctx)
            ctx.info(
                f"Inputting text to element {index} on: {page_url}"
            ) if ctx else None
            session_id = _generate_session_id(page_url, user_token, client_type)
            logger.info(
                f"Tool called: input_text(session_id={session_id}, index={index}, text={text[:50]}...), trace_id={trace_id}"
            )
            result: ActionResult = await self.browser_server.input_text(
                session_id, index, text
            )
            logger.info(
                f"input_text result: success={result.success}, message={result.message}"
            )
            return result.to_dict()

        @self.mcp.tool()
        async def select_option(index: int, option_text: str, ctx: Context) -> dict:
            """
            Select an option from a dropdown (<select> element) by highlight index.

            Args:
                index: The highlight index of the dropdown element
                option_text: The text of the option to select (exact match)

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.

            Not supported on WeChat Mini Program clients (no DOM).
            """
            trace_id = ctx.request_id if ctx else "N/A"
            if _client_type_from_meta(ctx) == "wxmp":
                return {"success": False, "message": "unsupported on wxmp client"}
            page_url, user_token, client_type = _request_context(ctx)
            ctx.info(
                f"Selecting option '{option_text}' on element {index} of: {page_url}"
            ) if ctx else None
            session_id = _generate_session_id(page_url, user_token, client_type)
            logger.info(
                f"Tool called: select_option(session_id={session_id}, index={index}, option_text={option_text}), trace_id={trace_id}"
            )
            result: ActionResult = await self.browser_server.select_option(
                session_id, index, option_text
            )
            logger.info(
                f"select_option result: success={result.success}, message={result.message}"
            )
            return result.to_dict()

        # ===== Scrolling =====

        @self.mcp.tool()
        async def scroll(
            direction: str = "down", num_pages: float = 1, ctx: Context = None
        ) -> dict:
            """
            Scroll vertically on the page.

            Args:
                direction: Scroll direction - "down" or "up"
                num_pages: Number of page heights to scroll (1 = one full viewport height)

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.

            Not supported on WeChat Mini Program clients (no DOM).
            """
            trace_id = ctx.request_id if ctx else "N/A"
            if _client_type_from_meta(ctx) == "wxmp":
                return {"success": False, "message": "unsupported on wxmp client"}
            page_url, user_token, client_type = _request_context(ctx)
            ctx.info(
                f"Scrolling {direction} {num_pages} pages on: {page_url}"
            ) if ctx else None
            session_id = _generate_session_id(page_url, user_token, client_type)
            logger.info(
                f"Tool called: scroll(session_id={session_id}, direction={direction}, num_pages={num_pages}), trace_id={trace_id}"
            )
            down = direction.lower() == "down"
            result: ActionResult = await self.browser_server.scroll(
                session_id, down, num_pages
            )
            logger.info(
                f"scroll result: success={result.success}, message={result.message}"
            )
            return result.to_dict()

        @self.mcp.tool()
        async def scroll_horizontally(
            direction: str = "right", pixels: int = 500, ctx: Context = None
        ) -> dict:
            """
            Scroll horizontally on the page.

            Args:
                direction: Scroll direction - "right" or "left"
                pixels: Number of pixels to scroll (default: 500)

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.

            Not supported on WeChat Mini Program clients (no DOM).
            """
            trace_id = ctx.request_id if ctx else "N/A"
            if _client_type_from_meta(ctx) == "wxmp":
                return {"success": False, "message": "unsupported on wxmp client"}
            page_url, user_token, client_type = _request_context(ctx)
            ctx.info(
                f"Scrolling {direction} {pixels}px on: {page_url}"
            ) if ctx else None
            session_id = _generate_session_id(page_url, user_token, client_type)
            logger.info(
                f"Tool called: scroll_horizontally(session_id={session_id}, direction={direction}, pixels={pixels}), trace_id={trace_id}"
            )
            right = direction.lower() == "right"
            result: ActionResult = await self.browser_server.scroll_horizontally(
                session_id, right, pixels
            )
            logger.info(
                f"scroll_horizontally result: success={result.success}, message={result.message}"
            )
            return result.to_dict()

        # ===== JavaScript =====

        @self.mcp.tool()
        async def execute_javascript(script: str, ctx: Context = None) -> dict:
            """
            Execute arbitrary JavaScript code in the page context.

            Args:
                script: The JavaScript code to execute (e.g., "window.scrollBy(0, 200)")

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.

            Not supported on WeChat Mini Program clients (no eval).
            """
            trace_id = ctx.request_id if ctx else "N/A"
            if _client_type_from_meta(ctx) == "wxmp":
                return {"success": False, "message": "unsupported on wxmp client"}
            page_url, user_token, client_type = _request_context(ctx)
            ctx.info(f"Executing JavaScript on: {page_url}") if ctx else None
            session_id = _generate_session_id(page_url, user_token, client_type)
            logger.info(
                f"Tool called: execute_javascript(session_id={session_id}, script={script[:50]}...), trace_id={trace_id}"
            )
            result: ActionResult = await self.browser_server.execute_javascript(
                session_id, script
            )
            logger.info(
                f"execute_javascript result: success={result.success}, message={result.message}"
            )
            return result.to_dict()

        # ===== Map-specific tools (target window.__map on map.html) =====

        @self.mcp.tool()
        async def map_run_search(keyword: str, ctx: Context) -> dict:
            """
            Trigger AMap POI keyword search on the page (map.html only).

            Args:
                keyword: Search term (place name / address / etc.)

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_run_search '{keyword}' on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_run_search(session_id={session_id}, keyword={keyword!r}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_run_search(session_id, keyword, client_type=client_type)
            logger.info(
                f"map_run_search success={result.get('success')}, info={result.get('info')}"
            )
            return result

        @self.mcp.tool()
        async def map_search_and_zoom(
            keyword: str, zoom: int = 15, ctx: Context = None
        ) -> dict:
            """
            Search a POI and set the map's zoom level after the result lands.

            Args:
                keyword: Search term
                zoom: Target zoom level (default 15)

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(
                f"map_search_and_zoom '{keyword}' zoom={zoom} on: {page_url}"
            ) if ctx else None
            logger.info(
                f"Tool called: map_search_and_zoom(session_id={session_id}, keyword={keyword!r}, zoom={zoom}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_search_and_zoom(
                session_id, keyword, zoom, client_type=client_type,
            )
            logger.info(f"map_search_and_zoom success={result.get('success')}")
            return result

        @self.mcp.tool()
        async def map_get_state(ctx: Context) -> dict:
            """
            Read the current map state (center, zoom, bounds, overlay count, info text).

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_get_state on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_get_state(session_id={session_id}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_get_state(session_id, client_type=client_type)
            logger.info(
                f"map_get_state center={result.get('center')} zoom={result.get('zoom')}"
            )
            return result

        @self.mcp.tool()
        async def map_set_center(lng: float, lat: float, ctx: Context) -> dict:
            """
            Re-center the map to the given (lng, lat).

            Args:
                lng: Longitude (WGS-84 / GCJ-02 depending on AMap coordinate mode)
                lat: Latitude

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_set_center ({lng}, {lat}) on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_set_center(session_id={session_id}, lng={lng}, lat={lat}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_set_center(session_id, lng, lat, client_type=client_type)
            logger.info(f"map_set_center success={result.get('success')}")
            return result

        @self.mcp.tool()
        async def map_set_zoom(zoom: int, ctx: Context) -> dict:
            """
            Set the map's zoom level.

            Args:
                zoom: Target zoom level (typically 3–18)

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_set_zoom {zoom} on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_set_zoom(session_id={session_id}, zoom={zoom}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_set_zoom(session_id, zoom, client_type=client_type)
            logger.info(f"map_set_zoom success={result.get('success')}")
            return result

        @self.mcp.tool()
        async def map_zoom_in(ctx: Context = None) -> dict:
            """
            Zoom the map in by one level.

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_zoom_in on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_zoom_in(session_id={session_id}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_zoom_in(session_id, client_type=client_type)
            return result

        @self.mcp.tool()
        async def map_zoom_out(ctx: Context = None) -> dict:
            """
            Zoom the map out by one level.

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_zoom_out on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_zoom_out(session_id={session_id}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_zoom_out(session_id, client_type=client_type)
            return result

        @self.mcp.tool()
        async def map_add_marker(
            lng: float, lat: float, title: str = "", ctx: Context = None
        ) -> dict:
            """
            Add a marker at (lng, lat) with optional title.

            Args:
                lng: Longitude
                lat: Latitude
                title: Optional marker tooltip / label

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(
                f"map_add_marker ({lng}, {lat}) '{title}' on: {page_url}"
            ) if ctx else None
            logger.info(
                f"Tool called: map_add_marker(session_id={session_id}, lng={lng}, lat={lat}, title={title!r}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_add_marker(
                session_id, lng, lat, title, client_type=client_type,
            )
            return result

        @self.mcp.tool()
        async def map_add_marker_with_info(
            lng: float,
            lat: float,
            title: str = "",
            info_html: str = None,
            poi: dict = None,
            ctx: Context = None,
        ) -> dict:
            """
            Add a clickable marker that opens a rich info window when clicked.

            This is the **preferred** tool for "list of POIs from
            map_search_nearby" — the user sees all N markers on the map at
            once, and clicks each one to read its details. Avoids the
            "single info window" problem of looping `map_add_marker` +
            `map_open_info_window` (where only the last window is visible).

            Args:
                lng, lat: Marker position.
                title: Tooltip / hover text.
                info_html: Pre-formatted HTML for the popup. If both
                           `info_html` and `poi` are supplied, `info_html`
                           wins.
                poi: An AMap POI object (e.g. one item from
                     `map_search_nearby`'s `pois[]` array). The helper
                     formats a standardized card from it: name / type /
                     distance / address / tel / business hours / rating /
                     cost / first photo. All fields are HTML-escaped.

            Behavior:
                - The marker stays visible on the map.
                - Clicking the marker opens the popup at the marker.
                - Only one popup is open at a time; clicking another marker
                  closes the previous one automatically.
                - Clicking elsewhere on the map closes the popup.

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` comes from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(
                f"map_add_marker_with_info ({lng}, {lat}) '{title}' on: {page_url}"
            ) if ctx else None
            logger.info(
                f"Tool called: map_add_marker_with_info(session_id={session_id}, lng={lng}, "
                f"lat={lat}, title={title!r}, has_poi={poi is not None}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_add_marker_with_info(
                session_id, lng, lat, title,
                info_html=info_html,
                poi=poi,
                client_type=client_type,
            )
            return result

        @self.mcp.tool()
        async def map_clear_markers(ctx: Context = None) -> dict:
            """
            Clear all markers currently shown on the map.

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_clear_markers on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_clear_markers(session_id={session_id}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_clear_markers(session_id, client_type=client_type)
            return result

        @self.mcp.tool()
        async def map_locate(ctx: Context = None) -> dict:
            """
            Trigger browser geolocation; result lands as a marker on the map.

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` from the `X-User-Token` header. Requires the
            AMap Geolocation plugin to be loaded and the user to grant
            permission.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_locate on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_locate(session_id={session_id}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_locate(session_id, client_type=client_type)
            return result

        @self.mcp.tool()
        async def map_draw_polyline(
            path: list, options: dict = None, ctx: Context = None
        ) -> dict:
            """
            Draw a polyline (route) through the given points.

            Args:
                path: List of points. Each point may be [lng, lat] or {"lng": .., "lat": ..}.
                options: {color, width, opacity, showDir}. Defaults: blue 6px.

            Note: `page_url` from `ctx.request_context.meta`;
            `user_token` from `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_draw_polyline ({len(path)} pts) on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_draw_polyline(session_id={session_id}, points={len(path)}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_draw_polyline(
                session_id, path, options or {}, client_type=client_type,
            )
            return result

        @self.mcp.tool()
        async def map_draw_polygon(
            path: list, options: dict = None, ctx: Context = None
        ) -> dict:
            """
            Draw a polygon (filled area) with the given vertices.

            Args:
                path: List of points forming the polygon ring.
                options: {color, width, fillColor, fillOpacity}. Defaults: purple, 0.2 fill.

            Note: `page_url` from `ctx.request_context.meta`;
            `user_token` from `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_draw_polygon ({len(path)} pts) on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_draw_polygon(session_id={session_id}, points={len(path)}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_draw_polygon(
                session_id, path, options or {}, client_type=client_type,
            )
            return result

        @self.mcp.tool()
        async def map_draw_circle(
            lng: float,
            lat: float,
            radius: float,
            options: dict = None,
            ctx: Context = None,
        ) -> dict:
            """
            Draw a circle centered at (lng, lat) with the given radius (meters).

            Useful for "附近 X 米" / service-range / delivery-zone visualization.

            Args:
                lng, lat: Center coordinates.
                radius: Radius in meters.
                options: {color, width, fillColor, fillOpacity}. Defaults: blue, 0.15 fill.

            Note: `page_url` from `ctx.request_context.meta`;
            `user_token` from `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(
                f"map_draw_circle ({lng}, {lat}) r={radius} on: {page_url}"
            ) if ctx else None
            logger.info(
                f"Tool called: map_draw_circle(session_id={session_id}, lng={lng}, lat={lat}, radius={radius}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_draw_circle(
                session_id, lng, lat, radius, options or {}, client_type=client_type,
            )
            return result

        @self.mcp.tool()
        async def map_open_info_window(
            lng: float, lat: float, content: str, ctx: Context = None
        ) -> dict:
            """
            Open an AMap info window (popup) at (lng, lat) with given HTML/text content.

            Args:
                lng, lat: Popup anchor coordinates.
                content: HTML string or plain text. Plain text is auto-escaped.

            Note: `page_url` from `ctx.request_context.meta`;
            `user_token` from `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(
                f"map_open_info_window ({lng}, {lat}) on: {page_url}"
            ) if ctx else None
            logger.info(
                f"Tool called: map_open_info_window(session_id={session_id}, lng={lng}, lat={lat}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_open_info_window(
                session_id, lng, lat, content, client_type=client_type,
            )
            return result

        @self.mcp.tool()
        async def map_close_info_window(ctx: Context = None) -> dict:
            """Close the currently open info window (no-op if none)."""
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_close_info_window on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_close_info_window(session_id={session_id}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_close_info_window(session_id, client_type=client_type)
            return result

        @self.mcp.tool()
        async def map_fit_view(ctx: Context = None) -> dict:
            """
            Auto-fit the map view to show all overlays (markers, polylines, polygons, circles).

            Note: `page_url` from `ctx.request_context.meta`;
            `user_token` from `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_fit_view on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_fit_view(session_id={session_id}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_fit_view(session_id, client_type=client_type)
            return result

        @self.mcp.tool()
        async def map_clear_overlays(type: str = "all", ctx: Context = None) -> dict:
            """
            Clear overlays selectively.

            Args:
                type: 'all' | 'shape' | 'polyline' | 'polygon' | 'circle' | 'marker'.
                      'shape' = polyline+polygon+circle (keeps markers).

            Note: `page_url` from `ctx.request_context.meta`;
            `user_token` from `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(
                f"map_clear_overlays ({type}) on: {page_url}"
            ) if ctx else None
            logger.info(
                f"Tool called: map_clear_overlays(session_id={session_id}, type={type!r}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_clear_overlays(session_id, type, client_type=client_type)
            return result

        @self.mcp.tool()
        async def map_remove_overlay(
            type: str, index: int, ctx: Context = None
        ) -> dict:
            """
            Remove a single overlay at the given index in the named bucket.

            Args:
                type: 'polyline' | 'polygon' | 'circle'
                index: 0-based position in that bucket.

            Note: `page_url` from `ctx.request_context.meta`;
            `user_token` from `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(
                f"map_remove_overlay ({type}[{index}]) on: {page_url}"
            ) if ctx else None
            logger.info(
                f"Tool called: map_remove_overlay(session_id={session_id}, type={type!r}, index={index}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_remove_overlay(
                session_id, type, index, client_type=client_type,
            )
            return result

        @self.mcp.tool()
        async def map_geocode(
            address: str, city: str = None, ctx: Context = None
        ) -> dict:
            """
            Geocode an address (or place name) to coordinates.

            Args:
                address: Free-form address or place name (e.g. "上海迪士尼度假区")
                city: Optional city bias (e.g. "上海"); defaults to "全国".

            Returns: { success, lng, lat, formatted_address, level, all: [...] }.

            Note: `page_url` from `ctx.request_context.meta`;
            `user_token` from `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(
                f"map_geocode {address!r} (city={city!r}) on: {page_url}"
            ) if ctx else None
            logger.info(
                f"Tool called: map_geocode(session_id={session_id}, address={address!r}, city={city!r}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_geocode(session_id, address, city, client_type=client_type)
            return result

        @self.mcp.tool()
        async def map_list_overlays(ctx: Context = None) -> dict:
            """List overlay counts and info-window state (for self-check / cleanup)."""
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(f"map_list_overlays on: {page_url}") if ctx else None
            logger.info(
                f"Tool called: map_list_overlays(session_id={session_id}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_list_overlays(session_id, client_type=client_type)
            return result

        @self.mcp.tool()
        async def map_search_nearby(
            lng: float,
            lat: float,
            radius: float,
            type: str = None,
            keyword: str = None,
            city: str = None,
            exclude_keywords: list = None,
            include_keywords: list = None,
            ctx: Context = None,
        ) -> dict:
            """
            POI nearby search via AMap PlaceSearch (type + location + radius).

            Use this for "X 类地点 半径 Y km 内" queries — it is the only
            tool that combines category/keyword with a radius constraint,
            which the keyword-only `map_run_search` cannot do.

            Args:
                lng, lat: Center of the search circle (e.g. from `map_geocode`).
                radius: Radius in meters (e.g. 5000 for "5 km").
                type: Optional AMap POI category code (e.g. "141201"=高中,
                      "060100"=购物中心). See the skill doc's POI 类型表.
                keyword: Optional free-text filter. Pass empty / null to
                         search by category alone.
                city: Optional city bias (e.g. "北京"). Strongly recommended
                      for ambiguous addresses — `map_geocode` should usually
                      supply it.
                exclude_keywords: Optional list of substrings. POIs whose
                      `name` contains any of these are dropped. Use this to
                      strip noise — e.g. ["驾校","培训","复读"] when
                      searching for 高中.
                include_keywords: Optional list of substrings. If non-empty,
                      only POIs whose `name` contains at least one of these
                      are kept. Use this to require a positive signal — e.g.
                      ["高中","中学","附中"].

            Returns: { success, count, total_before_filter, filtered_out,
                      excluded_by_keyword: {kw: count, ...},
                      pois: [...] } sorted by `distance` ascending.
                      `distance` is meters from the center.

            Note: `page_url` comes from `ctx.request_context.meta`;
            `user_token` comes from the `X-User-Token` header.
            """
            trace_id = ctx.request_id if ctx else "N/A"
            page_url, user_token, client_type = _request_context(ctx)
            session_id = _generate_session_id(page_url, user_token, client_type)
            ctx.info(
                f"map_search_nearby center=({lng},{lat}) r={radius} type={type!r} on: {page_url}"
            ) if ctx else None
            logger.info(
                f"Tool called: map_search_nearby(session_id={session_id}, lng={lng}, lat={lat}, "
                f"radius={radius}, type={type!r}, keyword={keyword!r}, "
                f"exclude={exclude_keywords!r}, include={include_keywords!r}), trace_id={trace_id}"
            )
            result = await self.browser_server.map_search_nearby(
                session_id, lng, lat, radius,
                type=type,
                keyword=keyword,
                city=city,
                exclude_keywords=exclude_keywords,
                include_keywords=include_keywords,
                client_type=client_type,
            )
            logger.info(
                f"map_search_nearby success={result.get('success')} count={result.get('count')} "
                f"filtered_out={result.get('filtered_out')}"
            )
            return result


async def main():
    parser = argparse.ArgumentParser(
        description="BrowserUseServerMCPController2 - MCP server for browser control (MCP-call-meta)"
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=8765,
        help="BrowserUseServer WebSocket port (default: 8765)",
    )
    parser.add_argument(
        "--mcp-port",
        type=int,
        default=8766,
        help="MCP Server HTTP port (default: 8766)",
    )
    parser.add_argument(
        "--host", default="localhost", help="Server host (default: localhost)"
    )
    args = parser.parse_args()

    controller = BrowserUseServerMCPController(
        server_host=args.host, server_port=args.server_port, mcp_port=args.mcp_port
    )
    await controller.start()


if __name__ == "__main__":
    print("Starting BrowserUseServerMCPController (MCP-call-meta)...")
    print()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nController stopped.")
        # Force exit since MCP server thread may not stop cleanly
        sys.exit(0)
