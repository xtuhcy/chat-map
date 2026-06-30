"""Public-by-design config endpoint for the wxmp client.

The wxmp client (WeChat Mini Program) is a *decompilable* runtime
— its `.wxapkg` package can be unpacked and the source read
trivially. Any key shipped inside the wxmp package is effectively
public. So the wxmp pulls all sensitive configuration from this
endpoint on launch.

Currently exposes:
  * `amap_wx_key` — AMap Mini Program SDK key for `getGeo`,
    `getPoiAround`, `getRegeo`, etc. (see wxmp/utils/WxmpMapClient.js)

If the value is unset (empty string), the wxmp falls back to no
SDK (map_* tools that need AMap will return
`{success: false, message: 'amapSdk not provided'}`).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from web.config import get_config

router = APIRouter()


@router.get("/api/wx-config")
async def wx_config() -> JSONResponse:
    cfg = get_config()
    return JSONResponse(
        {
            # Public to anyone who can reach the server. This is fine
            # because the key is *scoped* to the wxmp AppID in the AMap
            # console — a leaked key only costs the wxmp author's
            # quota, not arbitrary third parties.
            "amap_wx_key": cfg.amap_wx_key or "",
        }
    )