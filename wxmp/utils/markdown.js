// markdown.js — minimal markdown → WXML-friendly converter.
//
// WeChat Mini Program doesn't have a DOM, so we can't use `marked`
// directly (it targets HTML strings). WXML has its own subset of
// tags — `<text>`, `<view>`, etc. — and accepts plain text strings
// as children. The chat UI uses `rich-text` for rich content, which
// accepts an HTML-ish string.
//
// What we support (keep it small — the chat messages are short):
//   * `**bold**` → `<strong>...</strong>`
//   * `*em*` → `<em>...</em>`
//   * `` `code` `` → `<code>...</code>`
//   * `[label](url)` → `<a href="url">label</a>`
//   * newlines → `<br/>`
//   * HTML special chars escaped first (defense in depth — rich-text
//     in wxmp v2.7+ is XSS-safe by default for `<a>`, but better
//     safe).
//
// Output is suitable for `<rich-text nodes="{{...}}">` consumption.

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function render(md) {
  if (md == null) return '';
  let src = String(md);
  // Escape first so subsequent regexes don't accidentally match markup.
  src = escapeHtml(src);
  // Inline code (backticks) — do this BEFORE bold/em so the asterisks
  // inside backticks aren't interpreted.
  src = src.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
  // Bold (**x**) before em (*x*) so the same delimiter set works.
  src = src.replace(/\*\*([^*]+)\*\*/g, (_, x) => `<strong>${x}</strong>`);
  // Em (*x*) — single asterisk not preceded/followed by another.
  src = src.replace(/(^|[^*])\*([^*]+)\*/g, (_, pre, x) => `${pre}<em>${x}</em>`);
  // Links [label](url). URL validation is permissive — we don't want
  // to break `data:` or relative refs the LLM might produce.
  src = src.replace(
    /\[([^\]]+)\]\(([^)\s]+)\)/g,
    (_, label, url) => `<a href="${escapeHtml(url)}">${label}</a>`,
  );
  // Newlines → <br/>.
  src = src.replace(/\n/g, '<br/>');
  return src;
}

/**
 * Convert markdown text to a rich-text nodes array (the shape
 * `<rich-text nodes="{{nodes}}">` expects in WeChat Mini Program).
 * Falls back to a single text node if the input is plain.
 *
 * @param {string} md
 * @returns {Array<{name: string, attrs: object, children: Array}>}
 */
function toRichTextNodes(md) {
  const html = render(md);
  // rich-text's `nodes` accepts a pre-rendered HTML string directly,
  // so we just return [{name: 'div', attrs: {}, children: []}] with
  // a special 'html' attr... but the simpler form is to return the
  // rendered HTML as a single text node. wxmp's <rich-text> will
  // parse it.
  return [{ name: 'div', attrs: {}, children: [{ type: 'text', text: html }] }];
}

module.exports = { render, toRichTextNodes };