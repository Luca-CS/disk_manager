// Shared UI toolkit. Rule: data (file names, paths...) is untrusted and only
// ever reaches the DOM through textContent / el() children — never innerHTML.

// ------------------------------------------------------------------ DOM
/**
 * el('div', {class: 'card', onClick: fn, dataset: {id: 1}, style: {width: '40%'}}, child, 'text', ...)
 * Strings and numbers become text nodes. null/false children are skipped.
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'style') Object.assign(node.style, v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, String(v));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.firstChild.remove();
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ------------------------------------------------------------------ formatting (fr-FR)
const nf = new Intl.NumberFormat('fr-FR');
const nf1 = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1, minimumFractionDigits: 1 });
const nf0 = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 0 });
const UNITS = ['o', 'Ko', 'Mo', 'Go', 'To', 'Po'];

/** Binary units with French labels, like Windows Explorer: 1 536 → "1,5 Ko". */
export function fmtBytes(n, { precise = false } = {}) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const neg = n < 0;
  let v = Math.abs(n);
  let i = 0;
  while (v >= 1024 && i < UNITS.length - 1) { v /= 1024; i++; }
  const s = i === 0 ? nf0.format(v) : (v >= 100 && !precise ? nf0.format(v) : nf1.format(v));
  return `${neg ? '−' : ''}${s} ${UNITS[i]}`;
}

/** Split for hero display: {value: "106,1", unit: "Go"}. */
export function bytesParts(n) {
  const [value, unit] = fmtBytes(n).split(' ');
  return { value, unit };
}

export const fmtNumber = (n) => (n === null || n === undefined ? '—' : nf.format(n));
export const fmtPercent = (ratio, digits = 0) =>
  `${new Intl.NumberFormat('fr-FR', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(ratio * 100)} %`;

const dateFmt = new Intl.DateTimeFormat('fr-FR', { day: '2-digit', month: '2-digit', year: 'numeric' });
const dateTimeFmt = new Intl.DateTimeFormat('fr-FR', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
const rtf = new Intl.RelativeTimeFormat('fr-FR', { numeric: 'auto' });

/** Epoch seconds → "16/10/2026". */
export const fmtDate = (ts) => (ts ? dateFmt.format(new Date(ts * 1000)) : '—');
export const fmtDateTime = (ts) => (ts ? dateTimeFmt.format(new Date(ts * 1000)) : '—');

/** Epoch seconds → "il y a 3 jours". */
export function fmtRelative(ts) {
  if (!ts) return '—';
  const diff = ts - Date.now() / 1000;
  const abs = Math.abs(diff);
  const steps = [[60, 'second', 1], [3600, 'minute', 60], [86400, 'hour', 3600], [2592000, 'day', 86400],
    [31536000, 'month', 2592000], [Infinity, 'year', 31536000]];
  for (const [limit, unit, div] of steps) {
    if (abs < limit) return rtf.format(Math.round(diff / div), unit);
  }
  return fmtDate(ts);
}

export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 1) return '< 1 s';
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return s ? `${m} min ${s} s` : `${m} min`;
}

export const plural = (n, one, many) => `${fmtNumber(n)} ${n > 1 ? many : one}`;

// ------------------------------------------------------------------ paths
export const baseName = (p) => (p || '').replace(/[\\]+$/, '').split('\\').pop() || p;
export const dirName = (p) => {
  const s = (p || '').replace(/[\\]+$/, '');
  const i = s.lastIndexOf('\\');
  return i <= 2 ? s.slice(0, 3) : s.slice(0, i);
};
export const joinPath = (base, name) => (base.endsWith('\\') ? base + name : `${base}\\${name}`);
/** Shorten the user profile prefix: C:\Users\me\Desktop → ~\Desktop */
export function prettyPath(p, home) {
  if (home && p && p.toLowerCase().startsWith(home.toLowerCase())) return `~${p.slice(home.length)}`;
  return p;
}

