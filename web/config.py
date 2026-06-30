"""Server-side configuration for the web UI.

Loads all secrets from `web/.env` (gitignored) via pydantic-settings.
The browser only ever sees the AMap key (which the AMap JS API needs
in-browser) — the LLM key, base URL, and model name stay on the
server and are injected into `RemoteBrowserUseAgent` at construction.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# web/.env is the single source of truth for runtime config. If the
# file is missing, pydantic-settings will raise on first read of a
# required field — which is what we want (fail fast at startup).
_ENV_FILE = Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    """All knobs the web app needs. See `web/.env.example` for defaults."""

    # --- AMap (browser-side; safe to expose via Jinja) ---
    amap_key: str = Field(..., description="AMap JS API key (browser-visible).")
    amap_security_code: str = Field(..., description="AMap securityJsCode (browser-visible).")

    # --- LLM (server-side only; never sent to the browser) ---
    llm_api_key: str = Field(..., description="OpenAI-compatible API key.")
    llm_base_url: str = Field(..., description="OpenAI-compatible base URL.")
    llm_model_name: str = Field(default="MiniMax-M2.7", description="Model name.")
    llm_reasoning_effort: str | None = Field(
        default=None,
        description=(
            "Reasoning depth for reasoning-capable models (e.g. deepseek-v4-flash, "
            "o3, gpt-5.x). One of: minimal, low, medium, high, xhigh. "
            "Empty / unset / 'off' / 'none' keeps thinking disabled and "
            "the request payload identical to pre-knob behaviour. The "
            "model must natively support reasoning for this to have any "
            "effect — non-reasoning models ignore it."
        ),
    )

    # --- Backend ports (bound to 127.0.0.1; not externally reachable) ---
    browser_use_host: str = Field(default="127.0.0.1")
    browser_use_port: int = Field(default=8765, description="BrowserUseServer WebSocket.")
    mcp_host: str = Field(default="127.0.0.1")
    mcp_port: int = Field(default=8766, description="MCP streamable-http port.")

    # --- Web (public-facing) ---
    web_host: str = Field(default="0.0.0.0", description="uvicorn bind host.")
    web_port: int = Field(default=8000, description="uvicorn bind port.")
    web_public_origin: str = Field(
        default="http://localhost:8000",
        description="Allowed Origin header for WS endpoints (CSRF defense).",
    )

    # --- Misc ---
    command_timeout: float = Field(
        default=60.0,
        description="Per-command timeout for BrowserUseServer (seconds).",
    )
    mcp_ready_timeout: float = Field(
        default=5.0,
        description="How long to wait for MCP port to bind on startup.",
    )
    user_token_secret: str = Field(
        default="change-me-in-prod-please",
        description="HMAC secret for /api/session user_token signing.",
    )

    # --- WeChat Mini Program (server-side only; never sent to the browser) ---
    wx_app_id: str = Field(
        default="",
        description=(
            "WeChat Mini Program AppID. Get from https://mp.weixin.qq.com/. "
            "Required to enable POST /api/wx-login (the wxmp client calls "
            "wx.login() and posts the resulting code here for openid exchange). "
            "Leave empty to disable wx-login; the browser path is unaffected."
        ),
    )
    wx_app_secret: str = Field(
        default="",
        description=(
            "WeChat Mini Program AppSecret. Paired with wx_app_id. Treat as "
            "a high-value secret — anyone with both can mint openids for your app."
        ),
    )
    wx_login_timeout: float = Field(
        default=5.0,
        description="HTTP timeout (seconds) for the upstream jscode2session call.",
    )
    amap_wx_key: str = Field(
        default="",
        description=(
            "AMap Mini Program SDK key, served to the wxmp client via "
            "GET /api/wx-config so the key never has to be hardcoded in "
            "the wxmp source (which is trivially decompilable from the "
            ".wxapkg). Get the key from https://lbs.amap.com/ — it's a "
            "separate 'Mini Program' key, scoped to your wxmp AppID."
        ),
    )
    wx_ws_origin_allow_pattern: str = Field(
        default=r"^https://servicewechat\.com/",
        description=(
            "Regex matched against the WebSocket `Origin` header to "
            "allow WeChat Mini Program clients. The wxmp runs inside "
            "the WeChat sandbox and always sends an `Origin` like "
            "`https://servicewechat.com/<APPID>/<version>/<runtime>` "
            "— this doesn't match the regular `WEB_PUBLIC_ORIGIN` "
            "allowlist, so we use a separate regex pattern. "
            "Set to empty string to disable."
        ),
    )

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_config() -> Settings:
    """Process-wide singleton — loaded once on first call.

    Cached so tests / hot-reload don't re-read the file. The agent
    layer also receives a snapshot of this object so config is fully
    captured at agent construction (no env-var drift mid-session).
    """
    return Settings()  # type: ignore[call-arg]
