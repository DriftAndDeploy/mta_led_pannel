/*
 * Layout editor for the 64x32 panel (web app → 07 Customize Layout).
 *
 * PanelSim draws a layout exactly the way the firmware's drawCustom() does
 * (same 5x7 font, bullets, 2x digits, weather icons, clipped scrolling), so
 * what you arrange here is what the panel shows. Element format, shared with
 * the server (EL in proxy_server.py) and firmware (EL_* in the sketch):
 *   [type, x, y, a, b, color]   color 0 = the element's usual colour
 *   bullets: x,y = centre, a = radius 4-7
 *   text:    x = anchor (b bits 0-1: 0 left, 1 centre, 2 right), y = top,
 *            a = font size 0-4 (weather 1-3)
 *   leave:   b bit 2 = time only ("8:32" instead of "Leave by 8:32")
 *   ticker:  scrolls in [x, x+a): Arrive By message + alerts; b bit 1 = also
 *            weather, bits 2-3 = font size 0-3
 * Font sizes: 0 = 3x5, 1 = 5x7, 2-4 = 5x7 scaled up with 1 px letter spacing
 * and a slim colon (size 2 = the big train time).
 */
(function () {
'use strict';

const EL = { bullet: 1, time: 2, leave: 3, ticker: 4, weather: 5, dot: 6, clock: 7,
             label: 9, bullet2: 10, time2: 11 };          // 8 / 12 (countdowns) removed
const EL_INFO = {
  1:  { name: 'Line bullet',        kind: 'bullet', def: [1, 8, 24, 7, 0, 0] },
  2:  { name: 'Train time',         kind: 'text',   def: [2, 32, 0, 2, 1, 0] },
  3:  { name: 'Leave by',           kind: 'text',   def: [3, 2, 25, 0, 0, 0] },
  4:  { name: 'Scrolling text',     kind: 'ticker', def: [4, 17, 20, 47, 4, 0] },
  5:  { name: 'Weather',            kind: 'text',   def: [5, 63, 24, 1, 2, 0] },
  6:  { name: 'Status dot',         kind: 'dot',    def: [6, 2, 1, 0, 0, 0] },
  7:  { name: 'Clock',              kind: 'text',   def: [7, 63, 0, 1, 2, 0] },
  9:  { name: 'Label',              kind: 'text',   def: [9, 2, 0, 1, 0, 0] },
  10: { name: 'Bullet (train 2)',   kind: 'bullet', def: [10, 6, 24, 6, 0, 0], two: true },
  11: { name: 'Time (train 2)',     kind: 'text',   def: [11, 59, 17, 2, 2, 0], two: true },
};
// Font size range per element type (ticker keeps its size in b, the rest in a)
const SIZES = { 2: [0, 4], 3: [0, 4], 7: [0, 4], 9: [0, 4], 11: [0, 4], 5: [1, 3], 4: [0, 3] };
const SIZE_NAMES = ['Tiny', 'Small', 'Big', 'Bigger', 'Huge'];
function getSize(e) { return e[0] === 4 ? (e[4] >> 2) & 3 : e[3]; }
function setSize(e, v) {
  const [lo, hi] = SIZES[e[0]];
  v = Math.max(lo, Math.min(hi, v));
  if (e[0] === 4) e[4] = (e[4] & 3) | (v << 2); else e[3] = v;
  return v;
}

// ── 5x7 font (Adafruit GFX glcdfont, ASCII 32-126, BSD licence) ──────────
const FONT = '000000000000005f00000007000700147f147f14242a7f2a12231308646236495620500008070300001c2241000041221c002a1c7f1c2a08083e080800807030000808080808000060600020100804023e5149453e00427f400072494949462141494d331814127f1027454545393c4a49493141211109073649494936464949291e0000140000004034000000081422411414141414004122140802015909063e415d594e7c1211127c7f494949363e414141227f4141413e7f494949417f090909013e414151737f0808087f00417f41002040413f017f081422417f404040407f021c027f7f0408107f3e4141413e7f090909063e4151215e7f09192946264949493203017f01033f4040403f1f2040201f3f4038403f631408146303047804036159494d43007f4141410204081020004141417f04020102044040404040000307080020545478407f284444383844444428384444287f385454541800087e090218a4a49c787f0804047800447d40002040403d007f1028440000417f40007c047804787c080404783844444438fc1824241818242418fc7c08040408485454542404043f44243c4040207c1c2040201c3c4030403c44281028444c9090907c4464544c440008364100000077000000413608000201020402';
function glyphCols(ch) {
  const c = ch.charCodeAt(0);
  if (c < 32 || c > 126) return null;
  const o = (c - 32) * 10;
  return [0, 1, 2, 3, 4].map(i => parseInt(FONT.substr(o + i * 2, 2), 16));
}

// Tiny 3x5 font (ASCII 32-126, lowercase as capitals): bit r*3+c = row r, column c
const TINY = '00002092002d5f7d3c9e42a16aaa0012449414910aa805d0140001c0200012a47b6f749a73e779a749ed79cf7bcf25277bef79ef0208141044540e38151120a763ea5bea3aeb624e3b6b72cf12cf6b4e5bed74972b245aed72495bfd5b6b2b6a12eb6f6a5aeb388e24977b6d2b6d5fed5aad24ad72a7324b48896926002a700000115bea3aeb624e3b6b72cf12cf6b4e5bed74972b245aed72495bfd5b6b2b6a12eb6f6a5aeb388e24977b6d2b6d5fed5aad24ad72a764d62492359300f0';
const tinyBits = ch => { const c = ch.charCodeAt(0); return c < 32 || c > 126 ? null : parseInt(TINY.substr((c - 32) * 4, 4), 16); };
const charAdv = (ch, size) => size <= 0 ? (ch === ':' ? 2 : 4) : size === 1 ? 6 : ch === ':' ? size + 1 : 5 * size + 1;
const textW = (t, size) => [...t].reduce((w, ch) => w + charAdv(ch, size), 0) - 1;
const textH = size => size <= 0 ? 5 : 7 * size;

// ── Frame buffer helpers ─────────────────────────────────────────────────
const q = c => [c[0] & 0xF8, c[1] & 0xFC, c[2] & 0xF8];   // RGB565 like the panel
function from565(v) { return [((v >> 11) & 31) << 3, ((v >> 5) & 63) << 2, (v & 31) << 3]; }
function to565(hex) {
  const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
  return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3);
}
function hexToRgb(h) { return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]; }

