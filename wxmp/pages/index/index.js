// pages/index/index.js — main page logic for the chat-map mini program.
//
// Responsibilities:
//   1. Open chat WebSocket to /ws/chat and the map WebSocket to
//      /ws/browser, using the openid-derived user_token from app.js.
//   2. Stream assistant text/thinking/tool events into a message list
//      rendered by the WXML template.
//   3. Hand server-pushed map_* commands off to WxmpMapClient, which
//      mutates map data (markers/polylines/...) via this.setData.
//   4. Send user turns, support cancel-and-replace.
//
// Frame protocol mirrors web/static/chat.js so the same FastAPI
// web/routers/chat.py server handler works for both clients.

const { createWs } = require('../../utils/ws_client.js');
const { uuidv5 } = require('../../utils/uuidv5.js');
const { WxmpMapClient } = require('../../utils/WxmpMapClient.js');
const { toRichTextNodes } = require('../../utils/markdown.js');

let nextMsgId = 1;

Page({
  data: {
    // Map state
    longitude: 116.397,   // Beijing default; will be replaced when mapSetCenter fires
    latitude: 39.908,
    scale: 12,
    markers: [],
    polylines: [],
    polygons: [],
    circles: [],
    infoWindow: null,
    showLocation: false,

    // Chat state
    status: 'off',
    statusText: '未连接',
    inputText: '',
    sending: false,
    messages: [],
    scrollIntoView: '',
  },

  onLoad() {
    this._chatWs = null;
    this._mapClient = null;
    this._initFailed = false;
    // Streaming state — keyed by message id so we can do path-based
    // setData updates that re-render only the changed field.
    this._idxById = new Map();
    this._toolMsgId = new Map();
    this._currentAssistant = null;
    this._setupConnection();
  },

  onUnload() {
    this._chatWs?.close();
    this._mapClient?.close();
  },

  async _setupConnection() {
    const app = getApp();
    let userToken = app.globalData.userToken;
    if (!userToken) {
      // app.js may still be mid-flight; wait briefly.
      for (let i = 0; i < 20 && !userToken; i++) {
        await new Promise((r) => setTimeout(r, 100));
        userToken = app.globalData.userToken;
      }
    }
    if (!userToken) {
      this._setStatus('error', '登录失败');
      this._appendError('无法获取 user_token，请在小程序后台检查 AppID/Secret 配置');
      return;
    }
    // The page_url the server binds to. We use the wxmp origin
    // (the server only treats it as an opaque routing key).
    const pageUrl = app.globalData.serverBase + '/map';
    const wsBase = app.globalData.serverBase.replace(/^http/, 'ws');

    // Chat WS
    this._setStatus('off', '连接中…');
    this._chatWs = createWs(wsBase + '/ws/chat');
    this._chatWs.on('open', () => this._onChatOpen(userToken, pageUrl));
    this._chatWs.on('message', (m) => this._onChatMessage(m));
    this._chatWs.on('close', (info) => this._onChatClose(info));
    this._chatWs.on('error', (err) => {
      console.warn('[chat] ws error', err);
      this._setStatus('error', '连接错误');
    });

    // Map WS (delegated to WxmpMapClient)
    // AMap WX SDK — downloaded from https://lbs.amap.com/api/wx/gettingstarted
    // and placed at `wxmp/utils/amap-wx.js`. The key is fetched
    // from GET /api/wx-config (see app.js doLoadWxConfig) and
    // stashed in app.globalData.amapWxKey — never hardcoded here.
    //
    // IMPORTANT: amap-wx.js uses a named export:
    //     module.exports.AMapWX = AMapWX
    // so the correct import is `const { AMapWX } = require(...)`,
    // not `const AMapWX = require(...)` (the latter gives you the
    // module wrapper object, not the class).
    let amapSdk = null;
    try {
      const wxKey = app.globalData.amapWxKey;
      if (!wxKey) {
        throw new Error(
          '/api/wx-config did not provide amap_wx_key. ' +
          'Set AMAP_WX_KEY in web/.env and restart the backend.',
        );
      }
      const { AMapWX } = require('../../utils/amap-wx.js');
      amapSdk = new AMapWX({ key: wxKey });
      console.log('[map] AMap WX SDK loaded ok');
    } catch (e) {
      // Surface the actual failure — a previous version silently
      // swallowed it, making "amapSdk not provided" impossible to
      // diagnose. Now the user sees the real cause in the console.
      console.error('[map] AMap WX SDK failed to load — search/geocode tools will return failure:', e);
      wx.showModal({
        title: 'AMap SDK 加载失败',
        content: String(e?.message || e),
        showCancel: false,
      });
    }
    this._mapClient = new WxmpMapClient({
      wsUrl: wsBase + '/ws/browser',
      userToken,
      pageUrl,
      amapSdk,
      page: this,
    });
    // page.setData wrappers that don't conflict with WxmpMapClient's _setData
    this._mapClient.init();
  },

  // ===== Chat WS =====

  _onChatOpen(userToken, pageUrl) {
    // The `client_type` field tells the chat handler (and through
    // it the MCP server) that this is a wxmp connection. The
    // browser client omits this field and defaults to "browser".
    this._chatWs.send({
      type: 'hello',
      user_token: userToken,
      page_url: pageUrl,
      client_type: 'wxmp',
    });
  },

  _onChatMessage(frame) {
    if (!frame || typeof frame !== 'object') return;
    switch (frame.type) {
      case 'ready':
        this._setStatus('on', '已连接');
        break;
      case 'reply_start':
        this._currentAssistant = {
          text: '', thinking: '',
          textMsgId: null, thinkingMsgId: null,
          tools: new Map(),
        };
        this._currentToolId = null;
        this.setData({ sending: true });
        break;
      case 'text':
        this._appendText(frame.delta || '');
        break;
      case 'thinking':
        this._appendThinking(frame.delta || '');
        break;
      case 'tool_start': {
        const id = frame.id || `t${Date.now()}`;
        this._currentToolId = id;
        this._appendToolStart(id, frame.name, frame.delta || '');
        break;
      }
      case 'tool_call_delta':
        this._appendToolDelta(frame.id, frame.delta || '');
        break;
      case 'tool_call_end':
        break;
      case 'tool_result_text':
        this._appendToolResultDelta(frame.id, frame.delta || '');
        break;
      case 'tool_result_data':
        this._appendToolResultDelta(frame.id, `[data: ${frame.media_type || 'unknown'}]`);
        break;
      case 'tool_result_end': {
        const state = frame.state || 'success';
        const icon = state === 'success' ? '✓' : state === 'failed' ? '✗' : '⊘';
        this._setToolState(frame.id, icon);
        break;
      }
      case 'model_call_end':
        break;
      case 'error':
        this._appendError(frame.message || '未知错误');
        break;
      case 'reply_end':
        this.setData({ sending: false });
        this._currentAssistant = null;
        this._toolMsgId.clear();
        break;
      case 'pong':
        break;
      default:
        console.debug('[chat] unknown frame', frame);
    }
  },

  _onChatClose(info) {
    this._setStatus('off', `已断开 (${info?.code ?? '?'})`);
    this.setData({ sending: false });
  },

  // ===== Message rendering =====

  _setStatus(status, text) {
    this.setData({ status, statusText: text });
  },

  _appendError(text) {
    this._pushMessage({
      id: nextMsgId++, role: 'assistant', kind: 'error', text,
    });
  },

  // ===== Streaming: text / thinking / tools =====
  //
  // The previous implementation rebuilt the whole `messages` array
  // on every delta via setData({messages}). WeChat's diff sometimes
  // "re-renders" the bubble from scratch in that case — visible as
  // "每次都重新输出" (each delta seems to start over instead of
  // appending).
  //
  // The fix is path-based setData (setData({[`messages[${i}].text`]:
  // newText})) which updates a single field and triggers a single
  // re-render of just that view. We track each streaming section
  // (text bubble, thinking block) by its own stable msgId so we can
  // find the right array index.

  _appendText(delta) {
    if (!this._currentAssistant || !delta) return;
    const a = this._currentAssistant;
    a.text = (a.text || '') + delta;
    if (a.textMsgId == null) {
      // First text delta of this turn — create the bubble.
      const id = nextMsgId++;
      const newMsg = { id, role: 'assistant', kind: 'text', text: a.text };
      this.setData({ messages: this.data.messages.concat([newMsg]) });
      this._idxById.set(id, this.data.messages.length - 1);
      a.textMsgId = id;
      this._scrollToEnd();
      return;
    }
    // Subsequent delta — path-based update.
    const idx = this._idxById.get(a.textMsgId);
    if (idx == null) return;
    this.setData({ [`messages[${idx}].text`]: a.text });
  },

  _appendThinking(delta) {
    if (!this._currentAssistant || !delta) return;
    const a = this._currentAssistant;
    a.thinking = (a.thinking || '') + delta;
    if (a.thinkingMsgId == null) {
      // First thinking delta — create the block, then insert it
      // BEFORE the text bubble if one exists, else just append.
      const id = nextMsgId++;
      const newMsg = { id, role: 'assistant', kind: 'thinking', text: a.thinking };
      const textIdx = a.textMsgId != null ? this._idxById.get(a.textMsgId) : null;
      const messages = this.data.messages.slice();
      if (textIdx != null) {
        messages.splice(textIdx, 0, newMsg);
      } else {
        messages.push(newMsg);
      }
      this.setData({ messages });
      // Rebuild index map after splice (indices shift).
      this._rebuildIdxMap();
      a.thinkingMsgId = id;
      this._scrollToEnd();
      return;
    }
    const idx = this._idxById.get(a.thinkingMsgId);
    if (idx == null) return;
    this.setData({ [`messages[${idx}].text`]: a.thinking });
  },

  _appendToolStart(toolId, name, args) {
    if (!this._currentAssistant) return;
    this._currentAssistant.tools.set(toolId, { name, args: args || '', result: '', state: '⏳' });
    const id = nextMsgId++;
    const newMsg = {
      id, role: 'assistant', kind: 'tool',
      toolId, toolName: name, toolArgs: args || '', toolResult: '', toolStateIcon: '⏳',
      expanded: false,  // default collapsed; user clicks header to expand
    };
    this.setData({ messages: this.data.messages.concat([newMsg]) });
    this._idxById.set(id, this.data.messages.length - 1);
    this._toolMsgId.set(toolId, id);
    this._scrollToEnd();
  },

  _appendToolDelta(toolId, delta) {
    const t = this._currentAssistant?.tools?.get(toolId);
    if (!t || !delta) return;
    t.args = (t.args || '') + delta;
    const msgId = this._toolMsgId?.get(toolId);
    if (msgId == null) return;
    const idx = this._idxById.get(msgId);
    if (idx == null) return;
    this.setData({ [`messages[${idx}].toolArgs`]: t.args });
  },

  _appendToolResultDelta(toolId, delta) {
    const t = this._currentAssistant?.tools?.get(toolId);
    if (!t || !delta) return;
    t.result = (t.result || '') + delta;
    const msgId = this._toolMsgId?.get(toolId);
    if (msgId == null) return;
    const idx = this._idxById.get(msgId);
    if (idx == null) return;
    this.setData({ [`messages[${idx}].toolResult`]: t.result });
  },

  _setToolState(toolId, icon) {
    const msgId = this._toolMsgId?.get(toolId);
    if (msgId == null) return;
    const idx = this._idxById.get(msgId);
    if (idx == null) return;
    this.setData({ [`messages[${idx}].toolStateIcon`]: icon });
  },

  // Rebuild the id → array-index map. O(n) but only called when
  // we splice in a thinking block.
  _rebuildIdxMap() {
    this._idxById.clear();
    const list = this.data.messages;
    for (let i = 0; i < list.length; i++) {
      this._idxById.set(list[i].id, i);
    }
  },

  _pushMessage(msg) {
    this.setData({ messages: this.data.messages.concat([msg]) });
    this._scrollToEnd();
  },

  _scrollToEnd() {
    const last = this.data.messages[this.data.messages.length - 1];
    if (last) this.setData({ scrollIntoView: 'msg-' + last.id });
  },

  // ===== User input =====
  //
  // We intentionally do NOT use `bindinput` + `value="{{inputText}}"`
  // (a controlled component). Every setData on `inputText` causes
  // a page-wide re-render in WeChat, which jostles the textarea
  // caret and triggers a visible "flash" while the cursor blinks.
  //
  // Instead, the textarea is uncontrolled: the user types, we read
  // the value at submit time via `e.detail.value` (provided by both
  // `bindconfirm` and `bindtap` on the send button), then we reset
  // the textarea by passing an empty value through to it.
  //
  // `bindblur` keeps a small in-memory mirror so we can re-show
  // the draft if the user re-focuses (e.g. after dismissing the
  // keyboard). It's a single setData on blur, which doesn't fire
  // during typing — no caret flash.

  onInputBlur(e) {
    this._draftText = e.detail.value || '';
  },

  onSend(e) {
    if (this.data.sending) {
      // Mid-turn → cancel.
      this._chatWs?.send({ type: 'cancel' });
      this.setData({ sending: false });
      return;
    }
    // Prefer the event's value (works for both the button tap and
    // the keyboard's "send" confirm). Fall back to the blur mirror.
    const text = ((e && e.detail && e.detail.value) || this._draftText || '').trim();
    if (!text) return;
    this._pushMessage({
      id: nextMsgId++, role: 'user', kind: 'text', text,
    });
    this._chatWs?.send({ type: 'user', content: text });
    // Clear the draft so the next send starts fresh.
    this._draftText = '';
    // Clear the <input> via the component API — no setData, no
    // re-render, no caret flash. `input.clear()` is available
    // since base lib 2.0; falls back gracefully on older runtimes.
    const input = this.selectComponent('#chat-input');
    if (input && typeof input.clear === 'function') {
      try { input.clear(); } catch (e) { /* noop on older clients */ }
    }
  },

  // ===== Map interactions =====

  onMarkerTap(e) {
    // Could open info window for the tapped marker; for v1 just log.
    console.debug('[map] marker tap', e.detail);
  },

  onToolTap(e) {
    // Toggle the tool card's expanded state. `e.currentTarget.dataset.id`
    // is the message id (set via data-id on the header in the WXML).
    const id = e.currentTarget.dataset.id;
    if (id == null) return;
    const idx = this._idxById.get(id);
    if (idx == null) return;
    const cur = this.data.messages[idx];
    if (!cur || cur.kind !== 'tool') return;
    this.setData({ [`messages[${idx}].expanded`]: !cur.expanded });
  },

  onInfoClose() {
    this.setData({ infoWindow: null });
  },
});