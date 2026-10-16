// Line + area chart of used space over time (SVG). Colors via CSS var() so themes apply live.
import { el, fmtBytes, fmtDate, fmtDateTime, fmtPercent, showTooltip, hideTooltip } from '../ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const M = { left: 58, right: 14, top: 12, bottom: 26 };
const GIB = 2 ** 30;

function svgEl(tag, attrs = {}, css = {}) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  for (const [k, v] of Object.entries(css)) n.style.setProperty(k, v);
  return n;
}

/** 0, step, 2*step... (<= max) with a 1/2/5 x 10^k step in Go (or Mo/Ko for tiny disks); 3-4 ticks. */
function niceTicks(max) {
  const unit = max >= GIB ? GIB : max >= 2 ** 20 ? 2 ** 20 : 2 ** 10;
  const raw = max / unit / 3.6;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 5, 10].find((m) => m * pow >= raw) ?? 10) * pow * unit;
  const ticks = [];
  for (let v = 0; v <= max + 1; v += step) ticks.push(v);
  return ticks;
}

const axisBytes = (v) => fmtBytes(v).replace(',0 ', ' ');

/**
 * Renders the usage history into `container` and re-renders on resize.
 * @param {HTMLElement} container
 * @param {{ts: number, used: number, total: number}[]} points epoch seconds / bytes, any order
 * @param {{total?: number}} opts
 * @returns {{destroy(): void}}
 */
export function usageChart(container, points, { total } = {}) {
  const pts = [...points].sort((a, b) => a.ts - b.ts);
  const yMax = Math.max(1, total || 0, ...pts.map((p) => Math.max(p.total || 0, p.used)));
  container.classList.add('ch-area');
  let raf = 0;

  function render() {
    container.replaceChildren();
    const W = Math.floor(container.clientWidth);
    if (W <= 0) return;
    const H = container.clientHeight > 100 ? Math.floor(container.clientHeight) : 220;
    const iw = W - M.left - M.right; const ih = H - M.top - M.bottom;
    const svg = svgEl('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Évolution de l\'espace utilisé' });
    container.append(svg);
    const y = (v) => M.top + ih - (v / yMax) * ih;

    // y grid + labels
    for (const t of niceTicks(yMax)) {
      const yy = Math.round(y(t)) + 0.5;
      svg.append(svgEl('line', { x1: M.left, x2: W - M.right, y1: yy, y2: yy },
        { stroke: t === 0 ? 'var(--axis)' : 'var(--grid)', 'stroke-width': '1px' }));
      const lab = svgEl('text', { x: M.left - 8, y: yy + 4, 'text-anchor': 'end' },
        { fill: 'var(--ink-3)', 'font-size': '11px', 'font-family': 'var(--font)' });
      lab.textContent = axisBytes(t);
      svg.append(lab);
    }

    if (!pts.length) return;
    if (pts.length === 1) {
      const cx = M.left + iw / 2;
      svg.append(svgEl('circle', { cx, cy: y(pts[0].used), r: 5 }, { fill: 'var(--accent)', stroke: 'var(--surface)', 'stroke-width': '2px' }));
      const dateLab = svgEl('text', { x: cx, y: H - 8, 'text-anchor': 'middle' },
        { fill: 'var(--ink-3)', 'font-size': '11px', 'font-family': 'var(--font)' });
      dateLab.textContent = fmtDate(pts[0].ts);
      svg.append(dateLab);
      container.append(el('div', { class: 'ch-area-note', text: 'L\'historique se remplit à chaque lancement' }));
      return;
    }

    const t0 = pts[0].ts; const t1 = pts[pts.length - 1].ts;
    const span = Math.max(1, t1 - t0);
    const x = (ts) => M.left + ((ts - t0) / span) * iw;

    // x ticks
    const n = iw < 420 ? 3 : iw < 700 ? 4 : 5;
    for (let i = 0; i < n; i++) {
      const f = i / (n - 1);
      const lab = svgEl('text', { x: M.left + f * iw, y: H - 8, 'text-anchor': i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle' },
        { fill: 'var(--ink-3)', 'font-size': '11px', 'font-family': 'var(--font)' });
      lab.textContent = fmtDate(t0 + f * span);
      svg.append(lab);
    }

    const line = pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.ts).toFixed(1)} ${y(p.used).toFixed(1)}`).join('');
    const base = (M.top + ih).toFixed(1);
    svg.append(svgEl('path', { d: `${line}L${x(t1).toFixed(1)} ${base}L${x(t0).toFixed(1)} ${base}Z` },
      { fill: 'var(--accent)', 'fill-opacity': '0.1', stroke: 'none' }));
    svg.append(svgEl('path', { d: line }, {
      fill: 'none', stroke: 'var(--accent)', 'stroke-width': '2px', 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));

    // crosshair
    const cross = svgEl('line', { y1: M.top, y2: M.top + ih }, { stroke: 'var(--axis)', 'stroke-width': '1px', display: 'none' });
    const dot = svgEl('circle', { r: 5 }, { fill: 'var(--accent)', stroke: 'var(--surface)', 'stroke-width': '2px', display: 'none' });
    const hit = svgEl('rect', { x: M.left, y: M.top, width: iw, height: ih }, { fill: 'transparent' });
    svg.append(cross, dot, hit);
    const nearest = (px) => {
      const ts = t0 + ((px - M.left) / iw) * span;
      let best = pts[0];
      for (const p of pts) if (Math.abs(p.ts - ts) < Math.abs(best.ts - ts)) best = p;
      return best;
    };
    hit.addEventListener('mousemove', (e) => {
      const r = svg.getBoundingClientRect();
      const p = nearest(e.clientX - r.left);
      const px = x(p.ts);
      cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.style.setProperty('display', 'block');
      dot.setAttribute('cx', px); dot.setAttribute('cy', y(p.used)); dot.style.setProperty('display', 'block');
      const tot = p.total || yMax;
      showTooltip(e.clientX, e.clientY, el('div', {},
        el('div', { class: 'tt-value', text: fmtBytes(p.used) }),
        el('div', { class: 'tt-label', text: `Libre : ${fmtBytes(Math.max(0, tot - p.used))}` }),
        el('div', { class: 'tt-meta', text: `${fmtPercent(tot ? p.used / tot : 0)} utilisé` }),
        el('div', { class: 'tt-meta', text: fmtDateTime(p.ts) })));
    });
    hit.addEventListener('mouseleave', () => {
      cross.style.setProperty('display', 'none'); dot.style.setProperty('display', 'none'); hideTooltip();
    });
  }

  const ro = new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(render); });
  ro.observe(container);
  render();
  return {
    destroy() {
      cancelAnimationFrame(raf); ro.disconnect(); hideTooltip();
      container.replaceChildren();
      container.classList.remove('ch-area');
    },
  };
}
