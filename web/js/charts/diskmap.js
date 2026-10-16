// Physical disk partition layout (Disk Management style). Labels are untrusted: el() text only.
import { el, icon, fmtBytes, fmtPercent, showTooltip, hideTooltip } from '../ui.js';

const MIN_WEIGHT = 0.04;
const GAP_RATIO = 0.01;
const ALIGNED_MIN_PX = 92;
const KIND_NAMES = {
  efi: 'Système EFI', msr: 'Réservée (MSR)', os: 'Système', data: 'Données', recovery: 'Récupération',
  other: 'Autre', gap: 'Non alloué',
};

/** Display weights ∝ size with a floor of `min`, renormalised to sum to 1. */
function displayWeights(sizes, min = MIN_WEIGHT) {
  const n = sizes.length;
  const total = sizes.reduce((a, b) => a + b, 0) || 1;
  if (n * min >= 1) return sizes.map(() => 1 / n);
  const w = sizes.map((s) => s / total);
  const fixed = new Array(n).fill(false);
  for (let guard = 0; guard < n + 1; guard++) {
    const nFixed = fixed.filter(Boolean).length;
    const freeSum = w.reduce((a, v, i) => a + (fixed[i] ? 0 : v), 0) || 1;
    const scale = (1 - nFixed * min) / freeSum;
    let changed = false;
    w.forEach((v, i) => { if (!fixed[i] && v * scale < min) { fixed[i] = true; changed = true; } });
    if (!changed) return w.map((v, i) => (fixed[i] ? min : v * scale));
  }
  return w;
}

/** Partitions sorted by offset, with "Non alloué" gaps (> 1% of the disk) inserted. */
function buildSegments(disk) {
  const parts = [...(disk.partitions || [])].sort((a, b) => a.offset - b.offset);
  const out = [];
  let cursor = 0;
  const gapIfAny = (until) => {
    if (until - cursor > disk.size * GAP_RATIO) out.push({ gap: true, size: until - cursor, offset: cursor });
  };
  for (const p of parts) {
    gapIfAny(p.offset);
    out.push({ gap: false, size: p.size, offset: p.offset, p });
    cursor = Math.max(cursor, p.offset + p.size);
  }
  gapIfAny(disk.size);
  return out;
}

function usedOf(p) {
  if (p.used != null) return Math.min(p.used, p.size);
  if (p.free != null) return Math.max(0, p.size - p.free);
  return null;
}

function letterOf(p) {
  const l = (p.letter || '').replace(/[:\\]+$/, '');
  return l ? `${l}:` : '';
}

function nameOf(p) {
  const l = letterOf(p);
  if (l && p.label) return `${p.label} (${l})`;
  return l || p.label || KIND_NAMES[p.kind] || KIND_NAMES.other;
}

const hueClass = (kind) => (kind === 'os' ? 'ch-disk-os' : kind === 'data' ? 'ch-disk-data' : 'ch-disk-neutral');

function healthBadge(health) {
  const ok = /healthy|ok|sain|bon/i.test(health || '');
  return el('span', { class: `badge ${ok ? 'good' : 'warning'}` }, icon(ok ? 'check' : 'alert'), health);
}

/**
 * Partition map of one physical disk.
 * @param {{model: string, size: number, media?: string, bus?: string, health?: string, partitions: object[]}} disk
 * @param {{onSelect?: (partition: object) => void}} opts
 * @returns {HTMLElement}
 */
