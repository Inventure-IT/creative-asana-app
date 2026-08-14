#!/usr/bin/env python3
"""
Generate a static, GitHub-Pages-hostable `index.html` from creative_asana_app.py.

The local app keeps the Asana PAT server-side and proxies the API. GitHub Pages
serves static files only, so the static build:
  * asks the user for a Personal Access Token on a login screen (stored only in the
    browser: sessionStorage, or localStorage if "remember" is checked),
  * talks to the Asana API directly from the browser (Asana supports CORS), and
  * reimplements the Python aggregation in JS, exposed through a fetch() shim that
    answers the very same /api/* routes the existing page already calls — so the
    entire UI (tabs, charts, date range, drill-ins) is reused verbatim.

Run:  python build_static.py     ->  writes ./index.html
Re-run whenever you change PROJECTS / caps / team members in creative_asana_app.py.
"""
import json
import creative_asana_app as a

PROJECTS = json.dumps(a.PROJECTS)
GROUPS = json.dumps(a.GROUPS)
EST_FIELD = json.dumps(a.EST_FIELD)
EXCLUDE = json.dumps(sorted(a.EXCLUDE_SECTIONS))
ASSIGNEE_CAP = a.ASSIGNEE_HOURS_CAP
TEAM = json.dumps(a.TEAM_MEMBERS)
DEF_START = json.dumps(a.DEFAULT_START)
DEF_END = json.dumps(a.DEFAULT_END)

