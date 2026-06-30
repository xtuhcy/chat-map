// WxmpMapClient.js — WeChat Mini Program analog of BrowserUseClient.js,
// restricted to the `map_*` subset.
//
// Connects to /ws/browser over wx.connectSocket, registers as
// client_type="wxmp", and handles server-pushed map_* commands by
// either:
//   * mutating page-level map state (markers/polylines/polygons/circles
//     pushed via page.setData, since wxmp <map> renders declaratively),
//   * or invoking the AMap Mini Program SDK (qqmapsdk) for search /
//     geocoding / nearby POI.
//
// DOM-only methods (click_element, input_text, scroll, execute_javascript,
// etc.) are NOT supported on wxmp — if the server pushes one (it shouldn't,
// because the MCP controller rejects them on the wxmp client_type), we
// respond with {success: false, message: "unsupported on wxmp client"}.

const { uuidv5 } = require('./uuidv5.js');
const { createWs } = require('./ws_client.js');

class WxmpMapClient {
  /**
   * @param {object} opts
   * @param {string} opts.wsUrl - ws://host/ws/browser
   * @param {string} opts.userToken - HMAC-signed openid
   * @param {string} opts.pageUrl - the page_url the agent binds to
   * @param {object} opts.page - the wxmp Page instance (for setData)
   * @param {object} [opts.amapSdk] - AMap WX SDK instance (`new QQMapWX({key})`).
   *   Optional but required for any `map_*` tool that hits AMap's
   *   search/geocode/place API (map_run_search, map_geocode,
   *   map_search_nearby). Without it, those tools return
   *   `{success: false, message: 'amapSdk not provided'}`.
   */
  constructor(opts) {
    this._wsUrl = opts.wsUrl;
    this._userToken = opts.userToken;
    this._pageUrl = opts.pageUrl;
    this._page = opts.page;
    this._amapSdk = opts.amapSdk || null;
    this._sessionId = null;
    this._ws = null;
    this._requestId = 0;
    this._pending = new Map(); // requestId → {resolve, reject, timer}
    this._connected = false;
  }

  async init() {
    // Compute session_id from page_url + userToken using the same
    // UUID v5 algorithm as the Python server. The session_id is the
    // routing key — both sides MUST produce identical strings.
    //
    // We prefix with `wxmp|` to match `_generate_session_id` in
    // BrowserUseServerMCPController.py — that function appends
    // `client_type + "|"` to the combined string for non-browser
    // client_types. The browser (default) client computes WITHOUT
    // the prefix, so wxmp and browser sessions with the same
    // (url, user_token) never collide.
    this._sessionId = uuidv5('wxmp|' + this._pageUrl + '|' + this._userToken);

    this._ws = createWs(this._wsUrl);

    this._ws.on('open', () => {
      // First frame registers the session on the server side.
      this._ws.send({
        id: 1,
        method: 'init',
        params: { session_id: this._sessionId },
        client_type: 'wxmp',
      });
      this._connected = true;
      this._page?.onMapClientReady?.();
    });

    this._ws.on('message', (msg) => this._handleMessage(msg));
    this._ws.on('close', (info) => {
      this._connected = false;
      this._page?.onMapClientClose?.(info);
    });
    this._ws.on('error', (err) => {
      console.warn('[WxmpMapClient] ws error:', err);
      this._page?.onMapClientError?.(err);
    });
  }

  close() {
    this._ws?.close();
  }

  _handleMessage(msg) {
    if (!msg || typeof msg !== 'object') return;

    // Server-pushed command (id == 0 with method+params) — handle and respond.
    if (msg.id === 0 && msg.method && msg.params !== undefined) {
      this._handleServerCommand(msg.method, msg.params).catch((e) => {
        console.error('[WxmpMapClient] handler error:', msg.method, e);
        this._respond(msg.method, { success: false, message: String(e) });
      });
      return;
    }

    // Response to a client-initiated RPC.
    if (msg.id && this._pending.has(msg.id)) {
      const entry = this._pending.get(msg.id);
      this._pending.delete(msg.id);
      if (entry.timer) clearTimeout(entry.timer);
      if (msg.error) entry.reject(new Error(msg.error));
      else entry.resolve(msg.result);
    }
  }

