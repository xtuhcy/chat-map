// Chat Map — chat panel frontend.
//
// Responsibilities:
//   * Open a WebSocket to /ws/chat, send "hello" with user_token + page_url.
//   * Render streamed text / tool calls / errors as messages.
//   * Send user turns, support cancel-and-replace (new "user" frame
//     implicitly cancels the prior in-flight turn).
//   * Heartbeat pings to keep the connection alive.

const $ = (sel) => document.querySelector(sel);
const messagesEl = $("#messages");
const inputEl = $("#input");
const sendBtn = $("#send-btn");
const statusDot = $("#status-dot");
const statusText = $("#status-text");
const clearBtn = $("#clear-btn");

const wsScheme = location.protocol === "https:" ? "wss:" : "ws:";
const wsUrl = `${wsScheme}//${location.host}/ws/chat`;

let ws = null;
// Sections are appended to `wrap` strictly in event-arrival order so
// thinking / text / tool cards render in the same sequence the model
// produced them.
//
// Section lifecycle (strict chronological):
//   * `text`        — close any active thinking segment, then start/extend
//                     a text segment (one `<div class="bubble">`).
//   * `thinking`    — close any active text segment, then start/extend
//                     a thinking segment (one `<div class="thinking">`).
//   * `tool_start`  — close BOTH text and thinking segments (a tool
//                     interrupts both), then create a new tool card.
//   * `reply_end`   — close any open segments; the turn card stays.
//
// This means a single turn can contain multiple text bubbles and
// multiple thinking blocks — e.g. `text, think, text` becomes
// `[bubble1, thinkEl, bubble2]`, not the old sticky
// `[bubble=text1+text2, thinkEl]`. The wrap itself is the per-turn
// card; its children stay in arrival order.
let currentAssistantMsg = null;     // { wrap, bubble?, thinkEl?, tools: Map<id, toolCard> }
let currentToolResultMsg = null;     // tool result text sink for the active tool
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;

// ---------- status helpers ----------

function setStatus(state, text) {
    statusDot.classList.remove("dot-on", "dot-off", "dot-error");
    statusDot.classList.add(`dot-${state}`);
    statusText.textContent = text;
}

// ---------- message rendering ----------

function appendUserMessage(text) {
    const wrap = document.createElement("div");
    wrap.className = "msg msg-user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);
    messagesEl.appendChild(wrap);
    scrollToBottom();
}

function startAssistantMessage() {
    const wrap = document.createElement("div");
    wrap.className = "msg msg-assistant";
    messagesEl.appendChild(wrap);
    // bubble / thinkEl are null until the first delta of that kind
    // arrives. When a different kind of section starts, the active
    // one is "closed" (its reference nulled) so the next delta
    // creates a fresh segment — that is what makes the layout
    // strictly chronological (see the comment block at the top of
    // this file).
    currentAssistantMsg = { wrap, bubble: null, thinkEl: null, tools: new Map(), rawText: "" };
    scrollToBottom();
}

// --- segment lifecycle -------------------------------------------------

function closeActiveTextSegment() {
    if (!currentAssistantMsg) return;
    if (currentAssistantMsg.bubble) {
        // Strip the streaming cursor — this segment is final for now.
        // The DOM node stays put; only the reference is nulled so the
        // next text delta starts a fresh bubble.
        currentAssistantMsg.bubble.classList.remove("streaming");
        currentAssistantMsg.bubble = null;
    }
    // Reset the markdown accumulator for the *next* text segment.
    currentAssistantMsg.rawText = "";
}

function closeActiveThinkSegment() {
    if (!currentAssistantMsg) return;
    if (currentAssistantMsg.thinkEl) {
        // No streaming class to strip; just drop the reference.
        currentAssistantMsg.thinkEl = null;
    }
}

function startTextSegment() {
    if (!currentAssistantMsg) startAssistantMessage();
    if (!currentAssistantMsg.bubble) {
        const bubble = document.createElement("div");
        bubble.className = "bubble streaming";
        currentAssistantMsg.wrap.appendChild(bubble);
        currentAssistantMsg.bubble = bubble;
    }
    return currentAssistantMsg.bubble;
}

function startThinkSegment() {
    if (!currentAssistantMsg) startAssistantMessage();
    if (!currentAssistantMsg.thinkEl) {
        const thinkEl = document.createElement("div");
        thinkEl.className = "thinking";
        thinkEl.innerHTML = '<span class="thinking-label"></span><span class="thinking-body"></span>';
        currentAssistantMsg.wrap.appendChild(thinkEl);
        currentAssistantMsg.thinkEl = thinkEl;
    }
    return currentAssistantMsg.thinkEl;
}

