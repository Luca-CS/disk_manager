// Nettoyage intelligent — find what can be freed, pick it, confirm, clean.
// Only rule ids and item ids ever leave this view (never paths).
import { adviceGrid, dryRunBanner, pretty, revealButton } from '../components.js';
import {
  banner, bytesParts, button, clear, confirmDialog, el, emptyState, fmtBytes, fmtNumber, fmtRelative, icon,
  openModal, plural, progressBar, riskBadge, toast,
} from '../ui.js';

export const styles = 'clean';

const CATEGORIES = [
  ['system', 'Système'],
  ['browsers', 'Navigateurs'],
  ['apps', 'Applications'],
  ['dev', 'Outils de développement'],
  ['downloads', 'Téléchargements'],
  ['leftovers', 'Restes d’applications'],
];

const MODE = {
  purge: { text: 'Suppression définitive · régénéré automatiquement', short: 'suppression définitive', icon: 'zap' },
  recycle: { text: 'Vers la corbeille (restaurable)', short: 'envoyé à la corbeille', icon: 'undo' },
  recycle_bin: { text: 'Vide la corbeille (irréversible)', short: 'vidage de la corbeille', icon: 'trash' },
};

const ITEM_PAGE = 100;

export async function mount(root, ctx) {
  const { store, api } = ctx;
  ctx.setSubtitle('Libérez de l’espace en toute sécurité');

  let analysis = null;
  let loadError = null;
  let selection = new Map();       // rule id -> Set(item id)
  const expanded = new Set();
  const ruleViews = new Map();     // rule id -> {update}
  const handledRuns = new Set();
  let analyzing = false;
  let disposed = false;
  ctx.onDispose(() => { disposed = true; });

  const adminHolder = el('div');
  const mainHolder = el('div', { class: 'stack' });
  const barHolder = el('div', { class: 'cl-bar-holder' });
  const adviceWrap = el('div', { class: 'stack' });
  root.append(dryRunBanner(), adminHolder, mainHolder, barHolder, adviceWrap);

  // ---------------------------------------------------------------- helpers
  const selectable = (item) => !item.blocked;
  const ruleSelectable = (rule) => rule.items.filter(selectable);
  const selSet = (rule) => selection.get(rule.id) || new Set();
  const ruleSelectedSize = (rule) => {
    const s = selSet(rule);
    return rule.items.reduce((sum, i) => sum + (s.has(i.id) ? i.size : 0), 0);
  };
  const totals = () => {
    let count = 0; let size = 0;
    if (analysis) {
      for (const r of analysis.rules) {
        const s = selSet(r);
        for (const i of r.items) if (s.has(i.id)) { count += 1; size += i.size; }
      }
    }
    return { count, size };
  };

  function defaultSelection() {
    selection = new Map();
    for (const r of analysis.rules) {
      const ids = r.items.filter((i) => i.default_selected && selectable(i)).map((i) => i.id);
      if (ids.length) selection.set(r.id, new Set(ids));
    }
  }

  function setRule(rule, on) {
    if (on) selection.set(rule.id, new Set(ruleSelectable(rule).map((i) => i.id)));
    else selection.delete(rule.id);
  }

  function refreshAll() {
    for (const v of ruleViews.values()) v.update();
    renderBar();
  }

  // ---------------------------------------------------------------- loading analysis
  async function loadAnalysis() {
    loadError = null;
    try {
      analysis = (await api.get('/api/clean')).analysis;
      if (analysis) defaultSelection();
    } catch (e) {
      analysis = null;
      loadError = e.message;
    }
    if (!disposed) render();
  }

  async function startAnalysis() {
    if (store.running('clean_analyze')) { analyzing = false; return; }
    analyzing = true;
    try {
      await store.track('clean_analyze', api.post('/api/clean/analyze'));
    } catch (e) {
      toast('La recherche a échoué', { text: e.message, kind: 'error' });
    }
    analyzing = false;
    await loadAnalysis();
  }

  // ---------------------------------------------------------------- admin banner
  function renderAdmin() {
    clear(adminHolder);
    if (!analysis || analysis.is_admin || !analysis.rules.some((r) => r.blocked)) return;
    adminHolder.append(banner('accent', 'shieldUp',
      el('div', {},
        el('div', { class: 'strong', text: 'Certains éléments système demandent les droits administrateur' }),
        el('div', { class: 'secondary small', text: 'Ils sont visibles ci-dessous mais ne peuvent pas être sélectionnés. Relancez l’application en administrateur pour les nettoyer aussi.' })),
      button('Relancer en administrateur', { iconName: 'shieldUp', onClick: async () => {
        const ok = await confirmDialog({
          title: 'Relancer en administrateur ?', iconName: 'shieldUp',
          body: el('p', { text: 'Disk Manager va se fermer puis se rouvrir avec les droits administrateur. Windows vous demandera votre accord. Vous devrez relancer la recherche.' }),
          confirmLabel: 'Relancer',
        });
        if (!ok) return;
        try { await api.post('/api/app/elevate'); } catch (e) {
          toast('Impossible de relancer en administrateur', { text: e.message, kind: 'error' });
        }
      } })));
  }

  // ---------------------------------------------------------------- main states
  function progressCard(title, t, { freed } = {}) {
    const p = t?.progress || {};
    const ratio = p.total ? p.done / p.total : null;
    return el('div', { class: 'card task-card cl-progress' },
      el('div', { class: 'task-line' }, el('span', { class: 'spinner' }),
        el('div', { class: 'grow' },
          el('div', { class: 'strong', text: title }),
          el('div', { class: 'muted small', text: p.current ? p.current : 'Préparation…' }))),
      progressBar(ratio),
      el('div', { class: 'row between muted small num' },
        el('span', { text: p.total ? `Étape ${Math.min(p.done + 1, p.total)} sur ${p.total}` : '' }),
        freed !== undefined ? el('span', { text: `${fmtBytes(freed)} libérés pour l’instant` }) : null));
  }

  let runFreedBase = 0; let runLastIdx = 0; let runLastBytes = 0;
  function runFreed(t) {
    const p = t?.progress || {};
    if ((p.done || 0) > runLastIdx) { runFreedBase += runLastBytes; runLastBytes = 0; runLastIdx = p.done; }
    runLastBytes = p.bytes || 0;
    return runFreedBase + runLastBytes;
  }

  function renderHero() {
    mainHolder.append(el('div', { class: 'card cl-hero' },
      el('div', { class: 'cl-hero-icon' }, icon('sparkles')),
      el('div', { class: 'stack cl-hero-text' },
        el('h2', { text: 'Retrouvez de l’espace en quelques secondes' }),
        el('p', { class: 'secondary', text: 'Disk Manager va chercher les fichiers temporaires, les caches des navigateurs et des applications, les anciennes versions d’applications, les caches d’outils de développement et les anciens installateurs du dossier Téléchargements.' }),
        el('div', { class: 'row cl-reassure' }, icon('shield'),
          el('span', { class: 'strong', text: 'Rien n’est supprimé sans votre accord.' }),
          el('span', { class: 'muted', text: 'Vous choisissez et confirmez avant chaque nettoyage.' })),
        loadError ? banner('critical', 'alert', loadError) : null,
        el('div', {}, button('Rechercher ce qui peut être nettoyé', { variant: 'primary', iconName: 'search', onClick: startAnalysis })))));
  }

  function renderSummary() {
    const total = bytesParts(analysis.total);
    const head = el('div', { class: 'card cl-summary' },
      el('div', {},
        el('div', { class: 'stat-label', text: 'Espace récupérable trouvé' }),
        el('div', {}, el('span', { class: 'hero-value', text: total.value }), el('span', { class: 'hero-unit', text: total.unit })),
        el('div', { class: 'muted small', text: `Recherche faite ${fmtRelative(analysis.analyzed_at)}` })),
      el('div', { class: 'row wrap cl-summary-actions' },
        button('Tout sélectionner (sûr)', { iconName: 'check', onClick: () => {
          selection = new Map();
          for (const r of analysis.rules) {
            if (r.risk !== 'safe' || r.running.length) continue;
            const ids = ruleSelectable(r).map((i) => i.id);
            if (ids.length) selection.set(r.id, new Set(ids));
          }
          refreshAll();
        } }),
        button('Tout désélectionner', { variant: 'ghost', onClick: () => { selection = new Map(); refreshAll(); } }),
        button('Relancer la recherche', { variant: 'ghost', iconName: 'refresh', onClick: startAnalysis })));
    mainHolder.append(head);
  }

  function renderRules() {
    ruleViews.clear();
    if (!analysis.rules.length) {
      mainHolder.append(el('div', { class: 'card' }, emptyState({
        iconName: 'check', title: 'Votre disque est déjà propre', text: 'Aucun fichier à nettoyer n’a été trouvé pour le moment.' })));
      return;
    }
    for (const [key, label] of CATEGORIES) {
      const rules = analysis.rules.filter((r) => r.category === key);
      if (!rules.length) continue;
      const catSize = rules.reduce((s, r) => s + r.size, 0);
      mainHolder.append(el('div', { class: 'cl-category' },
        el('div', { class: 'row between cl-cat-head' },
          el('h2', { text: label }), el('span', { class: 'muted small num', text: fmtBytes(catSize) })),
        el('div', { class: 'stack tight' }, rules.map(ruleCard))));
    }
  }

  function ruleCard(rule) {
    const sel = ruleSelectable(rule);
    const cb = el('input', { type: 'checkbox', class: 'checkbox', 'aria-label': `Sélectionner ${rule.title}`, disabled: !sel.length });
    cb.addEventListener('click', (e) => e.stopPropagation());
    cb.addEventListener('change', () => { setRule(rule, cb.checked); refreshAll(); });

    const chevron = el('span', { class: 'cl-chevron' });
    const selInfo = el('div', { class: 'muted small num' });
    const itemsHost = el('div', { class: 'cl-items' });

    const modeInfo = MODE[rule.mode] || MODE.purge;
    const badges = el('div', { class: 'row wrap cl-badges' },
      riskBadge(rule.risk),
      el('span', { class: 'cl-mode muted small' }, icon(modeInfo.icon), modeInfo.text),
      rule.running.length ? el('span', { class: 'badge warning', title: 'Fermez l’application pour un nettoyage complet.' },
        icon('alert'), `${rule.running.join(', ')} ouvert`) : null,
      rule.blocked ? el('span', { class: 'badge', title: 'Relancez Disk Manager en administrateur.' }, icon('lock'), 'Admin requis') : null);

    const toggle = el('button', { class: 'cl-rule-main', type: 'button', 'aria-expanded': 'false', onClick: () => setOpen(!expanded.has(rule.id)) },
      chevron,
      el('div', { class: 'cl-rule-text' },
        el('div', { class: 'row wrap cl-rule-titleline' }, el('span', { class: 'strong', text: rule.title })),
        badges,
        rule.description ? el('div', { class: 'muted small cl-desc', text: rule.description }) : null),
      el('div', { class: 'cl-rule-size' },
        el('div', { class: 'strong num cl-size', text: fmtBytes(rule.size) }),
        el('div', { class: 'muted small', text: plural(rule.items.length, 'élément', 'éléments') }),
        selInfo));

    const card = el('div', { class: `card flush cl-rule ${rule.blocked ? 'blocked' : ''}` },
      el('div', { class: 'cl-rule-head' }, el('label', { class: 'cl-check' }, cb), toggle), itemsHost);

    let shown = ITEM_PAGE;
    const itemChecks = new Map();
    function renderItems() {
      clear(itemsHost); itemChecks.clear();
      if (!expanded.has(rule.id)) return;
      if (!rule.items.length) { itemsHost.append(el('div', { class: 'muted small cl-item', text: 'Aucun élément.' })); return; }
      for (const item of rule.items.slice(0, shown)) {
        const ic = el('input', { type: 'checkbox', class: 'checkbox', 'aria-label': `Sélectionner ${item.label}`, disabled: !selectable(item) });
        ic.checked = selSet(rule).has(item.id);
        ic.addEventListener('change', () => {
          const s = new Set(selSet(rule));
          if (ic.checked) s.add(item.id); else s.delete(item.id);
          if (s.size) selection.set(rule.id, s); else selection.delete(rule.id);
          refreshAll();
        });
        itemChecks.set(item.id, ic);
        itemsHost.append(el('label', { class: `cl-item ${item.blocked ? 'blocked' : ''}` },
          ic,
          el('div', { class: 'cl-item-text' },
            el('div', { class: 'row cl-item-label' }, el('span', { class: 'ellipsis', title: item.label, text: item.label }),
              item.blocked ? el('span', { class: 'badge', }, icon('lock'), 'Admin requis') : null),
            item.path && rule.mode !== 'recycle_bin' ? el('div', { class: 'muted small ellipsis mono cl-path', title: item.path, text: pretty(item.path) }) : null,
            item.note ? el('div', { class: 'muted small', text: item.note }) : null),
          item.path && rule.mode !== 'recycle_bin' ? revealButton(item.path) : null,
          el('span', { class: 'num strong cl-item-size', text: fmtBytes(item.size) })));
      }
      if (rule.items.length > shown) {
        itemsHost.append(el('div', { class: 'cl-more' },
          button(`Afficher plus (${fmtNumber(rule.items.length - shown)} restants)`, { size: 'sm', onClick: () => { shown += ITEM_PAGE; renderItems(); } })));
      }
    }

    function setOpen(open) {
      if (open) expanded.add(rule.id); else expanded.delete(rule.id);
      card.classList.toggle('open', open);
      toggle.setAttribute('aria-expanded', String(open));
      chevron.replaceChildren(icon(open ? 'chevronDown' : 'chevronRight'));
      renderItems();
    }

    function update() {
      const s = selSet(rule);
      const n = sel.filter((i) => s.has(i.id)).length;
      cb.checked = sel.length > 0 && n === sel.length;
      cb.indeterminate = n > 0 && n < sel.length;
      card.classList.toggle('selected', n > 0);
      const size = ruleSelectedSize(rule);
      selInfo.textContent = n > 0 && n < rule.items.length ? `${fmtBytes(size)} sélectionnés` : '';
      for (const [id, ic] of itemChecks) ic.checked = s.has(id);
    }

    ruleViews.set(rule.id, { update });
    setOpen(expanded.has(rule.id));
    update();
    return card;
  }

  // ---------------------------------------------------------------- action bar
  function renderBar() {
    clear(barHolder);
    if (!analysis || !analysis.rules.length) return;
    const { count, size } = totals();
    const deleting = !!store.running('clean_run');
    const label = count ? `Nettoyer (${fmtBytes(size)})` : 'Nettoyer';
    barHolder.append(el('div', { class: 'cl-bar' },
      el('div', { class: 'cl-bar-text' },
        el('div', { class: 'strong', text: count ? `${plural(count, 'élément sélectionné', 'éléments sélectionnés')} · ${fmtBytes(size)}` : 'Aucun élément sélectionné' }),
        el('div', { class: 'muted small', text: count ? 'Vous pourrez vérifier avant de confirmer.' : 'Cochez ce que vous souhaitez nettoyer.' })),
      button(label, { variant: 'primary', iconName: 'trash', disabled: !count || deleting, onClick: confirmAndRun })));
  }

  // ---------------------------------------------------------------- confirm + run
  function buildSelection() {
    const out = {};
    for (const r of analysis.rules) {
      const s = selSet(r);
      if (!s.size) continue;
      out[r.id] = r.items.every((i) => s.has(i.id)) ? 'all' : [...s];
    }
    return out;
  }

  async function confirmAndRun() {
    if (store.running('clean_run')) return;
    const { count, size } = totals();
    if (!count) return;
    const chosen = analysis.rules.filter((r) => selSet(r).size);
    const modes = new Set(chosen.map((r) => r.mode));
    const apps = [...new Set(chosen.flatMap((r) => r.running))];
    const body = [
      el('p', { text: 'Voici ce qui va être nettoyé :' }),
      el('div', { class: 'modal-list cl-confirm-list' }, chosen.map((r) =>
        el('div', {}, el('span', { class: 'ellipsis' },
          el('span', { class: 'strong', text: r.title }), el('span', { class: 'muted', text: ` · ${(MODE[r.mode] || MODE.purge).short}` })),
        el('span', { class: 'num strong', text: fmtBytes(ruleSelectedSize(r)) })))),
      modes.has('purge') ? el('p', { text: 'Ces fichiers sont supprimés définitivement. Ce sont des caches et fichiers temporaires que Windows et vos applications recréent automatiquement.' }) : null,
      modes.has('recycle') ? el('p', { text: 'Certains éléments sont envoyés à la corbeille : vous pourrez les restaurer, et l’espace sera libéré une fois la corbeille vidée.' }) : null,
      modes.has('recycle_bin') ? el('p', { class: 'strong', text: 'La corbeille sera vidée définitivement : son contenu ne pourra plus être restauré.' }) : null,
      apps.length ? banner('warning', 'alert', `Fermez ces applications pour un nettoyage complet : ${apps.join(', ')}. Les fichiers qu’elles utilisent seront ignorés.`) : null,
      store.get('meta')?.dry_run ? el('p', { class: 'muted small', text: 'Mode simulation : rien ne sera réellement supprimé.' }) : null,
    ];
    const ok = await confirmDialog({
      title: 'Confirmer le nettoyage', iconName: 'trash', tone: 'warning', body, confirmLabel: `Libérer ${fmtBytes(size)}`,
    });
    if (!ok) return;
    await run(buildSelection());
  }

  async function run(sel) {
    runFreedBase = 0; runLastIdx = 0; runLastBytes = 0;
    let task;
    try {
      task = await store.track('clean_run', api.post('/api/clean/run', { selection: sel }));
    } catch (e) {
      toast('Le nettoyage a échoué', { text: e.message, kind: 'error' });
      renderBar();
      return;
    }
    await finishRun(task);
  }

  async function finishRun(task) {
    if (handledRuns.has(task.id)) return;
    handledRuns.add(task.id);
    if (task.status === 'done' && task.result) showResult(task.result);
    else if (task.status === 'cancelled') toast('Nettoyage interrompu');
    else if (task.status === 'error') toast('Le nettoyage a échoué', { text: task.error, kind: 'error' });
    store.refreshVolumes().catch(() => {});
    // The server consumed the previous analysis: look again.
    analysis = null; selection = new Map(); analyzing = true;
    if (!disposed) render();
    await startAnalysis();
  }

  function showResult(r) {
    const count = (v) => (Array.isArray(v) ? v.length : (v || 0));
    const freed = bytesParts(r.freed || 0);
    const lines = (r.rules || []).map((x) => {
      const notes = [];
      const inUse = count(x.skipped_in_use);
      if (inUse) notes.push(`${plural(inUse, 'fichier en cours d’utilisation ignoré', 'fichiers en cours d’utilisation ignorés')}`);
      const refused = count(x.refused);
      if (refused) notes.push(`${plural(refused, 'élément refusé', 'éléments refusés')} par sécurité${x.refused[0]?.reason ? ` (${x.refused[0].reason})` : ''}`);
      const errors = count(x.errors);
      if (errors) notes.push(`${plural(errors, 'erreur', 'erreurs')}${x.errors[0]?.reason ? ` (${x.errors[0].reason})` : ''}`);
      return el('div', { class: 'cl-result-line' },
        el('div', { class: 'row between' },
          el('span', { class: 'strong', text: x.title }),
          el('span', { class: 'num strong', text: fmtBytes(x.bytes) })),
        el('div', { class: 'muted small', text: plural(x.files || 0, 'fichier supprimé', 'fichiers supprimés') }),
        notes.map((n) => el('div', { class: 'small cl-result-note' }, icon('info'), n)));
    });
    openModal({
      title: r.dry_run ? 'Simulation terminée' : 'Nettoyage terminé', iconName: 'check', wide: true,
      body: [
        el('div', { class: 'cl-result-hero' },
          el('div', {}, el('span', { class: 'hero-value', text: freed.value }), el('span', { class: 'hero-unit', text: freed.unit })),
          el('div', { class: 'secondary', text: r.dry_run ? 'auraient été libérés' : 'libérés' }),
          el('div', { class: 'muted small', text: plural(r.files || 0, 'fichier', 'fichiers') })),
        el('div', { class: 'stack tight cl-result-list' }, lines),
        r.dry_run ? el('p', { class: 'muted small', text: 'Mode simulation : aucun fichier n’a réellement été supprimé.' }) : null,
      ],
      actions: [{ label: 'Terminer', variant: 'primary', autofocus: true }],
    });
  }

  // ---------------------------------------------------------------- render
  function render() {
    clear(mainHolder);
    ruleViews.clear();
    const runTask = store.running('clean_run');
    const anaTask = store.running('clean_analyze');
    renderAdmin();
    if (runTask) {
      mainHolder.append(progressCard('Nettoyage en cours…', runTask, { freed: runFreed(runTask) }));
      clear(barHolder);
      return;
    }
    if (anaTask || analyzing) {
      mainHolder.append(progressCard('Recherche en cours…', anaTask));
      clear(barHolder);
      return;
    }
    if (!analysis) { renderHero(); clear(barHolder); return; }
    renderSummary();
    renderRules();
    renderBar();
  }

  // Live progress without rebuilding the whole view.
  ctx.on('task:clean_analyze', (t) => {
    if (t.status === 'running') render();
    else if (t.status === 'done' && !analyzing) loadAnalysis();
  });
  ctx.on('task:clean_run', (t) => {
    if (t.status === 'running') render();
    else if (t.status === 'done' || t.status === 'cancelled' || t.status === 'error') {
      if (!handledRuns.has(t.id)) finishRun(t);
    }
  });

  // ---------------------------------------------------------------- advice
  api.get('/api/clean/advice').then((r) => {
    if (disposed || !r.advice?.length) return;
    adviceWrap.append(
      el('div', { class: 'row between' }, el('h2', { text: 'Pour aller plus loin' }),
        el('span', { class: 'muted small', text: 'Ces éléments ne sont jamais supprimés automatiquement.' })),
      adviceGrid(r.advice));
  }).catch(() => {});

  // ---------------------------------------------------------------- go
  if (store.running('clean_run') || store.running('clean_analyze')) {
    render();
  } else {
    mainHolder.append(el('div', { class: 'card row muted' }, el('span', { class: 'spinner' }), 'Chargement…'));
    await loadAnalysis();
  }
}