// ------------------------------------------------------------------ colors (read from CSS tokens)
export function cssVar(name, root = document.documentElement) {
  return getComputedStyle(root).getPropertyValue(name).trim();
}
export const CATEGORY_KEYS = ['app', 'data', 'disk_image', 'video', 'image', 'document', 'code', 'archive', 'other'];
export const catColor = (key) => cssVar(`--cat-${CATEGORY_KEYS.includes(key) ? key : 'other'}`);
/** Branch colors: slot 0..7 in fixed order, anything else → neutral "other". */
export const seriesColor = (i) => cssVar(i >= 0 && i < 8 ? `--series-${i + 1}` : '--series-other');

// ------------------------------------------------------------------ icons (24×24, stroke)
const ICONS = {
  gauge: ['M4.2 18.5a9 9 0 1 1 15.6 0', 'M12 13.5l4-4.5', 'C12 13.5 1.6'],
  explorer: ['R3 3 18 18 2', 'M3 12h9', 'M12 3v18', 'M12 8.5h9'],
  pie: ['M12 3a9 9 0 1 0 9 9h-9z', 'M15 3.6A9 9 0 0 1 20.4 9H15z'],
  bigfile: ['M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z', 'M14 3v5h5', 'M9 17h6', 'M9 13h6'],
  copy: ['R8 8 13 13 2', 'M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3'],
  sparkles: ['M11 3.5l1.9 5 5 1.9-5 1.9-1.9 5-1.9-5-5-1.9 5-1.9z', 'M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z'],
  apps: ['R3.5 3.5 7 7 1.5', 'R13.5 3.5 7 7 1.5', 'R3.5 13.5 7 7 1.5', 'R13.5 13.5 7 7 1.5'],
  clock: ['C12 12 9', 'M12 7v5l3.2 2'],
  drive: ['R2.5 6 19 12 2.5', 'M2.5 13.5h19', 'M6.5 16.4h.01', 'M17.5 16.4h-5'],
  refresh: ['M20 12a8 8 0 1 1-2.3-5.6', 'M20 4v4.5h-4.5'],
  play: ['M8 5.5v13l10.5-6.5z'],
  stop: ['R6 6 12 12 2'],
  x: ['M6 6l12 12', 'M18 6L6 18'],
  check: ['M5 12.5l4.5 4.5L19 7.5'],
  trash: ['M4 7h16', 'M9 7V4.5h6V7', 'M6.5 7l1 13h9l1-13', 'M10 11v5.5', 'M14 11v5.5'],
  external: ['M14 4h6v6', 'M20 4l-8.5 8.5', 'M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5'],
  search: ['C11 11 6.5', 'M16 16l4.5 4.5'],
  chevronRight: ['M9 6l6 6-6 6'],
  chevronDown: ['M6 9l6 6 6-6'],
  chevronLeft: ['M15 6l-6 6 6 6'],
  arrowUp: ['M12 19V5', 'M6 11l6-6 6 6'],
  arrowDown: ['M12 5v14', 'M6 13l6 6 6-6'],
  alert: ['M12 3.5L2.5 20h19z', 'M12 10v4.5', 'M12 17.4v.01'],
  info: ['C12 12 9', 'M12 11v5.5', 'M12 7.6v.01'],
  shield: ['M12 3l7.5 3v5.5c0 4.5-3.2 8.2-7.5 9.5-4.3-1.3-7.5-5-7.5-9.5V6z', 'M8.5 12l2.5 2.5 4.5-4.5'],
  shieldUp: ['M12 3l7.5 3v5.5c0 4.5-3.2 8.2-7.5 9.5-4.3-1.3-7.5-5-7.5-9.5V6z', 'M12 15.5V9', 'M9.2 11.5L12 8.7l2.8 2.8'],
  lock: ['R5 11 14 9.5 2', 'M8 11V8a4 4 0 0 1 8 0v3'],
  folder: ['M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z'],
  file: ['M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z', 'M14 3v5h5'],
  files: ['M9 3h6l4 4v10a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2', 'M4 8v11a2 2 0 0 0 2 2h8'],
  sun: ['C12 12 4', 'M12 2.5v2', 'M12 19.5v2', 'M2.5 12h2', 'M19.5 12h2', 'M5.3 5.3l1.4 1.4', 'M17.3 17.3l1.4 1.4',
    'M5.3 18.7l1.4-1.4', 'M17.3 6.7l1.4-1.4'],
  moon: ['M20 14.5A8.5 8.5 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z'],
  monitor: ['R3 4 18 12 2', 'M8.5 20h7', 'M12 16v4'],
  power: ['M12 3v8.5', 'M6.6 6.5a8 8 0 1 0 10.8 0'],
  zap: ['M13 2.5L4.5 14H11l-1 7.5L18.5 10H12z'],
  download: ['M12 4v11', 'M7 10.5l5 5 5-5', 'M5 20h14'],
  sunburst: ['M12 3a9 9 0 1 1-9 9', 'M12 7.5a4.5 4.5 0 1 1-4.5 4.5', 'M12 3v4.5', 'M3 12h4.5'],
  list: ['M9 6h11', 'M9 12h11', 'M9 18h11', 'M4.5 6h.01', 'M4.5 12h.01', 'M4.5 18h.01'],
  bars: ['M5 20v-8', 'M10 20V5', 'M15 20v-11', 'M20 20v-5'],
  filter: ['M4 5h16l-6 7.5V19l-4 1.5v-8z'],
  undo: ['M4 7.5h11a4.5 4.5 0 0 1 0 9H8', 'M8 3.5l-4 4 4 4'],
  calendar: ['R3.5 5 17 15.5 2', 'M3.5 10h17', 'M8 3v4', 'M16 3v4'],
  terminal: ['R3 4 18 16 2', 'M7 9.5l3 2.5-3 2.5', 'M12.5 15H17'],
  sliders: ['M4 6h9', 'M17 6h3', 'C15 6 2', 'M4 12h3', 'M11 12h9', 'C9 12 2', 'M4 18h11', 'C17 18 2'],
  link: ['M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1', 'M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1'],
  cloud: ['M7 18.5a4.5 4.5 0 1 1 .9-8.9A6 6 0 0 1 19.5 11a3.8 3.8 0 0 1-.5 7.5z'],
  history: ['M3.5 12a8.5 8.5 0 1 0 2.5-6', 'M3.5 4v4.5H8', 'M12 7.5V12l3 2'],
  layers: ['M12 3l9 5-9 5-9-5z', 'M3 13l9 5 9-5'],
  dot: ['C12 12 3'],
};

