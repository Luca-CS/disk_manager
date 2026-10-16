// Doublons — files with identical content, with a "keep one" selection strategy and safe deletion.
import {
  dryRunBanner, isProtectedHint, needScan, pretty, recycleItems, revealButton,
} from '../components.js';
import {
  banner, button, clear, el, emptyState, fmtBytes, fmtDate, fmtDuration, fmtNumber, icon, plural, progressBar, toast,
} from '../ui.js';

export const styles = 'duplicates';

const MIB = 1024 * 1024;
const SIZES = [{ v: MIB, label: '1 Mo' }, { v: 10 * MIB, label: '10 Mo' }, { v: 100 * MIB, label: '100 Mo' }];
const STRATEGIES = [
  { key: 'oldest', label: 'Le plus ancien' },
  { key: 'newest', label: 'Le plus récent' },
  { key: 'shortest', label: 'Le chemin le plus court' },
];
const PHASES = {
  group: 'Regroupement par taille',
  partial: 'Comparaison rapide',
  full: 'Vérification complète du contenu',
};
const PAGE = 50;

export async function mount(root, ctx) {
  const { store, api } = ctx;
  ctx.setSubtitle('Des fichiers au contenu identique, comparés par empreinte. La recherche est en lecture seule.');

  let result = null;
  let loaded = false;
  let showIntro = false;
  let loadError = null;
  let taskError = null;
  let strategy = 'oldest';
  let shown = PAGE;
  let minSize = 10 * MIB;
  let includeSystem = false;
  const checked = new Set();
  const sizeOf = new Map(); // path -> size
  let disposed = false;
  ctx.onDispose(() => { disposed = true; });

  const host = el('div', { class: 'stack' });
  root.append(host);

  // ---------------------------------------------------------------- selection logic
  function keeperOf(g) {
    const files = g.files;
    const prot = files.find((f) => isProtectedHint(f.path));
    if (prot) return prot;
    const byAge = [...files].sort((a, b) => (a.mtime || 0) - (b.mtime || 0));
    if (strategy === 'newest') return byAge[byAge.length - 1];
    if (strategy === 'shortest') return [...byAge].sort((a, b) => a.path.length - b.path.length)[0];
    return byAge[0];
  }

  function recompute() {
    checked.clear(); sizeOf.clear();
    for (const g of result?.groups || []) {
      const keep = keeperOf(g);
      for (const f of g.files) {
        sizeOf.set(f.path, g.size);
        if (f !== keep && !isProtectedHint(f.path)) checked.add(f.path);
      }
    }
  }

  const checkedBytes = () => { let s = 0; for (const p of checked) s += sizeOf.get(p) || 0; return s; };
  const uncheckedCount = (g) => g.files.filter((f) => !checked.has(f.path)).length;

  // ---------------------------------------------------------------- data
  async function loadResult() {
    try {
      const r = await api.get('/api/dupes');
      if (disposed) return;
      result = r.result;
      loadError = null;
      shown = PAGE;
      recompute();
    } catch (e) {
      if (disposed) return;
      loadError = e.message;
    }
    loaded = true;
    render();
  }

  async function start() {
    if (store.running('dupes') || store.running('scan')) return;
    taskError = null;
    showIntro = false;
    try {
      const t = await store.track('dupes', api.post('/api/dupes', { min_size: minSize, include_system: includeSystem }));
      if (t.status === 'cancelled') toast('Recherche annulée');
    } catch (e) {
      taskError = e.message;
      toast('La recherche a échoué', { text: e.message, kind: 'error' });
      if (!disposed) render();
    }
  }

  async function trashSelection() {
    const items = [...checked].map((p) => ({ path: p, size: sizeOf.get(p) || 0 }));
    const r = await recycleItems(items, { source: 'dupes' });
    if (r && !disposed) await loadResult();
  }

  // ---------------------------------------------------------------- views
  const stat = (label, value, detail) => el('div', { class: 'card dp-stat' },
    el('div', { class: 'stat' }, el('div', { class: 'stat-label', text: label }),
      el('div', { class: 'stat-value num', text: value }), detail ? el('div', { class: 'stat-detail', text: detail }) : null));

  function intro() {
    const sizeSel = el('select', { class: 'select', 'aria-label': 'Taille minimale' },
      SIZES.map((s) => el('option', { value: s.v, text: s.label })));
    sizeSel.value = String(minSize);
    sizeSel.addEventListener('change', () => { minSize = Number(sizeSel.value); });
    const sys = el('input', { class: 'checkbox', type: 'checkbox', id: 'dp-sys' });
    sys.checked = includeSystem;
    sys.addEventListener('change', () => { includeSystem = sys.checked; });
    return el('div', { class: 'card dp-intro' },
      el('div', { class: 'row top dp-intro-head' },
        el('div', { class: 'dp-intro-icon' }, icon('copy')),
        el('div', { class: 'stack tight' },
          el('div', { class: 'card-title', text: 'Retrouvez les fichiers en double' }),
          el('p', { class: 'secondary', text: 'Disk Manager cherche les fichiers dont le contenu est strictement identique, même s’ils ont un nom ou un emplacement différent. La comparaison se fait par empreinte du contenu, en lecture seule : rien n’est modifié ni supprimé pendant la recherche.' }),
          el('p', { class: 'secondary', text: 'Vous choisirez ensuite quelle copie conserver. Il en reste toujours au moins une par groupe.' }))),
      el('div', { class: 'dp-options' },
        el('label', { class: 'row' }, el('span', { class: 'secondary', text: 'Taille minimale' }), sizeSel),
        el('label', { class: 'row', for: 'dp-sys' }, sys, el('span', { text: 'Inclure les dossiers d’applications (déconseillé)' }))),
      el('div', { class: 'card-footer' },
        button('Rechercher les doublons', { variant: 'primary', iconName: 'search', onClick: start }),
        result ? button('Retour aux résultats', { variant: 'ghost', onClick: () => { showIntro = false; render(); } }) : null));
  }

  function progressCard(t) {
    const p = t.progress || {};
    let ratio = null;
    let detail = '';
    if (p.total_bytes) {
      ratio = p.done_bytes / p.total_bytes;
      detail = `${fmtBytes(p.done_bytes)} sur ${fmtBytes(p.total_bytes)}`;
    } else if (p.total_files) {
      ratio = p.done_files / p.total_files;
    }
    if (p.total_files) detail += `${detail ? ' · ' : ''}${fmtNumber(p.done_files)} / ${fmtNumber(p.total_files)} fichiers`;
    if (p.groups) detail += `${detail ? ' · ' : ''}${plural(p.groups, 'groupe', 'groupes')} pour l’instant`;
    return el('div', { class: 'card task-card' },
      el('div', { class: 'task-line' }, el('span', { class: 'spinner' }),
        el('div', { class: 'grow' }, el('div', { class: 'strong', text: PHASES[p.phase] || 'Préparation…' }),
          el('div', { class: 'muted small num', text: detail || 'Lecture en cours…' })),
        button('Annuler', { size: 'sm', variant: 'ghost', iconName: 'stop', onClick: () => api.cancelTask(t.id).catch((e) => toast('Annulation impossible', { text: e.message, kind: 'error' })) })),
      progressBar(ratio));
  }

  function groupCard(g) {
    const unchecked = uncheckedCount(g);
    const card = el('div', { class: 'card flush dp-group' });
    card.append(el('div', { class: 'dp-group-head' },
      icon('files'),
      el('div', { class: 'grow dp-group-title' },
        el('div', { class: 'strong ellipsis', title: g.files[0]?.name, text: g.files[0]?.name || 'Fichier' }),
        el('div', { class: 'muted small num', text: `${fmtBytes(g.size)} × ${g.count}` })),
      el('span', { class: 'badge warning num', title: 'Espace occupé par les copies en trop' }, `${fmtBytes(g.wasted)} récupérables`)));
    for (const f of g.files) {
      const prot = isProtectedHint(f.path);
      const isOn = checked.has(f.path);
      const lastKept = !isOn && unchecked <= 1;
      const cb = el('input', { class: 'checkbox', type: 'checkbox', disabled: prot || lastKept,
        'aria-label': `Mettre à la corbeille ${f.name}`,
        title: prot ? 'Fichier système : Disk Manager ne le supprimera pas' : lastKept ? 'Au moins une copie doit être conservée' : undefined });
      cb.checked = isOn;
      cb.addEventListener('change', () => {
        if (cb.checked) {
          if (uncheckedCount(g) <= 1) { cb.checked = false; toast('Au moins une copie doit être conservée'); return; }
          checked.add(f.path);
        } else checked.delete(f.path);
        card.replaceWith(groupCard(g));
        renderSummary();
      });
      card.append(el('div', { class: `dp-file ${isOn ? 'selected' : ''}` },
        cb,
        el('div', { class: 'grow dp-file-path' },
          el('div', { class: 'ellipsis', title: f.path, text: pretty(f.path) })),
        !isOn ? (prot ? el('span', { class: 'badge', title: 'Fichier système : Disk Manager ne le supprimera pas' }, icon('lock'), 'Protégé')
          : el('span', { class: 'badge good' }, icon('check'), 'Conservé')) : null,
        el('span', { class: 'muted small num dp-file-date', text: fmtDate(f.mtime) }),
        revealButton(f.path)));
    }
    return card;
  }

  let summaryEl; let bulkEl; let listEl;
  function renderSummary() {
    if (!bulkEl) return;
    clear(bulkEl);
    bulkEl.classList.toggle('hidden', checked.size === 0);
    if (!checked.size) return;
    const running = !!store.running('recycle');
    bulkEl.append(
      el('div', {}, el('span', { class: 'strong num', text: plural(checked.size, 'fichier sélectionné', 'fichiers sélectionnés') }),
        el('span', { class: 'muted num', text: ` · ${fmtBytes(checkedBytes())} à libérer` })),
      el('span', { class: 'grow' }),
      button(`Mettre à la corbeille la sélection (${checked.size} · ${fmtBytes(checkedBytes())})`, {
        variant: 'primary', iconName: 'trash', disabled: running, onClick: trashSelection }));
  }

  function renderList() {
    clear(listEl);
    const groups = [...result.groups].sort((a, b) => b.wasted - a.wasted);
    for (const g of groups.slice(0, shown)) listEl.append(groupCard(g));
    if (groups.length > shown) {
      listEl.append(el('div', { class: 'row dp-more' },
        button(`Afficher plus (${fmtNumber(groups.length - shown)} restants)`, { onClick: () => { shown += PAGE; renderList(); } })));
    }
  }

  function resultView() {
    const r = result;
    const wrap = el('div', { class: 'stack' });
    if (r.cancelled) wrap.append(banner('warning', 'info', 'La recherche a été interrompue : ces résultats sont partiels.'));
    wrap.append(el('div', { class: 'grid cols-3 dp-stats' },
      stat('Groupes de doublons', fmtNumber(r.groups.length)),
      stat('Espace récupérable', fmtBytes(r.total_wasted), 'en ne gardant qu’une copie par groupe'),
      stat('Fichiers comparés', fmtNumber(r.files_hashed), r.elapsed ? `en ${fmtDuration(r.elapsed)}` : null)));
    if (!r.groups.length) {
      wrap.append(el('div', { class: 'card' }, emptyState({
        iconName: 'check', title: 'Aucun doublon trouvé',
        text: `Aucun fichier de plus de ${fmtBytes(r.min_size)} n’existe en double dans l’analyse.`,
        action: button('Essayer avec une taille plus petite', { onClick: () => { showIntro = true; render(); } }) })));
      return wrap;
    }
    const seg = el('div', { class: 'segmented', role: 'group', 'aria-label': 'Copie à conserver' },
      STRATEGIES.map((s) => el('button', { type: 'button', class: strategy === s.key ? 'active' : '', text: s.label,
        onClick: () => { strategy = s.key; recompute(); render(); } })));
    summaryEl = el('div', { class: 'card dp-strategy row wrap' },
      el('div', { class: 'grow' }, el('div', { class: 'strong', text: 'Quelle copie garder dans chaque groupe ?' }),
        el('div', { class: 'muted small', text: 'Les autres sont cochées pour la corbeille. Vous pouvez ajuster : une copie reste toujours conservée.' })),
      el('span', { class: 'secondary', text: 'Garder :' }), seg);
    listEl = el('div', { class: 'stack' });
    bulkEl = el('div', { class: 'dp-bulk hidden' });
    wrap.append(summaryEl, listEl, bulkEl);
    renderList();
    renderSummary();
    return wrap;
  }

  // ---------------------------------------------------------------- render
  function render() {
    if (disposed) return;
    clear(host);
    bulkEl = null;
    if (!store.get('scan')) {
      ctx.setActions();
      host.append(needScan(ctx, { what: 'les doublons' }));
      return;
    }
    host.append(dryRunBanner());
    const running = store.running('dupes');
    ctx.setActions(!running && result && !showIntro
      ? button('Nouvelle recherche', { iconName: 'search', onClick: () => { showIntro = true; render(); } }) : null);
    if (running) { host.append(progressCard(running)); return; }
    if (!loaded) { host.append(el('div', { class: 'row muted dp-loading' }, el('span', { class: 'spinner' }), 'Chargement…')); return; }
    if (taskError) host.append(banner('critical', 'alert', `La recherche a échoué : ${taskError}`));
    if (loadError) host.append(banner('critical', 'alert', `Impossible de charger les résultats : ${loadError}`,
      button('Réessayer', { size: 'sm', onClick: loadResult })));
    if (!result || showIntro) { host.append(intro()); return; }
    host.append(resultView());
  }

  ctx.on('task:dupes', (t) => {
    if (t.status === 'running') { render(); return; }
    if (t.status === 'error') { taskError = t.error || 'Erreur inconnue'; render(); return; }
    loadResult();
  });
  ctx.on('task:recycle', () => renderSummary());
  ctx.on('scan', () => {
    if (!store.get('scan')) { result = null; checked.clear(); render(); } else if (!store.running('dupes') && loaded) loadResult();
    else render();
  });

  render();
  if (store.get('scan')) loadResult(); else loaded = true;
}
