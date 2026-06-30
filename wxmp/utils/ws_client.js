// ws_client.js — minimal wx.connectSocket wrapper with auto-reconnect.
//
// WeChat Mini Program does NOT let multiple WebSockets coexist via the
// legacy global API (wx.onSocketMessage / wx.onSocketOpen / etc.) —
// those global handlers are *singletons*, so registering a second
// instance overwrites the first, and messages from the first socket
// are delivered to the second's handler (or vice versa).
//
// The fix: use the per-task API on the `SocketTask` object returned
// by `wx.connectSocket` — every method (`onOpen`, `onMessage`,
// `onClose`, `onError`, `send`, `close`) takes its own callback, so
// multiple WS instances stay independent. (Available since base lib
// 1.7.0 — we're on 3.x.)
//
// Reconnect logic mirrors web/static/chat.js (exponential backoff
// capped at 8s, max 5 attempts).

/**
 * Create a wxmp-flavored WebSocket.
 *
 * @param {string} url - ws://... endpoint
 * @param {object} [opts]
 * @param {number} [opts.maxReconnectAttempts=5]
 * @param {number} [opts.maxBackoffMs=8000]
 * @returns {object} - { send, on, close, readyState }
 */
function createWs(url, opts = {}) {
  const maxReconnectAttempts = opts.maxReconnectAttempts ?? 5;
  const maxBackoffMs = opts.maxBackoffMs ?? 8000;

  // Simple event emitter: on(event, handler) → returns unsubscribe.
  const listeners = {};
  const on = (event, handler) => {
    (listeners[event] ||= []).push(handler);
    return () => {
      const arr = listeners[event];
      if (!arr) return;
      const i = arr.indexOf(handler);
      if (i >= 0) arr.splice(i, 1);
    };
  };
  const emit = (event, payload) => {
    const arr = listeners[event];
    if (!arr) return;
    for (const h of arr.slice()) {
      try { h(payload); } catch (e) { console.error('[ws_client]', event, 'handler error:', e); }
    }
  };

  let socketTask = null;
  let reconnectAttempts = 0;
  let manuallyClosed = false;
  let readyState = 'CLOSED'; // CONNECTING | OPEN | CLOSING | CLOSED

  const open = () => {
    if (manuallyClosed) return;
    readyState = 'CONNECTING';
    emit('connecting');
    socketTask = wx.connectSocket({
      url,
      success: () => {},
      fail: (err) => {
        console.warn('[ws_client] connectSocket fail:', err);
        scheduleReconnect();
      },
    });

    // Per-task event listeners — these are scoped to THIS socket
    // task only, not global. Multiple createWs() instances coexist
    // without trampling each other.
    socketTask.onOpen(() => {
      readyState = 'OPEN';
      reconnectAttempts = 0;
      emit('open');
    });
    socketTask.onMessage((res) => {
      // WeChat may deliver `res.data` as a string or as a parsed
      // object depending on backend; normalize to a parsed object
      // for JSON-shaped messages.
      let payload = res.data;
      if (typeof payload === 'string') {
        try { payload = JSON.parse(payload); } catch (e) { /* leave as string */ }
      }
      emit('message', payload);
    });
    socketTask.onClose((res) => {
      readyState = 'CLOSED';
      emit('close', { code: res?.code ?? 1006, reason: res?.reason ?? '' });
      if (!manuallyClosed) scheduleReconnect();
    });
    socketTask.onError((err) => emit('error', err));
  };

  const scheduleReconnect = () => {
    if (manuallyClosed) return;
    if (reconnectAttempts >= maxReconnectAttempts) {
      emit('reconnect_failed');
      return;
    }
    const delay = Math.min(1000 * 2 ** reconnectAttempts, maxBackoffMs);
    reconnectAttempts++;
    emit('reconnect_scheduled', { attempt: reconnectAttempts, delay });
    setTimeout(open, delay);
  };

  const send = (data) => {
    if (readyState !== 'OPEN' || !socketTask) {
      console.warn('[ws_client] send while not OPEN, readyState=', readyState);
      return false;
    }
    // Per-task send: targets THIS socket only.
    socketTask.send({
      data: typeof data === 'string' ? data : JSON.stringify(data),
    });
    return true;
  };

  const close = (code = 1000, reason = '') => {
    manuallyClosed = true;
    readyState = 'CLOSING';
    if (socketTask) {
      socketTask.close({ code, reason });
    }
  };

  open();

  return { send, on, close, readyState: () => readyState };
}

module.exports = { createWs };