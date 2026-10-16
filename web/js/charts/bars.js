// Horizontal bar list, stacked bar and legend. Labels are untrusted: el() text only.
import { el, fmtBytes, fmtPercent, showTooltip, hideTooltip } from '../ui.js';

const DEFAULT_COLOR = 'var(--accent)';
const pctText = (r) => fmtPercent(r, r > 0 && r < 0.1 ? 1 : 0);

/**
 * Ranked horizontal bars.
 * @param {{key: string, label: string, value: number, sublabel?: string}[]} items
 * @param {{total?: number, colorOf?: Function, onClick?: Function, formatValue?: Function, maxItems?: number}} opts
 * @returns {HTMLElement}
 */
// Note: `colorOf(item)` receives the whole item ({key, label, value, ...}), not just its key.
export function barList(items, { total, colorOf, onClick, formatValue = fmtBytes, maxItems } = {}) {
  const rows = maxItems ? items.slice(0, maxItems) : items;
  const sum = total ?? items.reduce((a, i) => a + i.value, 0);
  const max = Math.max(1, ...rows.map((i) => i.value));
  const root = el('div', { class: 'ch-bars' });
  for (const it of rows) {
    const color = (colorOf && colorOf(it)) || DEFAULT_COLOR;
    const pct = sum > 0 ? it.value / sum : 0;
    const row = el(onClick ? 'button' : 'div', { class: 'ch-row', type: onClick ? 'button' : null },
      el('span', { class: 'dot', style: { background: color } }),
      el('div', { class: 'ch-row-label' },
        el('span', { text: it.label }),
        it.sublabel ? el('span', { class: 'ch-row-sub', text: it.sublabel }) : null),
      el('div', { class: 'ch-row-track' },
        el('div', { class: 'ch-row-fill', style: { width: `${(it.value / max) * 100}%`, background: color } })),
      el('div', { class: 'ch-row-value' },
        el('span', { class: 'ch-val', text: formatValue(it.value) }),
        el('span', { class: 'ch-pct', text: pctText(pct) })));
    if (onClick) row.addEventListener('click', () => onClick(it));
    root.append(row);
  }
  return root;
}

/**
 * One horizontal 100% bar. Segments under 0.3% of the total (about 2px) are skipped.
 * @param {{key: string, label: string, value: number}[]} segments
 * @param {{total?: number, colorOf?: Function, height?: number, onClick?: Function}} opts
 * @returns {HTMLElement}
 */
export function stackedBar(segments, { total, colorOf, height = 14, onClick } = {}) {
  const sum = segments.reduce((a, s) => a + s.value, 0);
  const whole = Math.max(total ?? sum, sum, 1);
  const root = el('div', { class: 'ch-stack', style: { height: `${height}px` }, role: 'group' });
  let used = 0;
  for (const s of segments) {
    const ratio = s.value / whole;
    if (ratio < 0.003) continue;
    used += s.value;
    const color = (colorOf && colorOf(s)) || DEFAULT_COLOR;
    const seg = el(onClick ? 'button' : 'div', {
      class: 'ch-stack-seg', type: onClick ? 'button' : null,
      style: { flex: `${s.value} 1 0`, background: color }, 'aria-label': `${s.label} : ${fmtBytes(s.value)}`,
    });
    const tip = (e) => showTooltip(e.clientX, e.clientY, el('div', {},
      el('div', { class: 'tt-value', text: fmtBytes(s.value) }),
      el('div', { class: 'tt-label' }, el('span', { class: 'tt-key', style: { background: color } }), s.label),
      el('div', { class: 'tt-meta', text: pctText(ratio) })));
    seg.addEventListener('mouseenter', tip);
    seg.addEventListener('mousemove', tip);
    seg.addEventListener('mouseleave', hideTooltip);
    if (onClick) seg.addEventListener('click', () => onClick(s));
    root.append(seg);
  }
  const rest = whole - used;
  if (rest / whole > 0.003) {
    root.append(el('div', { class: 'ch-stack-seg ch-stack-rest', style: { flex: `${rest} 1 0` } }));
  }
  return root;
}

/**
 * Legend with colored swatches.
 * @param {{key: string, label: string}[]} items
 * @param {{colorOf?: Function}} opts
 * @returns {HTMLElement}
 */
export function legend(items, { colorOf } = {}) {
  return el('div', { class: 'legend' }, items.map((it) => el('span', { class: 'legend-item' },
    el('span', { class: 'dot', style: { background: (colorOf && colorOf(it)) || DEFAULT_COLOR } }),
    it.label)));
}
