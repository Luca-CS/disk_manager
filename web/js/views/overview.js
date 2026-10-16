// Vue d'ensemble — reference view: hero usage, what can be freed, disks & partitions,
// composition of the last scan, and guidance cards.
import { barList, legend, stackedBar } from '../charts/bars.js';
import { diskMap } from '../charts/diskmap.js';
import { usageRing } from '../charts/ring.js';
import { adviceGrid, dryRunBanner, openTool, scanProgress, scanPrompt, startScan } from '../components.js';
import {
  banner, bytesParts, button, catColor, clear, el, fmtBytes, fmtNumber, fmtRelative, icon, plural,
  riskBadge, seriesColor, toast,
} from '../ui.js';

export const styles = 'overview';

export async function mount(root, ctx) {
  const { store, api } = ctx;
  ctx.setSubtitle('L’état de vos disques en un coup d’œil');

  const heroCard = el('div', { class: 'card ov-hero' });
  const freeCard = el('div', { class: 'card ov-free' });
  const disksCard = el('div', { class: 'card' });
  const compoWrap = el('div');
  const adviceWrap = el('div', { class: 'stack' });

  root.append(
    dryRunBanner(),
    el('div', { class: 'grid ov-top' }, heroCard, freeCard),
    scanProgress(ctx),
    compoWrap,
    disksCard,
    adviceWrap,
  );

  // ---------------------------------------------------------------- hero
  let system = null;
  function renderHero() {
    clear(heroCard);
    const vol = (store.get('volumes') || []).find((v) => v.is_system);
    if (!vol) { heroCard.append(el('div', { class: 'muted', text: 'Lecture du disque…' })); return; }
    const disk = system?.disks?.find((d) => d.partitions.some((p) => p.letter === vol.letter));
    const free = bytesParts(vol.free);
    const health = disk?.health;
    heroCard.append(
      el('div', { class: 'ov-hero-ring' }, usageRing(vol.used, vol.total, { label: 'utilisé' })),
      el('div', { class: 'ov-hero-text' },
        el('div', { class: 'stat-label', text: `${vol.label || 'Disque local'} (${vol.letter}:) — espace libre` }),
        el('div', {}, el('span', { class: 'hero-value', text: free.value }), el('span', { class: 'hero-unit', text: free.unit })),
        el('div', { class: 'secondary num', text: `${fmtBytes(vol.used)} utilisés sur ${fmtBytes(vol.total)} · ${vol.fs}` }),
        el('div', { class: 'row wrap ov-hero-badges' },
          disk ? el('span', { class: 'badge' }, icon('drive'), `${disk.media} ${disk.bus}`) : null,
          health ? el('span', { class: `badge ${health === 'Healthy' ? 'good' : health === 'Warning' ? 'warning' : 'critical'}` },
            icon(health === 'Healthy' ? 'check' : 'alert'), health === 'Healthy' ? 'Disque en bonne santé' : `Santé : ${health}`) : null,
          disk?.reliability?.temperature ? el('span', { class: 'badge', text: `${disk.reliability.temperature} °C` }) : null,
          disk ? el('span', { class: 'badge', title: disk.model, text: disk.model }) : el('span', { class: 'badge', text: 'Lecture des disques…' }))));
  }

  // ---------------------------------------------------------------- what can be freed
  async function renderFree(analysisTask) {
    clear(freeCard);
    const head = el('div', { class: 'card-header' },
      el('div', {}, el('div', { class: 'card-title', text: 'Espace récupérable' }),
        el('div', { class: 'card-sub', text: 'Caches, fichiers temporaires et restes d’applications' })));
    freeCard.append(head);
    const running = analysisTask && analysisTask.status === 'running';
    if (running) {
      const p = analysisTask.progress || {};
      freeCard.append(el('div', { class: 'stack' },
        el('div', { class: 'row' }, el('span', { class: 'spinner' }), el('span', { text: p.current ? `Analyse : ${p.current}` : 'Recherche en cours…' })),
        el('div', { class: 'progress' }, el('span', { style: { width: `${p.total ? (100 * p.done) / p.total : 5}%` } }))));
      return;
    }
    let analysis = null;
    try { analysis = (await api.get('/api/clean')).analysis; } catch { /* shown below */ }
    if (!analysis) {
      freeCard.append(el('div', { class: 'stack ov-free-empty' },
        el('p', { class: 'secondary', text: 'Repérez en quelques secondes ce qui peut être supprimé sans risque. Rien n’est supprimé sans votre accord.' }),
        el('div', {}, button('Rechercher', { variant: 'primary', iconName: 'sparkles', onClick: runAnalysis }))));
      return;
    }
    const total = bytesParts(analysis.total);
    const top = analysis.rules.filter((r) => !r.blocked).slice(0, 4);
    freeCard.append(
      el('div', { class: 'row ov-free-total' },
        el('div', {}, el('span', { class: 'hero-value', text: total.value }), el('span', { class: 'hero-unit', text: total.unit })),
        el('div', { class: 'muted small', text: `trouvés ${fmtRelative(analysis.analyzed_at)}` })),
      el('div', { class: 'stack tight ov-free-list' }, top.map((r) =>
        el('div', { class: 'row ov-free-row' }, el('span', { class: 'grow ellipsis', text: r.title }), riskBadge(r.risk),
          el('span', { class: 'num strong', text: fmtBytes(r.size) })))),
      el('div', { class: 'card-footer' },
        button('Voir et nettoyer', { variant: 'primary', iconName: 'sparkles', onClick: () => ctx.navigate('clean') }),
        button('Actualiser', { variant: 'ghost', iconName: 'refresh', onClick: runAnalysis })));
  }

  async function runAnalysis() {
    try {
      await store.track('clean_analyze', api.post('/api/clean/analyze'));
    } catch (e) {
      toast('La recherche a échoué', { text: e.message, kind: 'error' });
    }
    renderFree(null);
  }
  ctx.on('task:clean_analyze', (t) => { if (t.status === 'running') renderFree(t); });

  // ---------------------------------------------------------------- composition of the scan
  async function renderCompo() {
    clear(compoWrap);
    const scan = store.get('scan');
    if (!scan) {
      if (!store.running('scan')) {
        compoWrap.append(scanPrompt({ title: 'Analysez votre disque pour voir ce qui prend de la place' }));
      }
      return;
    }
    const compoCard = el('div', { class: 'card' });
    compoWrap.append(compoCard);
    compoCard.append(el('div', { class: 'card-header' },
      el('div', {}, el('div', { class: 'card-title', text: `Contenu de ${scan.root}` }),
        el('div', { class: 'card-sub', text: `${fmtBytes(scan.size)} · ${plural(scan.files, 'fichier', 'fichiers')} · analysé ${fmtRelative(scan.finished)}${scan.from_cache ? ' (analyse enregistrée)' : ''}` })),
      el('div', { class: 'card-actions' },
        button('Explorer', { iconName: 'explorer', onClick: () => ctx.navigate('explorer') }),
        button('Relancer', { variant: 'ghost', iconName: 'refresh', onClick: () => startScan(scan.root) }))));

    const [types, children] = await Promise.all([
      api.get('/api/scan/types').catch(() => null),
      api.get('/api/scan/children', { path: scan.root }).catch(() => null),
    ]);
    if (!types || !children) return;
    const cats = types.categories.filter((c) => c.size > 0);
    const body = el('div', { class: 'grid cols-2 ov-compo' });
    body.append(
      el('div', { class: 'stack' },
        el('div', { class: 'ov-section-title', text: 'Par type de fichier' }),
        stackedBar(cats.map((c) => ({ key: c.key, label: c.label, value: c.size })), {
          total: types.total, colorOf: (seg) => catColor(seg.key), onClick: (seg) => ctx.navigate('types', { cat: seg.key }) }),
        legend(cats.map((c) => ({ key: c.key, label: `${c.label} · ${fmtBytes(c.size)}` })), { colorOf: (it) => catColor(it.key) })),
      el('div', { class: 'stack' },
        el('div', { class: 'ov-section-title', text: 'Plus gros dossiers' }),
        barList(children.items.filter((i) => i.type === 'dir').slice(0, 6).map((d) => ({
          key: d.path, label: d.name, value: d.size, sublabel: plural(d.files, 'fichier', 'fichiers'),
        })), { total: scan.size, colorOf: (it) => seriesColor(children.items.findIndex((x) => x.path === it.key)),
        onClick: (item) => ctx.navigate('explorer', { path: item.key }) })));
    compoCard.append(body);
    if (scan.volume && scan.volume.unscanned > 2 * 1024 ** 3) {
      compoCard.append(el('div', { class: 'muted small ov-unscanned' }, icon('info'),
        ` ${fmtBytes(scan.volume.unscanned)} ne sont pas visibles dans l’analyse (points de restauration, fichiers système ou d’autres utilisateurs).`));
    }
  }

  // ---------------------------------------------------------------- disks
  function renderDisks() {
    clear(disksCard);
    disksCard.append(el('div', { class: 'card-header' },
      el('div', {}, el('div', { class: 'card-title', text: 'Disques et partitions' }),
        el('div', { class: 'card-sub', text: 'Disposition physique, santé et occupation' })),
      el('div', { class: 'card-actions' },
        button('Gestion des disques', { variant: 'ghost', iconName: 'external', size: 'sm', onClick: () => openTool('diskmgmt') }))));
    if (!system) { disksCard.append(el('div', { class: 'row muted' }, el('span', { class: 'spinner' }), 'Lecture des disques…')); return; }
    if (system.error && !system.disks.length) {
      disksCard.append(banner('warning', 'alert', `Informations détaillées indisponibles : ${system.error}`));
    }
    for (const d of system.disks) {
      const rel = d.reliability;
      disksCard.append(el('div', { class: 'ov-disk' },
        el('div', { class: 'row wrap ov-disk-head' },
          icon('drive'),
          el('div', { class: 'grow' }, el('div', { class: 'strong', text: d.model }),
            el('div', { class: 'muted small', text: `Disque ${d.number} · ${d.media} · ${d.bus} · ${fmtBytes(d.size)} · ${d.partition_style || ''}` })),
          el('span', { class: `badge ${d.health === 'Healthy' ? 'good' : d.health === 'Warning' ? 'warning' : d.health === 'Unhealthy' ? 'critical' : ''}` },
            icon(d.health === 'Healthy' ? 'check' : 'alert'), { Healthy: 'Bonne santé', Warning: 'Avertissement', Unhealthy: 'Défaillant' }[d.health] || 'Santé inconnue'),
          rel?.wear !== null && rel?.wear !== undefined ? el('span', { class: 'badge', text: `Usure ${rel.wear} %` }) : null,
          rel?.power_on_hours ? el('span', { class: 'badge', text: `${fmtNumber(rel.power_on_hours)} h de fonctionnement` }) : null),
        diskMap(d, {})));
    }
  }

  // ---------------------------------------------------------------- advice
  async function renderAdvice() {
    clear(adviceWrap);
    let advice = [];
    try { advice = (await api.get('/api/clean/advice')).advice || []; } catch { return; }
    if (!advice.length) return;
    adviceWrap.append(el('div', { class: 'row between' }, el('h2', { text: 'Conseils pour aller plus loin' }),
      el('span', { class: 'muted small', text: 'Ces éléments ne sont jamais supprimés automatiquement.' })));
    const grid = adviceGrid(advice);
    adviceWrap.append(grid);
  }

  // ---------------------------------------------------------------- wiring
  ctx.setActions(
    button('Actualiser', { variant: 'ghost', iconName: 'refresh', onClick: async () => {
      system = await api.get('/api/system', { refresh: 1 }).catch(() => system);
      await store.refreshVolumes();
      renderHero(); renderDisks();
    } }));
  ctx.on('volumes', renderHero);
  // Bars take explicit colors: repaint them when the theme changes.
  const onTheme = () => renderCompo();
  document.addEventListener('themechange', onTheme);
  ctx.onDispose(() => document.removeEventListener('themechange', onTheme));
  ctx.on('scan', () => { renderCompo(); renderAdvice(); });
  // While a first scan runs, the progress card replaces the scan prompt.
  ctx.on('task:scan', (t) => { if (t.status === 'running' && !store.get('scan')) clear(compoWrap); });

  renderHero();
  renderDisks();
  renderFree(store.running('clean_analyze'));
  renderCompo();
  renderAdvice();
  api.get('/api/system').then((s) => { system = s; renderHero(); renderDisks(); })
    .catch((e) => { system = { disks: [], error: e.message }; renderDisks(); });
}