export function diskMap(disk, { onSelect } = {}) {
  const segs = buildSegments(disk);
  const weights = displayWeights(segs.map((s) => s.size));

  const bar = el('div', { class: 'ch-disk-bar', role: 'group', 'aria-label': `Partitions de ${disk.model || 'disque'}` });
  const caps = el('div', { class: 'ch-disk-caps' });
  const list = el('div', { class: 'ch-disk-list' });
  const slots = [];
  let selected = null;

  const tooltip = (s) => {
    if (s.gap) {
      return el('div', {}, el('div', { class: 'tt-value', text: fmtBytes(s.size) }),
        el('div', { class: 'tt-label', text: KIND_NAMES.gap }));
    }
    const p = s.p;
    const u = usedOf(p);
    return el('div', {},
      el('div', { class: 'tt-value', text: fmtBytes(p.size) }),
      el('div', { class: 'tt-label', text: nameOf(p) }),
      el('div', { class: 'tt-meta', text: [KIND_NAMES[p.kind] || KIND_NAMES.other, p.fs, p.type].filter(Boolean).join(' · ') }),
      u != null ? el('div', { class: 'tt-meta', text: `Utilisé ${fmtBytes(u)} (${fmtPercent(p.size ? u / p.size : 0)}) · Libre ${fmtBytes(p.size - u)}` }) : null,
      el('div', { class: 'tt-meta', text: `Partition ${p.number ?? '—'} · décalage ${fmtBytes(p.offset)}` }));
  };

  segs.forEach((s, i) => {
    const interactive = !s.gap && onSelect;
    const kindCls = s.gap ? 'gap' : hueClass(s.p.kind);
    const u = s.gap ? null : usedOf(s.p);
    const seg = el(interactive ? 'button' : 'div', {
      class: `ch-disk-seg ${kindCls} ${!s.gap && u === null ? 'solid' : ''}`.trim(),
      type: interactive ? 'button' : null,
      style: { flex: `${weights[i]} 1 0` },
      'aria-label': s.gap ? `${KIND_NAMES.gap} ${fmtBytes(s.size)}` : `${nameOf(s.p)} ${fmtBytes(s.size)}`,
    });
    if (u !== null) seg.append(el('div', { class: 'ch-disk-used', style: { width: `${s.p.size ? (u / s.p.size) * 100 : 0}%` } }));
    const tip = (e) => showTooltip(e.clientX, e.clientY, tooltip(s));
    seg.addEventListener('mouseenter', tip);
    seg.addEventListener('mousemove', tip);
    seg.addEventListener('mouseleave', hideTooltip);
    if (interactive) {
      seg.addEventListener('click', () => {
        if (selected) selected.classList.remove('selected');
        selected = seg; seg.classList.add('selected');
        onSelect(s.p);
      });
    }
    bar.append(seg);

    // caption, rendered either under the segment or in the wrapped list
    const title = s.gap ? KIND_NAMES.gap : nameOf(s.p);
    const sub = s.gap ? fmtBytes(s.size)
      : [fmtBytes(s.p.size), s.p.fs].filter(Boolean).join(' · ');
    const slot = el('div', { class: 'ch-disk-cap', style: { flex: `${weights[i]} 1 0` } });
    caps.append(slot);
    slots.push({ slot, title, sub, s, listItem: null });
  });

  const mkAligned = (c) => [el('div', { class: 'ch-cap-name', text: c.title }), el('div', { class: 'ch-cap-sub', text: c.sub })];
  const mkListItem = (c) => {
    const hue = c.s.gap ? 'var(--grid)' : (c.s.p.kind === 'os' ? 'var(--accent)' : c.s.p.kind === 'data' ? 'var(--series-3)' : 'var(--ink-3)');
    return el('span', { class: 'ch-cap' }, el('span', { class: 'dot', style: { background: hue } }),
      el('span', { class: 'ch-cap-name', text: c.title }), el('span', { class: 'ch-cap-sub', text: c.sub }));
  };

  function layoutCaptions(width) {
    const avail = Math.max(0, width - 2 * (segs.length - 1));
    list.replaceChildren();
    slots.forEach((c, i) => {
      const room = width > 0 && weights[i] * avail >= ALIGNED_MIN_PX;
      c.slot.replaceChildren(...(room ? mkAligned(c) : []));
      if (!room) list.append(mkListItem(c));
    });
  }
  layoutCaptions(0);
  const ro = new ResizeObserver(() => layoutCaptions(bar.clientWidth));

  const head = el('div', { class: 'ch-disk-head' },
    icon('drive', { size: 18 }),
    el('span', { class: 'ch-disk-title', text: disk.model || 'Disque' }),
    el('span', { class: 'ch-disk-meta', text: [fmtBytes(disk.size), disk.media, disk.bus].filter(Boolean).join(' · ') }),
    disk.health ? healthBadge(disk.health) : null);

  const root = el('div', { class: 'ch-disk' }, head, bar, caps, list);
  ro.observe(bar);
  return root;
}