function appendToolCard(toolId, name) {
    if (!currentAssistantMsg) startAssistantMessage();
    const card = document.createElement("div");
    card.className = "tool-card";
    card.dataset.toolId = toolId;
    card.innerHTML = `
        <div class="tool-header" role="button" tabindex="0" aria-expanded="false">
            <span class="tool-toggle">▶</span>
            <span class="tool-name">${escapeHtml(name || "tool")}</span>
            <span class="tool-state">⏳</span>
        </div>
        <div class="tool-body">
            <div class="tool-args"></div>
            <div class="tool-result"></div>
        </div>
    `;
    const header = card.querySelector(".tool-header");
    const setExpanded = (expanded) => {
        card.classList.toggle("expanded", expanded);
        header.setAttribute("aria-expanded", expanded ? "true" : "false");
        card.querySelector(".tool-toggle").textContent = expanded ? "▼" : "▶";
    };
    const toggle = () => setExpanded(!card.classList.contains("expanded"));
    header.addEventListener("click", toggle);
    header.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
        }
    });
    currentAssistantMsg.wrap.appendChild(card);
    currentAssistantMsg.tools.set(toolId, card);
    scrollToBottom();
    return card;
}

function setToolState(toolId, state) {
    if (!currentAssistantMsg) return;
    const card = currentAssistantMsg.tools.get(toolId);
    if (!card) return;
    card.classList.add(`tool-state-${state}`);
    const stateEl = card.querySelector(".tool-state");
    const labels = { success: "✓", failed: "✗", cancelled: "⊘" };
    stateEl.textContent = labels[state] || state;
}

function appendError(text) {
    const wrap = document.createElement("div");
    wrap.className = "msg msg-assistant";
    const err = document.createElement("div");
    err.className = "error-bubble";
    err.textContent = text;
    wrap.appendChild(err);
    messagesEl.appendChild(wrap);
    scrollToBottom();
}

function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
}

// ---------- markdown rendering (streaming) ----------
//
// We accumulate the raw text from `text` deltas and re-parse the
// whole bubble on each animation frame. Re-parsing the full text
// is O(n) per frame; coalescing into rAF caps us at one parse per
// frame regardless of how fast tokens arrive. For chat responses
// (≤ a few kB) this is plenty fast.

let markdownRenderQueued = false;

function scheduleMarkdownRender() {
    if (markdownRenderQueued) return;
    markdownRenderQueued = true;
    requestAnimationFrame(() => {
        markdownRenderQueued = false;
        if (!currentAssistantMsg || !currentAssistantMsg.bubble) return;
        const raw = currentAssistantMsg.rawText;
        // marked v12 is safe by default: HTML in the source is escaped,
        // not parsed. The try/catch is a belt-and-suspenders fallback
        // for the (unlikely) case that the source contains something
        // the parser refuses.
        try {
            currentAssistantMsg.bubble.innerHTML =
                marked.parse(raw, { gfm: true, breaks: true });
        } catch (e) {
            currentAssistantMsg.bubble.textContent = raw;
            console.error("markdown parse failed:", e);
        }
        scrollToBottom();
    });
}

// Open markdown links in a new tab without losing the chat session.
// We intercept clicks at the message container so we don't have to
// touch the marked renderer (which changes signature across versions).
messagesEl.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a) return;
    e.preventDefault();
    window.open(a.href, "_blank", "noopener,noreferrer");
});

function finalizeAssistant() {
    if (!currentAssistantMsg) return;
    // Close any still-open text/thinking segments (strip streaming
    // cursor, null the reference). The DOM nodes stay in place.
    closeActiveTextSegment();
    closeActiveThinkSegment();
    currentAssistantMsg = null;
    currentToolResultMsg = null;
    scrollToBottom();
}

// ---------- WebSocket lifecycle ----------

async function fetchSession() {
    const res = await fetch("/api/session", { credentials: "same-origin" });
    if (!res.ok) throw new Error(`session ${res.status}`);
    const sess = await res.json();
    // Stash the user_token in localStorage so the map iframe (same
    // origin) can read it via `parent.localStorage`. We share the
    // token between the chat agent and the BrowserUseClient.js in
    // the iframe so that the MCP server can route tool calls to
    // the right websocket — both sides must hash (page_url, token)
    // to the same UUID v5.
    try { localStorage.setItem("chat_map_user_token", sess.user_token); }
    catch (e) { /* private mode etc. — fall back to per-request token */ }
    return sess;
}

