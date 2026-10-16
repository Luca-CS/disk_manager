// Demo page for the chart components, with realistic fake data.
import { el, clear, fmtBytes, fmtNumber, catColor, seriesColor, joinPath, CATEGORY_KEYS } from '../js/ui.js';
import { createTreemap } from '../js/charts/treemap.js';
import { createSunburst } from '../js/charts/sunburst.js';
import { barList, stackedBar, legend } from '../js/charts/bars.js';
import { diskMap } from '../js/charts/diskmap.js';
import { usageChart } from '../js/charts/area.js';
import { usageRing } from '../js/charts/ring.js';

const CAT_LABELS = {
  app: 'Applications', data: 'Données', disk_image: 'Images disque', video: 'Vidéos', image: 'Images',
  document: 'Documents', code: 'Code', archive: 'Archives', other: 'Autres',
};

// ------------------------------------------------------------------ fake data
function mulberry32(a) {
  return () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rnd = mulberry32(42);
const pick = (a) => a[Math.floor(rnd() * a.length)];
const WORDS = ['cache', 'data', 'assets', 'logs', 'backup', 'media', 'plugins', 'temp', 'models', 'build', 'dist',
  'packages', 'resources', 'config', 'shared', 'archive', 'sdk', 'runtime', 'bin', 'lib', 'docs', 'projects', 'saves'];
const EXTS = [['.exe', 'app'], ['.dll', 'app'], ['.mp4', 'video'], ['.iso', 'disk_image'], ['.jpg', 'image'],
  ['.pdf', 'document'], ['.js', 'code'], ['.zip', 'archive'], ['.db', 'data'], ['.dat', 'other']];
const ROOT_SIZE = 125357694976;
const THRESHOLD = ROOT_SIZE * 0.002;
const NOW = Math.floor(Date.now() / 1000);

function splitWeights(n, power = 2) {
  const w = Array.from({ length: n }, () => rnd() ** power + 0.03);
  const sum = w.reduce((a, b) => a + b, 0);
  return w.map((v) => v / sum);
}

function genDir(name, size, depth, fixedKids) {
  const node = { n: name, s: Math.round(size), t: 'd', fc: 0 };
  if (depth >= 4) { node.more = 1; node.fc = Math.max(1, Math.round(size / 300e3)); return node; }
  const kids = [];
  const dirShare = fixedKids ? 0.9 : 0.4 + rnd() * 0.45;
  const specs = fixedKids || Array.from({ length: 3 + Math.floor(rnd() * 5) }, () => [`${pick(WORDS)}${Math.floor(rnd() * 90)}`, null]);
  const ws = fixedKids ? fixedKids.map((k) => k[1]) : splitWeights(specs.length);
  const wsum = ws.reduce((a, b) => a + b, 0);
  specs.forEach((sp, i) => kids.push(genDir(sp[0], (size * dirShare * ws[i]) / wsum, depth + 1, null)));
  const nFiles = 5 + Math.floor(rnd() * 14);
  const fw = splitWeights(nFiles, 3);
  const fileTotal = size * (1 - dirShare);
  let smallS = 0; let smallK = 0;
  fw.forEach((w) => {
    const [ext, cat] = pick(EXTS);
    const s = Math.round(fileTotal * w);
    if (s < THRESHOLD / 6) { smallS += s; smallK += 1 + Math.floor(rnd() * 40); return; }
    kids.push({ n: `${pick(WORDS)}_${Math.floor(rnd() * 999)}${ext}`, s, t: 'f', cat, m: NOW - Math.floor(rnd() * 3 * 365 * 86400) });
  });
  if (smallS) kids.push({ n: '', s: smallS, t: 's', k: smallK });
  // tiny folders collapse into an "others" aggregate
  let oS = 0; let oK = 0;
  const kept = kids.filter((k) => {
    if (k.t === 'd' && k.s < THRESHOLD / 3) { oS += k.s; oK += k.fc || 1; return false; }
    return true;
  });
  if (oS) kept.push({ n: '', s: oS, t: 'o', k: oK });
  kept.sort((a, b) => b.s - a.s);
  node.ch = kept;
  node.fc = kept.reduce((a, k) => a + (k.t === 'd' ? k.fc : k.t === 'f' ? 1 : k.k), 0);
  return node;
}

function makeTree() {
  const root = genDir('C:\\', ROOT_SIZE, 0, [
    ['Windows', 38], ['Users', 26], ['Program Files', 14], ['Program Files (x86)', 8], ['ProgramData', 6],
    ['$Recycle.Bin', 2], ['Temp', 1.5], ['Logiciels', 3],
  ]);
  root.path = 'C:\\';
  return root;
}

function findNode(root, path) {
  if (path === root.path) return root;
  let node = root;
  const rest = path.slice(root.path.length).split('\\').filter(Boolean);
  for (const seg of rest) {
    node = (node.ch || []).find((c) => c.t === 'd' && c.n === seg);
    if (!node) return null;
  }
  return node;
}

function categoryTotals(node, acc = {}) {
  for (const c of node.ch || []) {
    if (c.t === 'f') acc[c.cat] = (acc[c.cat] || 0) + c.s;
    else if (c.t === 'd') categoryTotals(c, acc);
    else acc.other = (acc.other || 0) + c.s;
  }
  return acc;
}

const DISK = {
  model: 'NVMe SSD 256 Go', size: 256060514304, media: 'SSD NVMe', bus: 'NVMe', health: 'Sain',
  partitions: [
    { number: 1, letter: '', type: 'System', kind: 'efi', offset: 1048576, size: 104857600, label: '', fs: 'FAT32' },
    { number: 2, letter: '', type: 'Reserved', kind: 'msr', offset: 105906176, size: 16777216, label: '', fs: '' },
    { number: 3, letter: 'C', type: 'Basic', kind: 'os', offset: 122683392, size: 239293431808, label: 'Windows', fs: 'NTFS', used: 125357694976 },
    { number: 4, letter: '', type: 'Recovery', kind: 'recovery', offset: 239416115200, size: 524288000, label: '', fs: 'NTFS' },
    { number: 5, letter: '', type: 'Basic', kind: 'recovery', offset: 239940403200, size: 524288000, label: 'WINPE', fs: 'FAT32' },
    { number: 6, letter: '', type: 'Basic', kind: 'recovery', offset: 240464691200, size: 15032385536, label: 'OEM', fs: 'NTFS', used: 10200547328 },
    { number: 7, letter: '', type: 'Recovery', kind: 'recovery', offset: 255497076736, size: 1073741824, label: 'WinRE', fs: 'NTFS' },
  ],
};

function makePoints() {
  const total = DISK.partitions[2].size;
  const pts = [];
  let used = 118e9;
  for (let i = 0; i < 30; i++) {
    used += (rnd() - 0.3) * 0.9e9;
    pts.push({ ts: NOW - (29 - i) * 86400 + Math.floor(rnd() * 3600), used: Math.round(used), total });
  }
  return pts.sort(() => rnd() - 0.5); // unsorted on purpose
}

// ------------------------------------------------------------------ page
const tree = makeTree();
let current = tree;
const trail = [];
const nodeCount = (n) => 1 + (n.ch || []).reduce((a, c) => a + nodeCount(c), 0);

const root = document.getElementById('demo');
const info = el('div', { class: 'small secondary', text: 'Cliquez une cellule pour la sélectionner.' });
const pathLabel = el('span', { class: 'muted small' });
const tmBox = el('div', { class: 'ch-demo-box' });
const sbBox = el('div', { class: 'ch-demo-box' });
const staticBox = el('div', { class: 'stack' });
const diskBox = el('div');
const areaBox = el('div', { style: { height: '240px' } });
const ringBox = el('div', { class: 'ch-demo-rings' });

function describe(sel) {
  info.textContent = `${sel.type} — ${sel.name || '(agrégat)'} — ${fmtBytes(sel.size)}${sel.path ? ` — ${sel.path}` : ''}`;
}

function go(path) {
  const node = findNode(tree, path);
  if (!node || !node.ch) return;
  trail.push(current);
  show({ ...node, path });
}
function up() {
  if (trail.length) show(trail.pop());
}
function show(node) {
  current = node;
  pathLabel.textContent = `${node.path} — ${fmtNumber(nodeCount(node))} nœuds`;
  treemap.setData(node);
  sunburst.setData(node);
}

const treemap = createTreemap(tmBox, { onSelect: describe, onOpen: go, colorMode: 'type' });
const sunburst = createSunburst(sbBox, { onSelect: describe, onOpen: go, onUp: up, colorMode: 'branch' });

function modeSwitch(label, onPick, initial) {
  const btns = [['type', 'Par type'], ['branch', 'Par dossier']].map(([k, t]) => el('button', {
    type: 'button', class: k === initial ? 'active' : '',
    onClick: () => { btns.forEach((b) => b.classList.toggle('active', b === btn(k))); onPick(k); },
  }, t));
  const btn = (k) => btns[k === 'type' ? 0 : 1];
  return el('div', { class: 'segmented', role: 'group', 'aria-label': label }, btns);
}

function card(title, sub, body, actions) {
  return el('section', { class: 'card' },
    el('div', { class: 'card-header' }, el('div', {}, el('div', { class: 'card-title', text: title }),
      sub ? el('div', { class: 'card-sub', text: sub }) : null), actions ? el('div', { class: 'card-actions' }, actions) : null),
    body);
}

const themeBtn = el('button', { type: 'button', class: 'btn', onClick: toggleTheme }, 'Basculer le thème');
function toggleTheme() {
  const dark = document.documentElement.dataset.theme === 'dark'
    || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'light' : 'dark';
  document.dispatchEvent(new Event('themechange'));
}

function renderStatic() {
  const totals = categoryTotals(tree);
  const items = CATEGORY_KEYS.filter((k) => totals[k]).map((k) => ({ key: k, label: CAT_LABELS[k], value: totals[k], sublabel: null }))
    .sort((a, b) => b.value - a.value);
  const colorOf = (it) => catColor(it.key);
  clear(staticBox).append(
    stackedBar(items, { total: tree.s, colorOf, onClick: (s) => { info.textContent = `Segment : ${s.label}`; } }),
    legend(items, { colorOf }),
    barList(items, { total: tree.s, colorOf, onClick: (it) => { info.textContent = `Ligne : ${it.label} — ${fmtBytes(it.value)}`; } }),
    barList((tree.ch || []).filter((c) => c.t === 'd').map((c, i) => ({ key: c.n, label: c.n, value: c.s, sublabel: `${fmtNumber(c.fc)} fichiers`, i })),
      { total: tree.s, colorOf: (it) => seriesColor(it.i), maxItems: 6 }));
}

root.append(
  el('div', { class: 'row between' }, el('div', {}, el('h1', { text: 'Graphiques — démo' }), pathLabel), themeBtn),
  info,
  card('Treemap', 'Double-clic sur un dossier pour y entrer', tmBox, [
    modeSwitch('Couleurs du treemap', (m) => treemap.setColorMode(m), 'type'),
    el('button', { type: 'button', class: 'btn sm', onClick: up }, 'Dossier parent'),
  ]),
  card('Sunburst', 'Clic sur un arc = descendre, centre = remonter', sbBox,
    modeSwitch('Couleurs du sunburst', (m) => sunburst.setColorMode(m), 'branch')),
  card('Répartition par catégorie', null, staticBox),
  card('Disque physique', null, diskBox),
  card('Historique d\'utilisation', '30 derniers jours', areaBox),
  card('Anneaux d\'utilisation', null, ringBox),
);

renderStatic();
diskBox.append(diskMap(DISK, { onSelect: (p) => { info.textContent = `Partition ${p.number} — ${p.label || p.kind} — ${fmtBytes(p.size)}`; } }));
usageChart(areaBox, makePoints(), { total: DISK.partitions[2].size });
for (const [u, label] of [[0.42, 'Disque C:'], [0.83, 'Données'], [0.94, 'Sauvegarde']]) {
  ringBox.append(usageRing(u * 500e9, 500e9, { label }));
}
show({ ...tree });
document.addEventListener('themechange', renderStatic);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', renderStatic);