  _respond(method, result) {
    if (!this._connected) {
      console.warn('[WxmpMapClient] _respond while disconnected, dropping', method);
      return;
    }
    this._ws.send({
      id: 0,
      method,
      result,
      session_id: this._sessionId,
    });
  }

  async _handleServerCommand(method, params) {
    // Defensive: DOM methods should never reach here because the MCP
    // controller rejects them at the source, but respond defensively.
    const DOM_METHODS = new Set([
      'clickElement', 'inputText', 'selectOption',
      'scroll', 'scrollHorizontally', 'executeJavascript',
      'updateTree', 'getBrowserState', 'getCurrentUrl',
      'getLastUpdateTime', 'cleanUpHighlights',
    ]);
    if (DOM_METHODS.has(method)) {
      this._respond(method, { success: false, message: 'unsupported on wxmp client' });
      return;
    }

    let result;
    try {
      switch (method) {
        case 'mapRunSearch':              result = await this._mapRunSearch(params); break;
        case 'mapSearchAndZoom':          result = await this._mapSearchAndZoom(params); break;
        case 'mapGetState':               result = await this._mapGetState(); break;
        case 'mapSetCenter':              result = await this._mapSetCenter(params); break;
        case 'mapSetZoom':                result = await this._mapSetZoom(params); break;
        case 'mapZoomIn':                 result = await this._mapZoomIn(); break;
        case 'mapZoomOut':                result = await this._mapZoomOut(); break;
        case 'mapAddMarker':              result = await this._mapAddMarker(params); break;
        case 'mapAddMarkerWithInfo':      result = await this._mapAddMarkerWithInfo(params); break;
        case 'mapClearMarkers':           result = await this._mapClearMarkers(); break;
        case 'mapLocate':                 result = await this._mapLocate(); break;
        case 'mapDrawPolyline':           result = await this._mapDrawPolyline(params); break;
        case 'mapDrawPolygon':            result = await this._mapDrawPolygon(params); break;
        case 'mapDrawCircle':             result = await this._mapDrawCircle(params); break;
        case 'mapOpenInfoWindow':         result = await this._mapOpenInfoWindow(params); break;
        case 'mapCloseInfoWindow':        result = await this._mapCloseInfoWindow(); break;
        case 'mapFitView':                result = await this._mapFitView(); break;
        case 'mapClearOverlays':          result = await this._mapClearOverlays(params); break;
        case 'mapRemoveOverlay':          result = await this._mapRemoveOverlay(params); break;
        case 'mapGeocode':                result = await this._mapGeocode(params); break;
        case 'mapListOverlays':           result = await this._mapListOverlays(); break;
        case 'mapSearchNearby':           result = await this._mapSearchNearby(params); break;
        default:
          result = { success: false, message: `unknown method: ${method}` };
      }
    } catch (e) {
      result = { success: false, message: String(e?.message || e) };
    }
    this._respond(method, result);
  }

  // ===== helpers =====

  _setData(patch) {
    if (this._page && typeof this._page.setData === 'function') {
      this._page.setData(patch);
    }
  }

  _mapCtx() {
    return wx.createMapContext('map', this._page);
  }

  // ===== map_* implementations =====