const SVG_NS = 'http://www.w3.org/2000/svg';
/** icon('trash', {size: 16, class: 'x'}) → <svg> */
export function icon(name, { size, cls } = {}) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  if (size) { svg.setAttribute('width', size); svg.setAttribute('height', size); }
  if (cls) svg.setAttribute('class', cls);
  for (const spec of ICONS[name] || ICONS.dot) {
    let shape;
    if (spec.startsWith('R')) {
      const [x, y, w, h, rx] = spec.slice(1).trim().split(/\s+/).map(Number);
      shape = document.createElementNS(SVG_NS, 'rect');
      Object.entries({ x, y, width: w, height: h, rx: rx || 0 }).forEach(([k, v]) => shape.setAttribute(k, v));
    } else if (spec.startsWith('C')) {
      const [cx, cy, r] = spec.slice(1).trim().split(/\s+/).map(Number);
      shape = document.createElementNS(SVG_NS, 'circle');
      Object.entries({ cx, cy, r }).forEach(([k, v]) => shape.setAttribute(k, v));
    } else {
      shape = document.createElementNS(SVG_NS, 'path');
      shape.setAttribute('d', spec);
    }
    svg.append(shape);
  }
  return svg;
}

// ------------------------------------------------------------------ small components
export function button(label, { variant = '', iconName, onClick, title, disabled, size } = {}) {
  const b = el('button', { class: `btn ${variant} ${size || ''}`.trim(), type: 'button', title, onClick, disabled });
  if (iconName) b.append(icon(iconName));
  if (label) b.append(document.createTextNode(label));
  if (!label) b.classList.add('icon');
  return b;
}