class Frame {
  constructor() { this.px = Array.from({ length: 32 }, () => Array(64).fill(null)); this.clipL = 0; this.clipR = 64; }
  set(x, y, c) {
    if (x >= this.clipL && x < this.clipR && x >= 0 && y >= 0 && x < 64 && y < 32) this.px[y][x] = c ? q(c) : null;
  }
  rect(x, y, w, h, c) { for (let j = y; j < y + h; j++) for (let i = x; i < x + w; i++) this.set(i, j, c); }
  vline(x, y, h, c) { this.rect(x, y, 1, h, c); }
  // Adafruit GFX fillCircle, pixel for pixel
  circle(x0, y0, r, c) {
    this.vline(x0, y0 - r, 2 * r + 1, c);
    let f = 1 - r, ddx = 1, ddy = -2 * r, x = 0, y = r, px = x, py = y;
    const delta = 1;
    while (x < y) {
      if (f >= 0) { y--; ddy += 2; f += ddy; }
      x++; ddx += 2; f += ddx;
      if (x < y + 1) { this.vline(x0 + x, y0 - y, 2 * y + delta, c); this.vline(x0 - x, y0 - y, 2 * y + delta, c); }
      if (y !== py) { this.vline(x0 + py, y0 - px, 2 * px + delta, c); this.vline(x0 - py, y0 - px, 2 * px + delta, c); py = y; }
      px = x;
    }
  }
  glyph(ch, x, y, c, s = 1) {
    const cols = glyphCols(ch);
    if (!cols) return;
    cols.forEach((bits, col) => { for (let row = 0; row < 8; row++) if (bits & (1 << row)) this.rect(x + col * s, y + row * s, s, s, c); });
  }
  char(ch, x, y, size, c) {                          // = firmware drawChar()
    if (size <= 0) {
      const g = tinyBits(ch);
      if (g === null) return;
      for (let r = 0; r < 5; r++) for (let col = 0; col < 3; col++) if (g & (1 << (r * 3 + col))) this.set(x + col, y + r, c);
    } else if (size >= 2 && ch === ':') {
      this.rect(x, y + Math.trunc(size * 3 / 2), size, size, c);
      this.rect(x, y + Math.trunc(size * 9 / 2), size, size, c);
    } else this.glyph(ch, x, y, c, size);
  }
  bits7(x, y, rows, c, s = 1) { rows.forEach((r, j) => { for (let b = 0; b < 7; b++) if (r & (0x40 >> b)) this.rect(x + b * s, y + j * s, s, s, c); }); }
}

