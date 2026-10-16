#!/usr/bin/env node
// Generates the PWA icons (PNG) with no dependencies: a glowing cyan/violet
// voice orb on a deep-space background. Run: node scripts/make-icons.js
'use strict';

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const OUT = path.join(__dirname, '..', 'icons');

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const td = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td), 0);
  return Buffer.concat([len, td, crc]);
}

function encodePng(size, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  ihdr[10] = 0;
  ihdr[11] = 0;
  ihdr[12] = 0;
  const stride = size * 4;
  const raw = Buffer.alloc((stride + 1) * size);
  for (let y = 0; y < size; y++) {
    raw[y * (stride + 1)] = 0; // filter: none
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));
const mix = (a, b, t) => a + (b - a) * t;
const smooth = (e0, e1, x) => {
  const t = clamp((x - e0) / (e1 - e0));
  return t * t * (3 - 2 * t);
};

const BG_TOP = [10, 14, 38];
const BG_BOTTOM = [3, 4, 14];
const CYAN = [34, 230, 255];
const VIOLET = [139, 92, 255];
const WHITE = [235, 252, 255];

// Paint one pixel; returns [r,g,b,a] in 0..255. u,v in 0..1.
function shade(u, v, opts) {
  const { orbRadius, background, mono } = opts;
  const dx = u - 0.5;
  const dy = v - 0.5;
  const d = Math.sqrt(dx * dx + dy * dy);
  const r = orbRadius;

  if (mono) {
    // Monochrome notification badge: white orb + ring on transparent.
    const core = 1 - smooth(r - 0.01, r + 0.01, d);
    const ring = 1 - smooth(0.012, 0.03, Math.abs(d - r * 1.32));
    const a = clamp(core + ring);
    return [255, 255, 255, Math.round(a * 255)];
  }

  let col = [0, 0, 0];
  let alpha = 1;
  if (background) {
    const t = clamp(v);
    col = [mix(BG_TOP[0], BG_BOTTOM[0], t), mix(BG_TOP[1], BG_BOTTOM[1], t), mix(BG_TOP[2], BG_BOTTOM[2], t)];
    // faint grid
    const g = 14;
    const gx = Math.abs(((u * g) % 1) - 0.5);
    const gy = Math.abs(((v * g) % 1) - 0.5);
    const line = Math.max(1 - smooth(0, 0.04, 0.5 - gx), 1 - smooth(0, 0.04, 0.5 - gy));
    const fade = clamp(1 - d * 1.6);
    col = col.map((c, i) => c + line * fade * 22 * (i === 2 ? 1.4 : 0.8));
  } else {
    alpha = 0;
  }

  // Outer glow halo
  const glow = Math.exp(-Math.pow(Math.max(0, d - r) / (r * 0.45), 2)) * (d > r ? 1 : 0);
  const glowCol = [mix(CYAN[0], VIOLET[0], 0.5), mix(CYAN[1], VIOLET[1], 0.5), mix(CYAN[2], VIOLET[2], 0.5)];
  col = col.map((c, i) => c + glowCol[i] * glow * 0.55);
  alpha = Math.max(alpha, glow * 0.85);

  // Ring
  const ring = 1 - smooth(r * 0.025, r * 0.07, Math.abs(d - r * 1.28));
  col = col.map((c, i) => mix(c, CYAN[i], ring * 0.8));
  alpha = Math.max(alpha, ring * 0.9);

  // Orb body
  const inside = 1 - smooth(r - 0.004, r + 0.004, d);
  if (inside > 0) {
    const t = clamp(d / r);
    // light source offset to top-left
    const lx = u - (0.5 - r * 0.32);
    const ly = v - (0.5 - r * 0.36);
    const ld = Math.sqrt(lx * lx + ly * ly) / r;
    const base = [mix(CYAN[0], VIOLET[0], smooth(0.1, 1.0, t)), mix(CYAN[1], VIOLET[1], smooth(0.1, 1.0, t)), mix(CYAN[2], VIOLET[2], smooth(0.1, 1.0, t))];
    const hl = Math.exp(-ld * ld * 3.2);
    const rim = smooth(0.82, 1.0, t) * 0.35;
    const body = base.map((c, i) => clamp(mix(c, WHITE[i], hl * 0.75) * (1 - rim * 0.6) / 255) * 255);
    col = col.map((c, i) => mix(c, body[i], inside));
    alpha = Math.max(alpha, inside);
  }

  return [clamp(col[0], 0, 255), clamp(col[1], 0, 255), clamp(col[2], 0, 255), clamp(alpha) * 255].map(Math.round);
}

function render(size, opts) {
  const ss = 3; // supersampling for anti-aliasing
  const buf = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let r = 0, g = 0, b = 0, a = 0;
      for (let sy = 0; sy < ss; sy++) {
        for (let sx = 0; sx < ss; sx++) {
          const p = shade((x + (sx + 0.5) / ss) / size, (y + (sy + 0.5) / ss) / size, opts);
          const pa = p[3] / 255;
          r += p[0] * pa; g += p[1] * pa; b += p[2] * pa; a += pa;
        }
      }
      const n = ss * ss;
      const i = (y * size + x) * 4;
      buf[i] = a ? Math.round(r / a) : 0;
      buf[i + 1] = a ? Math.round(g / a) : 0;
      buf[i + 2] = a ? Math.round(b / a) : 0;
      buf[i + 3] = Math.round((a / n) * 255);
    }
  }
  return encodePng(size, buf);
}

const ICONS = [
  { file: 'icon-192.png', size: 192, opts: { orbRadius: 0.27, background: true } },
  { file: 'icon-512.png', size: 512, opts: { orbRadius: 0.27, background: true } },
  // Maskable: everything important sits inside the 40% radius safe zone.
  { file: 'maskable-512.png', size: 512, opts: { orbRadius: 0.21, background: true } },
  { file: 'apple-touch-icon-180.png', size: 180, opts: { orbRadius: 0.25, background: true } },
  { file: 'badge-96.png', size: 96, opts: { orbRadius: 0.24, mono: true } },
];

fs.mkdirSync(OUT, { recursive: true });
for (const icon of ICONS) {
  fs.writeFileSync(path.join(OUT, icon.file), render(icon.size, icon.opts));
  process.stdout.write(`wrote icons/${icon.file} (${icon.size}x${icon.size})\n`);
}
