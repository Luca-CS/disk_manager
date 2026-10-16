// API client. The session token arrives once in the URL fragment (#t=...),
// is kept in sessionStorage for reloads, and sent as X-DM-Token on every call.
// An elevated window gets a one-time code instead (#c=...): it is traded for the
// token, which then stays in memory only (sessionStorage can be written to the
// Edge profile, a folder the non-elevated user can read).

const TOKEN_KEY = 'dm-token';

async function exchangeCode(code) {
  try {
    const res = await fetch('/api/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
      cache: 'no-store',
      credentials: 'same-origin',
      redirect: 'error',
    });
    const data = await res.json();
    return res.ok && typeof data.token === 'string' ? data.token : '';
  } catch {
    return ''; // the first API call then reports the expired session
  }
}

async function readToken() {
  const m = /[#&]([tc])=([A-Za-z0-9_-]{20,})/.exec(location.hash);
  if (m) history.replaceState(null, '', location.pathname + location.search + '#/overview');
  if (m && m[1] === 'c') return exchangeCode(m[2]);
  let token = m ? m[2] : null;
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else token = sessionStorage.getItem(TOKEN_KEY);
  } catch { /* storage unavailable: keep the in-memory token */ }
  return token || '';
}

const TOKEN = await readToken();

export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const listeners = new Set();
/** Called with every ApiError (used by the shell to detect an expired session). */
export const onApiError = (fn) => listeners.add(fn);

export async function api(path, { method = 'GET', body, query } = {}) {
  let url = path;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') qs.set(k, v);
    const s = qs.toString();
    if (s) url += `?${s}`;
  }
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: { 'X-DM-Token': TOKEN, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: 'no-store',
      credentials: 'same-origin',
      redirect: 'error',
    });
  } catch {
    const err = new ApiError("Impossible de joindre l'application. Est-elle toujours lancée ?", 0, 'offline');
    listeners.forEach((fn) => fn(err));
    throw err;
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const e = data && data.error ? data.error : {};
    const err = new ApiError(e.message || `Erreur ${res.status}`, res.status, e.code || 'error');
    listeners.forEach((fn) => fn(err));
    throw err;
  }
  return data;
}

export const get = (path, query) => api(path, { query });
export const post = (path, body = {}) => api(path, { method: 'POST', body });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Poll a background task until it finishes.
 * @param {object|Promise<object>} taskOrPromise  task dict returned by a POST that starts a task
 * @param {{onProgress?: (task) => void, interval?: number}} opts
 * @returns {Promise<object>} the finished task (status 'done' | 'cancelled'); throws ApiError on 'error'
 */
export async function waitTask(taskOrPromise, { onProgress, interval = 400 } = {}) {
  let task = await taskOrPromise;
  while (task.status === 'running') {
    if (onProgress) onProgress(task);
    await sleep(interval);
    task = await get(`/api/tasks/${task.id}`);
  }
  if (onProgress) onProgress(task);
  if (task.status === 'error') throw new ApiError(task.error || 'La tâche a échoué.', 500, 'task_error');
  return task;
}

export const cancelTask = (id) => post(`/api/tasks/${id}/cancel`);