function connect() {
    setStatus("off", "连接中…");
    ws = new WebSocket(wsUrl);

    ws.addEventListener("open", async () => {
        try {
            const sess = await fetchSession();
            ws.send(JSON.stringify({
                type: "hello",
                user_token: sess.user_token,
                page_url: sess.page_url,
            }));
        } catch (e) {
            appendError("获取 session 失败：" + e.message);
            ws.close();
        }
    });

    ws.addEventListener("message", (ev) => {
        let frame;
        try { frame = JSON.parse(ev.data); }
        catch { return; }
        handleFrame(frame);
    });

    ws.addEventListener("close", (ev) => {
        setStatus("off", `已断开 (${ev.code})`);
        sendBtn.disabled = true;
        // Mark any in-flight tool as cancelled.
        if (currentAssistantMsg) {
            for (const [, card] of currentAssistantMsg.tools) {
                if (!card.className.match(/tool-state-(success|failed|cancelled)/)) {
                    setToolState(card.dataset.toolId, "cancelled");
                }
            }
            finalizeAssistant();
        }
        // Reconnect (exponential backoff, capped).
        if (reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
            const delay = Math.min(1000 * 2 ** reconnectAttempts, 8000);
            reconnectAttempts++;
            setTimeout(connect, delay);
        }
    });

    ws.addEventListener("error", () => {
        setStatus("error", "连接错误");
    });
}

function handleFrame(frame) {
    const t = frame.type;
    switch (t) {
        case "ready":
            setStatus("on", "已连接");
            sendBtn.disabled = false;
            reconnectAttempts = 0;
            break;
        case "reply_start":
            startAssistantMessage();
            sendBtn.classList.add("sending");
            sendBtn.textContent = "停止";
            break;
        case "text": {
            // Text always implies the previous thinking segment is done
            // (reasoning precedes the answer in every model we support).
            closeActiveThinkSegment();
            const bubble = startTextSegment();
            currentAssistantMsg.rawText += frame.delta || "";
            scheduleMarkdownRender();
            break;
        }
        case "thinking": {
            // Thinking always implies the previous text segment is done
            // (the model has gone back into reasoning).
            closeActiveTextSegment();
            const thinkEl = startThinkSegment();
            thinkEl.querySelector(".thinking-body").textContent += frame.delta || "";
            break;
        }
        case "tool_start": {
            // A tool call interrupts both streams; close them and
            // append a fresh card at the end of the turn.
            closeActiveTextSegment();
            closeActiveThinkSegment();
            const id = frame.id || `t${Date.now()}`;
            appendToolCard(id, frame.name);
            break;
        }
        case "tool_call_delta": {
            if (!currentAssistantMsg) return;
            const card = currentAssistantMsg.tools.get(frame.id);
            if (card) {
                const argsEl = card.querySelector(".tool-args");
                argsEl.textContent += frame.delta || "";
            }
            break;
        }
        case "tool_call_end":
            // No-op visually; we wait for tool_result_end to mark success/fail.
            break;
        case "tool_result_text": {
            if (!currentAssistantMsg) return;
            const card = currentAssistantMsg.tools.get(frame.id);
            if (card) {
                const resEl = card.querySelector(".tool-result");
                resEl.textContent += frame.delta || "";
            }
            break;
        }
        case "tool_result_data": {
            // For images we'd render <img>; for v1 we just show a placeholder.
            if (!currentAssistantMsg) return;
            const card = currentAssistantMsg.tools.get(frame.id);
            if (card) {
                const resEl = card.querySelector(".tool-result");
                const note = document.createElement("div");
                note.style.fontStyle = "italic";
                note.textContent = `[${frame.media_type || "data"} received: ${frame.url ? "url" : (frame.data || "").slice(0, 40) + "..."}]`;
                resEl.appendChild(note);
            }
            break;
        }
        case "tool_result_end": {
            if (!currentAssistantMsg) return;
            const state = frame.state || "success";
            // Map enum states: 'success' | 'failed' | 'cancelled' | ...
            const mapped = state === "success" ? "success" : state === "failed" ? "failed" : "cancelled";
            setToolState(frame.id, mapped);
            break;
        }
        case "model_call_end":
            // Could show token counts in the UI; v1 just logs.
            console.debug("tokens:", frame.input_tokens, frame.output_tokens);
            break;
        case "error":
            appendError(frame.message || "未知错误");
            break;
        case "reply_end":
            finalizeAssistant();
            sendBtn.classList.remove("sending");
            sendBtn.textContent = "发送";
            break;
        case "pong":
            // keepalive ack
            break;
        default:
            console.debug("unknown frame:", frame);
    }
}

// ---------- user actions ----------

function sendUserMessage() {
    const text = inputEl.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    appendUserMessage(text);
    inputEl.value = "";
    autoSize();
    ws.send(JSON.stringify({ type: "user", content: text }));
}

function cancelOrSend() {
    if (sendBtn.classList.contains("sending")) {
        // Mid-turn: send cancel.
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "cancel" }));
        }
    } else {
        sendUserMessage();
    }
}

function autoSize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
}

inputEl.addEventListener("input", autoSize);
inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        cancelOrSend();
    }
});
sendBtn.addEventListener("click", cancelOrSend);
clearBtn.addEventListener("click", () => {
    messagesEl.innerHTML = "";
});

// ---------- keepalive ----------

setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ping" }));
    }
}, 25000);

// ---------- go ----------

connect();
