// Shell: navigation, routing, theme, heartbeat, sidebar status.
import { cancelTask, get, onApiError, post, waitTask } from './api.js';
import { afterTask, store } from './store.js';
import { $, banner, button, clear, confirmDialog, el, fmtBytes, icon, meter, toast } from './ui.js';

const ROUTES = [
  { section: 'Analyse' },
  { id: 'overview', label: "Vue d'ensemble", icon: 'gauge' },
  { id: 'explorer', label: 'Explorateur', icon: 'explorer' },
  { id: 'types', label: 'Types de fichiers', icon: 'pie' },
  { id: 'largest', label: 'Gros fichiers', icon: 'bigfile' },
  { id: 'duplicates', label: 'Doublons', icon: 'copy' },
  { section: 'Libérer de l’espace' },
  { id: 'clean', label: 'Nettoyage intelligent', icon: 'sparkles' },
  { id: 'apps', label: 'Applications', icon: 'apps' },
  { section: 'Suivi' },
  { id: 'history', label: 'Historique', icon: 'history' },
];
const VIEWS = ROUTES.filter((r) => r.id).map((r) => r.id);

// ------------------------------------------------------------------ theme
const THEME_KEY = 'dm-theme';
function storedTheme() {
  try { return localStorage.getItem(THEME_KEY) || 'auto'; } catch { return 'auto'; }
}
function applyTheme(mode) {
  if (mode === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = mode;
  try { localStorage.setItem(THEME_KEY, mode); } catch { /* ignore */ }
  document.dispatchEvent(new Event('themechange'));
}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => document.dispatchEvent(new Event('themechange')));

// ------------------------------------------------------------------ routing
let current = null; // {id, module, disposers}
const loadedStyles = new Set();

function ensureStyle(name) {
  if (!name || loadedStyles.has(name)) return;
  loadedStyles.add(name);
  document.head.append(el('link', { rel: 'stylesheet', href: `css/views/${name}.css` }));
}

function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [id, qs] = h.split('?');
  return { id: VIEWS.includes(id) ? id : 'overview', params: Object.fromEntries(new URLSearchParams(qs || '')) };
}

export function navigate(id, params = {}) {
  const qs = new URLSearchParams(params).toString();
  const target = `#/${id}${qs ? `?${qs}` : ''}`;
  if (location.hash === target) render();
  else location.hash = target;
}

async function render() {
  const { id, params } = parseHash();
  if (current) {
    try { current.module.unmount?.(); } catch (e) { console.error(e); }
    current.disposers.forEach((fn) => { try { fn(); } catch { /* ignore */ } });
  }
  for (const a of document.querySelectorAll('.nav-item')) a.classList.toggle('active', a.dataset.view === id);
  const root = $('#view-root');
  clear(root);
  clear($('#view-actions'));
  $('#view-sub').textContent = '';
  root.classList.remove('view-enter');
  void root.offsetWidth; // restart the enter animation
  root.classList.add('view-enter');
  $('#content').scrollTop = 0;

  const route = ROUTES.find((r) => r.id === id);
  $('#view-title').textContent = route.label;
  document.title = `${route.label} — Disk Manager`;
  const disposers = [];
  const ctx = {
    params,
    navigate,
    store,
    api: { get, post, waitTask, cancelTask },
    setTitle: (t) => { $('#view-title').textContent = t; },
    setSubtitle: (t) => { $('#view-sub').textContent = t || ''; },
    setActions: (...nodes) => { clear($('#view-actions')).append(...nodes.flat().filter(Boolean)); },
    on: (key, fn) => { disposers.push(store.on(key, fn)); },
    onDispose: (fn) => disposers.push(fn),
  };
  try {
    const module = await import(`./views/${id}.js`);
    if (parseHash().id !== id) return; // user navigated away while loading
    current = { id, module, disposers };
    ensureStyle(module.styles);
    await module.mount(root, ctx);
  } catch (e) {
    console.error(e);
    current = { id, module: {}, disposers };
    root.append(banner('critical', 'alert', `Cette vue n’a pas pu s’afficher : ${e.message}`));
  }
}

// ------------------------------------------------------------------ sidebar
function buildNav() {
  const nav = $('#nav');
  for (const r of ROUTES) {
    if (r.section) { nav.append(el('div', { class: 'nav-section', text: r.section })); continue; }
    const item = el('a', { class: 'nav-item', href: `#/${r.id}`, dataset: { view: r.id } },
      icon(r.icon), el('span', { text: r.label }), el('span', { class: 'nav-badge' }));
    nav.append(item);
  }
  // A spinner on "Explorateur" while a scan runs, wherever the user is.
  store.on('task:scan', (t) => {
    const badge = document.querySelector('.nav-item[data-view="explorer"] .nav-badge');
    if (!badge) return;
    clear(badge);
    if (t && t.status === 'running') badge.append(el('span', { class: 'spinner', title: 'Analyse en cours' }));
  });
}

