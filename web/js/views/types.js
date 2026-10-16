// Types de fichiers — breakdown by category, extensions, biggest files of a category, file age.
import { barList, legend, stackedBar } from '../charts/bars.js';
import {
  categoryLabel, dryRunBanner, isProtectedHint, needScan, pretty, recycleItems, revealButton, scanProgress,
} from '../components.js';
import {
  banner, baseName, button, catColor, clear, debounce, el, emptyState, fmtBytes, fmtDate, fmtNumber, fmtRelative,
  hideTooltip, icon, plural, sortBy, toast,
} from '../ui.js';

export const styles = 'types';

const PAGE = 100;

export async function mount(root, ctx) {
  const { store, api } = ctx;
  ctx.setSubtitle('Ce qui remplit votre disque, par nature de fichier');

  // ---------------------------------------------------------------- state
  let types = null;
  let age = null;
  let cat = ctx.params.cat || null;
  let query = '';
  let sort = { key: 'size', dir: 'desc' };
  let extLimit = PAGE;
  let largest = null;       // {files} for the selected category, null while loading
  let checked = new Map();  // path -> size
  let seq = 0;              // stale-response guard (global data)
  let largestSeq = 0;
  let disposed = false;

  const host = el('div', { class: 'stack' });
  root.append(dryRunBanner(), scanProgress(ctx), host);

  const compoCard = el('div', { class: 'card' });
  const extCard = el('div', { class: 'card' });
  const bigCard = el('div', { class: 'card' });
  const ageCard = el('div', { class: 'card' });
  const extTable = el('div');

  ctx.onDispose(() => {
    disposed = true;
    seq += 1; largestSeq += 1;
    hideTooltip();
    document.removeEventListener('themechange', onTheme);
  });
  const onTheme = () => { if (types) { renderCompo(); renderAge(); renderExt(); renderBig(); } };
  document.addEventListener('themechange', onTheme);

  // ---------------------------------------------------------------- helpers
  const activeCats = () => (types ? types.categories.filter((c) => c.size > 0) : []);
  const catKnown = (k) => !!types && types.categories.some((c) => c.key === k);

  function syncUrl() {
    history.replaceState(null, '', cat ? `#/types?cat=${encodeURIComponent(cat)}` : '#/types');
  }

  function toggleCat(key) {
    cat = cat === key ? null : key;
    syncUrl();
    extLimit = PAGE;
    checked = new Map();
    renderCompo();
    renderExt();
    loadLargest();
  }

  // ---------------------------------------------------------------- data
  async function loadAll() {
    const scan = store.get('scan');
    if (!scan) {
      types = null; age = null; seq += 1;
      clear(host).append(needScan(ctx, { what: 'la répartition par type de fichier' }));
      return;
    }
    const mine = ++seq;
    if (!types) clear(host).append(el('div', { class: 'card' }, el('div', { class: 'row muted' }, el('span', { class: 'spinner' }), 'Lecture de l’analyse…')));
    else host.classList.add('loading-frame');
    let t; let a;
    try {
      [t, a] = await Promise.all([api.get('/api/scan/types'), api.get('/api/scan/age')]);
    } catch (e) {
      if (mine !== seq || disposed) return;
      host.classList.remove('loading-frame');
      clear(host).append(banner('critical', 'alert', `Impossible de lire les types de fichiers : ${e.message}`,
        button('Réessayer', { size: 'sm', iconName: 'refresh', onClick: loadAll })));
      toast('Lecture impossible', { text: e.message, kind: 'error' });
      return;
    }
    if (mine !== seq || disposed) return;
    types = t; age = a;
    host.classList.remove('loading-frame');
    if (cat && !catKnown(cat)) { cat = null; syncUrl(); }
    checked = new Map();
    clear(host).append(compoCard, extCard, bigCard, ageCard);
    renderCompo(); renderExt(); renderAge();
    loadLargest();
  }

  async function loadLargest() {
    const mine = ++largestSeq;
    largest = null;
    if (!cat) { renderBig(); return; }
    renderBig();
    try {
      const r = await api.get('/api/scan/largest', { category: cat, limit: 50 });
      if (mine !== largestSeq || disposed) return;
      largest = r.files || [];
    } catch (e) {
      if (mine !== largestSeq || disposed) return;
      largest = [];
      toast('Lecture des gros fichiers impossible', { text: e.message, kind: 'error' });
    }
    renderBig();
  }

  // ---------------------------------------------------------------- composition
  function renderCompo() {
    clear(compoCard);
    const cats = activeCats();
    compoCard.append(el('div', { class: 'card-header' },
      el('div', {}, el('div', { class: 'card-title', text: 'Répartition par type' }),
        el('div', { class: 'card-sub', text: `${fmtBytes(types.total)} analysés · cliquez sur une catégorie pour filtrer` })),
      cat ? el('div', { class: 'card-actions' }, button('Tout afficher', { variant: 'ghost', size: 'sm', iconName: 'x', onClick: () => toggleCat(cat) })) : null));
    if (!cats.length) { compoCard.append(emptyState({ iconName: 'pie', title: 'Rien à afficher', text: 'L’analyse ne contient aucun fichier.' })); return; }
    const colorOf = (it) => catColor(it.key);
    compoCard.append(el('div', { class: 'stack' },
      stackedBar(cats.map((c) => ({ key: c.key, label: c.label, value: c.size })), { total: types.total, colorOf, onClick: (s) => toggleCat(s.key) }),
      legend(cats.map((c) => ({ key: c.key, label: c.label })), { colorOf })));
    const list = barList(cats.map((c) => ({ key: c.key, label: c.label, value: c.size, sublabel: plural(c.count, 'fichier', 'fichiers') })),
      { total: types.total, colorOf, onClick: (it) => toggleCat(it.key) });
    [...list.children].forEach((row, i) => {
      const on = cats[i].key === cat;
      row.classList.toggle('ty-sel', on);
      row.setAttribute('aria-pressed', String(on));
    });
    list.classList.toggle('ty-filtering', !!cat);
    compoCard.append(el('div', { class: 'ty-bars' }, list));
  }

  // ---------------------------------------------------------------- extensions
  function renderExt() {
    clear(extCard);
    const input = el('input', { class: 'input', type: 'search', placeholder: 'Chercher une extension', value: query, 'aria-label': 'Chercher une extension',
      onInput: debounce((e) => { query = e.target.value; extLimit = PAGE; renderExtTable(); }, 200) });
    extCard.append(el('div', { class: 'card-header' },
      el('div', {}, el('div', { class: 'card-title', text: cat ? `Extensions — ${categoryLabel(cat)}` : 'Extensions' }),
        el('div', { class: 'card-sub', text: 'Les 300 extensions les plus volumineuses de l’analyse' })),
      el('div', { class: 'card-actions' }, el('div', { class: 'input-wrap ty-search' }, icon('search'), input))));
    extCard.append(extTable);
    renderExtTable();

    function renderExtTable() {
      // re-derive with the current query (the input only changes `query`)
      const qq = query.trim().toLowerCase().replace(/^\./, '');
      let list = types.extensions.map((e) => ({ ...e, avg: e.count ? e.size / e.count : 0, catLabel: categoryLabel(e.category) }));
      if (cat) list = list.filter((e) => e.category === cat);
      if (qq) list = list.filter((e) => e.ext.toLowerCase().replace(/^\./, '').includes(qq));
      list = sortBy(list, sort.key === 'category' ? 'catLabel' : sort.key, sort.dir);
      clear(extTable);
      if (!list.length) {
        extTable.append(emptyState({ iconName: 'search', title: 'Aucune extension trouvée', text: query ? 'Essayez une autre recherche.' : 'Cette catégorie ne contient pas d’extension.' }));
        return;
      }
      const max = Math.max(1, ...list.map((e) => e.size));
      const th = (label, key, cls = '') => {
        const active = sort.key === key;
        return el('th', { class: `sortable ${cls}`.trim(), 'aria-sort': active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none',
          onClick: () => {
            sort = { key, dir: active && sort.dir === 'desc' ? 'asc' : (key === 'ext' || key === 'category') && !active ? 'asc' : 'desc' };
            renderExtTable();
          } }, label, el('span', { class: 'sort-arrow', text: active ? (sort.dir === 'asc' ? '▲' : '▼') : '' }));
      };
      const tbody = el('tbody');
      for (const e of list.slice(0, extLimit)) {
        tbody.append(el('tr', {},
          el('td', {}, e.ext ? el('span', { class: 'mono', text: e.ext.startsWith('.') ? e.ext : `.${e.ext}` }) : el('span', { class: 'muted', text: '(sans extension)' })),
          el('td', {}, el('div', { class: 'cell-name' }, el('span', { class: 'dot', style: { background: catColor(e.category) } }), el('span', { text: e.catLabel }))),
          el('td', { class: 'num' }, el('div', { class: 'ty-size-cell' },
            el('span', { class: 'ty-size-num', text: fmtBytes(e.size) }),
            el('div', { class: 'inline-bar' }, el('span', { style: { width: `${(e.size / max) * 100}%`, background: catColor(e.category) } })))),
          el('td', { class: 'num', text: fmtNumber(e.count) }),
          el('td', { class: 'num', text: fmtBytes(e.avg) })));
      }
      extTable.append(el('div', { class: 'table-wrap ty-table-wrap' }, el('table', { class: 'table' },
        el('thead', {}, el('tr', {}, th('Extension', 'ext'), th('Catégorie', 'category'), th('Taille', 'size', 'num'),
          th('Fichiers', 'count', 'num'), th('Taille moyenne', 'avg', 'num'))),
        tbody)));
      if (list.length > extLimit) {
        extTable.append(el('div', { class: 'ty-more' }, button(`Afficher plus (${fmtNumber(list.length - extLimit)} restantes)`, {
          variant: 'ghost', iconName: 'chevronDown', onClick: () => { extLimit += PAGE; renderExtTable(); } })));
      }
    }
  }

  // ---------------------------------------------------------------- biggest files of the category
  function renderBig() {
    clear(bigCard);
    bigCard.classList.toggle('hidden', !cat);
    if (!cat) return;
    const label = categoryLabel(cat);
    const files = largest;
    const head = el('div', { class: 'card-header' },
      el('div', {}, el('div', { class: 'card-title', text: `Plus gros fichiers — ${label}` }),
        el('div', { class: 'card-sub', text: 'Rien n’est coché par défaut. Les fichiers vont dans la corbeille Windows.' })));
    const actions = el('div', { class: 'card-actions' });
    if (checked.size) {
      const total = [...checked.values()].reduce((a, b) => a + b, 0);
      actions.append(
        button('Tout désélectionner', { variant: 'ghost', size: 'sm', onClick: () => { checked = new Map(); renderBig(); } }),
        button(`Mettre à la corbeille (${checked.size} · ${fmtBytes(total)})`, { variant: 'danger', size: 'sm', iconName: 'trash', onClick: deleteChecked }));
    }
    actions.append(button('Voir tous les gros fichiers', { variant: 'ghost', size: 'sm', iconName: 'bigfile', onClick: () => ctx.navigate('largest', { category: cat }) }));
    head.append(actions);
    bigCard.append(head);

    if (files === null) { bigCard.append(el('div', { class: 'row muted' }, el('span', { class: 'spinner' }), 'Recherche des plus gros fichiers…')); return; }
    if (!files.length) {
      bigCard.append(emptyState({ iconName: 'file', title: 'Aucun gros fichier', text: 'Cette catégorie ne contient pas de fichier de plus de 1 Mo.' }));
      return;
    }
    const selectable = files.filter((f) => !isProtectedHint(f.path));
    const allBox = el('input', { type: 'checkbox', class: 'checkbox', 'aria-label': 'Tout sélectionner', disabled: !selectable.length,
      onChange: (e) => {
        if (e.target.checked) selectable.forEach((f) => checked.set(f.path, f.size)); else checked = new Map();
        renderBig();
      } });
    allBox.checked = selectable.length > 0 && selectable.every((f) => checked.has(f.path));
    allBox.indeterminate = !allBox.checked && checked.size > 0;

    const tbody = el('tbody');
    for (const f of files) {
      const prot = isProtectedHint(f.path);
      const box = el('input', { type: 'checkbox', class: 'checkbox', 'aria-label': `Sélectionner ${f.name}`, disabled: prot,
        title: prot ? 'Emplacement protégé' : undefined,
        onChange: (e) => { if (e.target.checked) checked.set(f.path, f.size); else checked.delete(f.path); renderBig(); } });
      box.checked = checked.has(f.path);
      tbody.append(el('tr', { class: checked.has(f.path) ? 'selected' : '' },
        el('td', { class: 'shrink' }, box),
        el('td', {}, el('div', { class: 'ty-file' },
          el('div', { class: 'row tight-row' }, el('span', { class: 'ellipsis strong ty-fname', title: f.name, text: f.name || baseName(f.path) }),
            prot ? el('span', { class: 'badge serious', title: 'Emplacement protégé : suppression impossible' }, icon('lock'), 'Protégé') : null),
          el('div', { class: 'ellipsis cell-path', title: f.path, text: pretty(f.path) }))),
        el('td', { class: 'num strong', text: fmtBytes(f.size) }),
        el('td', { class: 'num', title: fmtRelative(f.mtime), text: fmtDate(f.mtime) }),
        el('td', { class: 'shrink' }, revealButton(f.path))));
    }
    bigCard.append(el('div', { class: 'table-wrap ty-table-wrap' }, el('table', { class: 'table' },
      el('thead', {}, el('tr', {}, el('th', { class: 'shrink' }, allBox), el('th', { text: 'Nom' }),
        el('th', { class: 'num', text: 'Taille' }), el('th', { class: 'num', text: 'Modifié' }), el('th', { class: 'shrink' }))),
      tbody)));
  }

  async function deleteChecked() {
    const items = [...checked].map(([path, size]) => ({ path, size }));
    const r = await recycleItems(items, { source: 'types' });
    if (r) { checked = new Map(); loadAll(); }
  }

  // ---------------------------------------------------------------- age
  function renderAge() {
    clear(ageCard);
    ageCard.append(el('div', { class: 'card-header' },
      el('div', {}, el('div', { class: 'card-title', text: 'Ancienneté des fichiers' }),
        el('div', { class: 'card-sub', text: 'Selon la date de dernière modification' }))));
    const buckets = age?.buckets || [];
    const total = buckets.reduce((a, b) => a + b.size, 0);
    if (!buckets.length || !total) { ageCard.append(emptyState({ iconName: 'clock', title: 'Rien à afficher' })); return; }
    const n = buckets.length;
    const ramp = (i) => `color-mix(in srgb, var(--accent) ${Math.round(35 + (65 * i) / Math.max(1, n - 1))}%, var(--surface))`;
    const items = buckets.map((b, i) => ({ key: String(i), label: b.label, value: b.size, sublabel: plural(b.count, 'fichier', 'fichiers') }));
    ageCard.append(barList(items, { total, colorOf: (it) => ramp(+it.key) }));
    ageCard.append(el('p', { class: 'secondary small ty-caption', text: 'Les fichiers que vous n’avez pas ouverts ni modifiés depuis longtemps (barres foncées) sont de bons candidats à l’archivage : sauvegarde sur un disque externe ou dans le cloud, puis suppression ici.' }));
    ageCard.append(el('div', { class: 'card-footer' },
      button('Voir les gros fichiers non modifiés depuis 1 an', { iconName: 'bigfile', onClick: () => ctx.navigate('largest', { min_age_days: 365 }) })));
  }

  // ---------------------------------------------------------------- wiring
  ctx.on('scan', loadAll);
  loadAll();
}