  async _mapRunSearch({ keyword }) {
    const sdk = this._amapSdk;
    if (!sdk) return { success: false, message: 'amapSdk not provided' };
    // The 2020 AMap WX SDK doesn't expose a true city-wide keyword
    // POI search (the v3 place/text REST endpoint isn't wrapped).
    // Best available: `getInputtips` — returns input-suggestion
    // results (top-N autocomplete candidates) for a keyword.
    // This is a degradation from the browser side, which uses
    // AMap.PlaceSearch in map.html. The LLM will see fewer / less
    // specific results; the skill flow handles it.
    return new Promise((resolve) => {
      sdk.getInputtips({
        keywords: keyword,
        success: (data) => {
          const tips = (data && data.tips) || [];
          resolve({
            success: true,
            message: `found ${tips.length} suggestions`,
            keyword,
            info: tips.length ? (tips[0].address || tips[0].name || '') : '',
            // Pass through the full list so the LLM can inspect.
            tips,
          });
        },
        fail: (err) => resolve({
          success: false,
          message: (err && (err.errMsg || err.info)) || String(err),
          keyword,
        }),
      });
    });
  }

  async _mapSearchAndZoom({ keyword, zoom }) {
    const r = await this._mapRunSearch({ keyword });
    if (r.success) {
      this._setData({ scale: zoom });
    }
    return { ...r, zoom };
  }

  async _mapGetState() {
    const ctx = this._mapCtx();
    return new Promise((resolve) => {
      // wxmp doesn't have synchronous getters; chain two async calls.
      ctx.getCenterLocation({
        success: (loc) => {
          ctx.getScale({
            success: ({ scale }) => {
              const markers = this._page?.data?.markers || [];
              const polylines = this._page?.data?.polylines || [];
              const polygons = this._page?.data?.polygons || [];
              const circles = this._page?.data?.circles || [];
              resolve({
                success: true,
                center: { lng: loc.longitude, lat: loc.latitude },
                zoom: scale,
                bounds: null,
                hasGeolocation: false,
                overlayCount: markers.length + polylines.length + polygons.length + circles.length,
                infoText: '',
                searchInputValue: '',
              });
            },
            fail: () => resolve({ success: false, message: 'getScale failed' }),
          });
        },
        fail: () => resolve({ success: false, message: 'getCenterLocation failed' }),
      });
    });
  }

  async _mapSetCenter({ lng, lat }) {
    this._setData({ longitude: lng, latitude: lat });
    return { success: true, message: 'center updated', center: { lng, lat } };
  }

  async _mapSetZoom({ zoom }) {
    this._setData({ scale: zoom });
    return { success: true, message: 'zoom updated', zoom };
  }

  async _mapZoomIn() {
    const cur = this._page?.data?.scale ?? 12;
    const next = Math.min(cur + 1, 20);
    this._setData({ scale: next });
    return { success: true };
  }

  async _mapZoomOut() {
    const cur = this._page?.data?.scale ?? 12;
    const next = Math.max(cur - 1, 3);
    this._setData({ scale: next });
    return { success: true };
  }

  async _mapAddMarker({ lng, lat, title }) {
    const id = Date.now() + Math.floor(Math.random() * 1000);
    // The wxmp <map> component renders a default AMap drop pin when
    // no iconPath is set. That pin is TALLER than wide (roughly 24:32
    // aspect ratio). A square 32x32 would stretch it horizontally.
    // We use 24x32 to match the default pin's natural shape.
    const markers = (this._page?.data?.markers || []).concat([{
      id, longitude: lng, latitude: lat,
      width: 24, height: 32,
      title: title || '',
    }]);
    this._setData({ markers });
    return { success: true, message: 'marker added', lng, lat, title, id };
  }

