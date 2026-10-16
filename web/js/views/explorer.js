// Explorateur — treemap / sunburst / list of the scanned tree, with a details panel.
// Navigating between folders never remounts the view: state is updated in place and the URL
// is kept in sync with history.replaceState.
import { createSunburst } from '../charts/sunburst.js';
import { createTreemap } from '../charts/treemap.js';
import { legend } from '../charts/bars.js';
import {
  categoryLabel, dryRunBanner, isProtectedHint, needScan, pretty, recycleItems, reveal, revealButton,
  scanProgress, scanPrompt, startScan,
} from '../components.js';
import {
  banner, baseName, bytesParts, button, catColor, clear, dirName, el, emptyState, fmtBytes, fmtDate,
  fmtNumber, fmtPercent, fmtRelative, hideTooltip, icon, meter, openModal, plural, sortBy, toast,
} from '../ui.js';

export const styles = 'explorer';

const VIEW_KEY = 'dm-explorer-view';
const COLOR_KEY = 'dm-explorer-color';
const LIST_LIMIT = 300;

const readPref = (key, allowed, fallback) => {
  try { const v = localStorage.getItem(key); return allowed.includes(v) ? v : fallback; } catch { return fallback; }
};
const writePref = (key, value) => { try { localStorage.setItem(key, value); } catch { /* ignore */ } };

// ------------------------------------------------------------------ path helpers
const norm = (p) => (p || '').replace(/\\+$/, '').toLowerCase();
const samePath = (a, b) => norm(a) === norm(b);
const isInside = (path, root) => {
  const p = norm(path); const r = norm(root);
  return p === r || p.startsWith(`${r}\\`);
};

