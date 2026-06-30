/**
 * BrowserUseClient.js
 *
 * Frontend script that embeds PageController capabilities and communicates
 * with BrowserUseServer via WebSocket for remote browser control.
 *
 * Architecture:
 * - BrowserUseClient has PageController's capabilities (DOM extraction, element interaction)
 * - BrowserUseServer sends commands via WebSocket
 * - BrowserUseClient executes commands and controls the browser
 *
 * Usage:
 *   <script src="BrowserUseClient.js"></script>
 *   <script>
 *     const client = new BrowserUseClient('ws://localhost:8765');
 *     client.connect();
 *   </script>
 */

(function() {
    'use strict';

    // ======= PageController Core (from packages/page-controller/src) =======

    const WAIT_FOR_MS = 100;

    function waitFor(seconds) {
        return new Promise(resolve => setTimeout(resolve, seconds * 1000));
    }

    function isHTMLElement(el) {
        return !!el && el.nodeType === 1;
    }

    function isInputElement(el) {
        return el?.nodeType === 1 && el.tagName === 'INPUT';
    }

    function isTextAreaElement(el) {
        return el?.nodeType === 1 && el.tagName === 'TEXTAREA';
    }

    function isSelectElement(el) {
        return el?.nodeType === 1 && el.tagName === 'SELECT';
    }

    function isAnchorElement(el) {
        return el?.nodeType === 1 && el.tagName === 'A';
    }

    function getNativeValueSetter(element) {
        return Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), 'value').set;
    }

    function getIframeOffset(element) {
        const frame = element.ownerDocument.defaultView?.frameElement;
        if (!frame) return { x: 0, y: 0 };
        const rect = frame.getBoundingClientRect();
        return { x: rect.left, y: rect.top };
    }

    function blurLastClickedElement() {
        if (window._lastClickedElement) {
            const el = window._lastClickedElement;
            el.dispatchEvent(new PointerEvent('pointerout', { bubbles: true }));
            el.dispatchEvent(new PointerEvent('pointerleave', { bubbles: false }));
            el.dispatchEvent(new MouseEvent('mouseout', { bubbles: true }));
            el.dispatchEvent(new MouseEvent('mouseleave', { bubbles: false }));
            el.blur();
            window._lastClickedElement = null;
        }
    }

    async function scrollIntoViewIfNeeded(element) {
        if (typeof element.scrollIntoViewIfNeeded === 'function') {
            element.scrollIntoViewIfNeeded();
        } else {
            element.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' });
        }
    }

    async function movePointerToElement(element, x, y) {
        const offset = getIframeOffset(element);
        window.dispatchEvent(new CustomEvent('PageAgent::MovePointerTo', {
            detail: { x: x + offset.x, y: y + offset.y }
        }));
        await waitFor(0.3);
    }

    async function clickPointer() {
        window.dispatchEvent(new CustomEvent('PageAgent::ClickPointer'));
    }

    async function enablePassThrough() {
        window.dispatchEvent(new CustomEvent('PageAgent::EnablePassThrough'));
    }

    async function disablePassThrough() {
        window.dispatchEvent(new CustomEvent('PageAgent::DisablePassThrough'));
    }

    // ======= DOM Tree Extraction =======

    const SEMANTIC_TAGS = new Set(['nav', 'menu', 'header', 'footer', 'aside', 'dialog']);

    function resolveViewportExpansion(viewportExpansion) {
        return viewportExpansion ?? -1;
    }

    function getFlatTree(config = {}) {
        const interactiveBlacklist = config.interactiveBlacklist || [];
        const interactiveWhitelist = config.interactiveWhitelist || [];

        const tree = {
            rootId: 'root',
            map: {}
        };

        // Create root node to represent document.body
        tree.map['root'] = {
            id: 'root',
            type: 'ELEMENT_NODE',
            tagName: 'body',
            attributes: {},
            children: [],
            isInteractive: false,
            isVisible: true,
            ref: null
        };

        let elementIndex = 0;

        function generateNodeId() {
            return 'node_' + Math.random().toString(36).substr(2, 9);
        }

        function isVisible(el) {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.left >= 0;
        }

        function isInteractive(el) {
            // Check if element is in blacklist (by reference or selector)
            for (const item of interactiveBlacklist) {
                if (typeof item === 'string') {
                    try { if (el.matches(item)) return false; } catch { }
                } else if (item === el) {
                    return false;
                }
            }

            if (interactiveWhitelist.length > 0) {
                let inWhitelist = false;
                for (const item of interactiveWhitelist) {
                    if (typeof item === 'string') {
                        try { if (el.matches(item)) { inWhitelist = true; break; } } catch { }
                    } else if (item === el) {
                        inWhitelist = true;
                        break;
                    }
                }
                if (!inWhitelist) return false;
            }

            const interactiveSelectors = [
                'a[href]', 'button', 'input', 'select', 'textarea',
                '[onclick]', '[onmouseover]', '[role="button"]', '[role="link"]',
                '[contenteditable="true"]', '[tabindex]'
            ];

            return interactiveSelectors.some(selector => {
                try { return el.matches(selector); } catch { return false; }
            });
        }

        function processElement(el, parentId, depth) {
            if (depth > 50) return;

            const nodeId = generateNodeId();
            const tagName = el.tagName.toLowerCase();
            const isElemInteractive = isInteractive(el);
            const isVisibleEl = isVisible(el);

            // Get attributes
            const attrs = {};
            for (const attr of el.attributes) {
                if (!['style', 'class'].includes(attr.name) && attr.value.length < 200) {
                    attrs[attr.name] = attr.value;
                }
            }

            const node = {
                id: nodeId,
                type: 'ELEMENT_NODE',
                tagName,
                attributes: attrs,
                children: [],
                isInteractive: isElemInteractive,
                isVisible: isVisibleEl,
                ref: el
            };

            if (isElemInteractive && isVisibleEl) {
                node.highlightIndex = elementIndex++;
            }

            tree.map[nodeId] = node;
            if (parentId && tree.map[parentId]) {
                tree.map[parentId].children.push(nodeId);
            }

            // Process all child nodes (elements and text)
            for (const child of el.childNodes) {
                if (child.nodeType === Node.TEXT_NODE) {
                    // Text node - check if it has meaningful content
                    const text = child.textContent?.trim();
                    if (text && text.length > 0) {
                        const textNodeId = generateNodeId();
                        const textNode = {
                            id: textNodeId,
                            type: 'TEXT_NODE',
                            text: text,
                            isVisible: isVisibleEl
                        };
                        tree.map[textNodeId] = textNode;
                        node.children.push(textNodeId);
                    }
                } else if (child.nodeType === Node.ELEMENT_NODE) {
                    processElement(child, nodeId, depth + 1);
                }
            }
        }

        // Process from body
        processElement(document.body, 'root', 0);

        return tree;
    }

    function flatTreeToString(flatTree, includeAttributes = [], keepSemanticTags = false) {
        const DEFAULT_INCLUDE_ATTRIBUTES = [
            'title', 'type', 'checked', 'name', 'role', 'value', 'placeholder',
            'alt', 'aria-label', 'aria-expanded', 'data-state', 'aria-checked',
            'id', 'for', 'target', 'aria-haspopup', 'aria-controls', 'aria-owns',
            'contenteditable'
        ];

        const includeAttrs = [...includeAttributes, ...DEFAULT_INCLUDE_ATTRIBUTES];

        function capTextLength(text, maxLength) {
            return text.length > maxLength ? text.substring(0, maxLength) + '...' : text;
        }

        function buildTreeNode(nodeId) {
            const node = flatTree.map[nodeId];
            if (!node) return null;

            const children = [];
            if (node.children) {
                for (const childId of node.children) {
                    const child = buildTreeNode(childId);
                    if (child) children.push(child);
                }
            }

            // Handle text nodes
            if (node.type === 'TEXT_NODE') {
                return {
                    type: 'text',
                    text: node.text || node.textContent || '',
                    isVisible: node.isVisible,
                    parent: null,
                    children: []
                };
            }

            return {
                type: 'element',
                tagName: node.tagName,
                attributes: node.attributes || {},
                isVisible: node.isVisible,
                isInteractive: node.isInteractive,
                highlightIndex: node.highlightIndex,
                children
            };
        }

        function getAllTextTillNextClickableElement(node, maxDepth = -1) {
            const textParts = [];
            const collectText = (currentNode, currentDepth) => {
                if (maxDepth !== -1 && currentDepth > maxDepth) return;
                if (currentNode.type === 'text') {
                    if (currentNode.text) {
                        textParts.push(currentNode.text);
                    }
                } else if (currentNode.type === 'element') {
                    // Skip this element if it's highlighted (clickable)
                    if (currentNode !== node && currentNode.highlightIndex !== undefined) return;
                    for (const child of currentNode.children) {
                        collectText(child, currentDepth + 1);
                    }
                }
            };
            collectText(node, 0);
            return textParts.join('\n').trim();
        }

        function matchAttributes(attrs, patterns) {
            const result = {};
            for (const pattern of patterns) {
                if (pattern.includes('*')) {
                    const regex = new RegExp('^' + pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*') + '$');
                    for (const key of Object.keys(attrs)) {
                        if (regex.test(key) && attrs[key].trim()) {
                            result[key] = attrs[key].trim();
                        }
                    }
                } else {
                    const value = attrs[pattern];
                    if (value && value.trim()) result[pattern] = value.trim();
                }
            }
            return result;
        }

        const rootNode = buildTreeNode(flatTree.rootId);
        if (!rootNode) return '';

        const result = [];
        const processNode = (node, depth) => {
            const depthStr = '\t'.repeat(depth);

            if (node.type === 'element') {
                const isSemantic = keepSemanticTags && node.tagName && SEMANTIC_TAGS.has(node.tagName);

                if (node.highlightIndex !== undefined) {
                    const text = getAllTextTillNextClickableElement(node);
                    let attributesHtmlStr = '';

                    if (includeAttrs.length > 0 && node.attributes) {
                        const attributesToInclude = matchAttributes(node.attributes, includeAttrs);
                        if (Object.keys(attributesToInclude).length > 0) {
                            attributesHtmlStr = Object.entries(attributesToInclude)
                                .map(([key, value]) => `${key}=${capTextLength(value, 20)}`)
                                .join(' ');
                        }
                    }

                    const highlightIndicator = `[${node.highlightIndex}]`;
                    let line = `${depthStr}${highlightIndicator}<${node.tagName || ''}`;
                    if (attributesHtmlStr) line += ` ${attributesHtmlStr}`;
                    if (text) {
                        line += `>${text.trim()}`;
                    }
                    line += ' />';
                    result.push(line);
                }

                const emitSemantic = isSemantic && node.highlightIndex === undefined;
                const mark = emitSemantic ? result.length : -1;

                if (emitSemantic) {
                    result.push(`${depthStr}<${node.tagName}>`);
                }

                for (const child of node.children) {
                    processNode(child, node.highlightIndex !== undefined ? depth + 1 : depth);
                }

                if (emitSemantic) {
                    if (result.length === mark + 1) result.pop();
                    else result.push(`${depthStr}</${node.tagName}>`);
                }
            } else if (node.type === 'text') {
                // Text node - output if parent is visible top element
                // This matches the original PageController logic
                if (node.text && node.isVisible) {
                    result.push(`${depthStr}${node.text.trim()}`);
                }
            }
        };

        processNode(rootNode, 0);
        return result.join('\n');
    }

    function getSelectorMap(flatTree) {
        const selectorMap = new Map();
        for (const nodeId in flatTree.map) {
            const node = flatTree.map[nodeId];
            if (node.isInteractive && typeof node.highlightIndex === 'number') {
                selectorMap.set(node.highlightIndex, node);
            }
        }
        return selectorMap;
    }

    function getElementTextMap(simplifiedHTML) {
        const elementTextMap = new Map();
        const lines = simplifiedHTML.split('\n').filter(line => line.trim());
        for (const line of lines) {
            const match = /\[(\d+)\]<[^>]+>([^<]*)/.exec(line);
            if (match) {
                elementTextMap.set(parseInt(match[1], 10), line);
            }
        }
        return elementTextMap;
    }

    function cleanUpHighlights() {
        const cleanupFunctions = window._highlightCleanupFunctions || [];
        for (const cleanup of cleanupFunctions) {
            if (typeof cleanup === 'function') cleanup();
        }
        window._highlightCleanupFunctions = [];
    }

    // ======= Element Actions =======

    function getElementByIndex(selectorMap, index) {
        const interactiveNode = selectorMap.get(index);
        if (!interactiveNode) throw new Error(`No interactive element found at index ${index}`);
        const element = interactiveNode.ref;
        if (!element) throw new Error(`Element at index ${index} does not have a reference`);
        if (!isHTMLElement(element)) throw new Error(`Element at index ${index} is not an HTMLElement`);
        return element;
    }

    async function clickElement(element) {
        blurLastClickedElement();
        window._lastClickedElement = element;

        await scrollIntoViewIfNeeded(element);
        const frame = element.ownerDocument.defaultView?.frameElement;
        if (frame) await scrollIntoViewIfNeeded(frame);

        const rect = element.getBoundingClientRect();
        const x = rect.left + rect.width / 2;
        const y = rect.top + rect.height / 2;

        await movePointerToElement(element, x, y);
        await clickPointer();
        await waitFor(0.1);

        const doc = element.ownerDocument;
        await enablePassThrough();
        const hitTarget = doc.elementFromPoint(x, y);
        await disablePassThrough();
        const target = hitTarget instanceof HTMLElement && element.contains(hitTarget) ? hitTarget : element;

        const pointerOpts = { bubbles: true, cancelable: true, clientX: x, clientY: y, pointerType: 'mouse' };
        const mouseOpts = { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 };

        target.dispatchEvent(new PointerEvent('pointerover', pointerOpts));
        target.dispatchEvent(new PointerEvent('pointerenter', { ...pointerOpts, bubbles: false }));
        target.dispatchEvent(new MouseEvent('mouseover', mouseOpts));
        target.dispatchEvent(new MouseEvent('mouseenter', { ...mouseOpts, bubbles: false }));

        target.dispatchEvent(new PointerEvent('pointerdown', pointerOpts));
        target.dispatchEvent(new MouseEvent('mousedown', mouseOpts));

        element.focus({ preventScroll: true });

        target.dispatchEvent(new PointerEvent('pointerup', pointerOpts));
        target.dispatchEvent(new MouseEvent('mouseup', mouseOpts));
        target.click();

        await waitFor(0.2);
        blurLastClickedElement();
    }

    async function inputTextElement(element, text) {
        const isContentEditable = element.isContentEditable;
        if (!isInputElement(element) && !isTextAreaElement(element) && !isContentEditable) {
            throw new Error('Element is not an input, textarea, or contenteditable');
        }

        await clickElement(element);

        if (isContentEditable) {
            // Simplified contenteditable handling
            element.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'deleteContent' }));
            element.innerText = '';
            element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContent' }));

            element.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: text }));
            element.innerText = text;
            element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));

            element.dispatchEvent(new Event('change', { bubbles: true }));
            element.blur();
        } else {
            getNativeValueSetter(element).call(element, text);
            element.dispatchEvent(new Event('input', { bubbles: true }));
        }

        await waitFor(0.1);
        blurLastClickedElement();
    }

    async function selectOptionElement(selectElement, optionText) {
        if (!isSelectElement(selectElement)) throw new Error('Element is not a select element');

        const options = Array.from(selectElement.options);
        const option = options.find(opt => opt.textContent?.trim() === optionText.trim());
        if (!option) throw new Error(`Option with text "${optionText}" not found`);

        selectElement.value = option.value;
        selectElement.dispatchEvent(new Event('change', { bubbles: true }));
        await waitFor(0.1);
    }

    async function scrollVertically(scrollAmount, element) {
        if (element) {
            let currentElement = element;
            let scrollSuccess = false;
            let scrolledElement = null;
            let scrollDelta = 0;

            for (let attempts = 0; attempts < 10 && currentElement; attempts++) {
                const computedStyle = window.getComputedStyle(currentElement);
                const hasScrollableY = /(auto|scroll|overlay)/.test(computedStyle.overflowY);
                const canScrollVertically = currentElement.scrollHeight > currentElement.clientHeight;

                if (hasScrollableY && canScrollVertically) {
                    const beforeScroll = currentElement.scrollTop;
                    const maxScroll = currentElement.scrollHeight - currentElement.clientHeight;
                    let amount = scrollAmount / 3;

                    if (amount > 0) amount = Math.min(amount, maxScroll - beforeScroll);
                    else amount = Math.max(amount, -beforeScroll);

                    currentElement.scrollTop = beforeScroll + amount;
                    const actualDelta = currentElement.scrollTop - beforeScroll;

                    if (Math.abs(actualDelta) > 0.5) {
                        scrollSuccess = true;
                        scrolledElement = currentElement;
                        scrollDelta = actualDelta;
                        break;
                    }
                }

                if (currentElement === document.body || currentElement === document.documentElement) break;
                currentElement = currentElement.parentElement;
            }

            if (scrollSuccess) return `Scrolled container (${scrolledElement?.tagName}) by ${scrollDelta}px`;
            return `No scrollable container found for element (${element.tagName})`;
        }

        // Page-level scrolling
        const scrollBefore = window.scrollY;
        const scrollMax = document.documentElement.scrollHeight - window.innerHeight;
        window.scrollBy(0, scrollAmount);

        const scrolled = window.scrollY - scrollBefore;
        if (Math.abs(scrolled) < 1) {
            return scrollAmount > 0 ? 'Already at the bottom of the page' : 'Already at the top of the page';
        }

        const reachedBottom = scrollAmount > 0 && window.scrollY >= scrollMax - 1;
        const reachedTop = scrollAmount < 0 && window.scrollY <= 1;

        if (reachedBottom) return `Scrolled page by ${scrolled}px. Reached the bottom.`;
        if (reachedTop) return `Scrolled page by ${scrolled}px. Reached the top.`;
        return `Scrolled page by ${scrolled}px.`;
    }

    async function scrollHorizontally(scrollAmount, element) {
        if (element) {
            let currentElement = element;
            let scrollSuccess = false;
            let scrolledElement = null;
            let scrollDelta = 0;

            for (let attempts = 0; attempts < 10 && currentElement; attempts++) {
                const computedStyle = window.getComputedStyle(currentElement);
                const hasScrollableX = /(auto|scroll|overlay)/.test(computedStyle.overflowX);
                const canScrollHorizontally = currentElement.scrollWidth > currentElement.clientWidth;

                if (hasScrollableX && canScrollHorizontally) {
                    const beforeScroll = currentElement.scrollLeft;
                    const maxScroll = currentElement.scrollWidth - currentElement.clientWidth;
                    let amount = scrollAmount / 3;

                    if (amount > 0) amount = Math.min(amount, maxScroll - beforeScroll);
                    else amount = Math.max(amount, -beforeScroll);

                    currentElement.scrollLeft = beforeScroll + amount;
                    const actualDelta = currentElement.scrollLeft - beforeScroll;

                    if (Math.abs(actualDelta) > 0.5) {
                        scrollSuccess = true;
                        scrolledElement = currentElement;
                        scrollDelta = actualDelta;
                        break;
                    }
                }

                if (currentElement === document.body || currentElement === document.documentElement) break;
                currentElement = currentElement.parentElement;
            }

            if (scrollSuccess) return `Scrolled container (${scrolledElement?.tagName}) horizontally by ${scrollDelta}px`;
            return `No horizontally scrollable container found for element (${element.tagName})`;
        }

        // Page-level horizontal scrolling
        const scrollBefore = window.scrollX;
        window.scrollBy(scrollAmount, 0);

        const scrolled = window.scrollX - scrollBefore;
        if (Math.abs(scrolled) < 1) {
            return scrollAmount > 0 ? 'Already at the right edge' : 'Already at the left edge';
        }
        return `Scrolled page horizontally by ${scrolled}px.`;
    }

    // ======= PageInfo =======

    function getPageInfo() {
        return {
            viewport_width: window.innerWidth,
            viewport_height: window.innerHeight,
            page_width: document.documentElement.scrollWidth,
            page_height: document.documentElement.scrollHeight,
            pixels_above: window.scrollY,
            pixels_below: document.documentElement.scrollHeight - window.innerHeight - window.scrollY,
            pages_above: window.scrollY / window.innerHeight,
            pages_below: (document.documentElement.scrollHeight - window.innerHeight - window.scrollY) / window.innerHeight,
            total_pages: document.documentElement.scrollHeight / window.innerHeight,
            current_page_position: window.scrollY / (document.documentElement.scrollHeight - window.innerHeight) || 0
        };
    }

    // ======= BrowserUseClient Class =======

    class BrowserUseClient extends EventTarget {
        constructor(wsUrl, userToken = null) {
            super();
            this.wsUrl = wsUrl;
            this.userToken = userToken;
            this.ws = null;
            this.connected = false;
            this.sessionId = null;  // Generated async in connect()
            this.pendingRequests = new Map();
            this.requestId = 0;

            // PageController state
            this.selectorMap = new Map();
            this.elementTextMap = new Map();
            this.simplifiedHTML = '<EMPTY>';
            this.isIndexed = false;
            this.lastTimeUpdate = 0;
            this.config = {};

            // Setup URL change listener
            window.addEventListener('popstate', () => cleanUpHighlights());
            window.addEventListener('hashchange', () => cleanUpHighlights());
            window.addEventListener('beforeunload', () => cleanUpHighlights());
        }

        async _generateSessionId() {
            // Generate deterministic session ID from URL + userToken using UUID v5
            const url = window.location.href.split('#')[0];
            const combined = this.userToken ? `${url}|${this.userToken}` : url;
            return await this._uuidv5(combined);
        }

        async _uuidv5(name) {
            // UUID v5 - deterministic name-based UUID using Web Crypto API
            // Namespace URL: 6ba7b811-9dad-11d1-80b4-00c04fd430c8
            const namespace = new TextEncoder().encode('6ba7b811-9dad-11d1-80b4-00c04fd430c8');
            const nameBytes = new TextEncoder().encode(name);
            const data = new Uint8Array(namespace.length + nameBytes.length);
            data.set(namespace);
            data.set(nameBytes, namespace.length);

            const hash = await crypto.subtle.digest('SHA-1', data);
            const bytes = Array.from(new Uint8Array(hash)).slice(0, 16);

            bytes[6] = (bytes[6] & 0x0f) | 0x50;
            bytes[8] = (bytes[8] & 0x3f) | 0x80;

            const hex = bytes.map(b => b.toString(16).padStart(2, '0')).join('');
            return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
        }

        connect() {
            return new Promise((resolve, reject) => {
                try {
                    this.ws = new WebSocket(this.wsUrl);

                    this.ws.onopen = async () => {
                        // Generate session_id from URL before connecting
                        this.sessionId = await this._generateSessionId();
                        console.log('[BrowserUseClient] Connected to server, session_id:', this.sessionId);
                        this.connected = true;
                        // Send initial message with session_id
                        this.ws.send(JSON.stringify({
                            id: 1,
                            method: 'init',
                            params: { session_id: this.sessionId }
                        }));
                        this.dispatchEvent(new Event('connect'));
                        resolve();
                    };

                    this.ws.onmessage = (event) => this._handleMessage(event.data);
                    this.ws.onerror = (error) => {
                        console.error('[BrowserUseClient] WebSocket error:', error);
                        this.dispatchEvent(new Event('error'));
                    };
                    this.ws.onclose = () => {
                        console.log('[BrowserUseClient] Disconnected from server');
                        this.connected = false;
                        this.sessionId = null;  // Will be re-generated on reconnect
                        this.dispatchEvent(new Event('disconnect'));
                    };
                } catch (err) {
                    reject(err);
                }
            });
        }

        disconnect() {
            if (this.ws) {
                this.ws.close();
                this.ws = null;
            }
            this.connected = false;
        }

        _sendRequest(method, params = {}) {
            return new Promise((resolve, reject) => {
                if (!this.connected || !this.ws) {
                    reject(new Error('Not connected to server'));
                    return;
                }

                const id = ++this.requestId;
                const request = { id, method, params: { ...params, session_id: this.sessionId } };

                // Hold the timer handle so we can cancel it when the response
                // arrives — otherwise the timer fires 30s later and (harmlessly
                // but wastefully) holds a closure over `resolve`/`reject` per
                // request, leaking memory under sustained LLM traffic.
                const timer = setTimeout(() => {
                    if (this.pendingRequests.has(id)) {
                        this.pendingRequests.delete(id);
                        reject(new Error(`Request ${method} timed out`));
                    }
                }, 30000);

                this.pendingRequests.set(id, { resolve, reject, timer });
                this.ws.send(JSON.stringify(request));
            });
        }

        _handleMessage(data) {
            try {
                const message = JSON.parse(data);

                // Handle server-initiated commands (id=0)
                if (message.id === 0 && message.method && message.params !== undefined) {
                    this._handleServerCommand(message.method, message.params);
                    return;
                }

                // Handle responses to client-initiated requests
                if (message.id && this.pendingRequests.has(message.id)) {
                    const entry = this.pendingRequests.get(message.id);
                    this.pendingRequests.delete(message.id);
                    // Cancel the 30s timeout we set in _sendRequest — otherwise
                    // it fires 30s after the response already arrived, holding
                    // a closure over `resolve`/`reject` for no reason.
                    if (entry && entry.timer) clearTimeout(entry.timer);
                    if (message.error) entry.reject(new Error(message.error));
                    else entry.resolve(message.result);
                } else if (message.type === 'response') {
                    this.dispatchEvent(new CustomEvent(message.method, { detail: message.data }));
                }
            } catch (err) {
                console.error('[BrowserUseClient] Failed to parse message:', err);
            }
        }

        async _handleServerCommand(method, params) {
            try {
                let result;
                switch (method) {
                    case 'updateTree':
                        result = await this.updateTree();
                        break;
                    case 'getBrowserState':
                        result = await this.getBrowserState();
                        break;
                    case 'clickElement':
                        result = await this.clickElement(params.index);
                        break;
                    case 'inputText':
                        result = await this.inputText(params.index, params.text);
                        break;
                    case 'selectOption':
                        result = await this.selectOption(params.index, params.optionText);
                        break;
                    case 'scroll':
                        result = await this.scroll(params);
                        break;
                    case 'scrollHorizontally':
                        result = await this.scrollHorizontally(params);
                        break;
                    case 'executeJavascript':
                        result = await this.executeJavascript(params.script);
                        break;
                    case 'getCurrentUrl':
                        result = await this.getCurrentUrl();
                        break;
                    case 'cleanUpHighlights':
                        result = await this.cleanUpHighlights();
                        break;
                    // ======= Map-specific commands (target window.__map on map.html) =======
                    case 'mapRunSearch':
                        result = await this.mapRunSearch(params.keyword);
                        break;
                    case 'mapGetState':
                        result = await this.mapGetState();
                        break;
                    case 'mapSetCenter':
                        result = await this.mapSetCenter(params.lng, params.lat);
                        break;
                    case 'mapSetZoom':
                        result = await this.mapSetZoom(params.zoom);
                        break;
                    case 'mapZoomIn':
                        result = await this.mapZoomIn();
                        break;
                    case 'mapZoomOut':
                        result = await this.mapZoomOut();
                        break;
                    case 'mapAddMarker':
                        result = await this.mapAddMarker(params.lng, params.lat, params.title);
                        break;
                    case 'mapAddMarkerWithInfo':
                        result = await this.mapAddMarkerWithInfo(params.lng, params.lat, params.title, params.info_html, params.poi);
                        break;
                    case 'mapClearMarkers':
                        result = await this.mapClearMarkers();
                        break;
                    case 'mapLocate':
                        result = await this.mapLocate();
                        break;
                    case 'mapSearchAndZoom':
                        result = await this.mapSearchAndZoom(params.keyword, params.zoom);
                        break;
                    case 'mapDrawPolyline':
                        result = await this.mapDrawPolyline(params.path, params.options);
                        break;
                    case 'mapDrawPolygon':
                        result = await this.mapDrawPolygon(params.path, params.options);
                        break;
                    case 'mapDrawCircle':
                        result = await this.mapDrawCircle(params.lng, params.lat, params.radius, params.options);
                        break;
                    case 'mapOpenInfoWindow':
                        result = await this.mapOpenInfoWindow(params.lng, params.lat, params.content);
                        break;
                    case 'mapCloseInfoWindow':
                        result = await this.mapCloseInfoWindow();
                        break;
                    case 'mapFitView':
                        result = await this.mapFitView();
                        break;
                    case 'mapClearOverlays':
                        result = await this.mapClearOverlays(params.type);
                        break;
                    case 'mapRemoveOverlay':
                        result = await this.mapRemoveOverlay(params.type, params.index);
                        break;
                    case 'mapGeocode':
                        result = await this.mapGeocode(params.address, params.city);
                        break;
                    case 'mapListOverlays':
                        result = await this.mapListOverlays();
                        break;
                    case 'mapSearchNearby':
                        result = await this.mapSearchNearby(params);
                        break;
                    default:
                        console.error('[BrowserUseClient] Unknown server command:', method);
                        result = { success: false, message: `Unknown command: ${method}` };
                }
                // Send response back to server
                if (this.connected && this.ws) {
                    this.ws.send(JSON.stringify({ id: 0, method: method, result: result, session_id: this.sessionId }));
                }
            } catch (err) {
                console.error('[BrowserUseClient] Error handling server command:', err);
                if (this.connected && this.ws) {
                    this.ws.send(JSON.stringify({ id: 0, method: method, result: { success: false, message: String(err) }, session_id: this.sessionId }));
                }
            }
        }

        // ======= DOM Operations =======

        async updateTree() {
            cleanUpHighlights();
            this.lastTimeUpdate = Date.now();

            const blacklist = [
                ...(this.config.interactiveBlacklist || []),
                ...Array.from(document.querySelectorAll('[data-page-agent-not-interactive]'))
            ];

            const flatTree = getFlatTree({
                ...this.config,
                interactiveBlacklist: blacklist
            });

            this.simplifiedHTML = flatTreeToString(flatTree, this.config.includeAttributes, this.config.keepSemanticTags);
            this.selectorMap = getSelectorMap(flatTree);
            this.elementTextMap = getElementTextMap(this.simplifiedHTML);
            this.isIndexed = true;

            return this.simplifiedHTML;
        }

        async cleanUpHighlights() {
            cleanUpHighlights();
        }

        // ======= Element Actions =======

        _assertIndexed() {
            if (!this.isIndexed) {
                throw new Error('DOM tree not indexed yet. Call updateTree() first.');
            }
        }

        async clickElement(index) {
            try {
                this._assertIndexed();
                const element = getElementByIndex(this.selectorMap, index);
                const elemText = this.elementTextMap.get(index);
                await clickElement(element);

                if (isAnchorElement(element) && element.target === '_blank') {
                    return { success: true, message: `Clicked element (${elemText ?? index}). Link opened in a new tab.` };
                }
                return { success: true, message: `Clicked element (${elemText ?? index}).` };
            } catch (error) {
                return { success: false, message: `Failed to click element: ${error}` };
            }
        }

        async inputText(index, text) {
            try {
                this._assertIndexed();
                const element = getElementByIndex(this.selectorMap, index);
                const elemText = this.elementTextMap.get(index);
                await inputTextElement(element, text);
                return { success: true, message: `Input text (${text}) into element (${elemText ?? index}).` };
            } catch (error) {
                return { success: false, message: `Failed to input text: ${error}` };
            }
        }

        async selectOption(index, optionText) {
            try {
                this._assertIndexed();
                const element = getElementByIndex(this.selectorMap, index);
                const elemText = this.elementTextMap.get(index);
                await selectOptionElement(element, optionText);
                return { success: true, message: `Selected option (${optionText}) in element (${elemText ?? index}).` };
            } catch (error) {
                return { success: false, message: `Failed to select option: ${error}` };
            }
        }

        async scroll(options) {
            try {
                const { down, numPages, pixels, index } = options;
                this._assertIndexed();
                const scrollAmount = (pixels ?? numPages * window.innerHeight) * (down ? 1 : -1);
                const element = index !== undefined ? getElementByIndex(this.selectorMap, index) : null;
                const message = await scrollVertically(scrollAmount, element);
                return { success: true, message };
            } catch (error) {
                return { success: false, message: `Failed to scroll: ${error}` };
            }
        }

        async scrollHorizontally(options) {
            try {
                const { right, pixels, index } = options;
                this._assertIndexed();
                const scrollAmount = pixels * (right ? 1 : -1);
                const element = index !== undefined ? getElementByIndex(this.selectorMap, index) : null;
                const message = await scrollHorizontally(scrollAmount, element);
                return { success: true, message };
            } catch (error) {
                return { success: false, message: `Failed to scroll horizontally: ${error}` };
            }
        }

        async executeJavascript(script) {
            try {
                const asyncFn = eval(`(async () => { ${script} })`);
                const result = await asyncFn();
                return { success: true, message: `Executed JavaScript. Result: ${result}` };
            } catch (error) {
                return { success: false, message: `Error executing JavaScript: ${error}` };
            }
        }

        // ======= Map-specific Operations =======
        //
        // These methods target a page that exposes `window.__map` (see
        // map/map.html). On other pages they will return `{ success: false }`
        // rather than throwing, so the server can probe availability safely.

        _getMapInterface() {
            const m = window.__map;
            if (!m || !m.map) {
                throw new Error(
                    'window.__map is not available. ' +
                    'This page is not a Map page (or the AMap script has not finished loading).'
                );
            }
            return m;
        }

        async mapRunSearch(keyword) {
            try {
                const m = this._getMapInterface();
                const kw = (keyword || '').trim();
                if (!kw) return { success: false, message: '搜索关键词不能为空。' };
                m.runSearch(kw);
                // AMap.PlaceSearch is async — give the callback a moment to
                // update the info bar / markers before we return.
                await waitFor(1.0);
                const info = document.getElementById('info')?.textContent || '';
                return { success: true, message: `已发起搜索：${kw}`, keyword: kw, info };
            } catch (error) {
                return { success: false, message: `搜索失败: ${error}` };
            }
        }

        async mapSearchAndZoom(keyword, zoom = 15) {
            try {
                const m = this._getMapInterface();
                const kw = (keyword || '').trim();
                if (!kw) return { success: false, message: '搜索关键词不能为空。' };
                m.runSearch(kw);
                await waitFor(1.2);
                m.map.setZoom(Number(zoom));
                return { success: true, message: `已搜索「${kw}」并设置缩放为 ${zoom}`, keyword: kw, zoom: Number(zoom) };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapGetState() {
            try {
                const m = this._getMapInterface();
                const c = m.map.getCenter();
                const z = m.map.getZoom();
                return {
                    success: true,
                    center: { lng: c.lng, lat: c.lat },
                    zoom: z,
                    bounds: (() => {
                        try {
                            const b = m.map.getBounds();
                            return {
                                southWest: { lng: b.southwest.lng, lat: b.southwest.lat },
                                northEast: { lng: b.northeast.lng, lat: b.northeast.lat },
                            };
                        } catch { return null; }
                    })(),
                    hasGeolocation: !!m.geolocation,
                    overlayCount: m.map.getAllOverlays().length,
                    infoText: document.getElementById('info')?.textContent || '',
                    searchInputValue: document.getElementById('search-input')?.value || '',
                };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapSetCenter(lng, lat) {
            try {
                const m = this._getMapInterface();
                m.map.setCenter([Number(lng), Number(lat)]);
                return { success: true, message: `地图已居中到 (${lng}, ${lat})`, center: { lng: Number(lng), lat: Number(lat) } };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapSetZoom(zoom) {
            try {
                const m = this._getMapInterface();
                const z = Number(zoom);
                m.map.setZoom(z);
                return { success: true, message: `缩放级别已设置为 ${z}`, zoom: z };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapZoomIn() {
            try {
                this._getMapInterface().map.zoomIn();
                return { success: true };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapZoomOut() {
            try {
                this._getMapInterface().map.zoomOut();
                return { success: true };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapAddMarker(lng, lat, title) {
            try {
                const m = this._getMapInterface();
                m.addMarker([Number(lng), Number(lat)], title || '');
                return {
                    success: true,
                    message: `已添加标记 (${lng}, ${lat}) ${title ? '「' + title + '」' : ''}`,
                    lng: Number(lng), lat: Number(lat), title: title || ''
                };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        // Add a marker that, when clicked, opens a rich info window.
        // `info_html`: pre-formatted HTML string. If the LLM has a POI
        // object from map_search_nearby, pass `poi` instead — the helper
        // formats a standardized card (name / type / distance / address /
        // tel / business / rating / cost / photo).
        // Click behavior: only one info window is open at a time; clicking
        // a new marker closes the previous one.
        async mapAddMarkerWithInfo(lng, lat, title, info_html, poi) {
            try {
                const m = this._getMapInterface();
                if (!m.addMarkerWithInfo) {
                    return { success: false, message: 'addMarkerWithInfo 不可用（map.html 未升级到最新模板）。' };
                }
                const r = m.addMarkerWithInfo(
                    [Number(lng), Number(lat)],
                    title || '',
                    info_html != null ? info_html : (poi || null)
                );
                return { success: true, message: '已添加可点击标记。', ...r };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapClearMarkers() {
            try {
                const m = this._getMapInterface();
                if (!m.clearOverlays) {
                    return { success: false, message: 'clearOverlays 不可用。' };
                }
                m.clearOverlays('marker');
                return { success: true, message: '已清空地图标记。' };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapLocate() {
            try {
                const m = this._getMapInterface();
                if (!m.locate) {
                    return { success: false, message: 'locate 不可用（map.html 未升级）。' };
                }
                const r = await m.locate();
                return {
                    success: true,
                    lng: r.lng, lat: r.lat,
                    formatted_address: r.formatted_address,
                    message: `定位成功：${r.formatted_address || ''} @ ${r.lng.toFixed(5)}, ${r.lat.toFixed(5)}`,
                };
            } catch (error) {
                return { success: false, message: `定位失败: ${error}` };
            }
        }

        // ======= Map drawing / overlay operations =======

        async mapDrawPolyline(path, options = {}) {
            try {
                const m = this._getMapInterface();
                if (!m.addPolyline) {
                    return { success: false, message: 'addPolyline 不可用。' };
                }
                const r = m.addPolyline(path || [], options || {});
                return {
                    success: true,
                    message: `已绘制折线（${(path || []).length} 个点）。`,
                    ...r,
                };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapDrawPolygon(path, options = {}) {
            try {
                const m = this._getMapInterface();
                if (!m.addPolygon) {
                    return { success: false, message: 'addPolygon 不可用。' };
                }
                const r = m.addPolygon(path || [], options || {});
                return {
                    success: true,
                    message: `已绘制多边形（${(path || []).length} 个顶点）。`,
                    ...r,
                };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapDrawCircle(lng, lat, radius, options = {}) {
            try {
                const m = this._getMapInterface();
                if (!m.addCircle) {
                    return { success: false, message: 'addCircle 不可用。' };
                }
                const r = m.addCircle([Number(lng), Number(lat)], Number(radius), options || {});
                return {
                    success: true,
                    message: `已绘制圆形（半径 ${radius} 米）。`,
                    ...r,
                };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapOpenInfoWindow(lng, lat, content) {
            try {
                const m = this._getMapInterface();
                if (!m.openInfoWindow) {
                    return { success: false, message: 'openInfoWindow 不可用。' };
                }
                m.openInfoWindow([Number(lng), Number(lat)], content || '');
                return { success: true, message: '已打开信息窗。' };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapCloseInfoWindow() {
            try {
                const m = this._getMapInterface();
                if (m.closeInfoWindow) m.closeInfoWindow();
                return { success: true };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapFitView() {
            try {
                const m = this._getMapInterface();
                if (!m.fitView) {
                    return { success: false, message: 'fitView 不可用。' };
                }
                const r = m.fitView();
                const state = await this.mapGetState();
                return {
                    success: true,
                    message: '已自适应视图。',
                    fitted: r.fitted,
                    center: state.center,
                    zoom: state.zoom,
                };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapClearOverlays(type) {
            try {
                const m = this._getMapInterface();
                if (!m.clearOverlays) {
                    return { success: false, message: 'clearOverlays 不可用。' };
                }
                const r = m.clearOverlays(type || 'all');
                return { success: true, message: `已清空 (${r.cleared})。` };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapRemoveOverlay(type, index) {
            try {
                const m = this._getMapInterface();
                if (!m.removeOverlay) {
                    return { success: false, message: 'removeOverlay 不可用。' };
                }
                const r = m.removeOverlay(type, Number(index));
                return {
                    success: r.removed,
                    message: r.removed ? '已删除覆盖物。' : '未找到该覆盖物。',
                    remaining: r.remaining,
                };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        async mapGeocode(address, city) {
            try {
                const m = this._getMapInterface();
                if (!m.geocode) {
                    return { success: false, message: 'geocode 不可用。' };
                }
                const r = await m.geocode(address, city);
                return {
                    success: true,
                    message: `已解析：${r.formatted_address}`,
                    lng: r.lng, lat: r.lat,
                    formatted_address: r.formatted_address,
                    level: r.level,
                    all: r.all,
                };
            } catch (error) {
                return { success: false, message: `地理编码失败: ${error}` };
            }
        }

        async mapListOverlays() {
            try {
                const m = this._getMapInterface();
                if (!m.listOverlays) {
                    return { success: false, message: 'listOverlays 不可用。' };
                }
                return { success: true, ...m.listOverlays() };
            } catch (error) {
                return { success: false, message: String(error) };
            }
        }

        // POI nearby search — AMap PlaceSearch with type + location + radius.
        // params: { keyword, lng, lat, radius, type, city,
        //          exclude_keywords, include_keywords }
        //   - keyword: optional free-text
        //   - lng/lat: center (required)
        //   - radius: meters (required, e.g. 5000 for 5km)
        //   - type: AMap POI category code (optional, e.g. "141201" for 高中)
        //   - city: optional city bias
        //   - exclude_keywords: list (or comma-string) of substrings. POIs
        //                       whose `name` contains any of these are
        //                       dropped. E.g. ["驾校","培训","复读"] for 高中.
        //   - include_keywords: list (or comma-string) of substrings. If
        //                       non-empty, only POIs whose `name` contains
        //                       at least one of these are kept.
        // Returns: { success, count, total_before_filter, filtered_out,
        //          excluded_by_keyword: {kw: count, ...}, pois: [...] }
        // sorted by distance asc.
        async mapSearchNearby(params) {
            try {
                const m = this._getMapInterface();
                if (!m.searchNearby) {
                    return { success: false, message: 'searchNearby 不可用（map.html 未升级到最新模板）。' };
                }
                const lng = Number(params?.lng);
                const lat = Number(params?.lat);
                const radius = Number(params?.radius);
                if (!Number.isFinite(lng) || !Number.isFinite(lat)) {
                    return { success: false, message: 'mapSearchNearby: lng/lat 必填且必须是数字。' };
                }
                if (!Number.isFinite(radius) || radius <= 0) {
                    return { success: false, message: 'mapSearchNearby: radius 必填且必须为正数（米）。' };
                }
                const r = await m.searchNearby({
                    keyword: params?.keyword || "",
                    center: [lng, lat],
                    radius,
                    type: params?.type || undefined,
                    city: params?.city || undefined,
                    exclude_keywords: params?.exclude_keywords || undefined,
                    include_keywords: params?.include_keywords || undefined,
                });
                return {
                    success: true,
                    count: r.count,
                    total_before_filter: r.total_before_filter,
                    filtered_out: r.filtered_out,
                    excluded_by_keyword: r.excluded_by_keyword || {},
                    pois: r.pois,
                };
            } catch (error) {
                return { success: false, message: `mapSearchNearby 失败: ${error}` };
            }
        }

        // ======= Route Planning (AMap built-in plugins) =======

        async mapDrivingRoute(origin, destination, waypoints) {
            try {
                const m = this._getMapInterface();
                if (!m.drivingRoute) {
                    return { success: false, message: 'drivingRoute 不可用（map.html 未升级）。' };
                }
                const r = await m.drivingRoute(origin, destination, {
                    waypoints: waypoints || undefined,
                });
                return {
                    success: true,
                    distance_m: r.distance_m,
                    duration_s: r.duration_s,
                    path_points: r.path_points,
                    message: `驾车路线已绘制：${(r.distance_m / 1000).toFixed(1)} km，约 ${Math.round(r.duration_s / 60)} 分钟`,
                };
            } catch (error) {
                return { success: false, message: `驾车路线规划失败: ${error}` };
            }
        }

        async mapWalkingRoute(origin, destination) {
            try {
                const m = this._getMapInterface();
                if (!m.walkingRoute) {
                    return { success: false, message: 'walkingRoute 不可用（map.html 未升级）。' };
                }
                const r = await m.walkingRoute(origin, destination);
                return {
                    success: true,
                    distance_m: r.distance_m,
                    duration_s: r.duration_s,
                    path_points: r.path_points,
                    message: `步行路线已绘制：${(r.distance_m / 1000).toFixed(1)} km，约 ${Math.round(r.duration_s / 60)} 分钟`,
                };
            } catch (error) {
                return { success: false, message: `步行路线规划失败: ${error}` };
            }
        }

        async mapRidingRoute(origin, destination) {
            try {
                const m = this._getMapInterface();
                if (!m.ridingRoute) {
                    return { success: false, message: 'ridingRoute 不可用（map.html 未升级）。' };
                }
                const r = await m.ridingRoute(origin, destination);
                return {
                    success: true,
                    distance_m: r.distance_m,
                    duration_s: r.duration_s,
                    path_points: r.path_points,
                    message: `骑行路线已绘制：${(r.distance_m / 1000).toFixed(1)} km，约 ${Math.round(r.duration_s / 60)} 分钟`,
                };
            } catch (error) {
                return { success: false, message: `骑行路线规划失败: ${error}` };
            }
        }

        async mapTransferRoute(origin, destination, city) {
            try {
                const m = this._getMapInterface();
                if (!m.transferRoute) {
                    return { success: false, message: 'transferRoute 不可用（map.html 未升级）。' };
                }
                const r = await m.transferRoute(origin, destination, city);
                return {
                    success: true,
                    distance_m: r.distance_m,
                    duration_s: r.duration_s,
                    path_points: r.path_points,
                    cost: r.cost,
                    message: `公交路线已绘制：${(r.distance_m / 1000).toFixed(1)} km，约 ${Math.round(r.duration_s / 60)} 分钟${r.cost ? `，费用约 ${r.cost} 元` : ''}`,
                };
            } catch (error) {
                return { success: false, message: `公交路线规划失败: ${error}` };
            }
        }

        // ======= District (AMap built-in plugin) =======

        async mapDrawDistrict(name, opts) {
            try {
                const m = this._getMapInterface();
                if (!m.drawDistrict) {
                    return { success: false, message: 'drawDistrict 不可用（map.html 未升级）。' };
                }
                const r = await m.drawDistrict(name, opts || {});
                return {
                    success: true,
                    name: r.name,
                    adcode: r.adcode,
                    level: r.level,
                    polygon_count: r.polygon_count,
                    center: r.center,
                    message: `已绘制「${r.name}」行政区（${r.polygon_count} 个多边形）。`,
                };
            } catch (error) {
                return { success: false, message: `行政区查询失败: ${error}` };
            }
        }

        // ======= Geometry (AMap.GeometryUtil) =======

        async mapDistance(p1, p2) {
            try {
                const m = this._getMapInterface();
                if (!m.distance) {
                    return { success: false, message: 'distance 不可用（map.html 未升级）。' };
                }
                const r = m.distance(p1, p2);
                if (r.error) return { success: false, message: r.error };
                return {
                    success: true,
                    meters: r.meters,
                    km: r.km,
                    message: `两点距离：${r.km} km`,
                };
            } catch (error) {
                return { success: false, message: `距离计算失败: ${error}` };
            }
        }

        // ======= State Queries =======

        async getCurrentUrl() {
            return window.location.href;
        }

        async getLastUpdateTime() {
            return this.lastTimeUpdate;
        }

        async getBrowserState() {
            const url = window.location.href;
            const title = document.title;
            const pi = getPageInfo();
            const viewportExpansion = resolveViewportExpansion(this.config.viewportExpansion);

            await this.updateTree();

            const titleLine = `Current Page: [${title}](${url})`;
            const pageInfoLine = `Page info: ${pi.viewport_width}x${pi.viewport_height}px viewport, ${pi.page_width}x${pi.page_height}px total page size, ${pi.pages_above.toFixed(1)} pages above, ${pi.pages_below.toFixed(1)} pages below, ${pi.total_pages.toFixed(1)} total pages, at ${(pi.current_page_position * 100).toFixed(0)}% of page`;

            const elementsLabel = viewportExpansion === -1
                ? 'Interactive elements from top layer of the current page (full page):'
                : 'Interactive elements from top layer of the current page inside the viewport:';

            const hasContentAbove = pi.pixels_above > 4;
            const scrollHintAbove = hasContentAbove && viewportExpansion !== -1
                ? `... ${pi.pixels_above} pixels above (${pi.pages_above.toFixed(1)} pages) - scroll to see more ...`
                : '[Start of page]';

            const header = `${titleLine}\n${pageInfoLine}\n\n${elementsLabel}\n\n${scrollHintAbove}`;

            const hasContentBelow = pi.pixels_below > 4;
            const footer = hasContentBelow && viewportExpansion !== -1
                ? `... ${pi.pixels_below} pixels below (${pi.pages_below.toFixed(1)} pages) - scroll to see more ...`
                : '[End of page]';

            return { url, title, header, content: this.simplifiedHTML, footer };
        }

        dispose() {
            cleanUpHighlights();
            this.selectorMap.clear();
            this.elementTextMap.clear();
            this.simplifiedHTML = '<EMPTY>';
            this.isIndexed = false;
            this.disconnect();
        }
    }

    // Export to global
    window.BrowserUseClient = BrowserUseClient;

})();