# ---- Browser-side data layer + fetch shim + PAT auth (prepended before the UI script) ----
PREPEND = r'''
// ===== Static build: Asana data layer (runs entirely in the browser) =====
const ASANA_API = 'https://app.asana.com/api/1.0';
const EST_FIELD = __ESTFIELD__;
const EXCLUDE_SECTIONS = new Set(__EXCLUDE__);
const ASSIGNEE_HOURS_CAP = __CAP__;
const TEAM_MEMBERS = __TEAM__;
const PROJECTS = __PROJECTS__;
const GROUPS = __GROUPS__;
const DEF_START = __DEFSTART__, DEF_END = __DEFEND__;
const PROJECT_ROSTER = PROJECTS.map(p => p.name);
const PROJECT_NAMES = Object.fromEntries(PROJECTS.map(p => [p.gid, p.name]));
const PROJECT_CAPS  = Object.fromEntries(PROJECTS.map(p => [p.gid, p.cap == null ? null : p.cap]));

let TOKEN = null;
const _realFetch = window.fetch.bind(window);

function round2(x){ return Math.round((x + Number.EPSILON) * 100) / 100; }
function sum(arr){ return arr.reduce((a, b) => a + b, 0); }
function nowStr(){ const d = new Date(), p = n => String(n).padStart(2, '0');
  const h24 = d.getHours(), ap = h24 < 12 ? 'AM' : 'PM', h12 = h24 % 12 || 12;
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(h12)}:${p(d.getMinutes())}:${p(d.getSeconds())} ${ap}`; }

// Bound real network concurrency (Asana rate limits + browser per-host caps).
let _active = 0; const _waiters = [];
function _gate(fn){
  return new Promise((resolve, reject) => {
    const run = () => { _active++; fn().then(resolve, reject).finally(() => { _active--; const n = _waiters.shift(); if (n) n(); }); };
    if (_active < 8) run(); else _waiters.push(run);
  });
}

// GET an Asana collection (follows pagination, retries on 429). Returns the concatenated data array.
async function asanaGet(pathOrUrl){
  let out = [], next = pathOrUrl;
  while (next){
    const full = next.startsWith('http') ? next : ASANA_API + next;
    const page = await _gate(async () => {
      for (;;){
        const r = await _realFetch(full, { headers: { Authorization: 'Bearer ' + TOKEN } });
        if (r.status === 429){ const ra = Number(r.headers.get('Retry-After') || 1); await new Promise(s => setTimeout(s, ra * 1000)); continue; }
        if (!r.ok) throw new Error('Asana ' + r.status);
        return r.json();
      }
    });
    out = out.concat(page.data || []);
    next = page.next_page && page.next_page.uri ? page.next_page.uri : null;
  }
  return out;
}
const mapAll = (arr, fn) => Promise.all(arr.map(fn));

// POST one JSON body (only /batch_requests uses this), with the same 429 retry as asanaGet.
async function asanaPost(path, body){
  return _gate(async () => {
    for (;;){
      const r = await _realFetch(ASANA_API + path, { method: 'POST',
        headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify(body) });
      if (r.status === 429){ const ra = Number(r.headers.get('Retry-After') || 1); await new Promise(s => setTimeout(s, ra * 1000)); continue; }
      if (!r.ok) throw new Error('Asana ' + r.status);
      return r.json();
    }
  });
}

// Mirrors api_batch: Asana's /batch_requests runs up to 10 reads per HTTP request, and a load
// is dominated by hundreds of tiny per-task reads. Any batch Asana rejects — and any action
// that failed or has a second page — falls back to a plain GET, so batching can cost requests
// but can never lose rows.
const BATCH_MAX = 10;
async function asanaBatchChunk(paths){
  const out = {};
  let results = null;
  try {
    const res = await asanaPost('/batch_requests',
      { data: { actions: paths.map(p => ({ method: 'get', relative_path: p })) } });
    results = res && res.data;
    if (!Array.isArray(results) || results.length !== paths.length) results = null;
  } catch (err) { results = null; }
  if (!results){
    for (const [i, rows] of (await mapAll(paths, asanaGet)).entries()) out[paths[i]] = rows;
    return out;
  }
  await mapAll(paths.map((p, i) => [p, results[i]]), async ([p, r]) => {
    const body = (r && r.body) || {};
    out[p] = (r && r.status_code === 200 && body.data && !body.next_page)
      ? body.data : await asanaGet(p);
  });
  return out;
}
async function asanaBatch(paths){
  paths = [...paths];
  const chunks = [];
  for (let i = 0; i < paths.length; i += BATCH_MAX) chunks.push(paths.slice(i, i + BATCH_MAX));
  return Object.assign({}, ...await mapAll(chunks, asanaBatchChunk));
}

// ---- Estimated-hours layer (mirrors project_detail / get_summaries / get_assignee_load) ----
function taskMinutes(t){ for (const cf of (t.custom_fields || [])){ if (cf.name === EST_FIELD && cf.number_value != null) return cf.number_value; } return 0; }
function actualMinutes(t){ return t.actual_time_minutes || 0; }
function sectionName(t, gid){ let fb = ''; for (const m of (t.memberships || [])){ const sec = (m.section || {}).name; if (!sec) continue; if ((m.project || {}).gid === gid) return sec; fb = fb || sec; } return fb; }
function isExcluded(t, gid){ return EXCLUDE_SECTIONS.has(sectionName(t, gid).trim().toLowerCase()); }

// One field set for both halves of the app (mirrors TASK_FIELDS / SUBTASK_FIELDS): num_subtasks
// lets fetchTree skip the subtask call for a leaf task, and completed_at is what the
// logged-hours layer needs, so it no longer re-fetches these same lists.
const TASK_FIELDS = 'name,assignee.name,completed,completed_at,actual_time_minutes,num_subtasks,custom_fields.name,custom_fields.number_value,memberships.section.name,memberships.project.gid';
const SUBTASK_FIELDS = 'name,assignee.name,completed,completed_at,actual_time_minutes,custom_fields.name,custom_fields.number_value';
function fetchTasks(gid){ return asanaGet(`/projects/${gid}/tasks?opt_fields=${TASK_FIELDS}&limit=100`); }
const subtasksPath = g => `/tasks/${g}/subtasks?opt_fields=${SUBTASK_FIELDS}&limit=100`;
const entriesPath = g => `/tasks/${g}/time_tracking_entries?opt_fields=duration_minutes,entered_on,created_by.name&limit=100`;

// Every task and subtask of one project, fetched once and shared by the estimated and
// logged-hours layers (mirrors fetch_tree). The in-flight promise is cached, not just the
// result, so the two concurrent startup requests share one fetch instead of racing.
const _trees = {};
function fetchTree(gid, refresh){
  if (!refresh && _trees[gid]) return _trees[gid];
  return (_trees[gid] = (async () => {
    const tasks = await fetchTasks(gid);
    const parents = tasks.filter(t => (t.num_subtasks || 0) > 0).map(t => t.gid);
    const byPath = await asanaBatch(parents.map(subtasksPath));
    const subs = {};
    parents.forEach(g => { subs[g] = byPath[subtasksPath(g)]; });
    return { tasks, subs };
  })());
}

function buildTask(t, gid, children){
  const subs = [];
  for (const s of children){
    if (s.completed) continue;
    subs.push({ name: s.name || '(untitled)', assignee: (s.assignee || {}).name || 'Unassigned',
      hours: round2(taskMinutes(s) / 60), actual: round2(actualMinutes(s) / 60) });
  }
  return { gid: t.gid, name: t.name || '(untitled)', assignee: (t.assignee || {}).name || 'Unassigned',
    hours: round2(taskMinutes(t) / 60), actual: round2(actualMinutes(t) / 60), section: sectionName(t, gid), subtasks: subs };
}

async function projectDetail(gid, refresh){
  const tree = await fetchTree(gid, refresh);
  const tasks = tree.tasks.filter(t => !t.completed && !isExcluded(t, gid));
  const detailed = tasks.map(t => buildTask(t, gid, tree.subs[t.gid] || []));
  const totals = {}, actuals = {}, counts = {};
  for (const d of detailed){
    totals[d.assignee] = (totals[d.assignee] || 0) + d.hours;
    actuals[d.assignee] = (actuals[d.assignee] || 0) + d.actual;
    counts[d.assignee] = (counts[d.assignee] || 0) + 1;
    for (const s of d.subtasks){
      totals[s.assignee] = (totals[s.assignee] || 0) + s.hours;
      actuals[s.assignee] = (actuals[s.assignee] || 0) + s.actual;
      counts[s.assignee] = (counts[s.assignee] || 0) + 1;
    }
  }
  const ordered = Object.keys(totals).sort((x, y) => totals[y] - totals[x]);
  return { gid, name: PROJECT_NAMES[gid] || gid, labels: ordered,
    hours: ordered.map(n => round2(totals[n])), actual_hours: ordered.map(n => round2(actuals[n])),
    counts: ordered.map(n => counts[n]), ntasks: detailed.length, tasks: detailed, updated: nowStr() };
}

const CACHE = { summaries: null, detail: {}, logged_summaries: {}, logged_detail: {}, me: null };
// Mirrors get_me: the display name behind the PAT, so the UI can default a filter to "you".
// asanaGet always hands back an array, and /users/me is a single object, so take the first.
async function getMe(refresh){
  if (!refresh && CACHE.me) return CACHE.me;
  const rows = await asanaGet('/users/me?opt_fields=name');
  return (CACHE.me = { name: ((rows[0] || {}).name) || '' });
}
function summaryFromDetail(d){ return { gid: d.gid, name: d.name, ntasks: d.ntasks, hours: round2(sum(d.hours)), cap: PROJECT_CAPS[d.gid], updated: d.updated }; }

async function getDetail(gid, refresh){
  if (!refresh && CACHE.detail[gid]) return CACHE.detail[gid];
  const data = await projectDetail(gid, refresh);
  CACHE.detail[gid] = data;
  if (CACHE.summaries) CACHE.summaries = CACHE.summaries.map(s => s.gid === gid ? summaryFromDetail(data) : s);
  return data;
}
async function getSummaries(refresh){
  if (!refresh && CACHE.summaries) return CACHE.summaries;
  const details = await mapAll(PROJECTS, p => getDetail(p.gid, refresh));
  return (CACHE.summaries = details.map(summaryFromDetail));
}
// Task/subtask rows assigned to `name` within one project detail (est/actual/remaining + status).
function assigneeProjectTasks(d, name){
  const rows = [];
  for (const t of d.tasks){
    if (t.assignee === name)
      rows.push({ name: t.name, type: 'task', status: t.section, estimated: t.hours, actual: t.actual, remaining: round2(t.hours - t.actual), context: '' });
    for (const s of t.subtasks){
      if (s.assignee === name)
        rows.push({ name: s.name, type: 'subtask', status: t.section, estimated: s.hours, actual: s.actual, remaining: round2(s.hours - s.actual),
          context: t.assignee === name ? '' : `under "${t.name}" · ${t.assignee}` });
    }
  }
  return rows;
}
async function getAssigneeLoad(refresh){
  const details = await mapAll(PROJECTS, p => getDetail(p.gid, refresh));
  const est = {}, act = {}, counts = {}, breakdown = {};
  for (const d of details){
    d.labels.forEach((name, i) => {
      const e = d.hours[i], aa = d.actual_hours[i], cnt = d.counts[i];
      est[name] = (est[name] || 0) + e; act[name] = (act[name] || 0) + aa; counts[name] = (counts[name] || 0) + cnt;
      if (e || aa) (breakdown[name] = breakdown[name] || []).push({ project: d.name, estimated: round2(e), actual: round2(aa), remaining: round2(e - aa), tasks: assigneeProjectTasks(d, name) });
    });
  }
  const ordered = TEAM_MEMBERS.filter(n => n in est).sort((x, y) => (est[y] - act[y]) - (est[x] - act[x]));
  if ('Unassigned' in est) ordered.push('Unassigned');
  return { cap: ASSIGNEE_HOURS_CAP, labels: ordered,
    hours: ordered.map(n => round2(est[n] - act[n])), estimated: ordered.map(n => round2(est[n])),
    actual: ordered.map(n => round2(act[n])), counts: ordered.map(n => counts[n]),
    breakdown: Object.fromEntries(ordered.map(n => [n, breakdown[n] || []])), updated: nowStr() };
}

// ---- Logged-hours layer for a date range (mirrors logged_detail / get_logged_summaries) ----
// Mirrors entries_for_tasks: the per-task entry reads were the bulk of every load, so
// uncached ones go out BATCH_MAX at a time. Entries don't depend on the selected range, so a
// range change re-filters what is already here instead of re-querying.
const _entries = {};
async function entriesForTasks(gids, refresh){
  const out = {}, missing = [];
  for (const g of gids){
    if (!refresh && _entries[g]) out[g] = _entries[g];
    else missing.push(g);
  }
  if (missing.length){
    const byPath = await asanaBatch(missing.map(entriesPath));
    for (const g of missing) out[g] = _entries[g] = byPath[entriesPath(g)];
  }
  return out;
}
async function loggedDetail(gid, start, end, refresh){
  // Shares the estimated side's project tree, so the only calls left here are the per-item
  // time-entry lookups.
  const tree = await fetchTree(gid, refresh);
  const tasks = tree.tasks;
  // Tasks/subtasks completed within the date range (by completed_at), deduped by gid.
  const completedDates = {};
  const noteCompleted = it => {
    if (it.completed && it.completed_at){ const day = it.completed_at.slice(0, 10);
      if (start <= day && day <= end) completedDates[it.gid] = day; }
  };
  const cand = {};
  for (const t of tasks){ noteCompleted(t); if ((t.actual_time_minutes || 0) > 0) cand[t.gid] = t.name || '(untitled)'; }
  for (const subs of Object.values(tree.subs))
    for (const s of subs){ noteCompleted(s); if ((s.actual_time_minutes || 0) > 0) cand[s.gid] = s.name || '(untitled)'; }
  // One batched read for every candidate task, then filter to the range in memory.
  const byGid = await entriesForTasks(Object.keys(cand), refresh);
  const seen = new Set(), entries = [];
  for (const [g, name] of Object.entries(cand))
    for (const e of byGid[g] || []){
      const entered = e.entered_on || '';
      if (!entered || entered < start || entered > end) continue;
      if (e.gid && seen.has(e.gid)) continue;
      if (e.gid) seen.add(e.gid);
      entries.push({ task: name, by: (e.created_by || {}).name || 'Unknown', date: entered,
        minutes: e.duration_minutes || 0 });
    }
  const totals = {}, counts = {};
  for (const e of entries){
    totals[e.by] = (totals[e.by] || 0) + e.minutes; counts[e.by] = (counts[e.by] || 0) + 1;
  }
  const ordered = Object.keys(totals).sort((x, y) => totals[y] - totals[x]);
  entries.sort((x, y) => x.date < y.date ? -1 : x.date > y.date ? 1 : 0);
  return { gid, name: PROJECT_NAMES[gid] || gid, start, end, labels: ordered,
    hours: ordered.map(n => round2(totals[n] / 60)),
    counts: ordered.map(n => counts[n]),
    total_hours: round2(sum(Object.values(totals)) / 60),
    completed: Object.keys(completedDates).length,
    nentries: entries.length,
    entries: entries.map(e => ({ task: e.task, by: e.by, date: e.date, hours: round2(e.minutes / 60) })), updated: nowStr() };
}
function loggedSummaryFromDetail(d){ return { gid: d.gid, name: d.name, start: d.start, end: d.end, hours: d.total_hours, completed: d.completed, nentries: d.nentries, cap: PROJECT_CAPS[d.gid], updated: d.updated }; }

async function getLoggedDetail(gid, refresh, start, end){
  const key = start + ':' + end;
  if (!refresh && CACHE.logged_detail[key] && CACHE.logged_detail[key][gid]) return CACHE.logged_detail[key][gid];
  const data = await loggedDetail(gid, start, end, refresh);
  (CACHE.logged_detail[key] = CACHE.logged_detail[key] || {})[gid] = data;
  if (CACHE.logged_summaries[key]) CACHE.logged_summaries[key] = CACHE.logged_summaries[key].map(s => s.gid === gid ? loggedSummaryFromDetail(data) : s);
  return data;
}
async function getLoggedSummaries(refresh, start, end){
  const key = start + ':' + end;
  if (!refresh && CACHE.logged_summaries[key]) return CACHE.logged_summaries[key];
  const details = await mapAll(PROJECTS, p => getLoggedDetail(p.gid, refresh, start, end));
  return (CACHE.logged_summaries[key] = details.map(loggedSummaryFromDetail));
}

// ---- Route /api/* (same shapes the existing UI expects) ----
async function handleApi(p){
  const u = new URL(p, location.origin), path = u.pathname, q = u.searchParams;
  const refresh = q.get('refresh') === '1';
  let start = q.get('start') || DEF_START, end = q.get('end') || DEF_END;
  if (start > end){ const t = start; start = end; end = t; }
  if (path === '/api/projects') return getSummaries(refresh);
  if (path === '/api/logged') return getLoggedSummaries(refresh, start, end);
  if (path === '/api/assignees') return getAssigneeLoad(refresh);
  if (path === '/api/groups') return GROUPS;
  if (path === '/api/me') return getMe(refresh);
  if (path.startsWith('/api/logged/')) return getLoggedDetail(path.split('/').pop(), refresh, start, end);
  if (path.startsWith('/api/project/')) return getDetail(path.split('/').pop(), refresh);
  throw new Error('Unknown API ' + path);
}
// Intercept the page's own /api/* calls; everything else (Asana, CDN) passes through.
window.fetch = async (input, init) => {
  const url = typeof input === 'string' ? input : (input && input.url) || '';
  const p = url.replace(location.origin, '');
  if (p.startsWith('/api/')){
    try { return new Response(JSON.stringify(await handleApi(p)), { status: 200, headers: { 'Content-Type': 'application/json' } }); }
    catch (err) { return new Response(JSON.stringify({ error: String(err && err.message || err) }), { status: 502, headers: { 'Content-Type': 'application/json' } }); }
  }
  return _realFetch(input, init);
};

// ---- PAT auth / login screen ----
const STORE_KEY = 'asana_pat';
function loadToken(){ return sessionStorage.getItem(STORE_KEY) || localStorage.getItem(STORE_KEY) || null; }
function clearToken(){ sessionStorage.removeItem(STORE_KEY); localStorage.removeItem(STORE_KEY); }
async function validateToken(tok){
  const r = await _realFetch(ASANA_API + '/users/me?opt_fields=name,email', { headers: { Authorization: 'Bearer ' + tok } });
  if (!r.ok) throw new Error(r.status === 401 ? 'Invalid token' : 'Asana ' + r.status);
  return (await r.json()).data;
}
function showSignout(who){
  let b = document.getElementById('signout-fab');
  if (!b){ b = document.createElement('button'); b.id = 'signout-fab'; b.className = 'signout'; document.body.appendChild(b); }
  b.textContent = 'Sign out'; b.title = who ? ('Signed in as ' + who) : ''; b.onclick = doLogout; b.style.display = 'block';
}
function hideSignout(){ const b = document.getElementById('signout-fab'); if (b) b.style.display = 'none'; }
function doLogout(){ clearToken(); TOKEN = null; window.__authed = false; hideSignout();
  if (typeof chart !== 'undefined' && chart){ try { chart.destroy(); } catch (e) {} chart = null; } location.hash = ''; renderLogin(); }

function renderLogin(err){
  window.__authed = false; hideSignout();
  document.getElementById('app').innerHTML = `
    <div class="login"><div class="login-card">
      <h1>Asana Dashboard</h1>
      <p class="sub">Connect with a Personal Access Token. Create one in Asana → My Settings → Apps → Manage developer apps → Personal access tokens.</p>
      <input type="password" id="pat" placeholder="Paste your Asana PAT" autocomplete="off" spellcheck="false">
      <label class="remember"><input type="checkbox" id="remember"> Remember on this device</label>
      ${err ? `<p class="login-err">${esc(err)}</p>` : ''}
      <button class="btn" id="connect">Connect</button>
      <p class="login-note">Your token is stored only in this browser — sessionStorage, or localStorage if you check “remember” — and sent directly to Asana over HTTPS. Don’t use this on a shared computer, and never commit a token to the repo.</p>
    </div></div>`;
  const inp = document.getElementById('pat'), go = document.getElementById('connect');
  const submit = async () => {
    const tok = inp.value.trim(); if (!tok) return;
    go.disabled = true; go.textContent = 'Connecting…'; TOKEN = tok;
    try {
      const me = await validateToken(tok);
      (document.getElementById('remember').checked ? localStorage : sessionStorage).setItem(STORE_KEY, tok);
      window.__authed = true; showSignout(me.name || me.email || 'Signed in'); location.hash = ''; window.__startApp();
    } catch (e){ TOKEN = null; renderLogin(e && e.message || String(e)); }
  };
  go.onclick = submit;
  inp.onkeydown = ev => { if (ev.key === 'Enter') submit(); };
  inp.focus();
}

async function bootAuth(){
  const tok = loadToken();
  if (!tok){ renderLogin(); return; }
  TOKEN = tok;
  try { const me = await validateToken(tok); window.__authed = true; showSignout(me.name || me.email || 'Signed in'); window.__startApp(); }
  catch (e){ clearToken(); TOKEN = null; renderLogin('Saved token is no longer valid — please reconnect.'); }
}
'''

