#!/usr/bin/env python3
"""
Asana Widget Dashboard.

A self-contained local app. Starts a small web server that serves a single-page
dashboard of project "widgets". Click a widget to drill into a detail view with a
per-assignee estimated-hours bar chart, a Refresh button, and a "last updated" time.

Run:  python georgia_grown_app.py     (opens http://localhost:8765)

Add more widgets by appending to PROJECTS below.
Token lookup: ASANA_PAT env var, falling back to the Windows User env var (registry).
"""

import json
import os
import sys
import threading
import urllib.request
import urllib.error
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Each entry becomes a clickable widget on the dashboard.
PROJECTS = [
    {"gid": "1214228966572515", "name": "Georgia Grown Market MSA"},
    {"gid": "1214228966572497", "name": "Mid Eastern MSA"},
    {"gid": "1214228966572546", "name": "Cohen's Retreat MSA"},
]

EST_FIELD = "Estimated time"             # stored in minutes
EXCLUDE_SECTIONS = {"completed"}         # status columns excluded from hour totals (case-insensitive)
JUNE_PREFIX = "2026-06"                  # time entries whose entered_on falls in this month
PORT = 8765
API = "https://app.asana.com/api/1.0"
PROJECT_NAMES = {p["gid"]: p["name"] for p in PROJECTS}

# In-memory cache so navigating between pages never re-hits the Asana API.
# Data is fetched once and reused until the user explicitly clicks Refresh.
CACHE = {"summaries": None, "detail": {}, "june_summaries": None, "june_detail": {}}
CACHE_LOCK = threading.Lock()


def get_token():
    tok = os.environ.get("ASANA_PAT")
    if tok:
        return tok.strip()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            val, _ = winreg.QueryValueEx(k, "ASANA_PAT")
            if val:
                return val.strip()
    except (ImportError, FileNotFoundError, OSError):
        pass
    sys.exit('ERROR: ASANA_PAT not found. Set it with:  setx ASANA_PAT "your_token"')


TOKEN = get_token()


def fetch_tasks(gid):
    """Return all tasks for a project (name + assignee + estimated time + section)."""
    fields = "name,assignee.name,custom_fields.name,custom_fields.number_value,memberships.section.name,memberships.project.gid"
    url = f"{API}/projects/{gid}/tasks?opt_fields={fields}&limit=100"
    tasks = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req) as resp:
            page = json.load(resp)
        tasks.extend(page.get("data", []))
        nxt = page.get("next_page")
        url = nxt["uri"] if nxt and nxt.get("uri") else None
    return tasks


def fetch_subtasks(task_gid):
    """Return subtasks for a task (name + assignee + estimated time + completed)."""
    fields = "name,assignee.name,completed,custom_fields.name,custom_fields.number_value"
    url = f"{API}/tasks/{task_gid}/subtasks?opt_fields={fields}&limit=100"
    subs = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req) as resp:
            page = json.load(resp)
        subs.extend(page.get("data", []))
        nxt = page.get("next_page")
        url = nxt["uri"] if nxt and nxt.get("uri") else None
    return subs


def task_minutes(t):
    for cf in t.get("custom_fields", []):
        if cf.get("name") == EST_FIELD and cf.get("number_value") is not None:
            return cf["number_value"]
    return 0


def section_name(t, gid):
    """The task's section (status column) within project `gid`, or ''."""
    fallback = ""
    for m in t.get("memberships", []):
        sec = (m.get("section") or {}).get("name")
        if not sec:
            continue
        if (m.get("project") or {}).get("gid") == gid:
            return sec
        fallback = fallback or sec
    return fallback


def is_excluded(t, gid):
    """True if the task sits in an excluded status column (e.g. Completed) of this project."""
    return section_name(t, gid).strip().lower() in EXCLUDE_SECTIONS


def build_task(t, gid):
    """Shape one task plus its (incomplete) subtasks for the drill-down list."""
    parent_min = task_minutes(t)
    subs, sub_min = [], 0
    for s in fetch_subtasks(t["gid"]):
        if s.get("completed"):
            continue  # completed subtasks are not shown or counted
        m = task_minutes(s)
        sub_min += m
        subs.append({
            "name": s.get("name", "(untitled)"),
            "assignee": (s.get("assignee") or {}).get("name") or "Unassigned",
            "hours": round(m / 60, 2),
        })
    return {
        "gid": t["gid"],
        "name": t.get("name", "(untitled)"),
        "assignee": (t.get("assignee") or {}).get("name") or "Unassigned",
        "hours": round(parent_min / 60, 2),   # parent's own estimate (attributed to parent assignee)
        "section": section_name(t, gid),
        "subtasks": subs,                     # each subtask attributed to its own assignee
    }


