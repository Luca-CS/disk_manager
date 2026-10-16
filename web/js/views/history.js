// Historique — space freed, disk usage over time, what changed between two analyses, deletion journal.
import { usageChart } from '../charts/area.js';
import { pretty } from '../components.js';
import {
  banner, bytesParts, button, clear, el, emptyState, fmtBytes, fmtDateTime, fmtNumber, icon, plural,
} from '../ui.js';

export const styles = 'history';

const ACTIONS = {
  recycle: { label: 'Corbeille', cls: '' },
  purge: { label: 'Nettoyage', cls: 'accent' },
  empty_recycle_bin: { label: 'Corbeille vidée', cls: 'warning' },
};
const SOURCES = {
  explorer: 'Explorateur', largest: 'Plus gros fichiers', dupes: 'Doublons', types: 'Types de fichiers', old: 'Anciens fichiers',
};
const PAGE = 30;
const DAY = 86400;

export async function mount(root, ctx) {
  const { api, store } = ctx;
  ctx.setSubtitle('Ce que vous avez libéré et comment votre disque évolue');

  const tiles = el('div', { class: 'grid cols-3 hi-tiles' });
  const usageCard = el('div', { class: 'card' });
  const compareCard = el('div', { class: 'card' });
  const journalCard = el('div', { class: 'card' });
  root.append(dryRunNote(), tiles, usageCard, compareCard, journalCard);

  function dryRunNote() {
    return store.get('meta')?.dry_run
      ? banner('warning', 'info', 'Mode simulation activé : les opérations simulées sont signalées dans le journal et ne comptent pas dans l’espace libéré.')
      : null;
  }

  root.append(el('div', { class: 'muted small hi-loading row' }, el('span', { class: 'spinner' }), 'Chargement de l’historique…'));
  const loading = root.lastChild;

  let data;
  let ruleTitles = {};
  try {
    const [h, c] = await Promise.all([
      api.get('/api/history'),
      api.get('/api/clean').catch(() => null),
    ]);
    data = h;
    for (const r of c?.analysis?.rules || []) ruleTitles[r.id] = r.title;
  } catch (e) {
    loading.remove();
    root.append(el('div', { class: 'card' }, emptyState({ iconName: 'alert', title: 'Historique indisponible', text: e.message })));
    return;
  }
  loading.remove();

  const journal = data.journal || [];
  const snapshots = [...(data.snapshots || [])].sort((a, b) => b.ts - a.ts);
  const usage = data.usage || [];

  // ---------------------------------------------------------------- tiles
  function tile(label, valueNode, detail) {
    return el('div', { class: 'card stat hi-tile' },
      el('div', { class: 'stat-label', text: label }), valueNode,
      detail ? el('div', { class: 'stat-detail', text: detail }) : null);
  }
  const real = journal.filter((e) => !e.dry_run);
  const freed = bytesParts(real.reduce((s, e) => s + (e.bytes || 0), 0));
  const simulated = journal.length - real.length;
  tiles.append(
    tile('Espace libéré au total',
      el('div', {}, el('span', { class: 'hero-value hi-big', text: freed.value }), el('span', { class: 'hero-unit', text: freed.unit })),
      simulated ? `${plural(simulated, 'simulation non comptée', 'simulations non comptées')}` : 'Depuis la première utilisation'),
    tile('Opérations', el('div', { class: 'stat-value', text: fmtNumber(journal.length) }),
      journal.length ? `Dernière : ${fmtDateTime(journal[0].ts)}` : 'Aucune pour le moment'),
    tile('Dernière analyse', el('div', { class: 'stat-value', text: snapshots.length ? fmtDateTime(snapshots[0].ts) : '—' }),
      snapshots.length ? `${snapshots[0].root} · ${fmtBytes(snapshots[0].total)}` : 'Aucune analyse enregistrée'));

  // ---------------------------------------------------------------- usage chart
  const sysRoot = (store.get('meta')?.paths?.system_drive || 'C:\\').toLowerCase();
  let points = usage.filter((p) => (p.root || '').toLowerCase() === sysRoot);
  let chartRoot = store.get('meta')?.paths?.system_drive || 'C:\\';
  if (!points.length && usage.length) { chartRoot = usage[usage.length - 1].root; points = usage.filter((p) => p.root === chartRoot); }
  const driveName = chartRoot.replace(/\\$/, '');
  usageCard.append(el('div', { class: 'card-header' },
    el('div', {}, el('div', { class: 'card-title', text: `Espace utilisé sur ${driveName}` }),
      el('div', { class: 'card-sub', text: 'Relevé à chaque analyse et chaque nettoyage' }))));
  if (points.length < 2) {
    usageCard.append(emptyState({
      iconName: 'bars', title: 'Pas encore assez de relevés',
      text: 'La courbe apparaîtra après quelques utilisations de Disk Manager.' }));
  } else {
    const holder = el('div', { class: 'hi-chart' });
    usageCard.append(holder);
    const total = Math.max(...points.map((p) => p.total || 0));
    const chart = usageChart(holder, points, { total });
    ctx.onDispose(() => chart?.destroy?.());
  }

  // ---------------------------------------------------------------- compare
  compareCard.append(el('div', { class: 'card-header' },
    el('div', {}, el('div', { class: 'card-title', text: 'Ce qui a changé' }),
      el('div', { class: 'card-sub', text: 'Comparez deux analyses du même disque ou dossier' }))));
  const compareBody = el('div', { class: 'stack' });
  compareCard.append(compareBody);

  const roots = [...new Set(snapshots.map((s) => s.root))];
  const cmp = { root: snapshots[0]?.root, a: null, b: null, limit: PAGE, seq: 0 };

  const snapLabel = (s) => `${fmtDateTime(s.ts)} · ${s.root} · ${fmtBytes(s.total)}`;
  function initPair() {
    const list = snapshots.filter((s) => s.root === cmp.root);
    cmp.b = list[0]?.id ?? null;
    cmp.a = list[1]?.id ?? null;
  }

  function select(label, value, list, onChange) {
    const s = el('select', { class: 'input', 'aria-label': label });
    for (const sn of list) {
      const o = el('option', { value: sn.id, text: snapLabel(sn) });
      if (sn.id === value) o.selected = true;
      s.append(o);
    }
    s.addEventListener('change', () => onChange(s.value));
    return el('label', { class: 'stack tight hi-select' }, el('span', { class: 'muted small', text: label }), s);
  }

  async function renderCompare() {
    clear(compareBody);
    const list = snapshots.filter((s) => s.root === cmp.root);
    if (list.length < 2) {
      compareBody.append(emptyState({
        iconName: 'history', title: 'Une seule analyse enregistrée',
        text: 'Relancez une analyse plus tard pour voir ce qui a changé.' }));
      if (roots.length > 1) compareBody.append(rootPicker());
      return;
    }
    const idOf = (v) => list.find((s) => String(s.id) === String(v))?.id;
    compareBody.append(el('div', { class: 'hi-selects' },
      roots.length > 1 ? rootPicker() : null,
      select('Avant (plus ancienne)', cmp.a, list, (v) => { cmp.a = idOf(v); cmp.limit = PAGE; loadCompare(); }),
      select('Après (plus récente)', cmp.b, list, (v) => { cmp.b = idOf(v); cmp.limit = PAGE; loadCompare(); })));
    const out = el('div', { class: 'stack' });
    compareBody.append(out);
    loadCompare.out = out;
    await loadCompare();
  }

  function rootPicker() {
    const s = el('select', { class: 'input', 'aria-label': 'Disque ou dossier' });
    for (const r of roots) { const o = el('option', { value: r, text: r }); if (r === cmp.root) o.selected = true; s.append(o); }
    s.addEventListener('change', () => { cmp.root = s.value; cmp.limit = PAGE; initPair(); renderCompare(); });
    return el('label', { class: 'stack tight hi-select' }, el('span', { class: 'muted small', text: 'Disque ou dossier' }), s);
  }

  async function loadCompare() {
    const out = loadCompare.out;
    if (!out) return;
    clear(out);
    const snapA = snapshots.find((s) => s.id === cmp.a);
    const snapB = snapshots.find((s) => s.id === cmp.b);
    if (cmp.a === cmp.b || !snapA || !snapB) {
      out.append(banner('accent', 'info', 'Choisissez deux analyses différentes à comparer.'));
      return;
    }
    const mySeq = ++cmp.seq;
    out.append(el('div', { class: 'row muted' }, el('span', { class: 'spinner' }), 'Comparaison en cours…'));
    let res;
    try {
      res = await api.get('/api/history/compare', { a: cmp.a, b: cmp.b });
    } catch (e) {
      if (mySeq !== cmp.seq) return;
      clear(out).append(banner('critical', 'alert', `Comparaison impossible : ${e.message}`));
      return;
    }
    if (mySeq !== cmp.seq) return;
    clear(out);
    const span = Math.abs(snapB.ts - snapA.ts);
    const days = span / DAY;
    const dur = days >= 1.5 ? plural(Math.round(days), 'jour', 'jours') : days >= 1 ? '1 jour'
      : span >= 3600 ? plural(Math.round(span / 3600), 'heure', 'heures') : 'moins d’une heure';
    out.append(deltaHeader(res.delta, dur));

    const changes = [...(res.changes || [])].sort((x, y) => Math.abs(y.delta) - Math.abs(x.delta));
    if (!changes.length) {
      out.append(el('p', { class: 'muted', text: 'Aucun dossier n’a changé de façon notable entre ces deux analyses.' }));
      return;
    }
    const tbody = el('tbody');
    const more = el('div', { class: 'hi-more' });
    const fill = () => {
      clear(tbody);
      for (const c of changes.slice(0, cmp.limit)) tbody.append(changeRow(c));
      clear(more);
      if (changes.length > cmp.limit) {
        more.append(button(`Afficher plus (${fmtNumber(changes.length - cmp.limit)} restants)`, { onClick: () => { cmp.limit += PAGE; fill(); } }));
      }
    };
    fill();
    out.append(
      el('div', { class: 'table-wrap' }, el('table', { class: 'table hi-table' },
        el('thead', {}, el('tr', {}, el('th', { text: 'Dossier' }), el('th', { class: 'num', text: 'Avant' }),
          el('th', { class: 'num', text: 'Après' }), el('th', { class: 'num', text: 'Évolution' }))),
        tbody)),
      more);
  }

  function deltaHeader(delta, dur) {
    const grow = delta > 0;
    const none = delta === 0;
    const tone = none ? '' : grow ? 'serious' : 'good';
    const text = none ? `Aucun changement en ${dur}`
      : `${grow ? '+' : '−'}${fmtBytes(Math.abs(delta))} en ${dur}`;
    return el('div', { class: `hi-delta ${tone}` },
      icon(none ? 'dot' : grow ? 'arrowUp' : 'arrowDown'),
      el('div', {},
        el('div', { class: 'hi-delta-value num', text }),
        el('div', { class: 'small', text: none ? 'La taille analysée est restée identique.'
          : grow ? 'L’espace occupé a augmenté.' : 'De l’espace a été libéré.' })));
  }

  function changeRow(c) {
    const d = c.delta || 0;
    const tone = d > 0 ? 'serious' : d < 0 ? 'good' : '';
    const sign = d > 0 ? '+' : d < 0 ? '−' : '';
    return el('tr', {},
      el('td', {}, el('div', { class: 'ellipsis hi-path', title: c.path, text: pretty(c.path) }),
        c.before === null || c.before === undefined ? el('span', { class: 'badge accent' }, 'Nouveau')
          : c.after === null || c.after === undefined ? el('span', { class: 'badge' }, 'Supprimé') : null),
      el('td', { class: 'num muted', text: c.before === null || c.before === undefined ? '—' : fmtBytes(c.before) }),
      el('td', { class: 'num', text: c.after === null || c.after === undefined ? '—' : fmtBytes(c.after) }),
      el('td', { class: 'num' }, el('span', { class: `hi-evo ${tone}` },
        d === 0 ? null : icon(d > 0 ? 'arrowUp' : 'arrowDown'), `${sign}${fmtBytes(Math.abs(d))}`)));
  }

  if (snapshots.length) { initPair(); renderCompare(); } else {
    compareBody.append(emptyState({
      iconName: 'history', title: 'Aucune analyse enregistrée',
      text: 'Relancez une analyse plus tard pour voir ce qui a changé.' }));
  }

  // ---------------------------------------------------------------- journal
  journalCard.append(el('div', { class: 'card-header' },
    el('div', {}, el('div', { class: 'card-title', text: 'Journal des suppressions' }),
      el('div', { class: 'card-sub', text: 'Tout ce que Disk Manager a supprimé ou envoyé à la corbeille' }))));
  if (!journal.length) {
    journalCard.append(emptyState({
      iconName: 'trash', title: 'Aucune suppression pour le moment',
      text: 'Quand vous nettoierez votre disque, chaque opération sera listée ici.' }));
  } else {
    let jLimit = 50;
    const jBody = el('tbody');
    const jMore = el('div', { class: 'hi-more' });
    const fillJ = () => {
      clear(jBody);
      for (const e of journal.slice(0, jLimit)) jBody.append(journalRow(e));
      clear(jMore);
      if (journal.length > jLimit) {
        jMore.append(button(`Afficher plus (${fmtNumber(journal.length - jLimit)} restants)`, { onClick: () => { jLimit += 50; fillJ(); } }));
      }
    };
    fillJ();
    journalCard.append(
      el('div', { class: 'table-wrap' }, el('table', { class: 'table hi-table' },
        el('thead', {}, el('tr', {}, el('th', { class: 'shrink', text: 'Date' }), el('th', { class: 'shrink', text: 'Action' }),
          el('th', { text: 'Détails' }), el('th', { class: 'num', text: 'Espace' }), el('th', { class: 'num', text: 'Erreurs' }))),
        jBody)),
      jMore);
  }
  journalCard.append(el('p', { class: 'muted small hi-note' }, icon('info'),
    ' Ce journal est enregistré uniquement sur votre ordinateur, dans data/journal.jsonl.'));

  function journalRow(e) {
    const act = ACTIONS[e.action] || { label: e.action || 'Action', cls: '' };
    const count = (v) => (Array.isArray(v) ? v.length : (typeof v === 'number' ? v : 0));
    const paths = (Array.isArray(e.targets) ? e.targets : Array.isArray(e.items) ? e.items : [])
      .map((t) => (typeof t === 'string' ? t : t?.path || t?.label || '')).filter(Boolean);
    const n = e.files ?? (count(e.items) || count(e.targets));
    const ruleId = e.rule || (typeof e.source === 'string' && e.source.startsWith('clean:') ? e.source.slice(6) : null);
    const title = ruleId ? (ruleTitles[ruleId] || ruleId) : e.action === 'empty_recycle_bin' ? 'Corbeille Windows' : null;
    const sourceText = SOURCES[e.source] || (ruleId ? 'Nettoyage intelligent' : e.source);
    const errs = count(e.errors) + count(e.refused);
    return el('tr', {},
      el('td', { class: 'shrink num', text: fmtDateTime(e.ts) }),
      el('td', { class: 'shrink' }, el('div', { class: 'row wrap hi-badges' },
        el('span', { class: `badge ${act.cls}`, text: act.label }),
        e.dry_run ? el('span', { class: 'badge', title: 'Rien n’a été réellement supprimé.' }, icon('info'), 'Simulation') : null)),
      el('td', {},
        title ? el('div', { class: 'strong', text: title }) : null,
        el('div', { class: 'muted small', text: [sourceText || null, n ? plural(n, 'fichier', 'fichiers') : null].filter(Boolean).join(' · ') || '—' }),
        paths.slice(0, 2).map((p) => el('div', { class: 'muted small mono ellipsis hi-path', title: p, text: pretty(p) })),
        paths.length > 2 ? el('div', { class: 'muted small', text: `… et ${fmtNumber(paths.length - 2)} autres` }) : null),
      el('td', { class: 'num strong', text: fmtBytes(e.bytes || 0) }),
      el('td', { class: 'num' }, errs ? el('span', { class: 'badge warning', text: fmtNumber(errs) }) : el('span', { class: 'muted', text: '0' })));
  }
}
