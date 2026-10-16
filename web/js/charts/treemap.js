// Canvas treemap: squarified, nested, HiDPI. Names are untrusted: they only reach
// the canvas through fillText and the tooltip through el() text nodes.
import {
  el, fmtBytes, fmtNumber, fmtPercent, fmtDate, cssVar, catColor, seriesColor, joinPath,
  showTooltip, hideTooltip,
} from '../ui.js';

const HEADER_H = 18;
const MIN_CELL = 4;
const CAT_LABELS = {
  app: 'Applications', data: 'Données', disk_image: 'Images disque', video: 'Vidéos', image: 'Images',
  document: 'Documents', code: 'Code', archive: 'Archives', other: 'Autres',
};
const TYPE_NAMES = { d: 'dir', f: 'file', s: 'small', o: 'other' };

// ------------------------------------------------------------------ pure helpers
/**
 * Squarified treemap (Bruls, Huizing, van Wijk). `values` must be positive and
 * sorted descending. Returns one {x, y, w, h} per value filling the box exactly.
 */
function squarify(values, x, y, w, h) {
  const n = values.length;
  const out = new Array(n);
  const sum = values.reduce((a, b) => a + b, 0);
  if (!n || sum <= 0) return out.fill({ x, y, w: 0, h: 0 });
  const areas = values.map((v) => (v / sum) * w * h);
  let rx = x; let ry = y; let rw = w; let rh = h;
  let i = 0;
  while (i < n) {
    const side = Math.min(rw, rh);
    let j = i; let rowSum = 0; let rowMin = Infinity; let rowMax = 0; let best = Infinity;
    while (j < n) {
      const a = areas[j];
      const s = rowSum + a;
      const mn = Math.min(rowMin, a);
      const mx = Math.max(rowMax, a);
      const worst = side > 0 ? Math.max((side * side * mx) / (s * s), (s * s) / (side * side * mn)) : 0;
      if (j > i && worst > best) break;
      best = worst; rowSum = s; rowMin = mn; rowMax = mx; j++;
    }
    if (rw >= rh) {
      const colW = rh > 0 ? rowSum / rh : 0;
      let cy = ry;
      for (let k = i; k < j; k++) {
        const hh = colW > 0 ? areas[k] / colW : 0;
        out[k] = { x: rx, y: cy, w: colW, h: hh };
        cy += hh;
      }
      rx += colW; rw -= colW;
    } else {
      const rowH = rw > 0 ? rowSum / rw : 0;
      let cx = rx;
      for (let k = i; k < j; k++) {
        const ww = rowH > 0 ? areas[k] / rowH : 0;
        out[k] = { x: cx, y: ry, w: ww, h: rowH };
        cx += ww;
      }
      ry += rowH; rh -= rowH;
    }
    i = j;
  }
  return out;
}

function hexToRgb(c) {
  let m = /^#([0-9a-f]{3})$/i.exec(c);
  if (m) return [...m[1]].map((h) => parseInt(h + h, 16));
  m = /^#([0-9a-f]{6})/i.exec(c);
  if (m) return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16));
  m = /rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i.exec(c || '');
  return m ? [+m[1], +m[2], +m[3]] : [128, 128, 128];
}
const rgbToHex = (rgb) => `#${rgb.map((v) => Math.round(v).toString(16).padStart(2, '0')).join('')}`;
/** Mix `a` toward `b`; t = share of b (0..1). */
function mix(a, b, t) {
  const ra = hexToRgb(a); const rb = hexToRgb(b);
  return rgbToHex(ra.map((v, i) => v + (rb[i] - v) * t));
}
function luminance(c) {
  const [r, g, b] = hexToRgb(c).map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
const contrastText = (bg) => (luminance(bg) > 0.4 ? '#0b0b0b' : '#ffffff');

/** Largest prefix of `text` (with an ellipsis) that fits in maxW pixels. */
function fitText(ctx, text, maxW) {
  if (maxW <= 0) return '';
  if (ctx.measureText(text).width <= maxW) return text;
  let lo = 0; let hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(`${text.slice(0, mid)}…`).width <= maxW) lo = mid; else hi = mid - 1;
  }
  return lo ? `${text.slice(0, lo)}…` : '';
}

