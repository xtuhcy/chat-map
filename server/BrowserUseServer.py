"""
BrowserUseServer.py

Python WebSocket server that sends commands to BrowserUseClient for browser control.

Architecture:
- BrowserUseServer (this) <--ws--> BrowserUseClient (in browser)
- BrowserUseClient has PageController capabilities and controls the browser
- Server sends commands via WebSocket, client executes and returns results

Commands supported:
- updateTree: Extract DOM and return simplified HTML
- getBrowserState: Get structured browser state for LLM
- clickElement: Click element by index
- inputText: Input text into element by index
- selectOption: Select dropdown option by text
- scroll: Scroll vertically
- scrollHorizontally: Scroll horizontally
- executeJavascript: Execute arbitrary JavaScript
- getCurrentUrl: Get current URL
- getLastUpdateTime: Get last tree update timestamp

Usage:
    python BrowserUseServer.py [--host HOST] [--port PORT]

Dependencies:
    pip install websockets

Example:
    # Start server
    python BrowserUseServer.py --port 8765

    # In browser, load BrowserUseClient.js and connect:
    const client = new BrowserUseClient('ws://localhost:8765');
    await client.connect();
"""

import asyncio
import json
import sys
import argparse
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("Error: websockets package required. Install with: pip install websockets")
    sys.exit(1)


@dataclass
class ActionResult:
    """Result of an element action"""
    success: bool
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {'success': self.success, 'message': self.message}


@dataclass
class BrowserState:
    """Structured browser state for LLM consumption"""
    url: str
    title: str
    header: str
    content: str
    footer: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BrowserCommand:
    """Commands that can be sent to BrowserUseClient"""

    # DOM Operations
    UPDATE_TREE = "updateTree"
    GET_BROWSER_STATE = "getBrowserState"
    GET_CURRENT_URL = "getCurrentUrl"
    GET_LAST_UPDATE_TIME = "getLastUpdateTime"
    CLEAN_UP_HIGHLIGHTS = "cleanUpHighlights"

    # Element Actions
    CLICK_ELEMENT = "clickElement"
    INPUT_TEXT = "inputText"
    SELECT_OPTION = "selectOption"

    # Scrolling
    SCROLL = "scroll"
    SCROLL_HORIZONTALLY = "scrollHorizontally"

    # JavaScript
    EXECUTE_JAVASCRIPT = "executeJavascript"

    # Map-specific (target window.__map exposed by map.html)
    MAP_RUN_SEARCH = "mapRunSearch"
    MAP_GET_STATE = "mapGetState"
    MAP_SET_CENTER = "mapSetCenter"
    MAP_SET_ZOOM = "mapSetZoom"
    MAP_ZOOM_IN = "mapZoomIn"
    MAP_ZOOM_OUT = "mapZoomOut"
    MAP_ADD_MARKER = "mapAddMarker"
    MAP_ADD_MARKER_WITH_INFO = "mapAddMarkerWithInfo"
    MAP_CLEAR_MARKERS = "mapClearMarkers"
    MAP_LOCATE = "mapLocate"
    MAP_SEARCH_AND_ZOOM = "mapSearchAndZoom"
    # Drawing / overlay operations
    MAP_DRAW_POLYLINE = "mapDrawPolyline"
    MAP_DRAW_POLYGON = "mapDrawPolygon"
    MAP_DRAW_CIRCLE = "mapDrawCircle"
    MAP_OPEN_INFO_WINDOW = "mapOpenInfoWindow"
    MAP_CLOSE_INFO_WINDOW = "mapCloseInfoWindow"
    MAP_FIT_VIEW = "mapFitView"
    MAP_CLEAR_OVERLAYS = "mapClearOverlays"
    MAP_REMOVE_OVERLAY = "mapRemoveOverlay"
    MAP_GEOCODE = "mapGeocode"
    MAP_LIST_OVERLAYS = "mapListOverlays"
    MAP_SEARCH_NEARBY = "mapSearchNearby"