// ── Pieces drawn exactly like the firmware ───────────────────────────────
function drawBullet(f, cx, cy, r, line, rgb) {
  let dim = 0.9;
  if (line === 'L') dim = 0.5;
  else if (['1', '2', '3', 'B', 'D', 'F', 'M'].includes(line)) dim = 0.8;
  else if (['7', 'G'].includes(line)) dim = 0.7;
  f.circle(cx, cy, r, rgb.map(v => Math.floor(v * dim)));
  const dark = ['N', 'Q', 'R', 'W'].includes(line);
  const x0 = cx - Math.trunc(line.length * 6 / 2) + 1;
  [...line].forEach((ch, i) => {
    const cols = glyphCols(ch);
    cols && cols.forEach((bits, col) => { for (let row = 0; row < 8; row++) if (bits & (1 << row)) {
      if (dark) f.set(x0 + i * 6 + col, cy - 3 + row, null); else f.set(x0 + i * 6 + col, cy - 3 + row, [255, 255, 255]);
    } });
  });
}
function textAt(f, t, x, y, size, align, c) {       // = firmware drawTextAt()
  if (!t) return null;
  const w = textW(t, size);
  let x0 = align === 1 ? x - Math.trunc(w / 2) : align === 2 ? x - w + 1 : x;
  const left = x0;
  for (const ch of t) { f.char(ch, x0, y, size, c); x0 += charAdv(ch, size); }
  return [left, y, w, textH(size)];
}

// Weather run: icons are control characters, same as the firmware's drawRunClip()
const BMP = {
  sun: [0x08, 0x22, 0x1C, 0x5D, 0x1C, 0x22, 0x08], moon: [0x1C, 0x38, 0x70, 0x70, 0x70, 0x38, 0x1C],
  cloud: [0x00, 0x0C, 0x3E, 0x7F, 0x7F, 0x3E, 0x00], smallsun: [0x38, 0x7C, 0x7C, 0x38],
  sidecloud: [0x06, 0x1F, 0x3F, 0x1F], topcloud: [0x0C, 0x3E, 0x7F, 0x3E], drops: [0x2A, 0x54],
  flakes: [0x55, 0x2A], bolt: [0x18, 0x0C, 0x18], canopy: [0x1C, 0x3E, 0x7F], handle: [0x08, 0x08, 0x28, 0x10],
};
function wxIcon(f, x, y, icon, s) {
  const grey = [150, 150, 165], yellow = [255, 190, 0];
  if (icon === 1) f.bits7(x, y, BMP.sun, yellow, s);
  else if (icon === 2) f.bits7(x, y, BMP.moon, [210, 210, 160], s);
  else if (icon === 3) { f.bits7(x, y, BMP.smallsun, yellow, s); f.bits7(x, y + 3 * s, BMP.sidecloud, grey, s); }
  else if (icon === 5) { f.bits7(x, y, BMP.topcloud, grey, s); f.bits7(x, y + 5 * s, BMP.drops, [60, 130, 255], s); }
  else if (icon === 6) { f.bits7(x, y, BMP.topcloud, grey, s); f.bits7(x, y + 5 * s, BMP.flakes, [230, 230, 255], s); }
  else if (icon === 7) { f.bits7(x, y, BMP.topcloud, grey, s); f.bits7(x, y + 4 * s, BMP.bolt, yellow, s); }
  else f.bits7(x, y, BMP.cloud, grey, s);
}
const runAdv = (ch, s) => {                      // icons never go below 1x
  const c = ch.charCodeAt(0), k = Math.max(1, s);
  return c >= 1 && c <= 0x10 ? 7 * k + 1 : c === 0x11 ? 3 * k + 1 : c === 0x12 ? 3 * k : charAdv(ch, s);
};
const runW = (t, s) => [...t].reduce((w, ch) => w + runAdv(ch, s), 0);
function drawRun(f, t, x, y, s, c) {
  const k = Math.max(1, s);
  for (const ch of t) {
    const code = ch.charCodeAt(0), a = runAdv(ch, s);
    if (code >= 1 && code <= 7) wxIcon(f, x, y, code, k);
    else if (code === 0x10) { f.bits7(x, y, BMP.canopy, [80, 170, 255], k); f.bits7(x, y + 3 * k, BMP.handle, [180, 180, 180], k); }
    else if (code === 0x11) {
      for (let i = 0; i < 3; i++) { f.rect(x + i * k, y, k, k, c); f.rect(x + i * k, y + 2 * k, k, k, c); }
      f.rect(x, y + k, k, k, c); f.rect(x + 2 * k, y + k, k, k, c);
    }
    else if (code !== 0x12) f.char(ch, x, y, s, c);
    x += a;
  }
  return x;
}

