// SVG sunburst (up to 4 rings). Untrusted names only reach the DOM through
// textContent / el() children.
import {
  el, fmtBytes, fmtNumber, fmtPercent, fmtDate, cssVar, catColor, seriesColor, joinPath,
  showTooltip, hideTooltip,
} from '../ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const MAX_RINGS = 4;
const MIN_ANGLE = (0.5 * Math.PI) / 180;
const TAU = Math.PI * 2;
const CAT_LABELS = {
  app: 'Applications', data: 'Données', disk_image: 'Images disque', video: 'Vidéos', image: 'Images',
  document: 'Documents', code: 'Code', archive: 'Archives', other: 'Autres',
};
const TYPE_NAMES = { d: 'dir', f: 'file', s: 'small', o: 'other' };

// ------------------------------------------------------------------ pure helpers
function hexToRgb(c) {
  let m = /^#([0-9a-f]{3})$/i.exec(c);
  if (m) return [...m[1]].map((h) => parseInt(h + h, 16));
  m = /^#([0-9a-f]{6})/i.exec(c);
  if (m) return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16));
  m = /rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i.exec(c || '');
  return m ? [+m[1], +m[2], +m[3]] : [128, 128, 128];
}
const rgbToHex = (rgb) => `#${rgb.map((v) => Math.round(v).toString(16).padStart(2, '0')).join('')}`;
/** Mix `a` toward `b`; t = share of b. */
function mix(a, b, t) {
  const ra = hexToRgb(a); const rb = hexToRgb(b);
  return rgbToHex(ra.map((v, i) => v + (rb[i] - v) * t));
}

/** Flat list of arcs {node, path, depth(1..4), a0, a1, branch}; angles in radians from 12 o'clock. */
function buildArcs(root) {
  const arcs = [];
  const rootIndex = new Map((root.ch || []).map((c, i) => [c, i]));
  const walk = (node, path, a0, a1, depth, branchOf) => {
    if (depth > MAX_RINGS || !node.ch) return;
    const kids = node.ch.filter((c) => c.s > 0).sort((a, b) => b.s - a.s);
    const denom = Math.max(node.s, kids.reduce((a, c) => a + c.s, 0));
    if (!denom) return;
    let cursor = a0;
    for (const k of kids) {
      const ang = ((a1 - a0) * k.s) / denom;
      if (ang >= MIN_ANGLE) {
        const real = k.t === 'd' || k.t === 'f';
        const kp = real ? joinPath(path, k.n) : null;
        const branch = node === root ? rootIndex.get(k) : branchOf;
        arcs.push({ node: k, path: kp, depth, a0: cursor, a1: cursor + ang, branch });
        if (k.t === 'd') walk(k, kp, cursor, cursor + ang, depth + 1, branch);
      }
      cursor += ang;
    }
  };
  walk(root, root.path || '', 0, TAU, 1, -1);
  return arcs;
}

function arcPath(cx, cy, r0, r1, a0, a1) {
  if (a1 - a0 >= TAU - 1e-4) a1 = a0 + TAU - 1e-4;
  const p = (r, a) => `${(cx + r * Math.sin(a)).toFixed(2)} ${(cy - r * Math.cos(a)).toFixed(2)}`;
  const large = a1 - a0 > Math.PI ? 1 : 0;
  return `M${p(r1, a0)}A${r1} ${r1} 0 ${large} 1 ${p(r1, a1)}L${p(r0, a1)}A${r0} ${r0} 0 ${large} 0 ${p(r0, a0)}Z`;
}

function svgEl(tag, attrs = {}, css = {}) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  for (const [k, v] of Object.entries(css)) n.style.setProperty(k, v);
  return n;
}

function watchTheme(cb) {
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  document.addEventListener('themechange', cb);
  mq.addEventListener('change', cb);
  return () => { document.removeEventListener('themechange', cb); mq.removeEventListener('change', cb); };
}

// ------------------------------------------------------------------ component
/**
 * Sunburst of the current folder. The host drills down by calling setData() with a subtree.
 * @param {HTMLElement} container
 * @param {{onSelect?: Function, onOpen?: Function, onUp?: Function, colorMode?: 'type'|'branch'}} opts
 * @returns {{setData(tree: object): void, setColorMode(mode: string): void, resize(): void, destroy(): void}}
 */
