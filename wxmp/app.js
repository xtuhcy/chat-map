// app.js — Mini Program entry point.
//
// On launch:
//   1. wx.login() → get a one-time `code`.
//   2. POST {code} to <server>/api/wx-login → exchange for {user_token, openid}.
//   3. Cache user_token in wx.setStorageSync so page-level code can read it.
//
// We do NOT cache the user_token across app launches by default — the
// token is bound to a specific openid + secret, so it's safe to keep,
// but the page re-derives session state from /api/wx-login on each
// launch in case the server config changed.
const { uuidv5 } = require('./utils/uuidv5.js');

// IMPORTANT: replace this with your server's actual address.
// WeChat requires the domain to be in the request legal domains list
// on the Mini Program management backend.
const SERVER_BASE = 'http://127.0.0.1:8000';

App({
  globalData: {
    serverBase: SERVER_BASE,
    userToken: null,
    openid: null,
    // AMap Mini Program SDK key, fetched from GET /api/wx-config
    // on launch. NEVER hardcode in this codebase — the .wxapkg is
    // trivially decompilable. Empty string if the backend doesn't
    // expose AMAP_WX_KEY, in which case map_* tools that need AMap
    // return `{success: false, message: 'amapSdk not provided'}`.
    amapWxKey: '',
  },

  onLaunch() {
    this.doWxLogin();
    this.doLoadWxConfig();
  },

  // Pull server-side config (currently just the AMap Mini Program
  // key) so the wxmp source never has to contain it. Best-effort:
  // on any failure we keep `amapWxKey` as '' and the UI falls back
  // to "AMap SDK not loaded" mode.
  async doLoadWxConfig() {
    try {
      const resp = await new Promise((resolve, reject) => {
        wx.request({
          url: this.globalData.serverBase + '/api/wx-config',
          method: 'GET',
          timeout: 5000,
          success: (r) => {
            if (r.statusCode >= 200 && r.statusCode < 300) resolve(r.data);
            else reject(new Error(`wx-config ${r.statusCode}: ${JSON.stringify(r.data)}`));
          },
          fail: reject,
        });
      });
      this.globalData.amapWxKey = (resp && resp.amap_wx_key) || '';
      if (!this.globalData.amapWxKey) {
        console.warn('[app] /api/wx-config returned no amap_wx_key — map_* tools that need AMap will be unavailable');
      } else {
        console.log('[app] /api/wx-config ok');
      }
    } catch (e) {
      console.error('[app] /api/wx-config failed:', e);
      // No modal here — the page-level SDK loader will surface a
      // user-visible error if the key is actually needed.
    }
  },

  async doWxLogin() {
    try {
      const loginRes = await new Promise((resolve, reject) => {
        wx.login({
          success: resolve,
          fail: reject,
        });
      });
      if (!loginRes.code) {
        throw new Error('wx.login returned no code');
      }

      const resp = await new Promise((resolve, reject) => {
        wx.request({
          url: this.globalData.serverBase + '/api/wx-login',
          method: 'POST',
          data: { code: loginRes.code },
          header: { 'content-type': 'application/json' },
          // 5s is plenty for jscode2session round-trip; default
          // 60s is too long to wait when something is misconfigured.
          timeout: 5000,
          success: (r) => {
            if (r.statusCode >= 200 && r.statusCode < 300) resolve(r.data);
            else reject(new Error(`wx-login ${r.statusCode}: ${JSON.stringify(r.data)}`));
          },
          fail: reject,
        });
      });

      this.globalData.userToken = resp.user_token;
      this.globalData.openid = resp.openid;
      try { wx.setStorageSync('chat_map_user_token', resp.user_token); } catch (e) {}
      console.log('[app] wx-login ok, openid_len=' + (resp.openid || '').length);
    } catch (e) {
      // wx.request's fail callback and wx.login's fail callback
      // return `{errMsg, errno?, ...}` plain objects — NOT Error
      // instances. So `e.message` is undefined and String(e) yields
      // "[object Object]". Inspect several known fields.
      console.error('[app] wx-login failed (raw):', e);
      let msg = '';
      if (e == null) {
        msg = 'unknown error (e is null)';
      } else if (typeof e === 'string') {
        msg = e;
      } else if (e.errMsg) {
        msg = e.errMsg;
        if (e.errno) msg += ` (errno=${e.errno})`;
      } else if (e.message) {
        msg = e.message;
      } else {
        try { msg = JSON.stringify(e); } catch (_) { msg = String(e); }
      }
      // The backend's error body may be more informative than errMsg.
      // We do a best-effort merge if `e` looks like {errMsg, data: {error,...}}.
      if (e && e.data && typeof e.data === 'object') {
        const d = e.data;
        const extra = d.message || d.error || d.errmsg;
        if (extra) msg += ` — ${extra}`;
      }
      wx.showModal({
        title: '登录失败',
        content: msg,
        showCancel: false,
      });
    }
  },
});