def project_detail(gid):
    tasks = [t for t in fetch_tasks(gid) if not is_excluded(t, gid)]
    # Fetch subtasks for all tasks concurrently to keep the drill-down snappy.
    with ThreadPoolExecutor(max_workers=8) as ex:
        detailed = list(ex.map(lambda t: build_task(t, gid), tasks))

    # Attribute each task's own hours to its assignee, and each subtask's hours to
    # the subtask's assignee (not the parent owner). counts = work items per assignee.
    totals, counts = {}, {}
    for d in detailed:
        totals[d["assignee"]] = totals.get(d["assignee"], 0) + d["hours"]
        counts[d["assignee"]] = counts.get(d["assignee"], 0) + 1
        for s in d["subtasks"]:
            totals[s["assignee"]] = totals.get(s["assignee"], 0) + s["hours"]
            counts[s["assignee"]] = counts.get(s["assignee"], 0) + 1
    ordered = sorted(totals, key=lambda n: totals[n], reverse=True)
    return {
        "gid": gid,
        "name": PROJECT_NAMES.get(gid, gid),
        "labels": ordered,
        "hours": [round(totals[n], 2) for n in ordered],
        "counts": [counts[n] for n in ordered],
        "ntasks": len(detailed),
        "tasks": detailed,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---- Cache layer: only hits Asana when refresh=True; otherwise serves cached data ----

def summary_from_detail(d):
    return {
        "gid": d["gid"],
        "name": d["name"],
        "ntasks": d["ntasks"],
        "hours": round(sum(d["hours"]), 2),
        "updated": d["updated"],
    }


def get_detail(gid, refresh=False):
    """Cached project detail. Hits Asana only when refresh or not yet cached."""
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["detail"].get(gid)
        if cached is not None:
            return cached
    data = project_detail(gid)
    with CACHE_LOCK:
        CACHE["detail"][gid] = data
        # Keep any cached dashboard summary in sync with this fresh detail.
        if CACHE["summaries"] is not None:
            CACHE["summaries"] = [
                summary_from_detail(data) if s["gid"] == gid else s
                for s in CACHE["summaries"]
            ]
    return data


def get_summaries(refresh=False):
    """Cached dashboard widgets. Builds from (cached) per-project detail."""
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["summaries"]
        if cached is not None:
            return cached
    out = [summary_from_detail(get_detail(p["gid"], refresh=refresh)) for p in PROJECTS]
    with CACHE_LOCK:
        CACHE["summaries"] = out
    return out


# ---- "Hours logged in June": actual time-tracking entries dated in JUNE_PREFIX ----

def fetch_time_entries(task_gid):
    fields = "duration_minutes,entered_on,created_by.name"
    url = f"{API}/tasks/{task_gid}/time_tracking_entries?opt_fields={fields}&limit=100"
    out = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req) as resp:
            page = json.load(resp)
        out.extend(page.get("data", []))
        nxt = page.get("next_page")
        url = nxt["uri"] if nxt and nxt.get("uri") else None
    return out


def fetch_subtasks_time(task_gid):
    fields = "name,actual_time_minutes"
    url = f"{API}/tasks/{task_gid}/subtasks?opt_fields={fields}&limit=100"
    out = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req) as resp:
            page = json.load(resp)
        out.extend(page.get("data", []))
        nxt = page.get("next_page")
        url = nxt["uri"] if nxt and nxt.get("uri") else None
    return out


def june_entries_for_item(item):
    """item = (task_name, task_gid). Return June time entries logged on it."""
    name, gid = item
    res = []
    for e in fetch_time_entries(gid):
        entered = e.get("entered_on") or ""
        if entered.startswith(JUNE_PREFIX):
            res.append({
                "entry_gid": e.get("gid"),
                "task": name,
                "by": (e.get("created_by") or {}).get("name") or "Unknown",
                "date": entered,
                "minutes": e.get("duration_minutes") or 0,
            })
    return res