/** Flat, parents-before-children list of cells for the given canvas size. */
function buildLayout(root, W, H) {
  const rects = [];
  const rootIndex = new Map((root.ch || []).map((c, i) => [c, i]));
  const place = (parent, path, box, depth, parentBranch) => {
    const kids = (parent.ch || []).filter((c) => c.s > 0).sort((a, b) => b.s - a.s);
    if (!kids.length || box.w < MIN_CELL || box.h < MIN_CELL) return false;
    const slots = squarify(kids.map((k) => k.s), box.x, box.y, box.w, box.h);
    let placed = false;
    kids.forEach((k, i) => {
      const s = slots[i];
      const x0 = Math.round(s.x); const y0 = Math.round(s.y);
      const x1 = Math.round(s.x + s.w); const y1 = Math.round(s.y + s.h);
      if (x1 - x0 < MIN_CELL || y1 - y0 < MIN_CELL) return;
      placed = true;
      const real = k.t === 'd' || k.t === 'f';
      const cell = {
        x: x0 + 1, y: y0 + 1, w: x1 - x0 - 2, h: y1 - y0 - 2, node: k, depth,
        path: real ? joinPath(path, k.n) : null,
        branch: parent === root ? rootIndex.get(k) : parentBranch, hh: 0, open: false,
      };
      rects.push(cell);
      if (k.t === 'd' && k.ch && k.ch.length) {
        const hh = cell.w >= 70 && cell.h >= HEADER_H + 14 ? HEADER_H : 0;
        const inner = { x: cell.x + 1, y: cell.y + hh + 1, w: cell.w - 2, h: cell.h - hh - 2 };
        cell.hh = hh;
        cell.open = place(k, cell.path, inner, depth + 1, cell.branch) || hh > 0;
      }
    });
    return placed;
  };
  place(root, root.path || '', { x: 0, y: 0, w: W, h: H }, 1, -1);
  return rects;
}

function readPalette() {
  const catMemo = {}; const serMemo = {};
  return {
    font: cssVar('--font') || 'system-ui, sans-serif',
    surface: cssVar('--surface'), s2: cssVar('--surface-2'), s3: cssVar('--surface-3'),
    ink: cssVar('--ink'), ink2: cssVar('--ink-2'), axis: cssVar('--axis'), accent: cssVar('--accent'),
    cat: (k) => (catMemo[k] ??= catColor(k)),
    series: (i) => (serMemo[i] ??= seriesColor(i)),
  };
}

function watchTheme(cb) {
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  document.addEventListener('themechange', cb);
  mq.addEventListener('change', cb);
  return () => { document.removeEventListener('themechange', cb); mq.removeEventListener('change', cb); };
}

// ------------------------------------------------------------------ component
/**
 * Nested squarified treemap on a canvas filling `container`.
 * @param {HTMLElement} container
 * @param {{onSelect?: Function, onOpen?: Function, colorMode?: 'type'|'branch'}} opts
 * @returns {{setData(tree: object): void, setColorMode(mode: string): void, select(path: string|null): void,
 *   resize(): void, destroy(): void}}
 */
