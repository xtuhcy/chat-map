# WeChat Mini Program (wxmp) Client

Standalone WeChat Mini Program that talks to the same chat-map backend
as the browser client. Uses **real WeChat identity** (`openid` from
`wx.login()`) as the `user_token`, which gives the system a real user
identity for routing and quota — and fully solves the multi-tab / NAT
multi-user session-id collision that the browser client can't.

## File map

| Path | Purpose |
|---|---|
| `app.js` | Login flow: `wx.login()` → POST `/api/wx-login` → cache `user_token` |
| `app.json` | Mini Program config (pages, window, location permission) |
| `app.wxss` | Global styles (shared by all pages) |
| `pages/index/index.{js,json,wxml,wxss}` | Main split-screen page (map + chat) |
| `utils/uuidv5.js` | Port of `client/BrowserUseClient.js:_uuidv5` for session_id generation |
| `utils/ws_client.js` | `wx.connectSocket` wrapper with exponential-backoff reconnect |
| `utils/markdown.js` | Minimal markdown → rich-text nodes (no `marked` in wxmp) |
| `utils/WxmpMapClient.js` | Map execution client (server-pushed `map_*` commands → `<map>` data) |

## Setup (one-time)

1. **Download the AMap Mini Program SDK** from
   <https://lbs.amap.com/api/wx/gettingstarted> and place it at
   `wxmp/utils/amap-wx.js`. The SDK uses a **named export**
   (`module.exports.AMapWX = AMapWX`), so import it as:
   ```js
   const { AMapWX } = require('../../utils/amap-wx.js');
   const amapSdk = new AMapWX({ key: 'YOUR_AMAP_WX_KEY' });
   ```
   ⚠️ **Don't** write `const AMapWX = require(...)` — that gives you
   the module wrapper object, not the class, and `new AMapWX({...})`
   will throw `TypeError: AMapWX is not a constructor`. Pass
   `amapSdk` into `new WxmpMapClient({ ..., amapSdk })`. The WX key
   is a *separate* key from the browser JS API key, scoped to your
   Mini Program's AppID.
2. **Edit `app.js`** and set `SERVER_BASE` to your chat-map backend
   (e.g. `https://chat-map.example.com`).
3. **Add your server domain** to the Mini Program management backend:
   - 开发管理 → 开发设置 → 服务器域名:
     - `request合法域名`: add `https://chat-map.example.com`
     - `socket合法域名`: add `wss://chat-map.example.com`
   - (Dev tools allow `不校验合法域名` to skip this for local testing.)
4. **Set `WX_APP_ID` / `WX_APP_SECRET`** in the backend's
   `web/.env` to your Mini Program's AppID and AppSecret.

## Run

Open WeChat DevTools → 导入项目 → select the `wxmp/` directory.
Click 编译 to launch in the simulator. Use 真机调试 to test on a
phone.

## Differences from the browser client

| | Browser (web/) | WeChat Mini Program (wxmp/) |
|---|---|---|
| Identity | Host-bucket hash (anonymous) | WeChat `openid` (real user) |
| Login | `GET /api/session` | `wx.login()` → `POST /api/wx-login` |
| Map rendering | Browser AMap JS SDK + DOM | wxmp `<map>` component + AMap WX SDK |
| `client_type` | `browser` (default) | `wxmp` |
| Session_id derivation | `UUIDv5(url + "\|" + token)` | `UUIDv5(url + "\|" + token)` (same) |
| DOM tools (`click_element`, `scroll`, ...) | ✅ | ❌ rejected by MCP server |
| `map_*` tools | ✅ | ✅ |

## Authentication flow

```
[wxmp]            [chat-map backend]            [WeChat API]
  │  wx.login()       │                            │
  │ ──────────────────┼───────────────────────────> │
  │ <code>            │                            │
  │                   │                            │
  │ POST /api/wx-login {code}                       │
  │ ─────────────────> │                            │
  │                   │ GET jscode2session          │
  │                   │ ─────────────────────────> │
  │                   │ <{openid, session_key}>     │
  │                   │                            │
  │ <{user_token, openid}>                         │
  │  (token = HMAC(openid))                         │
  │                   │                            │
  │ WebSocket /ws/chat + /ws/browser                │
  │ <══════════════════════════════════════════════> │
```

The downstream chain treats `user_token` as opaque, so once the
wx-login endpoint mints a real openid-based token, the rest of the
stack (`AgentSession` → `RemoteBrowserUseAgent` → MCP server) needs
no change.