// Higher-level UI pieces shared by several views (scan launcher, progress,
// safe deletion flow, Explorer shortcuts).
import { post } from './api.js';
import { afterTask, store } from './store.js';
import {
  banner, baseName, button, clear, confirmDialog, el, emptyState, fmtBytes, fmtDuration, fmtNumber,
  fmtRelative, icon, meter, plural, prettyPath, progressBar, toast,
} from './ui.js';

// ------------------------------------------------------------------ labels
export function categoryLabel(key) {
  const cats = store.get('meta')?.categories || [];
  return cats.find((c) => c.key === key)?.label || 'Autres';
}

export const home = () => store.get('meta')?.paths?.home || '';
export const pretty = (p) => prettyPath(p, home());

// ------------------------------------------------------------------ Explorer shortcuts
export async function reveal(path, { select = true } = {}) {
  try {
    await post('/api/shell/reveal', { path, select });
  } catch (e) {
    toast("Impossible d'ouvrir l'Explorateur", { text: e.message, kind: 'error' });
  }
}

export function revealButton(path, { label = '', select = true, size = 'sm' } = {}) {
  return button(label, {
    variant: 'ghost', size, iconName: 'external', title: "Afficher dans l'Explorateur Windows",
    onClick: (e) => { e.stopPropagation(); reveal(path, { select }); },
  });
}

export async function openTool(target) {
  try {
    await post('/api/shell/open', { target });
  } catch (e) {
    toast("Impossible d'ouvrir cet outil Windows", { text: e.message, kind: 'error' });
  }
}

// ------------------------------------------------------------------ scanning
/** Start a scan of `path` and track it globally. Resolves with the finished task. */
export async function startScan(path) {
  if (store.running('scan')) {
    toast('Une analyse est déjà en cours.');
    return null;
  }
  try {
    const task = await store.track('scan', post('/api/scan', { path }));
    await afterTask('scan');
    if (task.status === 'done' && task.result && !task.result.cancelled) {
      toast('Analyse terminée', {
        kind: 'good',
        text: `${fmtBytes(task.result.size)} · ${plural(task.result.files, 'fichier', 'fichiers')} en ${fmtDuration(task.result.duration)}`,
      });
    }
    return task;
  } catch (e) {
    toast("L'analyse a échoué", { text: e.message, kind: 'error' });
    return null;
  }
}

/** Reopen a scan saved on disk (instant). */
export async function loadCachedScan(root) {
  try {
    const task = await store.track('scan', post('/api/scan/load', { path: root }));
    await afterTask('scan');
    return task;
  } catch (e) {
    toast("Impossible de rouvrir l'analyse", { text: e.message, kind: 'error' });
    return null;
  }
}

/**
 * Card that lets the user pick what to analyze: volumes, saved scans, or any folder.
 * `onStarted(path)` is called right after a scan is launched (e.g. to navigate).
 */
export function scanPrompt({ title = 'Analysez votre disque', text, onStarted } = {}) {
  const volumes = store.get('volumes') || [];
  const cached = store.get('cached') || [];
  const input = el('input', { class: 'input grow', type: 'text', placeholder: 'C:\\Users\\vous\\Documents', spellcheck: 'false',
    'aria-label': 'Dossier à analyser' });
  const launch = (path) => { startScan(path); if (onStarted) onStarted(path); };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && input.value.trim()) launch(input.value.trim()); });

  const volButtons = volumes.filter((v) => v.type === 'fixed' || v.type === 'removable').map((v) => {
    const ratio = v.total ? v.used / v.total : 0;
    return el('button', { class: 'scan-volume', type: 'button', onClick: () => launch(v.root) },
      el('div', { class: 'row' }, icon('drive'),
        el('div', { class: 'grow' }, el('div', { class: 'strong', text: `${v.label || 'Disque local'} (${v.letter}:)` }),
          el('div', { class: 'muted small', text: `${fmtBytes(v.free)} libres sur ${fmtBytes(v.total)}` }))),
      meter(ratio));
  });

  const cachedRows = cached.map((c) => el('div', { class: 'row between scan-cached' },
    el('div', { class: 'row' }, icon('history'), el('span', {}, `Analyse de ${c.root} enregistrée `, el('span', { class: 'muted', text: fmtRelative(c.saved) }))),
    button('Rouvrir', { size: 'sm', iconName: 'refresh', onClick: () => { loadCachedScan(c.root); if (onStarted) onStarted(c.root); } })));

  return el('div', { class: 'card scan-prompt' },
    el('div', { class: 'card-header' }, el('div', {},
      el('div', { class: 'card-title', text: title }),
      el('div', { class: 'card-sub', text: text || 'Lecture seule : rien n’est modifié pendant l’analyse. Un disque complet prend environ 30 à 60 secondes.' }))),
    el('div', { class: 'scan-volumes' }, volButtons),
    cachedRows.length ? el('div', { class: 'stack tight scan-cached-list' }, cachedRows) : null,
    el('div', { class: 'row scan-custom' },
      el('div', { class: 'input-wrap grow' }, icon('folder'), input),
      button('Analyser ce dossier', { iconName: 'search', onClick: () => input.value.trim() && launch(input.value.trim()) })));
}