def june_detail(gid):
    """Per-person hours logged in June for one project (tasks + subtasks)."""
    tfields = "name,actual_time_minutes,num_subtasks"
    url = f"{API}/projects/{gid}/tasks?opt_fields={tfields}&limit=100"
    tasks = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req) as resp:
            page = json.load(resp)
        tasks.extend(page.get("data", []))
        nxt = page.get("next_page")
        url = nxt["uri"] if nxt and nxt.get("uri") else None

    # Only query time entries for items that actually have logged time.
    # Dedupe by task gid: a subtask added directly to the project would otherwise be
    # reached twice (project task list + parent's subtasks) and double-counted.
    cand_by_gid = {}
    for t in tasks:
        if (t.get("actual_time_minutes") or 0) > 0:
            cand_by_gid[t["gid"]] = t.get("name", "(untitled)")
    parents = [t["gid"] for t in tasks if (t.get("num_subtasks") or 0) > 0]
    with ThreadPoolExecutor(max_workers=8) as ex:
        for subs in ex.map(fetch_subtasks_time, parents):
            for s in subs:
                if (s.get("actual_time_minutes") or 0) > 0:
                    cand_by_gid[s["gid"]] = s.get("name", "(untitled)")
    candidates = [(name, gid) for gid, name in cand_by_gid.items()]

    # Collect entries, deduping by entry gid as a final guard against any double-pull.
    seen, entries = set(), []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for lst in ex.map(june_entries_for_item, candidates):
            for e in lst:
                if e["entry_gid"] and e["entry_gid"] in seen:
                    continue
                if e["entry_gid"]:
                    seen.add(e["entry_gid"])
                entries.append(e)

    totals, counts = {}, {}
    for e in entries:
        totals[e["by"]] = totals.get(e["by"], 0) + e["minutes"]
        counts[e["by"]] = counts.get(e["by"], 0) + 1
    ordered = sorted(totals, key=lambda n: totals[n], reverse=True)
    entries.sort(key=lambda e: e["date"])
    return {
        "gid": gid,
        "name": PROJECT_NAMES.get(gid, gid),
        "labels": ordered,
        "hours": [round(totals[n] / 60, 2) for n in ordered],
        "counts": [counts[n] for n in ordered],
        "total_hours": round(sum(totals.values()) / 60, 2),
        "nentries": len(entries),
        "entries": [
            {"task": e["task"], "by": e["by"], "date": e["date"], "hours": round(e["minutes"] / 60, 2)}
            for e in entries
        ],
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def june_summary_from_detail(d):
    return {
        "gid": d["gid"],
        "name": d["name"],
        "hours": d["total_hours"],
        "nentries": d["nentries"],
        "updated": d["updated"],
    }


def get_june_detail(gid, refresh=False):
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["june_detail"].get(gid)
        if cached is not None:
            return cached
    data = june_detail(gid)
    with CACHE_LOCK:
        CACHE["june_detail"][gid] = data
        if CACHE["june_summaries"] is not None:
            CACHE["june_summaries"] = [
                june_summary_from_detail(data) if s["gid"] == gid else s
                for s in CACHE["june_summaries"]
            ]
    return data


def get_june_summaries(refresh=False):
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["june_summaries"]
        if cached is not None:
            return cached
    out = [june_summary_from_detail(get_june_detail(p["gid"], refresh=refresh)) for p in PROJECTS]
    with CACHE_LOCK:
        CACHE["june_summaries"] = out
    return out


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Asana Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { --blue:#5b9bd5; --blue-d:#3f7cb5; --green:#3aa76d; --green-d:#2c8757; }
  body { font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0; background:#f5f6f8; color:#1e1f21; }
  .wrap { max-width:1040px; margin:32px auto; padding:0 20px; }
  h1 { font-size:22px; margin:0 0 2px; }
  .sub { color:#6b6f76; font-size:13px; margin:0 0 24px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:18px; }
  .card { background:#fff; border-radius:12px; padding:20px; box-shadow:0 1px 4px rgba(0,0,0,.08);
          cursor:pointer; transition:transform .08s, box-shadow .08s; border:1px solid transparent; }
  .card:hover { transform:translateY(-2px); box-shadow:0 6px 18px rgba(0,0,0,.12); border-color:var(--blue); }
  .card h3 { margin:0 0 14px; font-size:16px; }
  .stats { display:flex; gap:24px; }
  .stat .n { font-size:24px; font-weight:600; color:var(--blue-d); }
  .stat .l { font-size:11px; color:#8a8f98; text-transform:uppercase; letter-spacing:.04em; }
  .card .go { margin-top:14px; font-size:12px; color:var(--blue-d); }
  .card-updated { margin-top:8px; font-size:11px; color:#a0a4ab; }
  .section-h { font-size:16px; margin:30px 0 12px; padding-top:6px; border-top:1px solid #e6e9ec; }
  .card.june:hover { border-color:var(--green); }
  .card.june .stat .n, .card.june .go { color:var(--green-d); }
  /* detail */
  .head { display:flex; align-items:center; justify-content:space-between; gap:16px; }
  .left { display:flex; align-items:center; gap:14px; }
  .crumbs { font-size:15px; display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
  .crumb { color:var(--blue-d); text-decoration:none; cursor:pointer; }
  .crumb:hover { text-decoration:underline; }
  .crumb-sep { color:#b9c0c8; }
  .crumb-cur { font-weight:600; }
  button.btn { background:var(--blue); color:#fff; border:0; border-radius:8px; padding:9px 16px; font-size:14px; cursor:pointer; }
  button.btn:hover { background:var(--blue-d); }
  button.btn:disabled { background:#b9c7d4; cursor:default; }
  .back { background:#eef1f4; color:#1e1f21; }
  .back:hover { background:#e0e4e9; }
  .panel { background:#fff; border-radius:12px; padding:26px 30px 36px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
  #updated { font-size:12px; color:#8a8f98; }
  .chart-box { position:relative; height:480px; margin-top:10px; }
  .muted { color:#8a8f98; }
  .hint { font-size:12px; color:#8a8f98; margin-top:8px; }
  /* drill-down task list */
  .drill-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:6px; }
  .drill-head h2 { font-size:17px; margin:0; }
  .drill-total { font-size:13px; color:#6b6f76; }
  table.tasks { width:100%; border-collapse:collapse; font-size:14px; }
  table.tasks th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.04em;
                   color:#8a8f98; border-bottom:2px solid #eef1f4; padding:8px 10px; }
  table.tasks td { padding:9px 10px; border-bottom:1px solid #f0f2f4; vertical-align:top; }
  table.tasks tr.parent td { font-weight:600; }
  table.tasks tr.sub td { font-weight:400; color:#4b4f56; }
  .badge { display:inline-block; font-size:11px; padding:2px 8px; border-radius:10px; background:#eef1f4; color:#4b4f56; }
  .sub-name { padding-left:22px; position:relative; }
  .sub-name::before { content:'↳'; position:absolute; left:6px; color:#b9c0c8; }
  .done { text-decoration:line-through; color:#a0a4ab; }
  .hours { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
</style>
</head>
<body>
<div class="wrap" id="app"></div>
<script>
const app = document.getElementById('app');
let chart = null;

function fmtErr(e){ return '<p class="muted">Error: '+e+'</p>'; }
function esc(s){ return (s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function h2(x){ return Number(x || 0).toFixed(2); }   // always two decimals: 40.00, 40.50, 40.75
function setCrumbs(items) {
  // items: [{label, fn?}] — entries with fn render as links; the last/plain one is the current page.
  const el = document.getElementById('crumbs');
  if (!el) return;
  el.innerHTML = items.map((it, i) => {
    const last = i === items.length - 1;
    return (last || !it.fn)
      ? `<span class="crumb-cur">${esc(it.label)}</span>`
      : `<a href="#" class="crumb" data-ci="${i}">${esc(it.label)}</a>`;
  }).join('<span class="crumb-sep">›</span>');
  el.querySelectorAll('a.crumb').forEach(a => {
    a.onclick = (e) => { e.preventDefault(); items[+a.dataset.ci].fn(); };
  });
}

async function renderDashboard() {
  if (chart) { chart.destroy(); chart = null; }
  app.innerHTML = `
    <div class="head">
      <h1>Asana Dashboard</h1>
      <button class="btn" id="dash-refresh">Refresh</button>
    </div>
    <p class="sub">Click a widget to drill in. Data is cached — click Refresh to re-pull from Asana.</p>
    <h2 class="section-h">Estimated hours (excludes Completed)</h2>
    <div class="grid" id="grid"><p class="muted">Loading widgets…</p></div>
    <h2 class="section-h">Hours logged in June</h2>
    <div class="grid" id="grid-june"><p class="muted">Loading widgets…</p></div>`;
  const btn = document.getElementById('dash-refresh');

  async function loadEstimated(refresh) {
    const grid = document.getElementById('grid');
    try {
      const widgets = await (await fetch('/api/projects' + (refresh ? '?refresh=1' : ''))).json();
      grid.innerHTML = '';
      widgets.forEach(w => {
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `<h3>${esc(w.name)}</h3>
          <div class="stats">
            <div class="stat"><div class="n">${h2(w.hours)}</div><div class="l">Est. Hours</div></div>
            <div class="stat"><div class="n">${w.ntasks}</div><div class="l">Tasks</div></div>
          </div>
          <div class="go">Open chart →</div>
          <div class="card-updated">Updated ${esc(w.updated)}</div>`;
        card.addEventListener('click', () => { location.hash = '#/p/' + w.gid; });
        grid.appendChild(card);
      });
    } catch (e) { grid.innerHTML = fmtErr(e); }
  }

  async function loadJune(refresh) {
    const grid = document.getElementById('grid-june');
    try {
      const widgets = await (await fetch('/api/june' + (refresh ? '?refresh=1' : ''))).json();
      grid.innerHTML = '';
      widgets.forEach(w => {
        const card = document.createElement('div');
        card.className = 'card june';
        card.innerHTML = `<h3>${esc(w.name)}</h3>
          <div class="stats">
            <div class="stat"><div class="n">${h2(w.hours)}</div><div class="l">Hours (June)</div></div>
            <div class="stat"><div class="n">${w.nentries}</div><div class="l">Entries</div></div>
          </div>
          <div class="go">Open chart →</div>
          <div class="card-updated">Updated ${esc(w.updated)}</div>`;
        card.addEventListener('click', () => { location.hash = '#/june/' + w.gid; });
        grid.appendChild(card);
      });
    } catch (e) { grid.innerHTML = fmtErr(e); }
  }

  async function loadAll(refresh) {
    btn.disabled = true; btn.textContent = refresh ? 'Refreshing…' : 'Refresh';
    await Promise.all([loadEstimated(refresh), loadJune(refresh)]);
    btn.disabled = false; btn.textContent = 'Refresh';
  }
  btn.onclick = () => loadAll(true);
  loadAll(false);   // navigation = cached
}

async function renderDetail(gid) {
  app.innerHTML = `
    <div class="head">
      <div id="crumbs" class="crumbs">Loading…</div>
      <button class="btn" id="refresh">Refresh</button>
    </div>
    <p class="sub" id="sub"></p>
    <div class="panel">
      <div id="view"></div>
      <p id="updated">Loading…</p>
    </div>`;
  const toDash = () => { location.hash = ''; };
  const btn = document.getElementById('refresh');
  let detailData = null;

  function showChart() {
    const d = detailData;
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:d.name}]);
    document.getElementById('view').innerHTML =
      '<div class="chart-box"><canvas id="chart"></canvas></div>' +
      '<p class="hint">Tip: click a bar to see the tasks (and subtasks) that make up that assignee.</p>';
    if (chart) { chart.destroy(); chart = null; }
    chart = new Chart(document.getElementById('chart'), {
      type:'bar',
      data:{ labels:d.labels, datasets:[{ label:'Estimated hours', data:d.hours,
        backgroundColor:'#5b9bd5', borderColor:'#3f7cb5', borderWidth:1, _counts:d.counts }] },
      options:{ responsive:true, maintainAspectRatio:false,
        onClick:(evt, els) => { if (els.length) showTasks(d.labels[els[0].index]); },
        onHover:(evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins:{ legend:{display:false}, tooltip:{ callbacks:{
          label: ctx => `Estimated: ${h2(ctx.parsed.y)} h`,
          afterLabel: ctx => `Items: ${ctx.dataset._counts[ctx.dataIndex]} · click to view` } } },
        scales:{ x:{ title:{display:true,text:'Assignee'}, ticks:{ callback:(v,i) => [d.labels[i], h2(d.hours[i]) + ' h'] } },
                 y:{ beginAtZero:true, title:{display:true,text:'Estimated hours'}, ticks:{ callback:v => h2(v) } } } }
    });
  }

  function showTasks(assignee) {
    if (chart) { chart.destroy(); chart = null; }
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:detailData.name, fn:showChart}, {label:assignee}]);
    const all = detailData.tasks;
    let rows = '', total = 0, items = 0;

    function subRow(s, context) {
      total += s.hours; items++;
      return `<tr class="sub"><td class="sub-name">${esc(s.name)}${context}</td>` +
        `<td class="muted">subtask</td>` +
        `<td class="hours">${s.hours ? h2(s.hours) + ' h' : '—'}</td></tr>`;
    }

    // 1. Tasks owned by this assignee, with only THEIR subtasks nested underneath.
    all.filter(t => t.assignee === assignee).forEach(t => {
      total += t.hours; items++;
      rows += `<tr class="parent"><td>${esc(t.name)}</td>` +
        `<td><span class="badge">${esc(t.section) || '—'}</span></td>` +
        `<td class="hours">${h2(t.hours)} h</td></tr>`;
      t.subtasks.filter(s => s.assignee === assignee).forEach(s => { rows += subRow(s, ''); });
    });

    // 2. This assignee's subtasks that live under someone else's task.
    all.filter(t => t.assignee !== assignee).forEach(t => {
      t.subtasks.filter(s => s.assignee === assignee).forEach(s => {
        rows += subRow(s, ` <span class="muted">(under "${esc(t.name)}" · ${esc(t.assignee)})</span>`);
      });
    });

    if (!items) rows = '<tr><td colspan="3" class="muted">No items.</td></tr>';
    document.getElementById('view').innerHTML =
      `<div class="drill-head">
         <h2>${esc(assignee)}</h2>
         <button class="btn back" id="tochart">← Back to chart</button>
       </div>
       <p class="drill-total">${items} item(s) · ${h2(total)} estimated hours (excludes Completed)</p>
       <table class="tasks">
         <thead><tr><th>Task / Subtask</th><th>Type / Status</th><th class="hours">Est. hours</th></tr></thead>
         <tbody>${rows}</tbody>
       </table>`;
    document.getElementById('tochart').onclick = showChart;
  }

  async function load(refresh) {
    btn.disabled = true; btn.textContent = refresh ? 'Refreshing…' : 'Refresh';
    try {
      detailData = await (await fetch('/api/project/' + gid + (refresh ? '?refresh=1' : ''))).json();
      document.getElementById('sub').textContent = `Estimated hours per assignee · ${detailData.ntasks} tasks (excludes Completed)`;
      document.getElementById('updated').textContent = 'Last updated: ' + detailData.updated + ' (cached — click Refresh to re-pull)';
      showChart();
    } catch (e) { document.getElementById('updated').innerHTML = fmtErr(e); }
    finally { btn.disabled = false; btn.textContent = 'Refresh'; }
  }
  btn.onclick = () => load(true);
  load(false);   // navigation = cached
}

async function renderJuneDetail(gid) {
  app.innerHTML = `
    <div class="head">
      <div id="crumbs" class="crumbs">Loading…</div>
      <button class="btn" id="refresh">Refresh</button>
    </div>
    <p class="sub" id="sub"></p>
    <div class="panel">
      <div id="view"></div>
      <p id="updated">Loading…</p>
    </div>`;
  const toDash = () => { location.hash = ''; };
  const btn = document.getElementById('refresh');
  let data = null;

  function showChart() {
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:data.name + ' · June'}]);
    document.getElementById('view').innerHTML =
      '<div class="chart-box"><canvas id="chart"></canvas></div>' +
      '<p class="hint">Tip: click a bar to see the individual time entries that person logged in June.</p>';
    if (chart) { chart.destroy(); chart = null; }
    chart = new Chart(document.getElementById('chart'), {
      type:'bar',
      data:{ labels:data.labels, datasets:[{ label:'Hours logged (June)', data:data.hours,
        backgroundColor:'#3aa76d', borderColor:'#2c8757', borderWidth:1, _counts:data.counts }] },
      options:{ responsive:true, maintainAspectRatio:false,
        onClick:(evt, els) => { if (els.length) showEntries(data.labels[els[0].index]); },
        onHover:(evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins:{ legend:{display:false}, tooltip:{ callbacks:{
          label: ctx => `Logged: ${h2(ctx.parsed.y)} h`,
          afterLabel: ctx => `Entries: ${ctx.dataset._counts[ctx.dataIndex]} · click to view` } } },
        scales:{ x:{ title:{display:true,text:'Logged by'}, ticks:{ callback:(v,i) => [data.labels[i], h2(data.hours[i]) + ' h'] } },
                 y:{ beginAtZero:true, title:{display:true,text:'Hours logged in June'}, ticks:{ callback:v => h2(v) } } } }
    });
  }

  function showEntries(person) {
    if (chart) { chart.destroy(); chart = null; }
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:data.name + ' · June', fn:showChart}, {label:person}]);
    const rows_ = data.entries.filter(e => e.by === person);
    const total = rows_.reduce((a, e) => a + e.hours, 0);
    let rows = '';
    rows_.forEach(e => {
      rows += `<tr><td>${esc(e.date)}</td><td>${esc(e.task)}</td><td class="hours">${h2(e.hours)} h</td></tr>`;
    });
    if (!rows_.length) rows = '<tr><td colspan="3" class="muted">No entries.</td></tr>';
    document.getElementById('view').innerHTML =
      `<div class="drill-head">
         <h2>${esc(person)}</h2>
         <button class="btn back" id="tochart">← Back to chart</button>
       </div>
       <p class="drill-total">${rows_.length} entr${rows_.length === 1 ? 'y' : 'ies'} · ${total.toFixed(2)} hours logged in June</p>
       <table class="tasks">
         <thead><tr><th>Date</th><th>Task</th><th class="hours">Hours</th></tr></thead>
         <tbody>${rows}</tbody>
       </table>`;
    document.getElementById('tochart').onclick = showChart;
  }

  async function load(refresh) {
    btn.disabled = true; btn.textContent = refresh ? 'Refreshing…' : 'Refresh';
    try {
      data = await (await fetch('/api/june/' + gid + (refresh ? '?refresh=1' : ''))).json();
      document.getElementById('sub').textContent = `Hours logged in June per person · ${data.nentries} time entries · ${h2(data.total_hours)} h total`;
      document.getElementById('updated').textContent = 'Last updated: ' + data.updated + ' (cached — click Refresh to re-pull)';
      showChart();
    } catch (e) { document.getElementById('updated').innerHTML = fmtErr(e); }
    finally { btn.disabled = false; btn.textContent = 'Refresh'; }
  }
  btn.onclick = () => load(true);
  load(false);
}

function route() {
  if (chart) { chart.destroy(); chart = null; }
  let m = location.hash.match(/^#\/june\/(\d+)/);
  if (m) return renderJuneDetail(m[1]);
  m = location.hash.match(/^#\/p\/(\d+)/);
  if (m) return renderDetail(m[1]);
  renderDashboard();
}
window.addEventListener('hashchange', route);
route();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = self.path.split("?")
        path = parts[0]
        refresh = "refresh=1" in (parts[1] if len(parts) > 1 else "")
        try:
            if path == "/api/projects":
                return self._json(200, get_summaries(refresh=refresh))
            if path == "/api/june":
                return self._json(200, get_june_summaries(refresh=refresh))
            if path.startswith("/api/june/"):
                gid = path.rsplit("/", 1)[-1]
                return self._json(200, get_june_detail(gid, refresh=refresh))
            if path.startswith("/api/project/"):
                gid = path.rsplit("/", 1)[-1]
                return self._json(200, get_detail(gid, refresh=refresh))
            if path == "/" or path.startswith("/index"):
                return self._send(200, "text/html; charset=utf-8", PAGE.encode())
            self._send(404, "text/plain", b"Not found")
        except urllib.error.HTTPError as e:
            self._json(502, {"error": f"Asana {e.code}"})

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # quiet


def main():
    url = f"http://localhost:{PORT}"
    print(f"Asana dashboard running at {url}  (Ctrl+C to stop)")
    webbrowser.open(url)
    ThreadingHTTPServer(("localhost", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
