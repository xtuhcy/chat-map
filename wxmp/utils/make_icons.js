// make_icons.js — generate simple PNG icons for the send / stop
// buttons. Run once from the project root: `node wxmp/utils/make_icons.js`.
//
// Outputs:
//   wxmp/utils/icons/send.png  (paper-plane / rightward arrow on transparent)
//   wxmp/utils/icons/stop.png  (filled square on transparent)
//
// Uses Node's built-in zlib for IDAT compression; no native deps.
// Icons are 80x80 RGBA so they look crisp at WeChat's typical
// button sizes (60-80 rpx).

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const W = 80;
const H = 80;

// CRC32 table (zlib doesn't expose this; we build it ourselves).
const crcTable = new Uint32Array(256);
for (let n = 0; n < 256; n++) {
  let c = n;
  for (let k = 0; k < 8; k++) {
    c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
  }
  crcTable[n] = c >>> 0;
}
function crc32(buf) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) {
    c = (crcTable[(c ^ buf[i]) & 0xFF] ^ (c >>> 8)) >>> 0;
  }
  return (c ^ 0xFFFFFFFF) >>> 0;
}

function makeChunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length, 0);
  const typeBuf = Buffer.from(type, 'ascii');
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([length, typeBuf, data, crcBuf]);
}

function encodePNG(width, height, getPixel) {
  // IHDR
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;   // bit depth
  ihdr[9] = 6;   // color type RGBA
  ihdr[10] = 0;  // compression
  ihdr[11] = 0;  // filter
  ihdr[12] = 0;  // interlace

  // Raw IDAT data: each row prefixed with filter byte (0 = none).
  const stride = 1 + width * 4;
  const raw = Buffer.alloc(height * stride);
  for (let y = 0; y < height; y++) {
    raw[y * stride] = 0;
    for (let x = 0; x < width; x++) {
      const [r, g, b, a] = getPixel(x, y);
      const off = y * stride + 1 + x * 4;
      raw[off] = r;
      raw[off + 1] = g;
      raw[off + 2] = b;
      raw[off + 3] = a;
    }
  }
  const idat = zlib.deflateSync(raw);

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
    makeChunk('IHDR', ihdr),
    makeChunk('IDAT', idat),
    makeChunk('IEND', Buffer.alloc(0)),
  ]);
}

// ===== Drawing helpers =====

// Soft-edged filled shape: antialias edges using distance from polygon edge.
function rasterize(width, height, sd /* signed-distance(x, y) → negative inside */) {
  return (x, y) => {
    const d = sd(x, y);
    // Anti-alias: smooth edge over ~0.7 px band.
    const a = Math.max(0, Math.min(1, 0.5 - d));
    if (a <= 0) return [0, 0, 0, 0];
    return [255, 255, 255, Math.round(a * 255)];
  };
}

// Distance to a line segment (px, py) — (qx, qy), unsigned.
function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

// Signed distance to a polygon. Negative inside, positive outside.
function signedDistPoly(x, y, poly) {
  // Ray-casting for inside/outside test.
  let inside = false;
  let minEdge = Infinity;
  for (let i = 0, j = poly.length - 2; i < poly.length - 1; j = i, i += 2) {
    const ax = poly[i],     ay = poly[i + 1];
    const bx = poly[i + 2], by = poly[i + 3];
    if (((ay > y) !== (by > y)) &&
        (x < (bx - ax) * (y - ay) / (by - ay + 1e-9) + ax)) {
      inside = !inside;
    }
    const d = distToSegment(x, y, ax, ay, bx, by);
    if (d < minEdge) minEdge = d;
  }
  return inside ? -minEdge : minEdge;
}

// ===== Send icon: paper plane pointing up-right =====
//
// 4-point poly: nose (top-right) → bottom wing → tail tip (left) → top wing.
function makeSendIcon() {
  // Coordinates in [0..W] × [0..H]
  const poly = [
    64, 12,  // nose (top-right)
    70, 60,  // bottom wing tip
    38, 46,  // tail tip (left)
    32, 24,  // top wing
  ];
  return encodePNG(W, H, (x, y) => {
    // Add an inner "fold" line for paper-plane look.
    const sd1 = signedDistPoly(x, y, poly);
    // The plane outline
    const d1 = sd1;
    // Anti-aliased fill
    const a1 = Math.max(0, Math.min(1, 0.5 - d1));
    if (a1 <= 0) return [0, 0, 0, 0];

    // Inner fold line from (32,24) → (38,46) — slightly darker.
    const foldDist = distToSegment(x + 0.5, y + 0.5, 32, 24, 38, 46);
    if (foldDist < 1.4 && a1 > 0.3) {
      // Subtle darker line.
      const foldAlpha = Math.max(0, 1 - foldDist / 1.4) * a1;
      // Blend toward a soft gray.
      const r = 255 - Math.round(80 * foldAlpha);
      const g = 255 - Math.round(80 * foldAlpha);
      const b = 255 - Math.round(80 * foldAlpha);
      return [r, g, b, Math.round(a1 * 255)];
    }

    return [255, 255, 255, Math.round(a1 * 255)];
  });
}

// ===== Stop icon: filled rounded square =====
//
// Square from (24,24) to (56,56) with corner radius 6.
function makeStopIcon() {
  const x0 = 24, y0 = 24, x1 = 56, y1 = 56, r = 6;
  return encodePNG(W, H, (x, y) => {
    // Distance to the rounded rect (negative inside).
    const cx = Math.max(x0, Math.min(x1, x));
    const cy = Math.max(y0, Math.min(y1, y));
    // For corner regions, use circle distance.
    let dx, dy;
    if (cx < x0 + r && cy < y0 + r) {
      dx = cx - (x0 + r); dy = cy - (y0 + r);
    } else if (cx > x1 - r && cy < y0 + r) {
      dx = cx - (x1 - r); dy = cy - (y0 + r);
    } else if (cx < x0 + r && cy > y1 - r) {
      dx = cx - (x0 + r); dy = cy - (y1 - r);
    } else if (cx > x1 - r && cy > y1 - r) {
      dx = cx - (x1 - r); dy = cy - (y1 - r);
    } else {
      dx = (x < x0) ? (x0 - x) : (x > x1) ? (x - x1) : 0;
      dy = (y < y0) ? (y0 - y) : (y > y1) ? (y - y1) : 0;
    }
    const d = Math.hypot(dx, dy);
    const a = Math.max(0, Math.min(1, 0.5 - d));
    if (a <= 0) return [0, 0, 0, 0];
    return [255, 255, 255, Math.round(a * 255)];
  });
}

// ===== Run =====
const outDir = path.join(__dirname, 'icons');
fs.mkdirSync(outDir, { recursive: true });

const sendPng = makeSendIcon();
fs.writeFileSync(path.join(outDir, 'send.png'), sendPng);
console.log(`send.png: ${sendPng.length} bytes`);

const stopPng = makeStopIcon();
fs.writeFileSync(path.join(outDir, 'stop.png'), stopPng);
console.log(`stop.png: ${stopPng.length} bytes`);