/** Live progress of the current scan. Re-renders on every poll; `ctx.on` handles cleanup. */
export function scanProgress(ctx) {
  const root = el('div', { class: 'card task-card scan-progress' });
  const render = (t) => {
    clear(root);
    if (!t || t.status !== 'running') { root.classList.add('hidden'); return; }
    root.classList.remove('hidden');
    const p = t.progress || {};
    if (p.phase === 'loading') {
      root.append(el('div', { class: 'task-line' }, el('span', { class: 'spinner' }),
        el('div', { class: 'grow' }, el('div', { class: 'strong', text: t.label }),
          el('div', { class: 'muted small', text: 'Ouverture de l’analyse enregistrée…' }))));
      return;
    }
    const rate = p.elapsed > 0.5 ? Math.round((p.files || 0) / p.elapsed) : 0;
    root.append(
      el('div', { class: 'task-line' }, el('span', { class: 'spinner' }),
        el('div', { class: 'grow' }, el('div', { class: 'strong', text: t.label }),
          el('div', { class: 'muted small num', text: `${fmtNumber(p.files || 0)} fichiers · ${fmtNumber(p.dirs || 0)} dossiers · ${fmtBytes(p.bytes || 0)}${rate ? ` · ${fmtNumber(rate)} fichiers/s` : ''}` })),
        button('Annuler', { size: 'sm', variant: 'ghost', iconName: 'stop', onClick: () => post(`/api/tasks/${t.id}/cancel`) })),
      progressBar(null),
      el('div', { class: 'task-current ellipsis', text: p.current || 'Préparation…' }));
  };
  render(store.running('scan'));
  ctx.on('task:scan', render);
  return root;
}

// ------------------------------------------------------------------ safe deletion (recycle bin)
/**
 * Confirm + send items to the recycle bin through the server (which re-validates every path).
 * items: [{path, size, name?}], source: 'explorer'|'largest'|'dupes'|'types'|'old'.
 * Resolves with the server result ({done, bytes, errors, refused, ...}) or null if cancelled.
 */
export async function recycleItems(items, { source }) {
  if (!items.length) return null;
  const total = items.reduce((s, i) => s + (i.size || 0), 0);
  const list = el('div', { class: 'modal-list' }, items.slice(0, 200).map((i) =>
    el('div', {}, el('span', { class: 'ellipsis', title: i.path, text: pretty(i.path) }), el('span', { class: 'num muted', text: fmtBytes(i.size) }))));
  const ok = await confirmDialog({
    title: items.length === 1 ? `Mettre « ${baseName(items[0].path)} » à la corbeille ?` : `Mettre ${fmtNumber(items.length)} éléments à la corbeille ?`,
    iconName: 'trash', tone: 'warning', confirmLabel: `Mettre à la corbeille (${fmtBytes(total)})`,
    body: [
      el('p', { text: 'Les éléments iront dans la corbeille Windows : vous pourrez les restaurer. L’espace sera réellement libéré une fois la corbeille vidée.' }),
      list,
      items.length > 200 ? el('p', { class: 'muted small', text: `… et ${fmtNumber(items.length - 200)} autres.` }) : null,
    ],
  });
  if (!ok) return null;
  try {
    const task = await store.track('recycle', post('/api/files/recycle', { paths: items.map((i) => i.path), source }));
    await afterTask('recycle');
    const r = task.result || {};
    reportDeletion(r, 'mis à la corbeille');
    return r;
  } catch (e) {
    toast('La suppression a échoué', { text: e.message, kind: 'error' });
    return null;
  }
}