export async function mount(root, ctx) {
  const { store, api } = ctx;
  ctx.setSubtitle('Parcourez ce qui occupe la place, dossier par dossier');

  // ---------------------------------------------------------------- state
  let scan = null;
  let scanRoot = '';
  let current = '';
  let viewMode = readPref(VIEW_KEY, ['map', 'sun', 'list'], 'map');
  let colorMode = readPref(COLOR_KEY, ['type', 'branch'], 'branch');
  let kids = null;            // GET /api/scan/children for `current`
  let tree = null;            // GET /api/scan/tree for `current`
  let itemByPath = new Map();
  let nodeCache = new Map();  // lower path -> children.node for deep selections
  let sel = null;
  let checked = new Map();    // path -> size
  let sort = { key: 'size', dir: 'desc' };
  let listLimit = LIST_LIMIT;
  let seq = 0;
  let chart = null;
  let chartKind = null;
  let frameBuilt = false;
  let disposed = false;

  const host = el('div', { class: 'stack ex-host' });
  root.append(dryRunBanner(), scanProgress(ctx), host);

  // frame elements (created in buildFrame)
  let crumbs; let upBtn; let viewSwitch; let colorSwitch; let colorWrap;
  let grid; let chartCard; let chartHost; let chartEmpty; let legendHost; let detailsCard; let listCard; let frame; let errorHost;

  ctx.onDispose(() => {
    disposed = true;
    seq += 1;
    destroyChart();
    hideTooltip();
    document.removeEventListener('keydown', onKeyDown);
  });

  // ---------------------------------------------------------------- navigation
  function syncUrl() {
    history.replaceState(null, '', `#/explorer?path=${encodeURIComponent(current)}`);
  }

  function parentOf(path) {
    if (samePath(path, scanRoot)) return null;
    const up = dirName(path);
    return isInside(up, scanRoot) ? up : scanRoot;
  }

  function go(path) {
    if (!path || !scan) return;
    if (!isInside(path, scanRoot)) path = scanRoot;
    sel = null;
    checked = new Map();
    listLimit = LIST_LIMIT;
    current = path;
    syncUrl();
    renderCrumbs();
    load();
  }

  function goUp() {
    const p = parentOf(current);
    if (p) go(p);
  }

  function onKeyDown(e) {
    if (e.key !== 'Backspace') return;
    const t = e.target;
    if (t && (t.closest?.('input, textarea, select, [contenteditable="true"]'))) return;
    if (document.querySelector('.modal-backdrop')) return;
    e.preventDefault();
    goUp();
  }
  document.addEventListener('keydown', onKeyDown);

  // ---------------------------------------------------------------- data
  async function load() {
    if (!scan) return;
    const mine = ++seq;
    frame?.classList.add('loading-frame');
    clear(errorHost);
    let path = current;
    let t = null; let k = null;
    try {
      for (;;) {
        try {
          [t, k] = await Promise.all([
            viewMode === 'list' ? Promise.resolve(null) : api.get('/api/scan/tree', { path, depth: 6 }),
            api.get('/api/scan/children', { path }),
          ]);
          break;
        } catch (e) {
          if (mine !== seq) return;
          if (e.status === 404 && !samePath(path, scanRoot)) { path = parentOf(path) || scanRoot; continue; }
          throw e;
        }
      }
    } catch (e) {
      if (mine !== seq) return;
      frame?.classList.remove('loading-frame');
      errorHost.append(banner('critical', 'alert', `Impossible de lire ce dossier : ${e.message}`,
        button('Réessayer', { size: 'sm', iconName: 'refresh', onClick: () => load() })));
      toast('Lecture du dossier impossible', { text: e.message, kind: 'error' });
      return;
    }
    if (mine !== seq || disposed) return;
    if (!samePath(path, current)) { current = path; syncUrl(); renderCrumbs(); }
    tree = t;
    if (tree) tree.path = current;
    kids = k;
    itemByPath = new Map(kids.items.filter((i) => i.path).map((i) => [norm(i.path), i]));
    nodeCache = new Map();
    // keep a selection only if it still exists in this folder
    if (sel && sel.path && !itemByPath.has(norm(sel.path)) && !(tree && sel.kind === 'dir')) sel = null;
    frame.classList.remove('loading-frame');
    renderAll();
  }

  // ---------------------------------------------------------------- chart
  function destroyChart() {
    if (chart) { try { chart.destroy(); } catch { /* ignore */ } }
    chart = null; chartKind = null;
    hideTooltip();
  }

  function ensureChart() {
    if (viewMode === 'list') { destroyChart(); return; }
    if (chart && chartKind === viewMode) return;
    destroyChart();
    const common = { colorMode, onSelect: (p) => selectFromChart(p), onOpen: (p) => go(p) };
    chart = viewMode === 'sun'
      ? createSunburst(chartHost, { ...common, onUp: () => goUp() })
      : createTreemap(chartHost, common);
    chartKind = viewMode;
  }

  function renderChart() {
    ensureChart();
    if (!chart) return;
    const empty = !tree || !tree.ch || !tree.ch.some((c) => c.s > 0);
    chartHost.classList.toggle('hidden', empty);
    clear(chartEmpty);
    chartEmpty.classList.toggle('hidden', !empty);
    if (empty) {
      chartEmpty.append(emptyState({ iconName: 'folder', title: 'Ce dossier est vide',
        text: kids?.node?.denied ? 'Windows n’autorise pas la lecture de ce dossier.' : 'Il ne contient aucun fichier occupant de la place.' }));
    }
    chart.setData(tree);
    chart.setColorMode(colorMode);
    if (sel?.path && chartKind === 'map') chart.select(sel.path);
    renderLegend();
  }

  function renderLegend() {
    clear(legendHost);
    if (viewMode === 'list' || colorMode !== 'type') return;
    const cats = store.get('meta')?.categories || [];
    legendHost.append(legend(cats.map((c) => ({ key: c.key, label: c.label })), { colorOf: (it) => catColor(it.key) }));
  }
  const onTheme = () => { renderLegend(); renderList(); };
  document.addEventListener('themechange', onTheme);
  ctx.onDispose(() => document.removeEventListener('themechange', onTheme));

  // ---------------------------------------------------------------- selection
  function selFromItem(it) {
    if (it.type === 'small') return { kind: 'small', name: it.name, path: null, size: it.size, count: it.count };
    if (it.type === 'dir') {
      return { kind: 'dir', name: it.name, path: it.path, size: it.size, files: it.files, dirs: it.dirs,
        mtime: it.mtime, denied: it.denied, cloud: it.cloud };
    }
    return { kind: 'file', name: it.name, path: it.path, size: it.size, mtime: it.mtime, cat: it.category };
  }

  function selectFromChart(p) {
    const known = p.path ? itemByPath.get(norm(p.path)) : null;
    if (known) sel = selFromItem(known);
    else if (p.type === 'small' || p.type === 'other') {
      sel = { kind: p.type, name: p.name, path: null, size: p.size, count: p.files };
    } else {
      sel = { kind: p.type === 'dir' ? 'dir' : 'file', name: p.name || baseName(p.path), path: p.path, size: p.size,
        files: p.type === 'dir' ? p.files : undefined, cat: p.cat };
      if (p.type === 'dir') enrichDeep(p.path);
    }
    renderDetails();
    markRows();
  }

  async function enrichDeep(path) {
    const key = norm(path);
    let node = nodeCache.get(key);
    if (!node) {
      const mine = seq;
      try { node = (await api.get('/api/scan/children', { path })).node; } catch { return; }
      if (mine !== seq) return;
      nodeCache.set(key, node);
    }
    if (sel && sel.path && samePath(sel.path, path)) {
      sel = { ...sel, size: node.size, files: node.files, dirs: node.dirs, mtime: node.mtime, denied: node.denied, cloud: node.cloud };
      renderDetails();
    }
  }

  function selectFromList(it) {
    if (sel && it.path && sel.path && samePath(sel.path, it.path)) {
      sel = null;
      chart?.select?.(null);
    } else {
      sel = selFromItem(it);
      chart?.select?.(sel.path);
    }
    renderDetails();
    markRows();
  }

  // ---------------------------------------------------------------- toolbar
  function buildToolbar() {
    upBtn = button('', { variant: 'ghost', iconName: 'arrowUp', title: 'Dossier parent (Retour arrière)', onClick: goUp });
    crumbs = el('nav', { class: 'breadcrumbs ex-crumbs', 'aria-label': 'Chemin' });
    viewSwitch = el('div', { class: 'segmented', role: 'group', 'aria-label': 'Affichage' });
    for (const [id, label, ic] of [['map', 'Carte', 'layers'], ['sun', 'Soleil', 'sunburst'], ['list', 'Liste', 'list']]) {
      viewSwitch.append(el('button', { type: 'button', dataset: { id }, onClick: () => setViewMode(id) }, icon(ic), label));
    }
    colorSwitch = el('div', { class: 'segmented', role: 'group', 'aria-label': 'Couleurs' });
    for (const [id, label] of [['type', 'Par type'], ['branch', 'Par dossier']]) {
      colorSwitch.append(el('button', { type: 'button', dataset: { id }, onClick: () => setColorMode(id) }, label));
    }
    colorWrap = el('div', {}, colorSwitch);
    return el('div', { class: 'card ex-toolbar' }, upBtn, crumbs, el('span', { class: 'grow' }), colorWrap, viewSwitch);
  }

  function markSwitches() {
    for (const b of viewSwitch.children) b.classList.toggle('active', b.dataset.id === viewMode);
    for (const b of colorSwitch.children) b.classList.toggle('active', b.dataset.id === colorMode);
    colorWrap.classList.toggle('hidden', viewMode === 'list');
  }

  function setViewMode(id) {
    if (id === viewMode) return;
    const wasList = viewMode === 'list';
    viewMode = id;
    writePref(VIEW_KEY, id);
    markSwitches();
    layoutForMode();
    if (wasList) load(); else renderChart();
  }

  function setColorMode(id) {
    colorMode = id;
    writePref(COLOR_KEY, id);
    markSwitches();
    chart?.setColorMode(id);
    renderLegend();
  }

  function renderCrumbs() {
    if (!crumbs) return;
    clear(crumbs);
    upBtn.disabled = !parentOf(current);
    const rootTrim = scanRoot.replace(/\\+$/, '');
    const rest = current.slice(rootTrim.length).replace(/^\\+/, '');
    const parts = rest ? rest.split('\\') : [];
    let acc = scanRoot;
    crumbs.append(el('button', { type: 'button', title: scanRoot, text: scanRoot, onClick: () => go(scanRoot) }));
    parts.forEach((part) => {
      acc = acc.endsWith('\\') ? acc + part : `${acc}\\${part}`;
      const target = acc;
      crumbs.append(icon('chevronRight'), el('button', { type: 'button', title: target, text: part, onClick: () => go(target) }));
    });
  }

  // ---------------------------------------------------------------- layout
  function layoutForMode() {
    const list = viewMode === 'list';
    chartCard.classList.toggle('hidden', list);
    if (list) {
      // the list takes the left column, next to the details panel
      grid.prepend(listCard);
      listCard.classList.add('ex-list-main');
    } else {
      frame.append(listCard);
      listCard.classList.remove('ex-list-main');
      grid.prepend(chartCard);
    }
  }

  function buildFrame() {
    clear(host);
    destroyChart();
    chartHost = el('div', { class: 'ex-chart' });
    chartEmpty = el('div', { class: 'hidden' });
    legendHost = el('div', { class: 'ex-legend' });
    chartCard = el('div', { class: 'card ex-chart-card' }, chartHost, chartEmpty, legendHost);
    detailsCard = el('div', { class: 'card ex-details' });
    listCard = el('div', { class: 'card ex-list' });
    errorHost = el('div');
    grid = el('div', { class: 'grid sidebar-right ex-grid' }, chartCard, detailsCard);
    frame = el('div', { class: 'stack ex-frame' }, grid, listCard);
    host.append(buildToolbar(), errorHost, frame);
    markSwitches();
    layoutForMode();
    frameBuilt = true;
  }

  // ---------------------------------------------------------------- details panel
  function renderDetails() {
    if (!detailsCard || !kids) return;
    clear(detailsCard);
    const cur = kids.node;
    const isCurrent = !sel;
    const s = sel || { kind: 'dir', name: baseName(cur.path) || cur.path, path: cur.path, size: cur.size,
      files: cur.files, dirs: cur.dirs, mtime: cur.mtime, denied: cur.denied, cloud: cur.cloud };
    const aggregate = s.kind === 'small' || s.kind === 'other';
    const isDir = s.kind === 'dir';
    const isRoot = !!s.path && samePath(s.path, scanRoot);
    const protectedPath = !!s.path && isProtectedHint(s.path);

    const tile = el('div', { class: 'ex-tile' }, icon(isDir ? 'folder' : aggregate ? 'files' : 'file'));
    if (s.kind === 'file') tile.style.color = catColor(s.cat);
    const kindLabel = isCurrent ? 'Dossier courant' : isDir ? 'Dossier' : s.kind === 'file' ? categoryLabel(s.cat)
      : s.kind === 'small' ? 'Petits fichiers' : 'Éléments regroupés';
    const title = isRoot ? s.path : (s.name || baseName(s.path));

    detailsCard.append(
      el('div', { class: 'row top ex-d-head' }, tile,
        el('div', { class: 'grow' }, el('div', { class: 'ex-d-name', text: title }),
          el('div', { class: 'muted small', text: kindLabel }))),
      s.path ? el('div', { class: 'ex-path', text: pretty(s.path) }) : null);

    const sz = bytesParts(s.size);
    detailsCard.append(el('div', { class: 'ex-size' },
      el('span', { class: 'ex-size-value', text: sz.value }), el('span', { class: 'hero-unit', text: sz.unit })));

    const ratio = (n, d) => (d > 0 ? n / d : 0);
    const share = (r) => fmtPercent(r, r > 0 && r < 0.1 ? 1 : 0);
    const bars = el('div', { class: 'stack tight ex-shares' });
    if (!isCurrent) {
      const r = ratio(s.size, cur.size);
      bars.append(el('div', { class: 'ex-share' },
        el('div', { class: 'row between small' }, el('span', { class: 'secondary', text: 'Du dossier courant' }), el('span', { class: 'num strong', text: share(r) })),
        meter(r)));
    }
    if (!isRoot) {
      const r = ratio(s.size, scan.size);
      bars.append(el('div', { class: 'ex-share' },
        el('div', { class: 'row between small' }, el('span', { class: 'secondary', text: 'De l’analyse entière' }), el('span', { class: 'num strong', text: share(r) })),
        meter(r)));
    }
    detailsCard.append(bars);

    const facts = el('dl', { class: 'ex-facts' });
    const fact = (k, v) => { facts.append(el('dt', { text: k }), el('dd', { class: 'num', text: v })); };
    if (aggregate) fact('Éléments', fmtNumber(s.count));
    if (isDir) {
      fact('Fichiers', fmtNumber(s.files));
      if (s.dirs !== undefined) fact('Dossiers', fmtNumber(s.dirs));
    }
    if (s.mtime) fact('Modifié', `${fmtDate(s.mtime)} · ${fmtRelative(s.mtime)}`);
    if (facts.children.length) detailsCard.append(facts);

    const flags = el('div', { class: 'row wrap ex-flags' });
    if (s.denied) flags.append(el('span', { class: 'badge warning' }, icon('lock'), 'Accès refusé'));
    if (s.cloud) flags.append(el('span', { class: 'badge accent' }, icon('cloud'), 'Fichiers en ligne (OneDrive)'));
    if (protectedPath) flags.append(el('span', { class: 'badge serious' }, icon('lock'), 'Protégé'));
    if (flags.children.length) detailsCard.append(flags);

    if (aggregate) {
      detailsCard.append(el('p', { class: 'secondary small ex-note', text: s.kind === 'small'
        ? `Regroupe ${plural(s.count, 'petit fichier', 'petits fichiers')} de ce dossier, trop légers pour apparaître un par un. Ensemble, ils occupent ${fmtBytes(s.size)}.`
        : `Regroupe ${plural(s.count, 'élément', 'éléments')} trop petits pour être affichés individuellement sur la carte (${fmtBytes(s.size)} au total).` }));
    }
    if (protectedPath) {
      detailsCard.append(el('p', { class: 'secondary small ex-note', text: 'Cet emplacement est nécessaire à Windows ou à votre profil : il ne peut pas être supprimé depuis ici.' }));
    }

    // actions
    const actions = el('div', { class: 'stack tight ex-actions' });
    if (isDir && !isCurrent && s.path) actions.append(button('Ouvrir', { variant: 'primary', iconName: 'folder', onClick: () => go(s.path) }));
    if (s.path) {
      actions.append(button('Afficher dans l’Explorateur', { iconName: 'external',
        onClick: () => reveal(s.path, { select: !isRoot && !isCurrent }) }));
    }
    const blocked = isRoot ? 'La racine de l’analyse ne peut pas être supprimée.'
      : aggregate ? 'Ces éléments sont regroupés : ouvrez le dossier pour les voir un par un.'
        : protectedPath ? 'Emplacement protégé.' : '';
    const del = button('Mettre à la corbeille', { variant: 'danger', iconName: 'trash', title: blocked || undefined,
      disabled: !!blocked || !s.path, onClick: () => deleteItems([{ path: s.path, size: s.size }]) });
    actions.append(del);
    if (!isCurrent) {
      actions.append(button('Revenir au dossier courant', { variant: 'ghost', size: 'sm', iconName: 'chevronLeft',
        onClick: () => { sel = null; chart?.select?.(null); renderDetails(); markRows(); } }));
    }
    detailsCard.append(actions);
  }

  async function deleteItems(items) {
    const r = await recycleItems(items, { source: 'explorer' });
    if (r) {
      sel = null;
      checked = new Map();
      // the refreshed 'scan' event reloads the view; also reload now in case nothing changed (dry-run)
      load();
    }
  }

  // ---------------------------------------------------------------- list
  const rowMap = new Map(); // lower path -> <tr>

  function markRows() {
    for (const [key, tr] of rowMap) tr.classList.toggle('selected', !!sel?.path && key === norm(sel.path));
  }

  function listRows() {
    const total = kids.node.size;
    const rows = kids.items.map((it) => ({
      ...it,
      files: it.type === 'small' ? it.count : it.type === 'dir' ? it.files : null,
      mtime: it.mtime ?? null,
      pct: total > 0 ? it.size / total : 0,
    }));
    const pinned = rows.filter((r) => r.type === 'small');
    const sorted = sortBy(rows.filter((r) => r.type !== 'small'), sort.key, sort.dir);
    return [...sorted, ...pinned];
  }

  function renderList() {
    if (!listCard || !kids) return;
    clear(listCard);
    rowMap.clear();
    const rows = listRows();
    const selectable = rows.filter((r) => r.path && !isProtectedHint(r.path));
    const max = Math.max(1, ...rows.map((r) => r.size));

    const head = el('div', { class: 'card-header' },
      el('div', {}, el('div', { class: 'card-title', text: 'Contenu du dossier' }),
        el('div', { class: 'card-sub', text: rows.length
          ? `${plural(rows.length, 'élément', 'éléments')} · ${fmtBytes(kids.node.size)}` : 'Aucun élément' })));
    if (checked.size) {
      const total = [...checked.values()].reduce((a, b) => a + b, 0);
      head.append(el('div', { class: 'card-actions' },
        button('Tout désélectionner', { variant: 'ghost', size: 'sm', onClick: () => { checked = new Map(); renderList(); } }),
        button(`Mettre à la corbeille (${checked.size} · ${fmtBytes(total)})`, { variant: 'danger', size: 'sm', iconName: 'trash',
          onClick: () => deleteItems([...checked].map(([path, size]) => ({ path, size }))) })));
    }
    listCard.append(head);

    if (!rows.length) {
      listCard.append(emptyState({ iconName: 'folder', title: 'Ce dossier est vide', text: 'Il ne contient rien qui occupe de la place.' }));
      return;
    }

    const allBox = el('input', { type: 'checkbox', class: 'checkbox', 'aria-label': 'Tout sélectionner', disabled: !selectable.length,
      onChange: (e) => {
        if (e.target.checked) selectable.forEach((r) => checked.set(r.path, r.size)); else checked = new Map();
        renderList();
      } });
    allBox.checked = selectable.length > 0 && selectable.every((r) => checked.has(r.path));
    allBox.indeterminate = !allBox.checked && checked.size > 0;

    const th = (label, key, cls = '') => {
      const active = sort.key === key;
      return el('th', { class: `sortable ${cls}`.trim(), 'aria-sort': active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none',
        onClick: () => {
          sort = { key, dir: active && sort.dir === 'desc' ? 'asc' : (key === 'name' && !active ? 'asc' : 'desc') };
          renderList();
        } }, label, el('span', { class: 'sort-arrow', text: active ? (sort.dir === 'asc' ? '▲' : '▼') : '' }));
    };

    const tbody = el('tbody');
    for (const r of rows.slice(0, listLimit)) {
      const prot = r.path && isProtectedHint(r.path);
      const box = el('input', { type: 'checkbox', class: 'checkbox', 'aria-label': `Sélectionner ${r.name}`,
        disabled: !r.path || prot, title: prot ? 'Emplacement protégé' : undefined,
        onClick: (e) => e.stopPropagation(),
        onChange: (e) => {
          if (e.target.checked) checked.set(r.path, r.size); else checked.delete(r.path);
          renderList();
        } });
      box.checked = !!r.path && checked.has(r.path);

      const lead = r.type === 'dir' ? icon('folder') : r.type === 'small' ? icon('files')
        : el('span', { class: 'dot', style: { background: catColor(r.category) }, title: categoryLabel(r.category) });
      const tr = el('tr', { class: 'clickable', onClick: () => selectFromList(r),
        onDblclick: () => { if (r.type === 'dir') go(r.path); } },
      el('td', { class: 'shrink' }, box),
      el('td', {}, el('div', { class: 'cell-name' }, lead,
        el('span', { class: 'ellipsis ex-name', title: r.path || r.name, text: r.type === 'small' ? `${fmtNumber(r.count)} petits fichiers` : r.name }),
        prot ? el('span', { class: 'badge serious', title: 'Emplacement protégé : suppression impossible' }, icon('lock'), 'Protégé') : null,
        r.denied ? el('span', { class: 'badge warning' }, icon('lock'), 'Accès refusé') : null,
        r.cloud ? el('span', { class: 'badge accent', title: 'Fichiers en ligne (OneDrive)' }, icon('cloud')) : null)),
      el('td', { class: 'num' }, el('div', { class: 'ex-size-cell' },
        el('span', { class: 'ex-size-num', text: fmtBytes(r.size) }),
        el('div', { class: 'inline-bar' }, el('span', { style: { width: `${(r.size / max) * 100}%` } })))),
      el('td', { class: 'num muted', text: fmtPercent(r.pct, r.pct > 0 && r.pct < 0.1 ? 1 : 0) }),
      el('td', { class: 'num', text: r.files === null ? '—' : fmtNumber(r.files) }),
      el('td', { class: 'num', title: r.mtime ? fmtRelative(r.mtime) : undefined, text: fmtDate(r.mtime) }),
      el('td', { class: 'shrink' }, r.path ? revealButton(r.path) : null));
      if (r.path) rowMap.set(norm(r.path), tr);
      tbody.append(tr);
    }

    listCard.append(el('div', { class: 'table-wrap ex-table-wrap' }, el('table', { class: 'table' },
      el('thead', {}, el('tr', {},
        el('th', { class: 'shrink' }, allBox),
        th('Nom', 'name'), th('Taille', 'size', 'num'), el('th', { class: 'num', text: '%' }),
        th('Fichiers', 'files', 'num'), th('Modifié', 'mtime', 'num'), el('th', { class: 'shrink' }))),
      tbody)));
    if (rows.length > listLimit) {
      listCard.append(el('div', { class: 'ex-more' }, button(`Afficher plus (${fmtNumber(rows.length - listLimit)} restants)`, {
        variant: 'ghost', iconName: 'chevronDown', onClick: () => { listLimit += LIST_LIMIT; renderList(); } })));
    }
    markRows();
  }

  function renderAll() {
    renderCrumbs();
    renderChart();
    renderDetails();
    renderList();
    ctx.setSubtitle(`${fmtBytes(kids.node.size)} dans ce dossier · ${plural(kids.node.files, 'fichier', 'fichiers')}`);
  }

  // ---------------------------------------------------------------- scan lifecycle
  function applyScan() {
    const next = store.get('scan');
    if (disposed) return;
    if (!next) {
      scan = null; frameBuilt = false; seq += 1;
      destroyChart();
      clear(host).append(needScan(ctx, { what: 'le contenu de vos dossiers' }));
      ctx.setSubtitle('Parcourez ce qui occupe la place, dossier par dossier');
      return;
    }
    const rootChanged = !scan || !samePath(next.root, scanRoot) || !frameBuilt;
    scan = next;
    scanRoot = next.root;
    sel = null;
    checked = new Map();
    if (rootChanged) {
      const wanted = ctx.params.path && isInside(ctx.params.path, scanRoot) ? ctx.params.path : scanRoot;
      current = frameBuilt || !scan ? scanRoot : wanted;
      buildFrame();
      ctx.params = {}; // the initial deep link is consumed
      syncUrl();
    } else if (!isInside(current, scanRoot)) {
      current = scanRoot;
    }
    renderCrumbs();
    load();
  }

  ctx.setActions(
    button('Relancer l’analyse', { iconName: 'refresh', onClick: () => { const s = store.get('scan'); if (s) startScan(s.root); } }),
    button('Analyser un autre dossier', { variant: 'ghost', iconName: 'search', onClick: () => {
      let modal = null;
      modal = openModal({
        title: 'Analyser un autre dossier', iconName: 'search', wide: true,
        body: scanPrompt({ title: 'Que voulez-vous analyser ?', onStarted: () => modal?.close() }),
        actions: [{ label: 'Fermer', variant: 'ghost' }],
      });
    } }));

  ctx.on('scan', applyScan);
  applyScan();
}