export function createSunburst(container, { onSelect, onOpen, onUp, colorMode = 'branch' } = {}) {
  let mode = colorMode;
  let tree = null;
  let arcs = [];
  let raf = 0;
  const svg = svgEl('svg', { role: 'img', 'aria-label': 'Sunburst de l\'occupation du disque' });
  container.classList.add('ch-sunburst');
  container.append(svg);

  function colors(a, pal) {
    const n = a.node;
    const dep = a.depth - 1;
    if (n.t === 's' || n.t === 'o') return { fill: pal.s2, hot: pal.axis };
    if (mode === 'branch') {
      const hue = seriesColor(a.branch);
      return { fill: mix(hue, pal.surface, 0.34 + 0.04 * dep), hot: hue };
    }
    if (n.t === 'f') {
      const hue = catColor(n.cat);
      return { fill: mix(hue, pal.surface, 0.38 + 0.03 * (dep % 3)), hot: hue };
    }
    return { fill: pal.s3, hot: pal.axis };
  }

  function tooltipFor(a) {
    const n = a.node;
    const ratio = tree && tree.s > 0 ? n.s / tree.s : 0;
    const meta = [`${fmtPercent(ratio, ratio < 0.1 ? 1 : 0)} du total`];
    let title = n.n;
    if (n.t === 'f') {
      meta.push(CAT_LABELS[n.cat] || CAT_LABELS.other);
      if (n.m) meta.push(`Modifié le ${fmtDate(n.m)}`);
    } else if (n.t === 'd') {
      meta.push(n.fc != null ? `Dossier · ${fmtNumber(n.fc)} fichiers` : 'Dossier');
      if (n.x) meta.push('Dossier non lisible');
    } else {
      title = n.n || (n.t === 's' ? 'Petits fichiers' : 'Autres éléments');
      meta.push(`${fmtNumber(n.k)} ${n.t === 's' ? 'petits fichiers' : 'éléments'}`);
    }
    return el('div', {},
      el('div', { class: 'tt-value', text: fmtBytes(n.s) }),
      el('div', { class: 'tt-label', text: title }),
      ...meta.map((m) => el('div', { class: 'tt-meta', text: m })));
  }

  const info = (a) => ({
    path: a.path, name: a.node.n || '', size: a.node.s, type: TYPE_NAMES[a.node.t], cat: a.node.cat,
    files: a.node.t === 'd' ? a.node.fc : a.node.k,
  });

  function render() {
    while (svg.firstChild) svg.firstChild.remove();
    const w = container.clientWidth; const h = container.clientHeight;
    if (w <= 0 || h <= 0) return;
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    const cx = w / 2; const cy = h / 2;
    const R = Math.max(40, Math.min(w, h) / 2 - 4);
    const r0 = R * 0.26;
    const ringW = (R - r0) / MAX_RINGS;
    const pal = { surface: cssVar('--surface'), s2: cssVar('--surface-2'), s3: cssVar('--surface-3'), axis: cssVar('--axis') };
    const gap = svgEl('g');
    const hoverLayer = svgEl('g', { 'pointer-events': 'none' });
    svg.append(gap, hoverLayer);

    arcs = tree ? buildArcs(tree) : [];
    arcs.forEach((a, i) => {
      const inner = r0 + (a.depth - 1) * ringW;
      a.d = arcPath(cx, cy, inner + 0.5, inner + ringW, a.a0, a.a1);
      a.col = colors(a, pal);
      const p = svgEl('path', { d: a.d, class: 'ch-arc', 'data-i': i },
        { fill: a.col.fill, stroke: 'var(--surface)', 'stroke-width': '1.5px', 'stroke-linejoin': 'round' });
      gap.append(p);
    });

    // center: current folder, click = up
    const g = svgEl('g', { class: 'ch-center', tabindex: onUp ? 0 : -1, role: 'button', 'aria-label': 'Dossier parent' });
    const disc = svgEl('circle', { cx, cy, r: r0 - 3 }, { fill: 'var(--surface)', stroke: 'var(--border)', 'stroke-width': '1px' });
    g.append(disc);
    const fs = Math.max(11, Math.min(16, r0 * 0.2));
    const maxChars = Math.max(6, Math.floor((r0 * 1.7) / (fs * 0.56)));
    const raw = tree ? (tree.n || tree.path || '') : '';
    const name = raw.length > maxChars ? `${raw.slice(0, maxChars - 1)}…` : raw;
    const mk = (txt, y, size, weight, fill) => {
      const t = svgEl('text', { x: cx, y, 'text-anchor': 'middle' },
        { fill, 'font-size': `${size}px`, 'font-weight': weight, 'font-family': 'var(--font)', 'pointer-events': 'none' });
      t.textContent = txt;
      return t;
    };
    g.append(mk(name, cy - fs * 0.2, fs, 600, 'var(--ink)'));
    g.append(mk(tree ? fmtBytes(tree.s) : '', cy + fs * 1.1, fs - 1, 400, 'var(--ink-2)'));
    if (onUp) g.append(mk('↑ Remonter', cy + fs * 2.4, Math.max(10, fs - 3), 400, 'var(--ink-3)'));
    svg.append(g);

    const showHover = (a) => {
      while (hoverLayer.firstChild) hoverLayer.firstChild.remove();
      if (!a) return;
      hoverLayer.append(svgEl('path', { d: a.d },
        { fill: a.col.hot, stroke: 'var(--ink)', 'stroke-width': '2px', 'stroke-linejoin': 'round' }));
    };
    const arcOf = (e) => {
      const i = e.target && e.target.getAttribute ? e.target.getAttribute('data-i') : null;
      return i === null ? null : arcs[+i];
    };
    svg.onmousemove = (e) => {
      const a = arcOf(e);
      showHover(a);
      if (a) showTooltip(e.clientX, e.clientY, tooltipFor(a)); else hideTooltip();
    };
    svg.onmouseleave = () => { showHover(null); hideTooltip(); };
    svg.onclick = (e) => {
      const a = arcOf(e);
      if (!a) return;
      if (onSelect) onSelect(info(a));
      if (a.node.t === 'd' && onOpen) onOpen(a.path);
    };
    const up = () => { if (onUp) onUp(); };
    g.addEventListener('click', up);
    g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); up(); } });
    g.addEventListener('mouseenter', () => disc.style.setProperty('fill', onUp ? 'var(--surface-2)' : 'var(--surface)'));
    g.addEventListener('mouseleave', () => disc.style.setProperty('fill', 'var(--surface)'));
  }

  const ro = new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(render); });
  ro.observe(container);
  const unwatch = watchTheme(render);

  return {
    /** Shows `tree` (a folder node with `path`) as the center + 4 rings of descendants. */
    setData(t) { tree = t || null; hideTooltip(); render(); },
    /** @param {'type'|'branch'} m */
    setColorMode(m) { mode = m === 'type' ? 'type' : 'branch'; render(); },
    resize: render,
    destroy() {
      cancelAnimationFrame(raf);
      ro.disconnect();
      unwatch();
      hideTooltip();
      svg.remove();
      container.classList.remove('ch-sunburst');
      arcs = []; tree = null;
    },
  };
}