/** Sets a button into a busy state; returns a function that restores it. */
export function busy(btn, text) {
  const saved = [...btn.childNodes];
  const wasDisabled = btn.disabled;
  btn.disabled = true;
  clear(btn).append(el('span', { class: 'spinner' }), text || '');
  return () => { clear(btn).append(...saved); btn.disabled = wasDisabled; };
}

export const RISK = {
  safe: { label: 'Sûr', cls: 'good', icon: 'check', help: 'Régénéré automatiquement, aucune donnée personnelle.' },
  moderate: { label: 'Modéré', cls: 'warning', icon: 'info', help: 'Régénérable, mais peut coûter du temps ou un re-téléchargement.' },
  review: { label: 'À examiner', cls: 'serious', icon: 'alert', help: 'Vérifiez les éléments avant de les supprimer.' },
};
export function riskBadge(risk) {
  const r = RISK[risk] || RISK.review;
  return el('span', { class: `badge ${r.cls}`, title: r.help }, icon(r.icon), r.label);
}

export function meter(ratio, { large = false } = {}) {
  const r = Math.max(0, Math.min(1, ratio || 0));
  const level = r >= 0.9 ? 'critical' : r >= 0.8 ? 'warning' : '';
  return el('div', { class: `meter ${large ? 'lg' : ''} ${level}`.trim(), role: 'meter', 'aria-valuenow': Math.round(r * 100),
    'aria-valuemin': 0, 'aria-valuemax': 100 }, el('div', { class: 'meter-fill', style: { width: `${r * 100}%` } }));
}

export function progressBar(ratio) {
  const bar = el('div', { class: `progress ${ratio === null || ratio === undefined ? 'indeterminate' : ''}` },
    el('span', { style: { width: `${Math.max(0, Math.min(1, ratio || 0)) * 100}%` } }));
  return bar;
}

export function emptyState({ iconName = 'info', title, text, action }) {
  return el('div', { class: 'empty' },
    el('div', { class: 'empty-icon' }, icon(iconName)),
    el('h3', { text: title }),
    text ? el('p', { text }) : null,
    action || null);
}

export function banner(kind, iconName, content, actions) {
  return el('div', { class: `banner ${kind}` }, icon(iconName),
    el('div', { class: 'grow' }, content), actions ? el('div', { class: 'banner-actions' }, actions) : null);
}

// ------------------------------------------------------------------ toasts
let toastHost;
export function toast(title, { text, kind = 'info', timeout = 5000 } = {}) {
  if (!toastHost) { toastHost = el('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' }); document.body.append(toastHost); }
  const iconName = kind === 'good' ? 'check' : kind === 'error' ? 'alert' : 'info';
  const t = el('div', { class: `toast ${kind}` }, icon(iconName),
    el('div', { class: 'grow' }, el('div', { class: 'toast-title', text: title }), text ? el('div', { class: 'toast-text', text }) : null));
  toastHost.append(t);
  const close = () => { t.classList.add('leaving'); setTimeout(() => t.remove(), 220); };
  if (timeout) setTimeout(close, timeout);
  t.addEventListener('click', close);
  return close;
}

// ------------------------------------------------------------------ modal / confirm
/**
 * openModal({title, iconName, tone: ''|'critical'|'warning', body: Node|[Nodes], actions: [{label, variant, onClick, iconName, autofocus}], wide})
 * onClick may return false to keep the modal open, or a Promise.
 * Returns {close, root}.
 */