  async _mapAddMarkerWithInfo({ lng, lat, title, info_html, poi }) {
    // Build the body HTML. Prefer the explicit info_html; if the
    // LLM passed a POI object, format its fields.
    let body = info_html || '';
    if (!body && poi) {
      const p = poi;
      const lines = [];
      if (p.name) lines.push(`<div style="font-weight:600;font-size:30rpx;">${p.name}</div>`);
      if (p.address) lines.push(`<div>📍 ${p.address}</div>`);
      if (p.tel) lines.push(`<div>📞 ${p.tel}</div>`);
      if (p.type) lines.push(`<div style="color:#94a3b8;font-size:24rpx;">${p.type}</div>`);
      if (typeof p.distance === 'number') {
        lines.push(`<div style="color:#94a3b8;font-size:24rpx;">📏 ${(p.distance / 1000).toFixed(2)} km</div>`);
      }
      body = lines.join('');
    }
    // Lightly fix common LLM HTML typos: missing space after a
    // tag name (`<divstyle`), stray `"` after attribute values
    // (`24rpx,"`), and `.` mistaken for `:` in CSS values
    // (`color.#94a3b8`). These show up because the agent emits
    // HTML inline and sometimes drops a character. Without this
    // fix the LLM-generated HTML renders as broken text.
    const safeHtml = body
      .replace(/<([a-z]+)([a-zA-Z])/g, '<$1 $2')   // <divstyle → <div style
      .replace(/([a-zA-Z0-9])"([>\s])/g, '$1$2')    // strip stray " before > or space
      .replace(/([a-z-]+)\.([#a-z0-9]+)/gi, '$1:$2'); // color.#hex → color:#hex
    // Build the marker with a native `callout` (tap-time bubble)
    // and stash the sanitized HTML in `infoWindow.html` for the
    // page's floating banner. `<rich-text nodes="{{html}}">`
    // renders a STRING as HTML — passing an array would treat it
    // as a node tree and escape the markup as plain text.
    const id = Date.now() + Math.floor(Math.random() * 1000);
    const newMarker = {
      id, longitude: lng, latitude: lat,
      width: 24, height: 32,
      title: title || '',
      callout: {
        content: title || poi?.name || '',
        color: '#ffffff',
        fontSize: 26,
        borderRadius: 8,
        borderWidth: 1,
        borderColor: '#1677ff',
        bgColor: '#1677ff',
        padding: 8,
        display: 'BYCLICK',
        textAlign: 'center',
      },
    };
    const markers = (this._page?.data?.markers || []).concat([newMarker]);
    this._setData({
      markers,
      infoWindow: { lng, lat, title: title || '', html: safeHtml },
    });
    return { success: true, message: 'marker with info added', lng, lat, title, id };
  }

  async _mapClearMarkers() {
    this._setData({ markers: [] });
    return { success: true, message: 'markers cleared' };
  }

  async _mapLocate() {
    // wxmp has no browser geolocation; use wx.getLocation.
    // fail() callback gets {errMsg, errno?}, not an Error — so
    // String(err) would render as "[object Object]". Extract
    // errMsg explicitly (same pattern as the chat-error path).
    return new Promise((resolve) => {
      wx.getLocation({
        type: 'gcj02',
        success: (loc) => resolve({
          success: true,
          lng: loc.longitude,
          lat: loc.latitude,
          formatted_address: '',
          message: 'located',
        }),
        fail: (err) => {
          const msg = (err && (err.errMsg || err.message))
            || (typeof err === 'string' ? err : '');
          resolve({ success: false, message: msg || 'getLocation failed' });
        },
      });
    });
  }

  async _mapDrawPolyline({ path, options }) {
    const id = Date.now() + Math.floor(Math.random() * 1000);
    const points = path.map((p) => Array.isArray(p) ? { longitude: p[0], latitude: p[1] } : p);
    const polylines = (this._page?.data?.polylines || []).concat([{
      id, points,
      color: options?.color || '#1677ff',
      width: options?.width || 6,
      dottedLine: false,
    }]);
    this._setData({ polylines });
    return { success: true, message: 'polyline drawn', id };
  }

  async _mapDrawPolygon({ path, options }) {
    const id = Date.now() + Math.floor(Math.random() * 1000);
    const points = path.map((p) => Array.isArray(p) ? { longitude: p[0], latitude: p[1] } : p);
    const polygons = (this._page?.data?.polygons || []).concat([{
      id, points,
      strokeWidth: options?.width || 3,
      strokeColor: options?.color || '#7e57ff',
      fillColor: options?.fillColor || '#7e57ff',
      fillOpacity: options?.fillOpacity ?? 0.2,
    }]);
    this._setData({ polygons });
    return { success: true, message: 'polygon drawn', id };
  }

  async _mapDrawCircle({ lng, lat, radius, options }) {
    const id = Date.now() + Math.floor(Math.random() * 1000);
    const circles = (this._page?.data?.circles || []).concat([{
      id,
      longitude: lng,
      latitude: lat,
      radius,
      strokeWidth: options?.width || 2,
      strokeColor: options?.color || '#1677ff',
      fillColor: options?.fillColor || '#1677ff',
      fillOpacity: options?.fillOpacity ?? 0.15,
    }]);
    this._setData({ circles });
    return { success: true, message: 'circle drawn', id };
  }

  async _mapOpenInfoWindow({ lng, lat, content }) {
    this._setData({ infoWindow: { lng, lat, html: content } });
    return { success: true, message: 'info window opened' };
  }

  async _mapCloseInfoWindow() {
    this._setData({ infoWindow: null });
    return { success: true };
  }

  async _mapFitView() {
    const points = [];
    const markers = this._page?.data?.markers || [];
    for (const m of markers) points.push({ longitude: m.longitude, latitude: m.latitude });
    const polylines = this._page?.data?.polylines || [];
    for (const pl of polylines) for (const p of (pl.points || [])) points.push(p);
    const polygons = this._page?.data?.polygons || [];
    for (const pg of polygons) for (const p of (pg.points || [])) points.push(p);
    const circles = this._page?.data?.circles || [];
    for (const c of circles) points.push({ longitude: c.longitude, latitude: c.latitude });

    if (!points.length) {
      return { success: true, message: 'no overlays to fit', fitted: false };
    }
    const ctx = this._mapCtx();
    await new Promise((resolve) => ctx.includePoints({
      points,
      padding: [40, 40, 40, 40],
      success: resolve, fail: resolve,
    }));
    const state = await this._mapGetState();
    return {
      success: true, message: 'fitted', fitted: true,
      center: state.center, zoom: state.zoom,
    };
  }

  async _mapClearOverlays({ type }) {
    if (type === 'all' || type === 'shape') {
      this._setData({ polylines: [], polygons: [], circles: [] });
    }
    if (type === 'all' || type === 'marker') {
      this._setData({ markers: [] });
    }
    if (type === 'polyline') this._setData({ polylines: [] });
    if (type === 'polygon')  this._setData({ polygons: [] });
    if (type === 'circle')   this._setData({ circles: [] });
    return { success: true, message: `cleared ${type}` };
  }

  async _mapRemoveOverlay({ type, index }) {
    const key = ({ polyline: 'polylines', polygon: 'polygons', circle: 'circles' })[type];
    if (!key) return { success: false, message: `unknown type: ${type}` };
    const arr = (this._page?.data?.[key] || []).slice();
    if (index < 0 || index >= arr.length) {
      return { success: false, message: `index out of range: ${index}` };
    }
    arr.splice(index, 1);
    this._setData({ [key]: arr });
    return { success: true, message: 'removed', remaining: arr.length };
  }

  async _mapGeocode({ address, city }) {
    const sdk = this._amapSdk;
    if (!sdk) return { success: false, message: 'amapSdk not provided' };
    // The 2020 AMap WX SDK's `getGeo` wraps the v3 geocode/geo REST
    // endpoint. Signature: {options: {address, city, batch?, sig?},
    // success, fail}. Success returns the full AMap response
    // {status, info, geocodes: [{location, formatted_address, ...}], ...}.
    return new Promise((resolve) => {
      sdk.getGeo({
        options: { address, city: city || '' },
        success: (res) => {
          if (!res || res.status !== '1' || !res.geocodes || !res.geocodes.length) {
            return resolve({
              success: false,
              message: (res && res.info) || 'no geocode result',
            });
          }
          const g = res.geocodes[0];
          // AMap returns location as "lng,lat" string — parse it.
          const [lng, lat] = (g.location || '').split(',').map((s) => parseFloat(s));
          if (Number.isNaN(lng) || Number.isNaN(lat)) {
            return resolve({ success: false, message: 'invalid location in response' });
          }
          resolve({
            success: true,
            message: 'geocoded',
            lng,
            lat,
            formatted_address: g.formatted_address || address,
            level: g.level || '',
            all: res.geocodes,
          });
        },
        fail: (err) => resolve({
          success: false,
          message: (err && (err.errMsg || err.info)) || String(err),
        }),
      });
    });
  }

  async _mapListOverlays() {
    const data = this._page?.data || {};
    return {
      success: true,
      polyline: data.polylines?.length || 0,
      polygon: data.polygons?.length || 0,
      circle: data.circles?.length || 0,
      markers: data.markers?.length || 0,
      infoWindow: data.infoWindow || null,
    };
  }

  async _mapSearchNearby({ lng, lat, radius, type, keyword, city, exclude_keywords, include_keywords }) {
    const sdk = this._amapSdk;
    if (!sdk) return { success: false, message: 'amapSdk not provided' };
    // The 2020 AMap WX SDK wraps the v3 place/around endpoint as
    // `getPoiAround`. Signature: {location, querytypes?, querykeywords?,
    // success, fail}. Success returns {markers, poisData} where
    // poisData is the raw AMap POI list (each entry has `name`,
    // `address`, `location: "lng,lat"`, `type`).
    //
    // The 2020 SDK does NOT support `type` as a category code (it
    // only accepts `querytypes`, which is a free-text type hint,
    // not the v3 category code the LLM uses). For type-filtered
    // searches the wxmp client is limited — we pass `querytypes` as
    // a hint and rely on `querykeywords` + the LLM's
    // exclude_keywords / include_keywords to do the actual
    // filtering client-side.
    return new Promise((resolve) => {
      sdk.getPoiAround({
        location: `${lng},${lat}`,
        querykeywords: keyword || '',
        querytypes: type || '',
        success: ({ poisData } = {}) => {
          const list = Array.isArray(poisData) ? poisData : [];
          // Apply keyword filters identically to the browser implementation.
          const excluded_by_keyword = {};
          const inc = include_keywords || [];
          const exc = exclude_keywords || [];
          const filtered = [];
          let filtered_out = 0;
          for (const poi of list) {
            const name = poi.name || '';
            if (inc.length && !inc.some((k) => name.includes(k))) {
              filtered_out++;
              continue;
            }
            let dropped = false;
            for (const k of exc) {
              if (name.includes(k)) {
                excluded_by_keyword[k] = (excluded_by_keyword[k] || 0) + 1;
                filtered_out++;
                dropped = true;
                break;
              }
            }
            if (!dropped) filtered.push(poi);
          }
          // AMap returns `location: "lng,lat"` (string). Parse it.
          // Compute distance from center for sorting.
          const pois = filtered.map((p) => {
            const [plng, plat] = (p.location || '').split(',').map((s) => parseFloat(s));
            const dLng = (plng - lng) * 111000 * Math.cos((lat * Math.PI) / 180);
            const dLat = (plat - lat) * 111000;
            const distance = Number.isFinite(plng) ? Math.sqrt(dLng * dLng + dLat * dLat) : undefined;
            return {
              name: p.name || '',
              location: { lng: plng, lat: plat },
              address: p.address || '',
              tel: p.tel || '',
              type: p.type || '',
              distance,
            };
          }).sort((a, b) => (a.distance || Infinity) - (b.distance || Infinity));
          resolve({
            success: true,
            count: pois.length,
            total_before_filter: list.length,
            filtered_out,
            excluded_by_keyword,
            pois,
          });
        },
        fail: (err) => resolve({
          success: false,
          message: (err && (err.errMsg || err.info)) || String(err),
        }),
      });
    });
  }
}

module.exports = { WxmpMapClient };