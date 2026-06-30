"""Chat Map web UI — FastAPI gateway.

Single-port (default :8000) gateway that wraps the existing
`BrowserUseServerMCPController` + `RemoteBrowserUseAgent` and exposes
a left-map / right-chat UI. All secrets live in `web/.env`; the
browser never receives LLM keys, only the AMap JS API key (which the
AMap SDK requires in-browser to load tiles).
"""

__version__ = "0.1.0"
