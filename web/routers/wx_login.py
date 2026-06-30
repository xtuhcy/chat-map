"""WeChat Mini Program login endpoint: POST /api/wx-login.

Flow:
  1. wxmp client calls `wx.login()` → gets a one-time `code`.
  2. wxmp POSTs `{code}` here.
  3. We exchange the code for `openid` + `session_key` via
     https://api.weixin.qq.com/sns/jscode2session (using WX_APP_ID /
     WX_APP_SECRET from web/.env).
  4. We HMAC-sign the `openid` with the existing user_token scheme
     and return `{user_token, openid}`. The downstream chain
     (`AgentSession` → MCP server) treats `user_token` as an opaque
     string, so this is the *only* place that needs to know the
     token is now a real WeChat identity instead of a host-bucket hash.

This endpoint is intentionally **separate** from `/api/session` (which
the browser still uses for its anonymous host-bucket token). The two
flows coexist — the web client keeps its existing auth, the wxmp
client gets real WeChat identity.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from web.config import get_config
from web.routers.session import _sign_user_token

logger = logging.getLogger("web.wx_login")
router = APIRouter()


# WeChat's jscode2session endpoint — must be called server-side because
# it requires the AppSecret which must never leave the server.
_JSCODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"


class WxLoginRequest(BaseModel):
    """Request body for POST /api/wx-login.

    `code` is the one-time credential returned by `wx.login()` on the
    client. It is single-use and short-lived (~5 min) — the wxmp
    client must exchange it promptly.
    """

    code: str = Field(..., min_length=1, description="wx.login() jscode.")


@router.post("/api/wx-login")
async def wx_login(req: WxLoginRequest) -> JSONResponse:
    cfg = get_config()

    # Refuse early if WeChat is not configured — the wxmp client will
    # surface a friendly error and the browser path is unaffected.
    if not cfg.wx_app_id or not cfg.wx_app_secret:
        logger.warning(
            "/api/wx-login called but WX_APP_ID / WX_APP_SECRET are unset",
        )
        return JSONResponse(
            {
                "error": "wx-login-not-configured",
                "message": (
                    "Server is missing WX_APP_ID / WX_APP_SECRET. "
                    "Set them in web/.env and restart."
                ),
            },
            status_code=503,
        )

    # Exchange the code for openid + session_key. We do this with
    # httpx (already a transitive dep; declared explicitly in
    # pyproject.toml's web extra) and a tight timeout so a hung
    # upstream can't pin the event loop.
    params = {
        "appid": cfg.wx_app_id,
        "secret": cfg.wx_app_secret,
        "js_code": req.code,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=cfg.wx_login_timeout) as client:
            resp = await client.get(_JSCODE2SESSION_URL, params=params)
    except httpx.HTTPError as e:
        logger.error("jscode2session network error: %s", e)
        return JSONResponse(
            {"error": "upstream-unreachable", "message": str(e)},
            status_code=502,
        )

    # WeChat returns JSON: { openid, session_key, unionid?, errcode, errmsg }.
    # `errcode == 0` and `errmsg == "ok"` means success.
    try:
        body = resp.json()
    except ValueError:
        logger.error(
            "jscode2session non-JSON response: status=%d body=%r",
            resp.status_code, resp.text[:200],
        )
        return JSONResponse(
            {"error": "upstream-bad-response", "message": "non-JSON from WeChat"},
            status_code=502,
        )

    errcode = body.get("errcode", 0)
    # WeChat historically used errcode=0 for success; some endpoints
    # also use the absence of `openid` as the failure marker. Be
    # defensive on both.
    if errcode != 0 or "openid" not in body:
        logger.warning(
            "jscode2session rejected code: errcode=%s errmsg=%s",
            errcode, body.get("errmsg"),
        )
        return JSONResponse(
            {
                "error": "wechat-rejected",
                "errcode": errcode,
                "errmsg": body.get("errmsg", ""),
            },
            status_code=400,
        )

    openid: str = body["openid"]
    # `session_key` is sensitive — never log it, never return it.
    # It's only useful to the server if we later want to decrypt
    # encrypted wxmp data (e.g. getUserProfile). We don't use it
    # yet, so drop it on the floor here.
    user_token = _sign_user_token(cfg, openid)

    logger.info(
        "/api/wx-login ok: openid_len=%d token_len=%d",
        len(openid), len(user_token),
    )
    return JSONResponse(
        {
            "user_token": user_token,
            # Echo the openid back to the client for display / debugging.
            # This is the *same* value embedded in the user_token —
            # anyone who can verify the HMAC can already extract it,
            # so this isn't an info leak.
            "openid": openid,
        },
    )