function renderFooter() {
  const footer = clear($('#sidebar-footer'));
  const meta = store.get('meta');
  const sys = (store.get('volumes') || []).find((v) => v.is_system);
  if (sys) {
    footer.append(el('div', { class: 'sidebar-disk' },
      el('div', { class: 'row between small' }, el('span', { class: 'strong', text: `Disque ${sys.letter}:` }),
        el('span', { class: 'muted num', text: `${fmtBytes(sys.free)} libres` })),
      meter(sys.total ? sys.used / sys.total : 0)));
  }
  footer.append(el('div', { class: 'local-badge', title: 'Aucune donnée ne quitte cet ordinateur : l’application n’effectue aucun appel réseau.' },
    icon('shield'), el('span', { text: '100 % local · hors ligne' })));

  const mode = storedTheme();
  const nextMode = { auto: 'light', light: 'dark', dark: 'auto' }[mode];
  const themeLabel = { auto: 'Thème : système', light: 'Thème : clair', dark: 'Thème : sombre' }[mode];
  const themeIcon = { auto: 'monitor', light: 'sun', dark: 'moon' }[mode];
  const row = el('div', { class: 'row sidebar-actions' },
    button('', { variant: 'ghost', iconName: themeIcon, title: `${themeLabel} (cliquer pour changer)`, onClick: () => { applyTheme(nextMode); renderFooter(); } }));
  if (meta && !meta.admin) {
    row.append(button('', { variant: 'ghost', iconName: 'shieldUp', title: 'Relancer en administrateur (pour nettoyer les fichiers système)', onClick: elevate }));
  } else if (meta?.admin) {
    row.append(el('span', { class: 'badge accent', title: 'Lancé en administrateur' }, icon('shieldUp'), 'Admin'));
  }
  row.append(el('span', { class: 'grow' }),
    button('', { variant: 'ghost', iconName: 'power', title: 'Quitter Disk Manager', onClick: quit }));
  footer.append(row);
}

async function elevate() {
  const ok = await confirmDialog({
    title: 'Relancer en administrateur ?', iconName: 'shieldUp', confirmLabel: 'Relancer',
    body: el('p', { text: 'Les droits administrateur permettent de nettoyer les fichiers système (Windows Update, fichiers temporaires de Windows, rapports d’erreurs). Windows va vous demander une confirmation, puis l’application redémarrera.' }),
  });
  if (!ok) return;
  try { await post('/api/app/elevate'); toast('Redémarrage en administrateur…'); } catch (e) { toast('Élévation impossible', { text: e.message, kind: 'error' }); }
}

async function quit() {
  const ok = await confirmDialog({ title: 'Quitter Disk Manager ?', iconName: 'power', confirmLabel: 'Quitter',
    body: el('p', { text: 'Votre dernière analyse est enregistrée et pourra être rouverte instantanément.' }) });
  if (!ok) return;
  try { await post('/api/app/quit'); } catch { /* the server is going away */ }
  window.close();
}

// ------------------------------------------------------------------ session
let sessionLost = false;
onApiError((err) => {
  if (sessionLost || (err.status !== 401 && err.code !== 'offline')) return;
  sessionLost = true;
  clearInterval(heartbeat);
  document.body.append(el('div', { class: 'modal-backdrop' }, el('div', { class: 'modal' },
    el('div', { class: 'modal-header' }, el('div', { class: 'modal-icon critical' }, icon('alert')),
      el('div', { class: 'modal-title', text: err.status === 401 ? 'Session expirée' : 'Application arrêtée' })),
    el('div', { class: 'modal-body' }, el('p', { text: 'Cette fenêtre n’est plus reliée à Disk Manager. Fermez-la et relancez l’application.' })))));
});

let heartbeat;
function startHeartbeat() {
  const beat = () => post('/api/ping').catch(() => {});
  beat();
  heartbeat = setInterval(beat, 15000);
}

// ------------------------------------------------------------------ boot
async function boot() {
  applyTheme(storedTheme());
  buildNav();
  startHeartbeat();
  window.addEventListener('hashchange', render);
  store.on('volumes', renderFooter);
  try {
    const meta = await store.loadMeta();
    $('#brand-sub').textContent = meta.computer ? `${meta.computer} · v${meta.version}` : `v${meta.version}`;
    if (meta.dry_run) document.body.classList.add('dry-run');
    await Promise.allSettled([store.refreshVolumes(), store.refreshScan()]);
    renderFooter();
    store.resumeTasks().catch(() => {});
  } catch (e) {
    toast('Démarrage incomplet', { text: e.message, kind: 'error' });
  }
  if (!location.hash || location.hash === '#') location.hash = '#/overview';
  else render();
  setInterval(() => store.refreshVolumes().catch(() => {}), 30000);
}

boot();

export { afterTask };