/** Toast summarizing a deletion result (shared by recycle and cleanup). */
export function reportDeletion(r, verb = 'libérés') {
  const refused = r.refused?.length || 0;
  const errors = r.errors?.length || 0;
  const parts = [];
  if (r.dry_run) parts.push('Mode simulation : rien n’a été supprimé.');
  if (refused) parts.push(`${plural(refused, 'élément refusé', 'éléments refusés')} par sécurité (${r.refused[0].reason})`);
  if (errors) parts.push(`${plural(errors, 'élément ignoré', 'éléments ignorés')} (fichier ouvert ou accès refusé)`);
  if (r.skipped_in_use) parts.push(`${plural(r.skipped_in_use, 'fichier en cours d’utilisation ignoré', 'fichiers en cours d’utilisation ignorés')}`);
  toast(`${fmtBytes(r.bytes ?? r.freed ?? 0)} ${verb}`, {
    kind: (r.bytes ?? r.freed) > 0 || !(refused || errors) ? 'good' : 'error',
    text: parts.join(' · ') || undefined, timeout: parts.length ? 9000 : 5000,
  });
}

// ------------------------------------------------------------------ guidance cards
/** Grid of advice cards from GET /api/clean/advice (things the app never deletes itself). */
export function adviceGrid(advice) {
  const grid = el('div', { class: 'grid auto advice-grid' });
  for (const a of advice) {
    const sevIcon = { warning: 'alert', tip: 'zap', info: 'info' }[a.severity] || 'info';
    let action = null;
    if (a.action?.type === 'open') {
      action = el('div', {}, button(a.action.label, { size: 'sm', iconName: 'external', onClick: () => openTool(a.action.target) }));
    } else if (a.action?.type === 'command') {
      action = el('div', { class: 'stack tight' },
        el('code', { class: 'advice-cmd mono', text: a.action.command }),
        el('div', { class: 'row' },
          button('Copier la commande', { size: 'sm', iconName: 'copy', onClick: async () => {
            try {
              await navigator.clipboard.writeText(a.action.command);
              toast('Commande copiée', { kind: 'good', text: a.action.admin ? 'À coller dans un terminal lancé en administrateur.' : 'À coller dans un terminal.' });
            } catch { toast('Copie impossible', { kind: 'error' }); }
          } }),
          a.action.admin ? el('span', { class: 'badge', title: 'Terminal administrateur requis' }, icon('shieldUp'), 'Admin') : null));
    }
    grid.append(el('div', { class: `card advice-card sev-${a.severity}` },
      el('div', { class: 'row top' }, el('div', { class: 'advice-icon' }, icon(sevIcon)),
        el('div', { class: 'grow stack tight' },
          el('div', { class: 'row between' }, el('div', { class: 'strong', text: a.title }),
            a.size ? el('span', { class: 'num strong', text: fmtBytes(a.size) }) : null),
          el('p', { class: 'secondary small', text: a.body }),
          action))));
  }
  return grid;
}

// ------------------------------------------------------------------ protection hint (display only)
/**
 * True when a path is in a location the server will refuse to delete (system folders,
 * profile roots, system files). Display hint only — the server is the authority.
 */
export function isProtectedHint(path) {
  const meta = store.get('meta');
  const sd = (meta?.paths?.system_drive || 'C:\\').toLowerCase();
  const homeDir = (meta?.paths?.home || '').toLowerCase();
  const p = (path || '').toLowerCase();
  const under = (pre) => p === pre || p.startsWith(`${pre}\\`);
  const prefixes = ['windows', 'program files', 'program files (x86)', 'programdata', 'recovery', '$recycle.bin',
    'system volume information', 'boot', '$windows.~bt', '$windows.~ws', 'windows.old', '$winreagent', '$sysreset']
    .map((d) => sd + d);
  if (prefixes.some(under)) return true;
  if (['pagefile.sys', 'hiberfil.sys', 'swapfile.sys', 'dumpstack.log'].some((f) => p === sd + f)) return true;
  if (homeDir && ['appdata\\local\\packages', 'appdata\\local\\microsoft\\windows', 'appdata\\roaming\\microsoft\\windows']
    .map((d) => `${homeDir}\\${d}`).some(under)) return true;
  return /\\(ntuser\.(dat|ini|pol)|usrclass\.dat)/.test(p);
}

/** Standard "needs a scan" placeholder for analysis views. */
export function needScan(ctx, { what = 'cette vue' } = {}) {
  const wrap = el('div', { class: 'stack' });
  wrap.append(
    scanProgress(ctx),
    el('div', { class: 'card' }, emptyState({
      iconName: 'search', title: 'Aucune analyse pour le moment',
      text: `Lancez une analyse pour afficher ${what}. Rien n’est modifié pendant l’analyse.`,
    })),
    scanPrompt({ title: 'Que voulez-vous analyser ?' }));
  return wrap;
}

export function dryRunBanner() {
  return store.get('meta')?.dry_run
    ? banner('warning', 'info', 'Mode simulation activé : les suppressions sont simulées, aucun fichier n’est modifié.')
    : null;
}