LOGIN_CSS = '''
  /* login + sign-out (static build) */
  .login { min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px; }
  .login-card { background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:34px 32px;
                max-width:430px; width:100%; box-shadow:0 10px 34px rgba(0,0,0,.5); }
  .login-card h1 { font-size:22px; margin:0 0 6px; }
  .login-card .sub { margin:0 0 18px; }
  .login-card input[type=password] { width:100%; box-sizing:border-box; background:var(--panel2); color:var(--text);
                border:1px solid var(--border); border-radius:8px; padding:11px 12px; font-size:14px; }
  .login-card .remember { display:flex; align-items:center; gap:8px; font-size:13px; color:var(--muted); margin:12px 0 16px; }
  .login-card .btn { width:100%; padding:11px; }
  .login-err { color:var(--red); font-size:13px; margin:0 0 12px; }
  .login-note { font-size:11px; color:var(--faint); margin-top:16px; line-height:1.55; }
  .signout { position:fixed; top:16px; right:18px; z-index:50; background:var(--panel2); color:var(--text);
             border:1px solid var(--border); border-radius:8px; padding:7px 12px; font-size:12px; cursor:pointer; }
  .signout:hover { background:#414854; }
'''

NEW_TAIL = """window.addEventListener('hashchange', () => { if (window.__authed) route(); });
window.__startApp = route;
bootAuth();"""


