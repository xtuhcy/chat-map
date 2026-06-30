// sha1.js — pure-JS SHA-1, no runtime dependencies.
//
// WeChat Mini Program does NOT expose the Web Crypto API
// (`crypto.subtle.digest`) — `wx.crypto` is undefined. So we vendor a
// minimal SHA-1 implementation that works in any ECMAScript runtime.
//
// This is the standard FIPS 180-4 algorithm, transcribed to be
// careful about JavaScript's signed-32-bit bitwise semantics. Every
// intermediate is `>>> 0`-coerced to unsigned 32-bit before being
// used, so the math is identical to the C reference.
//
// Returns: ArrayBuffer of 20 raw bytes. `toHex()` helper converts
// to a lowercase hex string.

function utf8Encode(str) {
  if (typeof TextEncoder !== 'undefined') {
    return new TextEncoder().encode(str);
  }
  const out = [];
  for (let i = 0; i < str.length; i++) {
    let c = str.charCodeAt(i);
    if (c < 0x80) {
      out.push(c);
    } else if (c < 0x800) {
      out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f));
    } else if (c < 0xd800 || c >= 0xe000) {
      out.push(
        0xe0 | (c >> 12),
        0x80 | ((c >> 6) & 0x3f),
        0x80 | (c & 0x3f),
      );
    } else {
      i++;
      const c2 = str.charCodeAt(i);
      const cp = 0x10000 + (((c & 0x3ff) << 10) | (c2 & 0x3ff));
      out.push(
        0xf0 | (cp >> 18),
        0x80 | ((cp >> 12) & 0x3f),
        0x80 | ((cp >> 6) & 0x3f),
        0x80 | (cp & 0x3f),
      );
    }
  }
  return new Uint8Array(out);
}

function rotr(x, n) {
  // JavaScript's `>>>` is unsigned right shift; `<<` is signed left
  // shift (truncated to 32 bits). For a true 32-bit rotate-right we
  // OR the two halves and force unsigned.
  return ((x >>> n) | (x << (32 - n))) >>> 0;
}

function sha1(message) {
  const msg = utf8Encode(message);
  const msgLen = msg.length;

  // Padding: append 0x80, then 0x00s, then 64-bit big-endian length
  // in bits. Total length becomes a multiple of 64 bytes.
  // Math: we need at least 1 byte (0x80) + 8 bytes (length) = 9
  // extra bytes beyond the input.
  const totalLen = (((msgLen + 9) + 63) >>> 6) << 6;
  const padded = new Uint8Array(totalLen);
  padded.set(msg);
  padded[msgLen] = 0x80;

  // 64-bit big-endian bit length. JS numbers can hold 53-bit
  // integers exactly, so for inputs < 2^53 bits (~1 PB) the
  // split-into-two-32-bit-halves approach is safe.
  const bitLen = msgLen * 8;
  const hi = Math.floor(bitLen / 0x100000000);
  const lo = bitLen - hi * 0x100000000;
  const dv = new DataView(padded.buffer);
  dv.setUint32(totalLen - 8, hi, false);
  dv.setUint32(totalLen - 4, lo, false);

  // Initial state.
  let H0 = 0x67452301 | 0;
  let H1 = 0xEFCDAB89 | 0;
  let H2 = 0x98BADCFE | 0;
  let H3 = 0x10325476 | 0;
  let H4 = 0xC3D2E1F0 | 0;

  const W = new Int32Array(80);

  for (let chunk = 0; chunk < totalLen; chunk += 64) {
    // Load chunk into W[0..15] as big-endian uint32.
    for (let i = 0; i < 16; i++) {
      W[i] = dv.getInt32(chunk + i * 4, false);
    }
    // Extend.
    for (let i = 16; i < 80; i++) {
      const v = W[i - 3] ^ W[i - 8] ^ W[i - 14] ^ W[i - 16];
      W[i] = (v << 1) | (v >>> 31);
    }

    let A = H0, B = H1, C = H2, D = H3, E = H4;
    for (let i = 0; i < 80; i++) {
      let f, k;
      if (i < 20) {
        f = (B & C) | (~B & D);
        k = 0x5A827999;
      } else if (i < 40) {
        f = B ^ C ^ D;
        k = 0x6ED9EBA1;
      } else if (i < 60) {
        f = (B & C) | (B & D) | (C & D);
        k = 0x8F1BBCDC;
      } else {
        f = B ^ C ^ D;
        k = 0xCA62C1D6;
      }
      // t = (A <<< 5) + f + E + k + W[i]   (mod 2^32)
      // `| 0` coerces signed 32-bit back; additions naturally
      // wrap because Int32Array stores signed int32.
      const t = (((A << 5) | (A >>> 27)) + f + E + k + W[i]) | 0;
      E = D;
      D = C;
      C = (B << 30) | (B >>> 2);
      B = A;
      A = t;
    }
    H0 = (H0 + A) | 0;
    H1 = (H1 + B) | 0;
    H2 = (H2 + C) | 0;
    H3 = (H3 + D) | 0;
    H4 = (H4 + E) | 0;
  }

  // Concatenate H0..H4 as 20 raw big-endian bytes.
  const out = new ArrayBuffer(20);
  const odv = new DataView(out);
  odv.setUint32(0,  H0, false);
  odv.setUint32(4,  H1, false);
  odv.setUint32(8,  H2, false);
  odv.setUint32(12, H3, false);
  odv.setUint32(16, H4, false);
  return out;
}

function toHex(buf) {
  const bytes = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < bytes.length; i++) {
    s += bytes[i].toString(16).padStart(2, '0');
  }
  return s;
}

module.exports = { sha1, toHex };