export function createTreemap(container, { onSelect, onOpen, colorMode = 'type' } = {}) {
  let mode = colorMode;
  let tree = null;
  let rects = [];
  let byPath = new Map();
  let W = 0; let H = 0; let dpr = 1;
  let hover = null; let selPath = null;
  let pal = readPalette();
  let raf = 0;
  const base = document.createElement('canvas');
  const canvas = document.createElement('canvas');
  canvas.className = 'ch-fade';
  canvas.tabIndex = 0;
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', 'Treemap de l\'occupation du disque');
  container.classList.add('ch-treemap');
  container.append(canvas);
  const ctx = canvas.getContext('2d');
  const bctx = base.getContext('2d');

  // ---- painting
  function paintLeaf(c, r, fill, text) {
    c.fillStyle = fill;
    c.fillRect(r.x, r.y, r.w, r.h);
    if (r.w < 36 || r.h < 18) return;
    const n = r.node;
    const maxW = r.w - 12;
    c.textBaseline = 'top'; c.textAlign = 'left'; c.fillStyle = text;
    c.font = `500 12px ${pal.font}`;
    const name = n.n || (n.t === 's' ? `${fmtNumber(n.k)} petits fichiers` : `${fmtNumber(n.k)} autres`);
    const t1 = fitText(c, name, maxW);
    if (!t1) return;
    c.fillText(t1, r.x + 6, r.y + 5);
    if (r.h >= 36) {
      c.font = `11px ${pal.font}`;
      c.globalAlpha = 0.8;
      const t2 = fitText(c, fmtBytes(n.s), maxW);
      if (t2) c.fillText(t2, r.x + 6, r.y + 21);
      c.globalAlpha = 1;
    }
  }

  function paintHeader(c, r, fill, text) {
    c.fillStyle = fill;
    c.fillRect(r.x, r.y, r.w, r.hh);
    c.textBaseline = 'middle'; c.textAlign = 'left'; c.fillStyle = text;
    c.font = `600 11.5px ${pal.font}`;
    const size = fmtBytes(r.node.s);
    c.font = `11px ${pal.font}`;
    const sw = c.measureText(size).width;
    const showSize = r.w > sw + 70;
    c.font = `600 11.5px ${pal.font}`;
    const t = fitText(c, r.node.n, r.w - 12 - (showSize ? sw + 10 : 0));
    const y = r.y + r.hh / 2 + 0.5;
    if (t) c.fillText(t, r.x + 6, y);
    if (showSize) {
      c.font = `11px ${pal.font}`; c.textAlign = 'right'; c.globalAlpha = 0.75;
      c.fillText(size, r.x + r.w - 6, y);
      c.globalAlpha = 1;
    }
  }

  function hueOf(r) {
    const n = r.node;
    if (mode === 'branch') return pal.series(r.branch);
    if (n.t === 's' || n.t === 'o') return null;
    return n.t === 'f' ? pal.cat(n.cat) : null;
  }

  function paintRect(c, r, hot) {
    const n = r.node;
    const hue = hueOf(r);
    const branch = mode === 'branch';
    if (n.t === 'd' && r.open) {
      c.fillStyle = branch ? mix(hue, pal.surface, 0.72) : pal.s2;
      c.fillRect(r.x, r.y, r.w, r.h);
      if (r.hh) {
        const hf = branch ? mix(hue, pal.surface, 0.5) : pal.s3;
        paintHeader(c, r, hf, branch ? contrastText(hf) : pal.ink2);
      }
      return;
    }
    let fill;
    let text;
    if (n.t === 'f') {
      fill = hot ? hue : mix(hue, pal.surface, 0.38 + 0.03 * (r.depth % 3));
      text = contrastText(fill);
    } else if (hue && (n.t === 's' || n.t === 'o')) { // aggregates, branch mode: lighter tint of the branch
      fill = hot ? mix(hue, pal.surface, 0.4) : mix(hue, pal.surface, 0.68);
      text = contrastText(fill);
    } else if (hue) { // collapsed folder, branch mode
      fill = hot ? hue : mix(hue, pal.surface, 0.5);
      text = contrastText(fill);
    } else { // neutral: aggregates and collapsed folders in type mode
      fill = hot ? pal.axis : (n.t === 'd' ? pal.s3 : pal.s2);
      text = pal.ink2;
    }
    paintLeaf(c, r, fill, text);
  }

  function drawBase() {
    pal = readPalette();
    bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    bctx.clearRect(0, 0, W, H);
    for (const r of rects) paintRect(bctx, r, false);
  }

  function outline(r, color) {
    ctx.strokeStyle = color; ctx.lineWidth = 2;
    if (r.w >= 4 && r.h >= 4) ctx.strokeRect(r.x + 1, r.y + 1, r.w - 2, r.h - 2);
    else ctx.strokeRect(r.x, r.y, r.w, r.h);
  }

  function draw() {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!rects.length) return;
    ctx.drawImage(base, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const sel = selPath ? byPath.get(selPath) : null;
    if (sel && sel !== hover) outline(sel, pal.accent);
    if (hover) {
      if (!hover.open) paintRect(ctx, hover, true);
      outline(hover, pal.ink);
    }
  }

  // ---- layout
  function relayout() {
    rects = tree ? buildLayout(tree, W, H) : [];
    byPath = new Map();
    for (const r of rects) if (r.path) byPath.set(r.path, r);
    hover = null;
    drawBase();
    draw();
  }

  function measure() {
    const w = Math.floor(container.clientWidth); const h = Math.floor(container.clientHeight);
    if (w <= 0 || h <= 0) return false;
    dpr = window.devicePixelRatio || 1;
    W = w; H = h;
    for (const c of [canvas, base]) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    return true;
  }

  function resize() {
    if (measure()) relayout();
  }

  // ---- interaction
  const info = (r) => {
    const n = r.node;
    return {
      path: r.path, name: n.n || '', size: n.s, type: TYPE_NAMES[n.t], cat: n.cat,
      files: n.t === 'd' ? n.fc : n.k,
    };
  };

  function hit(x, y) {
    for (let i = rects.length - 1; i >= 0; i--) {
      const r = rects[i];
      if (x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h) return r;
    }
    return null;
  }

  function tooltipFor(r) {
    const n = r.node;
    const ratio = tree && tree.s > 0 ? n.s / tree.s : 0;
    const share = fmtPercent(ratio, ratio < 0.1 ? 1 : 0);
    const meta = [];
    let title = n.n;
    if (n.t === 'f') {
      meta.push(CAT_LABELS[n.cat] || CAT_LABELS.other);
      if (n.m) meta.push(`Modifié le ${fmtDate(n.m)}`);
    } else if (n.t === 'd') {
      meta.push(n.fc != null ? `Dossier · ${fmtNumber(n.fc)} fichiers` : 'Dossier');
      if (n.x) meta.push('Dossier non lisible');
      else if (n.more || !n.ch) meta.push('Contenu non détaillé');
    } else {
      title = n.n || (n.t === 's' ? 'Petits fichiers' : 'Autres éléments');
      meta.push(`${fmtNumber(n.k)} ${n.t === 's' ? 'petits fichiers' : 'éléments'}`);
    }
    return el('div', {},
      el('div', { class: 'tt-value', text: fmtBytes(n.s) }),
      el('div', { class: 'tt-label', text: title }),
      el('div', { class: 'tt-meta', text: `${share} du total` }),
      ...meta.map((m) => el('div', { class: 'tt-meta', text: m })));
  }

  const onMove = (e) => {
    const r = hit(e.offsetX, e.offsetY);
    if (r !== hover) { hover = r; draw(); }
    if (r) { showTooltip(e.clientX, e.clientY, tooltipFor(r)); canvas.style.cursor = 'pointer'; } else {
      hideTooltip(); canvas.style.cursor = 'default';
    }
  };
  const onLeave = () => { hover = null; hideTooltip(); draw(); };
  const onClick = (e) => {
    const r = hit(e.offsetX, e.offsetY);
    if (!r) return;
    selPath = r.path;
    draw();
    if (onSelect) onSelect(info(r));
  };
  const onDbl = (e) => {
    const r = hit(e.offsetX, e.offsetY);
    if (r && r.node.t === 'd' && onOpen) onOpen(r.path);
  };
  const onKey = (e) => {
    if (e.key !== 'Enter' || !selPath || !onOpen) return;
    const r = byPath.get(selPath);
    if (r && r.node.t === 'd') onOpen(r.path);
  };
  canvas.addEventListener('mousemove', onMove);
  canvas.addEventListener('mouseleave', onLeave);
  canvas.addEventListener('click', onClick);
  canvas.addEventListener('dblclick', onDbl);
  canvas.addEventListener('keydown', onKey);

  const ro = new ResizeObserver(() => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(resize);
  });
  ro.observe(container);
  const unwatch = watchTheme(() => { drawBase(); draw(); });

  return {
    /** Replaces the displayed tree (root has `path`) and plays a short fade-in. */
    setData(t) {
      tree = t || null;
      selPath = null;
      hideTooltip();
      canvas.classList.remove('ch-in');
      if (measure() || W) relayout();
      void canvas.offsetWidth; // restart the CSS transition
      canvas.classList.add('ch-in');
    },
    /** @param {'type'|'branch'} m */
    setColorMode(m) {
      mode = m === 'branch' ? 'branch' : 'type';
      drawBase(); draw();
    },
    /** Persistent selection outline for a real path (null clears it). */
    select(path) { selPath = path || null; draw(); },
    resize,
    destroy() {
      cancelAnimationFrame(raf);
      ro.disconnect();
      unwatch();
      canvas.removeEventListener('mousemove', onMove);
      canvas.removeEventListener('mouseleave', onLeave);
      canvas.removeEventListener('click', onClick);
      canvas.removeEventListener('dblclick', onDbl);
      canvas.removeEventListener('keydown', onKey);
      hideTooltip();
      canvas.remove();
      container.classList.remove('ch-treemap');
      rects = []; byPath.clear(); tree = null;
    },
  };
}
