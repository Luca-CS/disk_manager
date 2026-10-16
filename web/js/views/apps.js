// Applications — installed programs with their size. Disk Manager never uninstalls anything itself.
import { dryRunBanner, openTool, revealButton } from '../components.js';
import {
  banner, button, clear, debounce, el, emptyState, fmtBytes, fmtDate, icon, plural, sortBy, toast,
} from '../ui.js';

export const styles = 'apps';

// "2024-03-15" or "20240315" -> epoch seconds (local noon, avoids timezone day shifts).
function installTs(s) {
  if (!s) return null;
  const m = /^(\d{4})-?(\d{2})-?(\d{2})/.exec(String(s));
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12);
  return Number.isNaN(d.getTime()) ? null : d.getTime() / 1000;
}

export async function mount(root, ctx) {
  const { store, api } = ctx;
  ctx.setSubtitle('Disk Manager ne désinstalle jamais rien lui-même : il ouvre l’outil Windows prévu pour cela.');

  let apps = [];
  let loading = true;
  let error = null;
  let q = '';
  let sort = { key: 'size', dir: 'desc' };
  let disposed = false;
  ctx.onDispose(() => { disposed = true; });

  const noScanSlot = el('div');
  const statsEl = el('div', { class: 'grid cols-3 ap-stats' });
  const tableEl = el('div', { class: 'card flush' });
  const search = el('input', { class: 'input', type: 'search', placeholder: 'Rechercher une application', 'aria-label': 'Rechercher', spellcheck: 'false' });
  search.addEventListener('input', debounce(() => { q = search.value.trim().toLowerCase(); renderList(); }, 150));
  root.append(dryRunBanner(), noScanSlot, statsEl,
    el('div', { class: 'row ap-filters' }, el('div', { class: 'input-wrap grow ap-search' }, icon('search'), search)),
    tableEl);

  const stat = (label, value, detail) => el('div', { class: 'card ap-stat' },
    el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: label }),
      el('div', { class: 'stat-value num ellipsis', text: value }), detail ? el('div', { class: 'stat-detail ellipsis', text: detail }) : null));

  function renderNoScan() {
    clear(noScanSlot);
    if (store.get('scan')) return;
    noScanSlot.append(banner('accent', 'info',
      'Les tailles affichées sont des estimations fournies par Windows. Analysez le disque C: pour obtenir des tailles mesurées.',
      button('Analyser', { size: 'sm', onClick: () => ctx.navigate('overview') })));
  }

  function renderStats() {
    clear(statsEl);
    const known = apps.filter((a) => a.size);
    const total = known.reduce((s, a) => s + a.size, 0);
    const biggest = known.reduce((m, a) => (!m || a.size > m.size ? a : m), null);
    statsEl.append(
      stat('Applications installées', String(apps.length)),
      stat('Taille totale connue', fmtBytes(total), known.length < apps.length ? `${plural(apps.length - known.length, 'application sans taille', 'applications sans taille')}` : null),
      stat('Plus grosse application', biggest ? biggest.name : '—', biggest ? fmtBytes(biggest.size) : null));
  }

  function sortHeader(label, key, cls = '') {
    const active = sort.key === key;
    const th = el('th', { class: `sortable ${cls}`.trim(), 'aria-sort': active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none' },
      label, el('span', { class: 'sort-arrow', text: active ? (sort.dir === 'asc' ? '↑' : '↓') : '' }));
    th.addEventListener('click', () => {
      sort = { key, dir: active ? (sort.dir === 'asc' ? 'desc' : 'asc') : (key === 'size' || key === 'ts' ? 'desc' : 'asc') };
      renderList();
    });
    return th;
  }

  function sourceBadge(a) {
    if (a.size_source === 'scan') return el('span', { class: 'badge good', title: 'Taille mesurée lors de votre analyse' }, 'mesurée');
    if (a.size_source === 'registry') return el('span', { class: 'badge', title: 'Estimation fournie par Windows, parfois inexacte' }, 'estimée');
    return null;
  }

  function renderList() {
    clear(tableEl);
    if (loading) { tableEl.append(el('div', { class: 'row ap-loading' }, el('span', { class: 'spinner' }), 'Lecture des applications installées…')); return; }
    if (error) {
      tableEl.append(banner('critical', 'alert', `Impossible de lire la liste : ${error}`, button('Réessayer', { size: 'sm', onClick: () => load() })));
      return;
    }
    const filtered = apps.filter((a) => !q || a.name.toLowerCase().includes(q) || (a.publisher || '').toLowerCase().includes(q));
    if (!filtered.length) {
      tableEl.append(emptyState({ iconName: 'apps', title: apps.length ? 'Aucune application ne correspond' : 'Aucune application trouvée',
        text: apps.length ? 'Modifiez votre recherche.' : 'Windows n’a renvoyé aucune application installée.' }));
      return;
    }
    const rows = sortBy(filtered.map((a) => ({ ...a, ts: installTs(a.install_date) })), sort.key, sort.dir);
    const max = Math.max(...rows.map((a) => a.size || 0), 1);
    const tbody = el('tbody');
    for (const a of rows) {
      tbody.append(el('tr', {},
        el('td', {}, el('div', { class: 'ap-name' },
          el('div', { class: 'ellipsis strong', title: a.name, text: a.name }),
          a.publisher ? el('div', { class: 'ellipsis muted small', title: a.publisher, text: a.publisher }) : null,
          a.shared_with ? el('div', { class: 'ellipsis muted small', text: `Dossier partagé avec « ${a.shared_with} »` }) : null)),
        el('td', { class: 'num' }, a.size
          ? el('div', { class: 'ap-size' }, el('div', { class: 'row ap-size-top' }, sourceBadge(a), el('span', { class: 'strong', text: fmtBytes(a.size) })),
            el('div', { class: 'inline-bar' }, el('span', { style: { width: `${(100 * a.size) / max}%` } })))
          : el('span', { class: 'muted', title: 'Taille inconnue', text: '—' })),
        el('td', { class: 'num ap-version', text: a.version || '—' }),
        el('td', { class: 'num', text: a.ts ? fmtDate(a.ts) : '—' }),
        el('td', {}, el('span', { class: 'badge', text: a.scope === 'user' ? 'Utilisateur' : 'Système' })),
        el('td', { class: 'shrink' }, el('div', { class: 'row ap-actions' },
          a.location ? revealButton(a.location, { label: 'Dossier', select: false }) : null,
          button('Désinstaller…', { size: 'sm', title: 'Ouvre « Applications et fonctionnalités » de Windows', onClick: async () => {
            await openTool('appwiz');
            toast(`Recherchez « ${a.name} » dans la liste`, { text: 'Windows se charge de la désinstallation.' });
          } })))));
    }
    tableEl.append(el('div', { class: 'table-wrap' }, el('table', { class: 'table ap-table' },
      el('thead', {}, el('tr', {}, sortHeader('Nom', 'name'), sortHeader('Taille', 'size', 'num'), sortHeader('Version', 'version', 'num'),
        sortHeader('Installée le', 'ts', 'num'), sortHeader('Portée', 'scope'), el('th', { class: 'shrink' }))),
      tbody)));
  }

  async function load(refresh = false) {
    loading = true; error = null;
    renderStats(); renderList();
    try {
      const r = await api.get('/api/apps', refresh ? { refresh: 1 } : undefined);
      if (disposed) return;
      apps = r.apps || [];
    } catch (e) {
      if (disposed) return;
      error = e.message;
      toast('Lecture impossible', { text: e.message, kind: 'error' });
    }
    loading = false;
    renderStats(); renderList();
  }

  ctx.setActions(button('Actualiser', { variant: 'ghost', iconName: 'refresh', onClick: () => load(true) }));
  ctx.on('scan', () => { renderNoScan(); load(); });
  renderNoScan();
  load();
}