def build():
    page = a.PAGE
    prepend = (PREPEND
               .replace('__ESTFIELD__', EST_FIELD)
               .replace('__EXCLUDE__', EXCLUDE)
               .replace('__CAP__', str(ASSIGNEE_CAP))
               .replace('__TEAM__', TEAM)
               .replace('__PROJECTS__', PROJECTS)
               .replace('__GROUPS__', GROUPS)
               .replace('__DEFSTART__', DEF_START)
               .replace('__DEFEND__', DEF_END))

    # 1) add login/sign-out CSS
    assert '</style>' in page
    page = page.replace('</style>', LOGIN_CSS + '</style>', 1)

    # 2) inject the data layer + auth as a script that runs BEFORE the UI script
    anchor = '<div class="wrap" id="app"></div>\n<script>'
    assert anchor in page
    page = page.replace(anchor,
                        '<div class="wrap" id="app"></div>\n<script>\n' + prepend + '\n</script>\n<script>', 1)

    # 3) gate the UI's auto-start on a valid token
    old_tail = "window.addEventListener('hashchange', route);\nroute();"
    assert old_tail in page
    page = page.replace(old_tail, NEW_TAIL, 1)

    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(page)
    print('Wrote index.html (%d bytes)' % len(page.encode('utf-8')))


if __name__ == '__main__':
    build()