export function openModal({ title, iconName = 'info', tone = '', body, actions = [], wide = false, onClose }) {
  const prevFocus = document.activeElement;
  const footer = el('div', { class: 'modal-footer' });
  const dialog = el('div', { class: `modal ${wide ? 'wide' : ''}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    el('div', { class: 'modal-header' }, el('div', { class: `modal-icon ${tone}` }, icon(iconName)),
      el('div', { class: 'grow' }, el('div', { class: 'modal-title', text: title }))),
    el('div', { class: 'modal-body' }, body),
    footer);
  const backdrop = el('div', { class: 'modal-backdrop' }, dialog);
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    backdrop.remove();
    document.removeEventListener('keydown', onKey);
    if (onClose) onClose();
    if (prevFocus && prevFocus.focus) prevFocus.focus();
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  backdrop.addEventListener('mousedown', (e) => { if (e.target === backdrop) close(); });
  let focusTarget = null;
  for (const a of actions) {
    const b = button(a.label, { variant: a.variant || '', iconName: a.iconName });
    b.addEventListener('click', async () => {
      if (!a.onClick) return close();
      const restore = a.busyText ? busy(b, a.busyText) : null;
      try {
        const r = await a.onClick();
        if (r !== false) close();
      } finally { if (restore && !closed) restore(); }
    });
    if (a.autofocus) focusTarget = b;
    footer.append(b);
  }
  document.body.append(backdrop);
  (focusTarget || footer.lastChild || dialog).focus?.();
  return { close, root: dialog };
}

/** Promise<boolean>. */
export function confirmDialog({ title, body, confirmLabel = 'Confirmer', tone = '', iconName = 'alert', danger = false }) {
  return new Promise((resolve) => {
    let answered = false;
    openModal({
      title, body, tone, iconName,
      onClose: () => { if (!answered) resolve(false); },
      actions: [
        { label: 'Annuler', variant: 'ghost', onClick: () => { answered = true; resolve(false); }, autofocus: true },
        { label: confirmLabel, variant: danger ? 'danger' : 'primary', onClick: () => { answered = true; resolve(true); } },
      ],
    });
  });
}

// ------------------------------------------------------------------ tooltip (shared by charts)
let tipEl;
/** showTooltip(x, y, [nodes]) — content is built with el(); hideTooltip(). */
export function showTooltip(clientX, clientY, content) {
  if (!tipEl) { tipEl = el('div', { class: 'chart-tooltip', role: 'tooltip' }); document.body.append(tipEl); }
  clear(tipEl);
  append(tipEl, [content]);
  tipEl.classList.add('visible');
  const pad = 14;
  const { innerWidth: W, innerHeight: H } = window;
  const r = tipEl.getBoundingClientRect();
  let x = clientX + pad;
  let y = clientY + pad;
  if (x + r.width > W - 8) x = clientX - r.width - pad;
  if (y + r.height > H - 8) y = clientY - r.height - pad;
  tipEl.style.left = `${Math.max(8, x)}px`;
  tipEl.style.top = `${Math.max(8, y)}px`;
}
export function hideTooltip() { if (tipEl) tipEl.classList.remove('visible'); }

// ------------------------------------------------------------------ misc
export function debounce(fn, ms = 200) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/** Sortable table helper state: returns comparator for a column key. */
export function sortBy(items, key, dir = 'desc') {
  const m = dir === 'asc' ? 1 : -1;
  return [...items].sort((a, b) => {
    const va = a[key]; const vb = b[key];
    if (va === vb) return 0;
    if (va === null || va === undefined) return 1;
    if (vb === null || vb === undefined) return -1;
    return (typeof va === 'string' ? va.localeCompare(vb, 'fr', { sensitivity: 'base' }) : va - vb) * m;
  });
}
