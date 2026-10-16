// Tiny observable app state shared by the shell and the views.
import { get, waitTask } from './api.js';

const state = {
  meta: null,        // GET /api/meta
  scan: null,        // current scan summary (GET /api/scan) or null
  cached: [],        // [{root, saved}] scans saved on disk
  volumes: [],       // GET /api/volumes
  recycleBin: null,  // {size, count}
  tasks: {},         // kind -> latest task dict (while tracked)
};
const subs = new Map();

function emit(key, value) {
  for (const fn of subs.get(key) || []) {
    try { fn(value); } catch (e) { console.error(e); }
  }
}

export const store = {
  get: (key) => state[key],

  set(key, value) {
    state[key] = value;
    emit(key, value);
  },

  /** Subscribe; returns an unsubscribe function. */
  on(key, fn) {
    if (!subs.has(key)) subs.set(key, new Set());
    subs.get(key).add(fn);
    return () => subs.get(key).delete(fn);
  },

  async loadMeta() {
    const meta = await get('/api/meta');
    store.set('meta', meta);
    return meta;
  },

  async refreshScan() {
    const r = await get('/api/scan');
    store.set('cached', r.cached || []);
    store.set('scan', r.scan);
    return r.scan;
  },

  async refreshVolumes() {
    const r = await get('/api/volumes');
    store.set('recycleBin', r.recycle_bin);
    store.set('volumes', r.volumes || []);
    return r.volumes;
  },

  /** Is a task of this kind currently running (tracked)? */
  running(kind) {
    const t = state.tasks[kind];
    return t && t.status === 'running' ? t : null;
  },

  /**
   * Track a background task: emits `task:<kind>` on every poll and when it ends.
   * Returns the finished task (throws on error). Kinds: scan, dupes, clean_analyze,
   * clean_run, recycle, empty_bin.
   */
  async track(kind, taskOrPromise) {
    const task = await taskOrPromise;
    state.tasks[kind] = task;
    emit(`task:${kind}`, task);
    try {
      const done = await waitTask(task, {
        onProgress: (t) => { state.tasks[kind] = t; emit(`task:${kind}`, t); },
      });
      state.tasks[kind] = done;
      emit(`task:${kind}`, done);
      return done;
    } catch (e) {
      state.tasks[kind] = { ...state.tasks[kind], status: 'error', error: e.message };
      emit(`task:${kind}`, state.tasks[kind]);
      throw e;
    }
  },

  /** After a reload, resume tracking tasks that are still running server-side. */
  async resumeTasks() {
    const r = await get('/api/tasks');
    for (const t of r.tasks || []) {
      if (!store.running(t.kind)) {
        store.track(t.kind, t).then(() => afterTask(t.kind)).catch(() => {});
      }
    }
  },
};

/** Refresh what a finished task may have changed. */
export async function afterTask(kind) {
  if (['scan', 'recycle', 'clean_run', 'empty_bin'].includes(kind)) {
    await Promise.allSettled([store.refreshScan(), store.refreshVolumes()]);
  }
}