// ── Render a layout. data = sample values; frame = animation frame (25 fps)
function render(layout, data, frameNo) {
  const f = new Frame();
  const boxes = [];
  const white = data.colorAll ? data.timeRgb : [255, 255, 255];
  const own = (e, dflt) => e[5] ? from565(e[5]) : dflt;
  layout.forEach((e, i) => {
    const [type, x, y, a, b] = e;
    const size = Math.max(0, Math.min(4, a)), align = b & 3;
    let box = null;
    switch (type) {
      case EL.bullet: case EL.bullet2: {
        const r = Math.max(4, Math.min(7, a));
        const two = type === EL.bullet2;
        drawBullet(f, x, y, r, two ? data.line2 : data.line, two ? data.line2Rgb : data.lineRgb);
        box = [x - r, y - r, 2 * r + 1, 2 * r + 1];
        break;
      }
      case EL.time:  box = textAt(f, data.time, x, y, size, align, own(e, data.timeRgb)); break;
      case EL.time2: box = textAt(f, data.time2, x, y, size, align, own(e, data.timeRgb)); break;
      case EL.leave: box = textAt(f, b & 4 ? data.leave : 'Leave by ' + data.leave, x, y, size, align, own(e, white)); break;
      case EL.weather: {
        const ws = Math.max(1, Math.min(3, a));
        const w = runW(data.wx, ws) - 1;
        const x0 = align === 1 ? x - Math.trunc(w / 2) : align === 2 ? x - w + 1 : x;
        drawRun(f, data.wx, x0, y, ws, own(e, data.colorAll ? data.timeRgb : [150, 200, 255]));
        box = [x0, y, w, 7 * ws];
        break;
      }
      case EL.dot: f.rect(x, y, 2, 2, [[0, 200, 80], [255, 200, 0], [220, 40, 40]][data.status]); box = [x, y, 2, 2]; break;
      case EL.clock: box = textAt(f, data.clock, x, y, size, align, own(e, [200, 200, 200])); break;
      case EL.label: box = textAt(f, data.label || ' ', x, y, size, align, own(e, white)); break;
      case EL.ticker: {
        const ts = (b >> 2) & 3;
        const L = Math.max(0, x), R = Math.min(64, x + Math.max(6, a));
        const parts = [];
        if (data.deadline) parts.push([data.deadline + '  ', own(e, data.colorAll ? data.timeRgb : [120, 230, 150]), 0]);
        if (b & 2) parts.push([data.wx, own(e, data.colorAll ? data.timeRgb : [150, 200, 255]), 12 * Math.max(1, ts)]);
        parts.push([data.alert + '  ', own(e, data.colorAll ? data.timeRgb : [255, 220, 0]), 0]);
        const total = parts.reduce((w, p) => w + runW(p[0], ts) + p[2], 0);
        const period = (R - L) + total + 2;               // same cycle as the firmware
        let cx = R - (frameNo % period);
        f.clipL = L; f.clipR = R;
        parts.forEach(([t, c, gap]) => { cx = drawRun(f, t, cx, y, ts, c) + gap; });
        f.clipL = 0; f.clipR = 64;
        box = [L, y, R - L, ts ? 7 * ts : 5];
        break;
      }
    }
    boxes.push(box);
  });
  return { frame: f, boxes };
}

function paint(canvas, frame, opts = {}) {
  const g = canvas.getContext('2d'), s = canvas.width / 64;
  g.fillStyle = '#08080a'; g.fillRect(0, 0, canvas.width, canvas.height);
  for (let y = 0; y < 32; y++) for (let x = 0; x < 64; x++) {
    const c = frame.px[y][x];
    g.fillStyle = c ? `rgb(${c[0]},${c[1]},${c[2]})` : '#19191d';
    g.beginPath(); g.arc(x * s + s / 2, y * s + s / 2, s * (c ? 0.42 : 0.28), 0, 2 * Math.PI); g.fill();
  }
  if (opts.box) {                                   // selection outline
    const [bx, by, bw, bh] = opts.box;
    g.strokeStyle = '#8ecbff'; g.lineWidth = Math.max(2, s / 4); g.setLineDash([s * 0.8, s * 0.5]);
    g.strokeRect(bx * s - 1, by * s - 1, bw * s + 2, bh * s + 2); g.setLineDash([]);
  }
}

window.PanelSim = { EL, EL_INFO, SIZES, SIZE_NAMES, getSize, setSize, render, paint, to565, from565, hexToRgb };
})();
