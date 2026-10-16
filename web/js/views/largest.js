// Gros fichiers — the biggest files of the current scan, filterable, with safe bulk deletion.
import {
  categoryLabel, dryRunBanner, isProtectedHint, needScan, pretty, recycleItems, revealButton,
} from '../components.js';
import {
  banner, button, catColor, clear, debounce, el, emptyState, fmtBytes, fmtDate, fmtNumber, fmtRelative, icon,
  plural, sortBy, toast,
} from '../ui.js';

export const styles = 'largest';

const AGES = [
  { value: '0', label: 'Toutes les dates' },
  { value: '182', label: 'Non modifiés depuis 6 mois' },
  { value: '365', label: 'Non modifiés depuis 1 an' },
  { value: '730', label: 'Non modifiés depuis 2 ans' },
];
const LIMITS = ['200', '500', '1000'];

export async function mount(root, ctx) {
  const { store, api } = ctx;
  ctx.setSubtitle('Les fichiers qui occupent le plus de place, pour décider en connaissance de cause');

  const filters = {
    category: ctx.params.category || '',
    age: AGES.some((a) => a.value === String(ctx.params.min_age_days)) ? String(ctx.params.min_age_days) : '0',
    limit: LIMITS.includes(String(ctx.params.limit)) ? String(ctx.params.limit) : '200',
    q: '',
  };
  let files = [];
  let loading = false;
  let loadError = null;
  let sort = { key: 'size', dir: 'desc' };
  const selected = new Map(); // path -> file
  let frameBuilt = false;
  let disposed = false;
  let loadSeq = 0;
  ctx.onDispose(() => { disposed = true; });

  const host = el('div', { class: 'stack' });
  root.append(host);

  let statsEl; let tableEl; let bulkEl;

  // ---------------------------------------------------------------- helpers
  function syncUrl() {
    const p = new URLSearchParams();
    if (filters.category) p.set('category', filters.category);
    if (filters.age !== '0') p.set('min_age_days', filters.age);
    if (filters.limit !== '200') p.set('limit', filters.limit);
    const qs = p.toString();
    history.replaceState(null, '', `#/largest${qs ? `?${qs}` : ''}`);
  }

  function visible() {
    const q = filters.q.trim().toLowerCase();
    const list = q ? files.filter((f) => f.name.toLowerCase().includes(q) || f.path.toLowerCase().includes(q)) : files;
    return sortBy(list, sort.key, sort.dir);
  }

  const stat = (label, value, detail) => el('div', { class: 'card lg-stat' },
    el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: label }),
      el('div', { class: 'stat-value num', text: value }), detail ? el('div', { class: 'stat-detail', text: detail }) : null));

  function selectedSize() {
    let s = 0;
    for (const f of selected.values()) s += f.size;
    return s;
  }

  // ---------------------------------------------------------------- frame (filters stay mounted)
  function select(options, value, label, onChange) {
    const s = el('select', { class: 'select', 'aria-label': label },
      options.map((o) => el('option', { value: o.value, text: o.label })));
    s.value = value;
    s.addEventListener('change', () => onChange(s.value));
    return s;
  }

  function buildFrame() {
    clear(host);
    const cats = store.get('meta')?.categories || [];
    const catSel = select([{ value: '', label: 'Toutes les catégories' }, ...cats.map((c) => ({ value: c.key, label: c.label }))],
      filters.category, 'Type de fichier', (v) => { filters.category = v; syncUrl(); load(); });
    const ageSel = select(AGES, filters.age, 'Ancienneté', (v) => { filters.age = v; syncUrl(); load(); });
    const limSel = select(LIMITS.map((l) => ({ value: l, label: `${l} plus gros` })), filters.limit, 'Nombre de fichiers',
      (v) => { filters.limit = v; syncUrl(); load(); });
    const search = el('input', { class: 'input', type: 'search', placeholder: 'Filtrer par nom ou chemin', 'aria-label': 'Filtrer', spellcheck: 'false' });
    search.addEventListener('input', debounce(() => { filters.q = search.value; renderList(); }, 200));

    statsEl = el('div', { class: 'grid cols-3 lg-stats' });
    tableEl = el('div', { class: 'card flush' });
    bulkEl = el('div', { class: 'lg-bulk hidden' });
    host.append(
      dryRunBanner(),
      el('div', { class: 'row wrap lg-filters' }, catSel, ageSel, limSel,
        el('div', { class: 'input-wrap grow lg-search' }, icon('search'), search)),
      statsEl, tableEl, bulkEl);
    frameBuilt = true;
  }

  // ---------------------------------------------------------------- list
  function renderStats(list) {
    clear(statsEl);
    const total = list.reduce((s, f) => s + f.size, 0);
    const sel = selected.size;
    statsEl.append(
      stat('Fichiers affichés', fmtNumber(list.length), files.length !== list.length ? `sur ${fmtNumber(files.length)} chargés` : null),
      stat('Taille totale', fmtBytes(total)),
      stat('Sélection', sel ? plural(sel, 'fichier', 'fichiers') : 'Aucune', sel ? fmtBytes(selectedSize()) : 'Cochez des fichiers pour les mettre à la corbeille'));
  }

  function renderBulk() {
    clear(bulkEl);
    bulkEl.classList.toggle('hidden', selected.size === 0);
    if (!selected.size) return;
    const busyNow = !!store.running('recycle');
    bulkEl.append(
      el('div', { class: 'lg-bulk-text' }, el('span', { class: 'strong num', text: plural(selected.size, 'fichier sélectionné', 'fichiers sélectionnés') }),
        el('span', { class: 'muted num', text: ` · ${fmtBytes(selectedSize())}` })),
      el('span', { class: 'grow' }),
      button('Désélectionner', { variant: 'ghost', onClick: () => { selected.clear(); renderList(); } }),
      button(`Mettre à la corbeille (${selected.size} · ${fmtBytes(selectedSize())})`, {
        variant: 'primary', iconName: 'trash', disabled: busyNow, onClick: trashSelected }));
  }

  async function trashSelected() {
    const items = [...selected.values()].map((f) => ({ path: f.path, size: f.size, name: f.name }));
    const r = await recycleItems(items, { source: 'largest' });
    if (r && !disposed) {
      selected.clear();
      await load({ keepSelection: false });
    }
  }

  function sortHeader(label, key, cls = '') {
    const active = sort.key === key;
    const th = el('th', { class: `sortable ${cls}`.trim(), 'aria-sort': active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
      label, el('span', { class: 'sort-arrow', text: active ? (sort.dir === 'asc' ? '↑' : '↓') : '' }));
    th.addEventListener('click', () => {
      sort = { key, dir: active ? (sort.dir === 'asc' ? 'desc' : 'asc') : (key === 'name' ? 'asc' : 'desc') };
      renderList();
    });
    return th;
  }

  function renderList() {
    if (!frameBuilt) return;
    const list = visible();
    renderStats(list);
    renderBulk();
    clear(tableEl);
    if (loading && !files.length) {
      tableEl.append(el('div', { class: 'lg-loading row' }, el('span', { class: 'spinner' }), 'Chargement des plus gros fichiers…'));
      return;
    }
    if (loadError) {
      tableEl.append(banner('critical', 'alert', `Impossible de charger la liste : ${loadError}`,
        button('Réessayer', { size: 'sm', onClick: () => load() })));
      return;
    }
    if (!list.length) {
      tableEl.append(emptyState({
        iconName: 'bigfile',
        title: files.length ? 'Aucun fichier ne correspond à votre recherche' : 'Aucun gros fichier trouvé',
        text: files.length ? 'Modifiez ou effacez le filtre de recherche.' : 'Essayez avec d’autres filtres (seuls les fichiers de plus de 1 Mo sont listés).',
      }));
      return;
    }

    const max = Math.max(...list.map((f) => f.size), 1);
    const selectable = list.filter((f) => !isProtectedHint(f.path));
    const nSel = selectable.filter((f) => selected.has(f.path)).length;
    const all = el('input', { class: 'checkbox', type: 'checkbox', 'aria-label': 'Tout sélectionner (fichiers affichés)',
      disabled: !selectable.length, title: 'Sélectionner les fichiers affichés' });
    all.checked = selectable.length > 0 && nSel === selectable.length;
    all.indeterminate = nSel > 0 && nSel < selectable.length;
    all.addEventListener('change', () => {
      for (const f of selectable) { if (all.checked) selected.set(f.path, f); else selected.delete(f.path); }
      renderList();
    });

    const tbody = el('tbody');
    for (const f of list) {
      const prot = isProtectedHint(f.path);
      const cb = el('input', { class: 'checkbox', type: 'checkbox', disabled: prot, 'aria-label': `Sélectionner ${f.name}` });
      cb.checked = selected.has(f.path);
      const tr = el('tr', { class: selected.has(f.path) ? 'selected' : '',
        title: prot ? 'Fichier système : Disk Manager ne le supprimera pas' : undefined });
      cb.addEventListener('change', () => {
        if (cb.checked) selected.set(f.path, f); else selected.delete(f.path);
        tr.classList.toggle('selected', cb.checked);
        renderStats(visible());
        renderBulk();
        // keep the header checkbox in sync without rebuilding the table
        const n = selectable.filter((x) => selected.has(x.path)).length;
        all.checked = n === selectable.length;
        all.indeterminate = n > 0 && n < selectable.length;
      });
      tr.append(
        el('td', { class: 'shrink' }, cb),
        el('td', {}, el('div', { class: 'cell-name' },
          prot ? icon('lock') : el('span', { class: 'dot', style: { background: catColor(f.category) } }),
          el('div', { class: 'lg-name' },
            el('div', { class: 'ellipsis strong', title: f.name, text: f.name }),
            el('div', { class: 'ellipsis cell-path', title: f.path, text: pretty(f.path) })),
          prot ? el('span', { class: 'badge', title: 'Fichier système : Disk Manager ne le supprimera pas' }, icon('lock'), 'Protégé') : null)),
        el('td', { class: 'num' }, el('div', { class: 'lg-size' },
          el('span', { class: 'strong', text: fmtBytes(f.size) }),
          el('div', { class: 'inline-bar' }, el('span', { style: { width: `${(100 * f.size) / max}%` } })))),
        el('td', { class: 'lg-type', text: categoryLabel(f.category) }),
        el('td', {}, el('div', { class: 'num', text: fmtDate(f.mtime) }), el('div', { class: 'muted small', text: fmtRelative(f.mtime) })),
        el('td', { class: 'shrink' }, revealButton(f.path)));
      tbody.append(tr);
    }
    tableEl.append(el('div', { class: 'table-wrap lg-table-wrap' }, el('table', { class: 'table lg-table' },
      el('thead', {}, el('tr', {}, el('th', { class: 'shrink' }, all), sortHeader('Nom', 'name'), sortHeader('Taille', 'size', 'num'),
        el('th', { text: 'Type' }), sortHeader('Modifié', 'mtime'), el('th', { class: 'shrink' }))),
      tbody)));
  }

  // ---------------------------------------------------------------- data
  async function load({ keepSelection = true } = {}) {
    if (!frameBuilt) return;
    const seq = ++loadSeq;
    loading = true; loadError = null;
    renderList();
    try {
      const r = await api.get('/api/scan/largest', { limit: filters.limit, category: filters.category, min_age_days: filters.age });
      if (disposed || seq !== loadSeq) return;
      files = r.files || [];
      if (keepSelection) {
        const paths = new Set(files.map((f) => f.path));
        for (const p of [...selected.keys()]) if (!paths.has(p)) selected.delete(p);
      } else selected.clear();
    } catch (e) {
      if (disposed || seq !== loadSeq) return;
      files = [];
      loadError = e.message;
      toast('Chargement impossible', { text: e.message, kind: 'error' });
    }
    loading = false;
    renderList();
  }

  function refresh() {
    if (!store.get('scan')) {
      frameBuilt = false; files = []; selected.clear();
      clear(host).append(needScan(ctx, { what: 'les plus gros fichiers' }));
      return;
    }
    if (!frameBuilt) buildFrame();
    load();
  }

  ctx.setActions(button('Actualiser', { variant: 'ghost', iconName: 'refresh', onClick: () => frameBuilt && load() }));
  ctx.on('scan', refresh);
  ctx.on('task:recycle', () => frameBuilt && renderBulk());
  refresh();
}
