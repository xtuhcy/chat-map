// uuidv5.js — deterministic UUID v5 generator for the wxmp client.
//
// MUST match client/BrowserUseClient.js:_uuidv5 byte-for-byte (and the
// Python server/BrowserUseServerMCPController.py:_generate_session_id).
// Any divergence → the wxmp's session_id won't match what the server
// computes for the same inputs → commands get lost.
//
// IMPORTANT: We hash the *string form* of the namespace UUID
// ("6ba7b811-9dad-11d1-80b4-00c04fd430c8"), NOT its 16 bytes. This is
// a quirk of the original BrowserUseClient.js implementation that we
// replicate here for compatibility. RFC 4122 says to use the raw
// bytes — do NOT "fix" this without also changing the JS and Python
// sides.
//
// WeChat Mini Program does NOT expose the browser Web Crypto API
// (`crypto.subtle.digest`) — `wx.crypto` is undefined. We use the
// vendored pure-JS SHA-1 in ./sha1.js instead, which works in any
// ECMAScript runtime.

const { sha1, toHex } = require('./sha1.js');

const NAMESPACE = '6ba7b811-9dad-11d1-80b4-00c04fd430c8'; // RFC 4122 URL namespace, ASCII bytes

/**
 * Generate a UUID v5 string from a name.
 * @param {string} name - the name to hash (URL + '|' + userToken for our use case)
 * @returns {string} canonical 8-4-4-4-12 hex UUID
 */
function uuidv5(name) {
  // SHA-1 the namespace-string + name (synchronous, pure JS).
  const hashBuf = sha1(NAMESPACE + name);
  const bytes = Array.from(new Uint8Array(hashBuf)).slice(0, 16);

  // Version 5 (set high nibble of byte 6 to 0x50).
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  // Variant RFC 4122 (set high two bits of byte 8 to 0b10).
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = bytes.map((b) => b.toString(16).padStart(2, '0')).join('');
  return (
    hex.slice(0, 8) + '-' +
    hex.slice(8, 12) + '-' +
    hex.slice(12, 16) + '-' +
    hex.slice(16, 20) + '-' +
    hex.slice(20, 32)
  );
}

module.exports = { uuidv5 };