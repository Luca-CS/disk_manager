// SVG ring meter. Colors come from CSS tokens via var(), so theme changes apply live.
import { fmtPercent } from '../ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const STROKE = 14;

function svgEl(tag, attrs = {}, css = {}) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  for (const [k, v] of Object.entries(css)) n.style.setProperty(k, v);
  return n;
}

/**
 * Circular usage meter: accent below 80%, warning from 80%, critical from 90%.
 * @param {number} used bytes
 * @param {number} total bytes
 * @param {{size?: number, label?: string}} opts
 * @returns {SVGSVGElement}
 */
export function usageRing(used, total, { size = 168, label } = {}) {
  const ratio = total > 0 ? Math.max(0, Math.min(1, used / total)) : 0;
  const r = (size - STROKE) / 2;
  const c = 2 * Math.PI * r;
  const color = ratio >= 0.9 ? 'var(--critical)' : ratio >= 0.8 ? 'var(--warning)' : 'var(--accent)';
  const pct = fmtPercent(ratio);
  const svg = svgEl('svg', {
    class: 'ch-ring', width: size, height: size, viewBox: `0 0 ${size} ${size}`, role: 'img',
    'aria-label': `${pct} utilisé${label ? ` — ${label}` : ''}`,
  });
  const mid = size / 2;
  svg.append(svgEl('circle', { cx: mid, cy: mid, r, fill: 'none', 'stroke-width': STROKE }, { stroke: 'var(--accent-wash)' }));
  const arc = svgEl('circle', {
    cx: mid, cy: mid, r, fill: 'none', 'stroke-width': STROKE, 'stroke-linecap': 'round',
    'stroke-dasharray': c, 'stroke-dashoffset': c, transform: `rotate(-90 ${mid} ${mid})`,
  }, { stroke: color, transition: 'stroke-dashoffset .9s cubic-bezier(.2,.7,.2,1)' });
  if (ratio === 0) arc.style.setProperty('display', 'none');
  svg.append(arc);

  const text = (txt, y, px, weight, fill) => {
    const t = svgEl('text', { x: mid, y, 'text-anchor': 'middle' },
      { fill, 'font-size': `${px}px`, 'font-weight': weight, 'font-family': 'var(--font)', 'font-variant-numeric': 'tabular-nums' });
    t.textContent = txt;
    return t;
  };
  const big = Math.round(size * 0.2);
  const small = Math.max(11, Math.round(size * 0.075));
  svg.append(text(pct, label ? mid + big * 0.05 : mid + big * 0.35, big, 600, 'var(--ink)'));
  if (label) svg.append(text(label, mid + big * 0.05 + small + 6, small, 400, 'var(--ink-3)'));

  // animate on first render (after the element is in the document and styled)
  requestAnimationFrame(() => requestAnimationFrame(() => arc.setAttribute('stroke-dashoffset', c * (1 - ratio))));
  return svg;
}