class BrowserUseServer:
    """
    WebSocket server that sends commands to BrowserUseClient.

    The server acts as the "brain" that decides what actions to take,
    while BrowserUseClient (running in the browser) handles the actual
    DOM manipulation and element interactions.

    Session Management:
    - Each client generates its own session_id from the page URL (UUID v5)
    - Clients are indexed by `(client_type, session_id)` for targeted
      command delivery. `client_type` is `"browser"` for the existing
      BrowserUseClient.js DOM-automation client and `"wxmp"` for the
      WeChat Mini Program client (which renders maps natively and
      implements only the `map_*` subset). Adding the dimension means
      a browser session and a wxmp session with the same URL+token
      never collide in `clients` or `_pending_commands`.
    - Use `get_client(client_type, session_id)` to look up the
      websocket for a given (client_type, session_id) pair.

    Command Flow:
    - Server -> Client: Send command via websocket.send()
    - Client -> Server: Response comes through main message loop
    - Pending commands are tracked by method name
      (single pending per method per (client_type, session_id))
    """

    # Known client_type values. The init frame's `client_type` field
    # defaults to CLIENT_TYPE_BROWSER for backwards compatibility —
    # BrowserUseClient.js (the original DOM client) does not send it.
    CLIENT_TYPE_BROWSER = "browser"
    CLIENT_TYPE_WXMP = "wxmp"
    _DEFAULT_CLIENT_TYPE = CLIENT_TYPE_BROWSER

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 8765,
        command_timeout: float = 60.0,
    ):
        self.host = host
        self.port = port
        self.command_timeout = command_timeout
        # (client_type, session_id) -> websocket.
        self.clients: Dict[tuple, Any] = {}
        self._server = None
        # Pending command futures: (client_type, session_id, method) -> asyncio.Future
        self._pending_commands: Dict[tuple, asyncio.Future] = {}

    async def start(self):
        """Start the WebSocket server"""
        print(f"Starting BrowserUseServer on ws://{self.host}:{self.port}")
        print("Waiting for client connections...")
        print()

        self._server = await websockets.serve(self._handle_client, self.host, self.port)

        try:
            await asyncio.Future()  # Run forever
        except asyncio.CancelledError:
            print("\nServer shutting down...")
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(self, websocket):
        """Handle a client connection.

        The first JSON frame from each client carries the
        `session_id` (and optionally `client_type`) used for routing
        subsequent commands. We register the websocket under the
        `(client_type, session_id)` key here, then loop on messages.

        `client_type` defaults to "browser" so the legacy
        BrowserUseClient.js (which doesn't send the field) keeps
        working unchanged.
        """
        client_addr = websocket.remote_address
        session_id = None
        client_type = self._DEFAULT_CLIENT_TYPE

        print(f"[+] Client connected: {client_addr}")

        try:
            async for message in websocket:
                # Extract session_id (and client_type) from first message
                if session_id is None:
                    try:
                        request = json.loads(message)
                        session_id = (
                            request.get('params', {}).get('session_id')
                            or request.get('session_id')
                        )
                        # Top-level `client_type` wins; fall back to
                        # params.client_type for forward-compat; default
                        # to "browser" for the legacy JS client.
                        client_type = (
                            request.get('client_type')
                            or request.get('params', {}).get('client_type')
                            or self._DEFAULT_CLIENT_TYPE
                        )
                        if session_id:
                            self.clients[(client_type, session_id)] = websocket
                            print(
                                f"[+] Client registered: client_type={client_type} "
                                f"session_id={session_id}",
                            )
                    except json.JSONDecodeError:
                        pass

                session_id = await self._handle_message(
                    websocket, client_type, session_id, message,
                )
        except websockets.exceptions.ConnectionClosed:
            print(
                f"[-] Client disconnected: {client_addr}, "
                f"client_type={client_type}, session_id={session_id}",
            )
        except Exception as e:
            print(f"[!] Error handling client {client_addr}: {e}")
        finally:
            # Cancel any pending commands for this (client_type, session_id).
            for key in list(self._pending_commands.keys()):
                # key = (client_type, session_id, method)
                if key[0] == client_type and key[1] == session_id:
                    future = self._pending_commands.pop(key, None)
                    if future and not future.done():
                        future.cancel()
            reg_key = (client_type, session_id)
            if reg_key in self.clients:
                del self.clients[reg_key]

    async def _handle_message(
        self,
        websocket,
        client_type: str,
        session_id: str | None,
        message: str,
    ):
        """Process an incoming message and send response.

        Responses to server-pushed commands (`id == 0 && result`)
        resolve a pending future keyed by `(client_type, session_id,
        method)`. Everything else is treated as a client-initiated
        RPC and routed through `_handlers`.
        """
        try:
            request = json.loads(message)

            # Check if this is a response to a server-initiated command
            if request.get('id') == 0 and request.get('method') and request.get('result') is not None:
                # This is a response to our command - fulfill the pending future
                key = (client_type, session_id, request['method'])
                future = self._pending_commands.pop(key, None)
                if future and not future.done():
                    future.set_result(request['result'])  # Return just the result
                return session_id

            # Normal request from client
            response = await self._process_request(request, session_id, client_type)
            await websocket.send(json.dumps(response))
            return session_id
        except json.JSONDecodeError:
            error_response = {'id': None, 'error': 'Invalid JSON'}
            await websocket.send(json.dumps(error_response))
            return session_id
        except Exception as e:
            error_response = {'id': None, 'error': str(e)}
            await websocket.send(json.dumps(error_response))
            return session_id

    async def _process_request(
        self,
        request: Dict[str, Any],
        session_id: str = None,
        client_type: str = None,
    ) -> Dict[str, Any]:
        """Process a command request from client"""
        request_id = request.get('id')
        method = request.get('method')
        params = request.get('params', {})

        if not method:
            return {'id': request_id, 'error': 'No method specified'}

        print(
            f"[*] Received: {method}({params}) "
            f"[client_type={client_type}, session_id={session_id}]",
        )

        # Route to appropriate handler
        handler = self._handlers.get(method)
        if not handler:
            return {'id': request_id, 'error': f'Unknown command: {method}'}

        try:
            result = await handler(self, params, session_id)
            print(f"[*] Response: {result}")
            return {'id': request_id, 'result': result}
        except Exception as e:
            print(f"[!] Error: {e}")
            return {'id': request_id, 'error': str(e)}

    # ======= Command Handlers =======
    # These handle responses from BrowserUseClient

    async def _handle_update_tree_response(self, params: Dict[str, Any], session_id: str = None) -> Dict[str, Any]:
        """Handle updateTree response - returns simplified HTML"""
        return {'html': params.get('html', '<EMPTY>')}

    async def _handle_browser_state_response(self, params: Dict[str, Any], session_id: str = None) -> Dict[str, Any]:
        """Handle getBrowserState response"""
        return params

    async def _handle_element_action_response(self, params: Dict[str, Any], session_id: str = None) -> Dict[str, Any]:
        """Handle clickElement, inputText, selectOption responses"""
        return params

    async def _handle_map_response(self, params: Dict[str, Any], session_id: str = None) -> Dict[str, Any]:
        """Handle map* responses — pass-through dict (success/message + structured data)."""
        return params

    # ======= Server Commands =======
    # These send commands TO the BrowserUseClient

    def build_command(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Build a command to send to client"""
        return {
            'id': 0,
            'method': method,
            'params': params or {}
        }

    async def send_command(
        self,
        websocket,
        method: str,
        client_type: str,
        session_id: str,
        params: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Send command to client and wait for response via pending futures.

        `client_type` and `session_id` together identify the
        registration slot — both are required because the pending
        future is keyed on `(client_type, session_id, method)` and
        we need to match responses back to the right slot.
        """
        command = self.build_command(method, params)

        key = (client_type, session_id, method)
        future = asyncio.get_event_loop().create_future()
        self._pending_commands[key] = future

        try:
            print(
                f"[*] Sending command: {method}({params}) "
                f"to client_type={client_type} session_id={session_id}",
            )
            await websocket.send(json.dumps(command))

            # Wait for response (will be fulfilled by _handle_message)
            response = await asyncio.wait_for(future, timeout=self.command_timeout)
            return response
        except asyncio.TimeoutError:
            self._pending_commands.pop(key, None)
            return {'error': 'Command timed out'}
        finally:
            self._pending_commands.pop(key, None)

    def get_client(self, session_id: str, client_type: str = None):
        """Get websocket by (client_type, session_id) for targeted command delivery.

        `client_type` defaults to "browser" for backwards compatibility
        with callers that only know the session_id (the original MCP
        controller code path). When both are supplied the lookup is
        exact; otherwise we fall back to scanning across all types.
        """
        if client_type is None:
            client_type = self._DEFAULT_CLIENT_TYPE
        return self.clients.get((client_type, session_id))

    def list_sessions(self) -> list:
        """List all active (client_type, session_id) tuples."""
        return list(self.clients.keys())

    # ======= Public API for controlling browser =======
    # Use these methods with session_id to control specific browser instances

    async def update_tree(self, session_id: str, client_type: str = None) -> str:
        """Tell client to update DOM tree and return simplified HTML"""
        client_type = client_type or self._DEFAULT_CLIENT_TYPE
        try:
            websocket = self.get_client(session_id, client_type)
            if not websocket:
                return '<ERROR: No client with session_id>'
            response = await self.send_command(
                websocket, BrowserCommand.UPDATE_TREE, client_type, session_id,
            )
            # Response is directly the HTML string from client
            if isinstance(response, str):
                return response
            # Fallback for dict response
            return response.get('html', '<ERROR: Invalid response>') if isinstance(response, dict) else '<ERROR: Invalid response>'
        except Exception as e:
            print(f"[ERROR] update_tree failed: {e}")
            return f'<ERROR: {e}>'

    async def get_browser_state(self, session_id: str, client_type: str = None) -> BrowserState:
        """Get full browser state for LLM consumption"""
        client_type = client_type or self._DEFAULT_CLIENT_TYPE
        try:
            websocket = self.get_client(session_id, client_type)
            if not websocket:
                return BrowserState(url='ERROR', title='No client found', header='', content='ERROR: No client with session_id', footer='')
            response = await self.send_command(
                websocket, BrowserCommand.GET_BROWSER_STATE, client_type, session_id,
            )
            # Response is directly the state dict from client
            if isinstance(response, dict):
                return BrowserState(
                    url=response.get('url', ''),
                    title=response.get('title', ''),
                    header=response.get('header', ''),
                    content=response.get('content', ''),
                    footer=response.get('footer', '')
                )
            return BrowserState(url='ERROR', title='Invalid response', header='', content='ERROR: Invalid response from client', footer='')
        except Exception as e:
            print(f"[ERROR] get_browser_state failed: {e}")
            return BrowserState(url='ERROR', title='Exception', header='', content=f'ERROR: {e}', footer='')

    async def click_element(self, session_id: str, index: int, client_type: str = None) -> ActionResult:
        """Click element by highlight index"""
        client_type = client_type or self._DEFAULT_CLIENT_TYPE
        try:
            websocket = self.get_client(session_id, client_type)
            if not websocket:
                return ActionResult(False, f'No client with session_id: {session_id}')
            response = await self.send_command(
                websocket, BrowserCommand.CLICK_ELEMENT, client_type, session_id,
                {'index': index},
            )
            if isinstance(response, dict):
                return ActionResult(response.get('success', False), response.get('message', ''))
            return ActionResult(False, 'Invalid response')
        except Exception as e:
            print(f"[ERROR] click_element failed: {e}")
            return ActionResult(False, f'Error: {e}')

    async def input_text(self, session_id: str, index: int, text: str, client_type: str = None) -> ActionResult:
        """Input text into element by highlight index"""
        client_type = client_type or self._DEFAULT_CLIENT_TYPE
        try:
            websocket = self.get_client(session_id, client_type)
            if not websocket:
                return ActionResult(False, f'No client with session_id: {session_id}')
            response = await self.send_command(
                websocket, BrowserCommand.INPUT_TEXT, client_type, session_id,
                {'index': index, 'text': text},
            )
            if isinstance(response, dict):
                return ActionResult(response.get('success', False), response.get('message', ''))
            return ActionResult(False, 'Invalid response')
        except Exception as e:
            print(f"[ERROR] input_text failed: {e}")
            return ActionResult(False, f'Error: {e}')

    async def select_option(self, session_id: str, index: int, optionText: str, client_type: str = None) -> ActionResult:
        """Select option in dropdown by highlight index"""
        client_type = client_type or self._DEFAULT_CLIENT_TYPE
        try:
            websocket = self.get_client(session_id, client_type)
            if not websocket:
                return ActionResult(False, f'No client with session_id: {session_id}')
            response = await self.send_command(
                websocket, BrowserCommand.SELECT_OPTION, client_type, session_id,
                {'index': index, 'optionText': optionText},
            )
            if isinstance(response, dict):
                return ActionResult(response.get('success', False), response.get('message', ''))
            return ActionResult(False, 'Invalid response')
        except Exception as e:
            print(f"[ERROR] select_option failed: {e}")
            return ActionResult(False, f'Error: {e}')

    async def scroll(
        self,
        session_id: str,
        down: bool = True,
        numPages: float = 1,
        index: int = None,
        client_type: str = None,
    ) -> ActionResult:
        """Scroll vertically"""
        client_type = client_type or self._DEFAULT_CLIENT_TYPE
        try:
            websocket = self.get_client(session_id, client_type)
            if not websocket:
                return ActionResult(False, f'No client with session_id: {session_id}')
            params = {'down': down, 'numPages': numPages}
            if index is not None:
                params['index'] = index
            response = await self.send_command(
                websocket, BrowserCommand.SCROLL, client_type, session_id, params,
            )
            if isinstance(response, dict):
                return ActionResult(response.get('success', False), response.get('message', ''))
            return ActionResult(False, 'Invalid response')
        except Exception as e:
            print(f"[ERROR] scroll failed: {e}")
            return ActionResult(False, f'Error: {e}')

    async def scroll_horizontally(
        self,
        session_id: str,
        right: bool = True,
        pixels: int = 800,
        index: int = None,
        client_type: str = None,
    ) -> ActionResult:
        """Scroll horizontally"""
        client_type = client_type or self._DEFAULT_CLIENT_TYPE
        try:
            websocket = self.get_client(session_id, client_type)
            if not websocket:
                return ActionResult(False, f'No client with session_id: {session_id}')
            params = {'right': right, 'pixels': pixels}
            if index is not None:
                params['index'] = index
            response = await self.send_command(
                websocket, BrowserCommand.SCROLL_HORIZONTALLY, client_type, session_id, params,
            )
            if isinstance(response, dict):
                return ActionResult(response.get('success', False), response.get('message', ''))
            return ActionResult(False, 'Invalid response')
        except Exception as e:
            print(f"[ERROR] scroll_horizontally failed: {e}")
            return ActionResult(False, f'Error: {e}')

    async def execute_javascript(self, session_id: str, script: str, client_type: str = None) -> ActionResult:
        """Execute arbitrary JavaScript"""
        client_type = client_type or self._DEFAULT_CLIENT_TYPE
        try:
            websocket = self.get_client(session_id, client_type)
            if not websocket:
                return ActionResult(False, f'No client with session_id: {session_id}')
            response = await self.send_command(
                websocket, BrowserCommand.EXECUTE_JAVASCRIPT, client_type, session_id,
                {'script': script},
            )
            if isinstance(response, dict):
                return ActionResult(response.get('success', False), response.get('message', ''))
            return ActionResult(False, 'Invalid response')
        except Exception as e:
            print(f"[ERROR] execute_javascript failed: {e}")
            return ActionResult(False, f'Error: {e}')

    # ======= Map-specific public API =======
    # These target the BrowserUseClient running inside map.html and forward
    # structured `{success, message, ...}` dicts straight from the client.

    async def _map_call(
        self,
        session_id: str,
        method: str,
        params: Dict[str, Any],
        client_type: str = None,
    ) -> Dict[str, Any]:
        """Shared helper: forward a map command to the client and return its dict response.

        `client_type` defaults to "browser" for backwards compatibility
        with the original MCP controller (which only knew about browser
        sessions). Pass `client_type="wxmp"` to route to a WeChat Mini
        Program session.
        """
        client_type = client_type or self._DEFAULT_CLIENT_TYPE
        try:
            websocket = self.get_client(session_id, client_type)
            if not websocket:
                return {
                    'success': False,
                    'message': (
                        f'No client for client_type={client_type} '
                        f'session_id={session_id}'
                    ),
                }
            response = await self.send_command(
                websocket, method, client_type, session_id, params,
            )
            if isinstance(response, dict):
                return response
            return {'success': False, 'message': f'Unexpected response: {response}'}
        except Exception as e:
            print(f"[ERROR] map call {method} failed: {e}")
            return {'success': False, 'message': f'Error: {e}'}

    async def map_run_search(self, session_id: str, keyword: str, client_type: str = None) -> Dict[str, Any]:
        """Trigger AMap POI search."""
        return await self._map_call(session_id, BrowserCommand.MAP_RUN_SEARCH, {'keyword': keyword}, client_type)

    async def map_search_and_zoom(self, session_id: str, keyword: str, zoom: int = 15, client_type: str = None) -> Dict[str, Any]:
        """Trigger POI search and set the zoom level."""
        return await self._map_call(
            session_id, BrowserCommand.MAP_SEARCH_AND_ZOOM, {'keyword': keyword, 'zoom': zoom}, client_type,
        )

    async def map_get_state(self, session_id: str, client_type: str = None) -> Dict[str, Any]:
        """Get current map state (center, zoom, bounds, overlayCount, infoText)."""
        return await self._map_call(session_id, BrowserCommand.MAP_GET_STATE, {}, client_type)

    async def map_set_center(self, session_id: str, lng: float, lat: float, client_type: str = None) -> Dict[str, Any]:
        """Re-center the map to (lng, lat)."""
        return await self._map_call(
            session_id, BrowserCommand.MAP_SET_CENTER, {'lng': lng, 'lat': lat}, client_type,
        )

    async def map_set_zoom(self, session_id: str, zoom: int, client_type: str = None) -> Dict[str, Any]:
        """Set the map's zoom level."""
        return await self._map_call(
            session_id, BrowserCommand.MAP_SET_ZOOM, {'zoom': zoom}, client_type,
        )

    async def map_zoom_in(self, session_id: str, client_type: str = None) -> Dict[str, Any]:
        """Zoom in one level."""
        return await self._map_call(session_id, BrowserCommand.MAP_ZOOM_IN, {}, client_type)

    async def map_zoom_out(self, session_id: str, client_type: str = None) -> Dict[str, Any]:
        """Zoom out one level."""
        return await self._map_call(session_id, BrowserCommand.MAP_ZOOM_OUT, {}, client_type)

    async def map_add_marker(self, session_id: str, lng: float, lat: float, title: str = '', client_type: str = None) -> Dict[str, Any]:
        """Add a marker at (lng, lat) with optional title."""
        return await self._map_call(
            session_id, BrowserCommand.MAP_ADD_MARKER, {'lng': lng, 'lat': lat, 'title': title}, client_type,
        )

    async def map_add_marker_with_info(
        self,
        session_id: str,
        lng: float,
        lat: float,
        title: str = '',
        info_html: Optional[str] = None,
        poi: Optional[Dict[str, Any]] = None,
        client_type: str = None,
    ) -> Dict[str, Any]:
        """Add a clickable marker that opens a rich info window on click.

        Either `info_html` (pre-formatted HTML) OR `poi` (an AMap POI object
        from `map_search_nearby`) must be provided. If both are present,
        `info_html` wins. If neither is provided, the click shows a minimal
        text card with just the `title`.

        Only one info window is visible at a time — clicking a new marker
        closes the previous one.
        """
        params: Dict[str, Any] = {'lng': lng, 'lat': lat, 'title': title}
        if info_html is not None:
            params['info_html'] = info_html
        if poi is not None:
            params['poi'] = poi
        return await self._map_call(
            session_id, BrowserCommand.MAP_ADD_MARKER_WITH_INFO, params, client_type,
        )

    async def map_clear_markers(self, session_id: str, client_type: str = None) -> Dict[str, Any]:
        """Clear all markers on the map."""
        return await self._map_call(session_id, BrowserCommand.MAP_CLEAR_MARKERS, {}, client_type)

    async def map_locate(self, session_id: str, client_type: str = None) -> Dict[str, Any]:
        """Trigger browser geolocation."""
        return await self._map_call(session_id, BrowserCommand.MAP_LOCATE, {}, client_type)

    async def map_draw_polyline(
        self,
        session_id: str,
        path: list,
        options: Optional[Dict[str, Any]] = None,
        client_type: str = None,
    ) -> Dict[str, Any]:
        """Draw a polyline (route) through the given points.

        `path` is a list of [lng, lat] or {lng, lat} points.
        `options` may include {color, width, opacity, showDir}.
        """
        return await self._map_call(
            session_id,
            BrowserCommand.MAP_DRAW_POLYLINE,
            {'path': path, 'options': options or {}},
            client_type,
        )

    async def map_draw_polygon(
        self,
        session_id: str,
        path: list,
        options: Optional[Dict[str, Any]] = None,
        client_type: str = None,
    ) -> Dict[str, Any]:
        """Draw a polygon (area) with the given vertices."""
        return await self._map_call(
            session_id,
            BrowserCommand.MAP_DRAW_POLYGON,
            {'path': path, 'options': options or {}},
            client_type,
        )

    async def map_draw_circle(
        self,
        session_id: str,
        lng: float,
        lat: float,
        radius: float,
        options: Optional[Dict[str, Any]] = None,
        client_type: str = None,
    ) -> Dict[str, Any]:
        """Draw a circle centered at (lng, lat) with given radius (meters)."""
        return await self._map_call(
            session_id,
            BrowserCommand.MAP_DRAW_CIRCLE,
            {'lng': lng, 'lat': lat, 'radius': radius, 'options': options or {}},
            client_type,
        )

    async def map_open_info_window(
        self,
        session_id: str,
        lng: float,
        lat: float,
        content: str,
        client_type: str = None,
    ) -> Dict[str, Any]:
        """Open an AMap info window at (lng, lat) with the given HTML/text content."""
        return await self._map_call(
            session_id,
            BrowserCommand.MAP_OPEN_INFO_WINDOW,
            {'lng': lng, 'lat': lat, 'content': content},
            client_type,
        )

    async def map_close_info_window(self, session_id: str, client_type: str = None) -> Dict[str, Any]:
        """Close the currently open info window."""
        return await self._map_call(session_id, BrowserCommand.MAP_CLOSE_INFO_WINDOW, {}, client_type)

    async def map_fit_view(self, session_id: str, client_type: str = None) -> Dict[str, Any]:
        """Auto-fit the map view to show all overlays."""
        return await self._map_call(session_id, BrowserCommand.MAP_FIT_VIEW, {}, client_type)

    async def map_clear_overlays(
        self, session_id: str, type: str = 'all', client_type: str = None,
    ) -> Dict[str, Any]:
        """Clear overlays: 'all' | 'shape' | 'polyline' | 'polygon' | 'circle' | 'marker'."""
        return await self._map_call(
            session_id, BrowserCommand.MAP_CLEAR_OVERLAYS, {'type': type}, client_type,
        )

    async def map_remove_overlay(
        self, session_id: str, type: str, index: int, client_type: str = None,
    ) -> Dict[str, Any]:
        """Remove a single overlay at the given index in the named bucket."""
        return await self._map_call(
            session_id,
            BrowserCommand.MAP_REMOVE_OVERLAY,
            {'type': type, 'index': index},
            client_type,
        )

    async def map_geocode(
        self,
        session_id: str,
        address: str,
        city: Optional[str] = None,
        client_type: str = None,
    ) -> Dict[str, Any]:
        """Geocode `address` (optionally biased by `city`) → (lng, lat, formatted_address)."""
        params: Dict[str, Any] = {'address': address}
        if city:
            params['city'] = city
        return await self._map_call(session_id, BrowserCommand.MAP_GEOCODE, params, client_type)

    async def map_list_overlays(self, session_id: str, client_type: str = None) -> Dict[str, Any]:
        """List overlay counts (polyline/polygon/circle/markers) and info-window state."""
        return await self._map_call(session_id, BrowserCommand.MAP_LIST_OVERLAYS, {}, client_type)

    async def map_search_nearby(
        self,
        session_id: str,
        lng: float,
        lat: float,
        radius: float,
        type: Optional[str] = None,
        keyword: Optional[str] = None,
        city: Optional[str] = None,
        exclude_keywords: Optional[list] = None,
        include_keywords: Optional[list] = None,
        client_type: str = None,
    ) -> Dict[str, Any]:
        """POI nearby search via AMap PlaceSearch (type + location + radius).

        Args:
            session_id: target browser session.
            lng, lat: center of the search circle.
            radius: meters.
            type: optional AMap POI category code (e.g. "141201" = 高中).
            keyword: optional free-text filter ("" to search by category alone).
            city: optional city bias (defaults to "全国" on the client side).
            exclude_keywords: optional list (or comma-string) of substrings.
                POIs whose `name` contains any of these are dropped.
                E.g. ["驾校","培训","复读"] when searching for 高中.
            include_keywords: optional list (or comma-string) of substrings.
                If non-empty, only POIs whose `name` contains at least one
                of these are kept.

        Returns: { success, count, total_before_filter, filtered_out,
                  excluded_by_keyword: {kw: count, ...},
                  pois: [...] } sorted by distance asc.
        """
        params: Dict[str, Any] = {
            "lng": lng,
            "lat": lat,
            "radius": radius,
        }
        if type:
            params["type"] = type
        if keyword:
            params["keyword"] = keyword
        if city:
            params["city"] = city
        if exclude_keywords:
            params["exclude_keywords"] = exclude_keywords
        if include_keywords:
            params["include_keywords"] = include_keywords
        return await self._map_call(session_id, BrowserCommand.MAP_SEARCH_NEARBY, params, client_type)

    async def get_current_url(self, session_id: str, client_type: str = None) -> str:
        """Get current page URL"""
        client_type = client_type or self._DEFAULT_CLIENT_TYPE
        try:
            websocket = self.get_client(session_id, client_type)
            if not websocket:
                return 'ERROR: No client with session_id'
            response = await self.send_command(
                websocket, BrowserCommand.GET_CURRENT_URL, client_type, session_id,
            )
            if isinstance(response, str):
                return response
            return str(response) if response else 'ERROR: Empty response'
        except Exception as e:
            print(f"[ERROR] get_current_url failed: {e}")
            return f'ERROR: {e}'

    # Handler map - handlers now receive (self, params, session_id)
    _handlers = {
        BrowserCommand.UPDATE_TREE: _handle_update_tree_response,
        BrowserCommand.GET_BROWSER_STATE: _handle_browser_state_response,
        BrowserCommand.CLICK_ELEMENT: _handle_element_action_response,
        BrowserCommand.INPUT_TEXT: _handle_element_action_response,
        BrowserCommand.SELECT_OPTION: _handle_element_action_response,
        BrowserCommand.SCROLL: _handle_element_action_response,
        BrowserCommand.SCROLL_HORIZONTALLY: _handle_element_action_response,
        BrowserCommand.EXECUTE_JAVASCRIPT: _handle_element_action_response,
        BrowserCommand.GET_CURRENT_URL: lambda self, p, sid: p.get('url', ''),
        BrowserCommand.GET_LAST_UPDATE_TIME: lambda self, p, sid: p.get('timestamp', 0),
        BrowserCommand.CLEAN_UP_HIGHLIGHTS: lambda self, p, sid: {'success': True, 'message': 'Cleaned up'},
        # Map commands (window.__map on map.html)
        BrowserCommand.MAP_RUN_SEARCH: _handle_map_response,
        BrowserCommand.MAP_GET_STATE: _handle_map_response,
        BrowserCommand.MAP_SET_CENTER: _handle_map_response,
        BrowserCommand.MAP_SET_ZOOM: _handle_map_response,
        BrowserCommand.MAP_ZOOM_IN: _handle_map_response,
        BrowserCommand.MAP_ZOOM_OUT: _handle_map_response,
        BrowserCommand.MAP_ADD_MARKER: _handle_map_response,
        BrowserCommand.MAP_CLEAR_MARKERS: _handle_map_response,
        BrowserCommand.MAP_LOCATE: _handle_map_response,
        BrowserCommand.MAP_SEARCH_AND_ZOOM: _handle_map_response,
        BrowserCommand.MAP_DRAW_POLYLINE: _handle_map_response,
        BrowserCommand.MAP_DRAW_POLYGON: _handle_map_response,
        BrowserCommand.MAP_DRAW_CIRCLE: _handle_map_response,
        BrowserCommand.MAP_OPEN_INFO_WINDOW: _handle_map_response,
        BrowserCommand.MAP_CLOSE_INFO_WINDOW: _handle_map_response,
        BrowserCommand.MAP_FIT_VIEW: _handle_map_response,
        BrowserCommand.MAP_CLEAR_OVERLAYS: _handle_map_response,
        BrowserCommand.MAP_REMOVE_OVERLAY: _handle_map_response,
        BrowserCommand.MAP_GEOCODE: _handle_map_response,
        BrowserCommand.MAP_LIST_OVERLAYS: _handle_map_response,
    }


async def main():
    parser = argparse.ArgumentParser(description='BrowserUseServer - Browser control via WebSocket')
    parser.add_argument('--host', default='localhost', help='Host to bind to (default: localhost)')
    parser.add_argument('--port', type=int, default=8765, help='Port to listen on (default: 8765)')
    parser.add_argument(
        '--command-timeout', type=float, default=60.0,
        help='Per-command timeout in seconds (default: 60)',
    )
    args = parser.parse_args()
    server = BrowserUseServer(
        host=args.host, port=args.port, command_timeout=args.command_timeout,
    )
    await server.start()


if __name__ == '__main__':
    print("=" * 60)
    print("BrowserUseServer - Browser Automation via WebSocket")
    print("=" * 60)
    print()
    print("Dependencies:")
    print("  pip install websockets")
    print()
    print("Usage:")
    print("  # Start server")
    print("  python BrowserUseServer.py --port 8765")
    print()
    print("  # In browser page, load and connect:")
    print("  <script src='BrowserUseClient.js'></script>")
    print("  <script>")
    print("    const client = new BrowserUseClient('ws://localhost:8765');")
    print("    await client.connect();")
    print("  </script>")
    print()
    print("  # From Python, send commands to control browser (via session_id):")
    print("  sessions = server.list_sessions()  # Get all connected clients")
    print("  if sessions:")
    print("      session_id = sessions[0]")
    print("      html = await server.update_tree(session_id)")
    print("      state = await server.get_browser_state(session_id)")
    print("      result = await server.click_element(session_id, 5)")
    print()
    print("-" * 60)
    print()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)