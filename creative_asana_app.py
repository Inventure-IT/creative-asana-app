#!/usr/bin/env python3
"""
Asana Widget Dashboard.

A self-contained local app. Starts a small web server that serves a single-page
dashboard of project "widgets". Click a widget to drill into a detail view with a
per-assignee estimated-hours bar chart, a Refresh button, and a "last updated" time.

Run:  python creative_asana_app.py     (opens http://localhost:8765)

Add more widgets by appending to PROJECTS below.
Token lookup: ASANA_PAT env var, falling back to the Windows User env var (registry).
"""

import json
import os
import sys
import threading
import urllib.request
import urllib.error
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Each entry becomes a clickable widget on the dashboard.
# "cap" (optional) is the project's monthly hour capacity, shown on the widgets.
PROJECTS = [
    {"gid": "1214228966572515", "name": "Georgia Grown Market MSA", "cap": 70},
    {"gid": "1214228966572497", "name": "Mid Eastern MSA", "cap": 40},
    {"gid": "1214228966572546", "name": "Cohen's Retreat MSA", "cap": 40},
    {"gid": "1214228966572508", "name": "Firebird MSA", "cap": 15},
    {"gid": "1214228966572503", "name": "Myrick Marine"},
    {"gid": "1214229029715234", "name": "Savannah Bee: Zendesk"},
    {"gid": "1214228966572536", "name": "CMD: Concierge Clinics"},
    {"gid": "1214228966572531", "name": "CMD: Pathologic"},
    {"gid": "1214228966572526", "name": "CMD: Products"},
    {"gid": "1214228966572521", "name": "Georgia Skin & Cancer Clinic"},
    {"gid": "1214228966572541", "name": "DocSmith.md MSA", "cap": 24},
    {"gid": "1214228966572551", "name": "Savannah Camellia Fest 2026"},
    {"gid": "1214755322546416", "name": "Project Twilight"},
    {"gid": "1214228966572578", "name": "Claude: Discovery and Engineering"},
    {"gid": "1214228966572573", "name": "Autotask Reporting"},
    {"gid": "1214228966572568", "name": "Brain Dump"},
    {"gid": "1214228966572563", "name": "Internal IIT Backlog"},
    {"gid": "1216154609521581", "name": "NuNu"},
    {"gid": "1216640931651593", "name": "Ross Wood Website Redesign", "cap": 30},   # one-month cap, not a recurring MSA
]

# Budget groups: several projects that share ONE combined monthly capacity.
# Each member project still appears individually in every other tab; the group only
# adds a single combined bucket to the Monthly Capacity tab (summing its members'
# billable hours against `cap`). Members are referenced by project gid.
GROUPS = [
    {"name": "CMD", "cap": 244, "gids": [
        "1214228966572536",   # CMD: Concierge Clinics
        "1214228966572531",   # CMD: Pathologic
        "1214228966572526",   # CMD: Products
        "1216154609521581",   # NuNu
    ]},
]

EST_FIELD = "Estimated time"             # stored in minutes
EXCLUDE_SECTIONS = {"completed"}         # status columns excluded from hour totals (case-insensitive)
DEFAULT_START = "2026-06-01"             # default date range (inclusive) for the "Hours logged" view
DEFAULT_END = "2026-06-30"               #   entries with start <= entered_on <= end are counted
ASSIGNEE_HOURS_CAP = 128                 # estimated hours each assignee is expected to fill (all projects)
# Team Capacity chart shows only these people (sorted by remaining), with Unassigned pinned far right.
TEAM_MEMBERS = ["Miranda Osborn", "Linh Trinh", "Julia Reeves", "Grant Roach"]
PORT = 8765
API = "https://app.asana.com/api/1.0"
PROJECT_NAMES = {p["gid"]: p["name"] for p in PROJECTS}
PROJECT_CAPS = {p["gid"]: p.get("cap") for p in PROJECTS}   # monthly hour capacity, or None

# In-memory cache so navigating between pages never re-hits the Asana API.
# Data is fetched once and reused until the user explicitly clicks Refresh.
# The "hours logged" caches are keyed by month: {month: ...}.
CACHE = {"summaries": None, "detail": {}, "june_summaries": {}, "june_detail": {}}
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

# Two long-lived, bounded thread pools shared across all requests.
#  - LEAF_POOL runs the many small API calls (subtasks, time entries). Capping it
#    keeps total concurrent connections to Asana within its limits.
#  - PROJECT_POOL drives whole projects concurrently; its workers only ever block
#    waiting on LEAF_POOL futures, so the two pools can't deadlock each other.
LEAF_POOL = ThreadPoolExecutor(max_workers=16)
PROJECT_POOL = ThreadPoolExecutor(max_workers=8)


def fetch_tasks(gid):
    """Return all tasks for a project (name + assignee + completed + estimated/actual time + section)."""
    fields = "name,assignee.name,completed,actual_time_minutes,custom_fields.name,custom_fields.number_value,memberships.section.name,memberships.project.gid"
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
    """Return subtasks for a task (name + assignee + estimated/actual time + completed)."""
    fields = "name,assignee.name,completed,actual_time_minutes,custom_fields.name,custom_fields.number_value"
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


def actual_minutes(t):
    """Total tracked time on a task/subtask, in minutes."""
    return t.get("actual_time_minutes") or 0


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
            "actual": round(actual_minutes(s) / 60, 2),
        })
    return {
        "gid": t["gid"],
        "name": t.get("name", "(untitled)"),
        "assignee": (t.get("assignee") or {}).get("name") or "Unassigned",
        "hours": round(parent_min / 60, 2),   # parent's own estimate (attributed to parent assignee)
        "actual": round(actual_minutes(t) / 60, 2),   # parent's own tracked time
        "section": section_name(t, gid),
        "subtasks": subs,                     # each subtask attributed to its own assignee
    }


def project_detail(gid):
    # Drop checked-off tasks and anything in an excluded (e.g. Completed) section.
    tasks = [t for t in fetch_tasks(gid) if not t.get("completed") and not is_excluded(t, gid)]
    # Fetch subtasks for all tasks concurrently (shared pool) to keep the drill-down snappy.
    detailed = list(LEAF_POOL.map(lambda t: build_task(t, gid), tasks))

    # Attribute each task's own hours to its assignee, and each subtask's hours to
    # the subtask's assignee (not the parent owner). Estimated and actual time are
    # attributed the same way so remaining = estimated - actual lines up per person.
    # counts = work items per assignee.
    totals, actuals, counts = {}, {}, {}
    for d in detailed:
        totals[d["assignee"]] = totals.get(d["assignee"], 0) + d["hours"]
        actuals[d["assignee"]] = actuals.get(d["assignee"], 0) + d["actual"]
        counts[d["assignee"]] = counts.get(d["assignee"], 0) + 1
        for s in d["subtasks"]:
            totals[s["assignee"]] = totals.get(s["assignee"], 0) + s["hours"]
            actuals[s["assignee"]] = actuals.get(s["assignee"], 0) + s["actual"]
            counts[s["assignee"]] = counts.get(s["assignee"], 0) + 1
    ordered = sorted(totals, key=lambda n: totals[n], reverse=True)
    return {
        "gid": gid,
        "name": PROJECT_NAMES.get(gid, gid),
        "labels": ordered,
        "hours": [round(totals[n], 2) for n in ordered],
        "actual_hours": [round(actuals[n], 2) for n in ordered],
        "counts": [counts[n] for n in ordered],
        "ntasks": len(detailed),
        "tasks": detailed,
        "updated": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
    }


# ---- Cache layer: only hits Asana when refresh=True; otherwise serves cached data ----

def summary_from_detail(d):
    return {
        "gid": d["gid"],
        "name": d["name"],
        "ntasks": d["ntasks"],
        "hours": round(sum(d["hours"]), 2),
        "cap": PROJECT_CAPS.get(d["gid"]),
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
    # Build every project's detail concurrently instead of one-at-a-time.
    details = PROJECT_POOL.map(lambda p: get_detail(p["gid"], refresh=refresh), PROJECTS)
    out = [summary_from_detail(d) for d in details]
    with CACHE_LOCK:
        CACHE["summaries"] = out
    return out


def assignee_project_tasks(d, name):
    """The task/subtask rows assigned to `name` within one project detail `d`,
    each with estimated / actual / remaining hours and status (section)."""
    rows = []
    for t in d["tasks"]:
        if t["assignee"] == name:
            rows.append({
                "name": t["name"], "type": "task", "status": t["section"],
                "estimated": t["hours"], "actual": t["actual"],
                "remaining": round(t["hours"] - t["actual"], 2), "context": "",
            })
        for s in t["subtasks"]:
            if s["assignee"] == name:
                rows.append({
                    "name": s["name"], "type": "subtask", "status": t["section"],
                    "estimated": s["hours"], "actual": s["actual"],
                    "remaining": round(s["hours"] - s["actual"], 2),
                    # note the parent when this subtask lives under someone else's task
                    "context": "" if t["assignee"] == name else f'under "{t["name"]}" · {t["assignee"]}',
                })
    return rows


def get_assignee_load(refresh=False):
    """Remaining hours (estimated - actual) per assignee across ALL projects, vs. the cap.

    Estimated and actual are both attributed to each item's assignee, so the chart's
    `hours` is the work each person still has left to do. Reuses the cached per-project
    detail, so it adds no Asana calls when the projects are already loaded.
    """
    details = list(PROJECT_POOL.map(lambda p: get_detail(p["gid"], refresh=refresh), PROJECTS))
    est, act, counts, breakdown = {}, {}, {}, {}
    for d in details:
        for name, e, a, cnt in zip(d["labels"], d["hours"], d["actual_hours"], d["counts"]):
            est[name] = est.get(name, 0) + e
            act[name] = act.get(name, 0) + a
            counts[name] = counts.get(name, 0) + cnt
            if e or a:
                breakdown.setdefault(name, []).append({
                    "project": d["name"], "estimated": round(e, 2),
                    "actual": round(a, 2), "remaining": round(e - a, 2),
                    "tasks": assignee_project_tasks(d, name),
                })
    # Only the named team members (sorted by remaining), with Unassigned pinned far right.
    ordered = sorted((n for n in TEAM_MEMBERS if n in est),
                     key=lambda n: est[n] - act[n], reverse=True)
    if "Unassigned" in est:
        ordered.append("Unassigned")
    return {
        "cap": ASSIGNEE_HOURS_CAP,
        "labels": ordered,
        "hours": [round(est[n] - act[n], 2) for n in ordered],   # remaining = estimated - actual
        "estimated": [round(est[n], 2) for n in ordered],
        "actual": [round(act[n], 2) for n in ordered],
        "counts": [counts[n] for n in ordered],
        "breakdown": {n: breakdown.get(n, []) for n in ordered},
        "updated": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
    }


# ---- "Hours logged": actual time-tracking entries dated in a given month (YYYY-MM) ----

def fetch_time_entries(task_gid):
    fields = "duration_minutes,entered_on,created_by.name,billable_status"
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
    fields = "name,actual_time_minutes,completed,completed_at"
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
    """item = (task_name, task_gid, start, end). Return time entries logged in [start, end]."""
    name, gid, start, end = item
    res = []
    for e in fetch_time_entries(gid):
        entered = e.get("entered_on") or ""
        if entered and start <= entered <= end:
            res.append({
                "entry_gid": e.get("gid"),
                "task": name,
                "by": (e.get("created_by") or {}).get("name") or "Unknown",
                "date": entered,
                "minutes": e.get("duration_minutes") or 0,
                # Only entries explicitly marked "billable" count toward budgets.
                # "nonBillable" and "notApplicable" (and unset) are treated as unbillable.
                "billable": e.get("billable_status") == "billable",
            })
    return res


def june_detail(gid, start=DEFAULT_START, end=DEFAULT_END):
    """Per-person hours logged in [start, end] for one project (tasks + subtasks)."""
    tfields = "name,actual_time_minutes,num_subtasks,completed,completed_at"
    url = f"{API}/projects/{gid}/tasks?opt_fields={tfields}&limit=100"
    tasks = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req) as resp:
            page = json.load(resp)
        tasks.extend(page.get("data", []))
        nxt = page.get("next_page")
        url = nxt["uri"] if nxt and nxt.get("uri") else None

    # Tasks/subtasks completed within the date range (by completed_at), deduped by gid.
    completed_dates = {}
    def note_completed(item):
        if item.get("completed") and item.get("completed_at"):
            day = item["completed_at"][:10]
            if start <= day <= end:
                completed_dates[item["gid"]] = day

    # Only query time entries for items that actually have logged time.
    # Dedupe by task gid: a subtask added directly to the project would otherwise be
    # reached twice (project task list + parent's subtasks) and double-counted.
    cand_by_gid = {}
    for t in tasks:
        note_completed(t)
        if (t.get("actual_time_minutes") or 0) > 0:
            cand_by_gid[t["gid"]] = t.get("name", "(untitled)")
    parents = [t["gid"] for t in tasks if (t.get("num_subtasks") or 0) > 0]
    for subs in LEAF_POOL.map(fetch_subtasks_time, parents):
        for s in subs:
            note_completed(s)
            if (s.get("actual_time_minutes") or 0) > 0:
                cand_by_gid[s["gid"]] = s.get("name", "(untitled)")
    candidates = [(name, gid, start, end) for gid, name in cand_by_gid.items()]

    # Collect entries, deduping by entry gid as a final guard against any double-pull.
    seen, entries = set(), []
    for lst in LEAF_POOL.map(june_entries_for_item, candidates):
        for e in lst:
            if e["entry_gid"] and e["entry_gid"] in seen:
                continue
            if e["entry_gid"]:
                seen.add(e["entry_gid"])
            entries.append(e)

    # Per person: total minutes, plus the billable / unbillable split. Only entries
    # flagged billable count toward project budgets; everything else is unbillable.
    totals, counts, bill, unbill = {}, {}, {}, {}
    for e in entries:
        totals[e["by"]] = totals.get(e["by"], 0) + e["minutes"]
        counts[e["by"]] = counts.get(e["by"], 0) + 1
        bucket = bill if e["billable"] else unbill
        bucket[e["by"]] = bucket.get(e["by"], 0) + e["minutes"]
    ordered = sorted(totals, key=lambda n: totals[n], reverse=True)
    entries.sort(key=lambda e: e["date"])
    return {
        "gid": gid,
        "name": PROJECT_NAMES.get(gid, gid),
        "start": start,
        "end": end,
        "labels": ordered,
        "hours": [round(totals[n] / 60, 2) for n in ordered],
        "billable": [round(bill.get(n, 0) / 60, 2) for n in ordered],
        "unbillable": [round(unbill.get(n, 0) / 60, 2) for n in ordered],
        "counts": [counts[n] for n in ordered],
        "total_hours": round(sum(totals.values()) / 60, 2),
        "billable_hours": round(sum(bill.values()) / 60, 2),
        "unbillable_hours": round(sum(unbill.values()) / 60, 2),
        "completed": len(completed_dates),
        "nentries": len(entries),
        "entries": [
            {"task": e["task"], "by": e["by"], "date": e["date"],
             "hours": round(e["minutes"] / 60, 2), "billable": e["billable"]}
            for e in entries
        ],
        "updated": datetime.now().strftime("%Y-%m-%d %I:%M:%S %p"),
    }


def june_summary_from_detail(d):
    return {
        "gid": d["gid"],
        "name": d["name"],
        "start": d["start"],
        "end": d["end"],
        "hours": d["total_hours"],
        "billable_hours": d["billable_hours"],
        "unbillable_hours": d["unbillable_hours"],
        "completed": d["completed"],
        "nentries": d["nentries"],
        "cap": PROJECT_CAPS.get(d["gid"]),
        "updated": d["updated"],
    }


def get_june_detail(gid, refresh=False, start=DEFAULT_START, end=DEFAULT_END):
    """Cached per-project logged-hours detail for a date range. Cache key = 'start:end'."""
    key = f"{start}:{end}"
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["june_detail"].get(key, {}).get(gid)
        if cached is not None:
            return cached
    data = june_detail(gid, start, end)
    with CACHE_LOCK:
        CACHE["june_detail"].setdefault(key, {})[gid] = data
        # Keep this range's cached summary in sync with the fresh detail.
        rsum = CACHE["june_summaries"].get(key)
        if rsum is not None:
            CACHE["june_summaries"][key] = [
                june_summary_from_detail(data) if s["gid"] == gid else s
                for s in rsum
            ]
    return data


def get_june_summaries(refresh=False, start=DEFAULT_START, end=DEFAULT_END):
    key = f"{start}:{end}"
    if not refresh:
        with CACHE_LOCK:
            cached = CACHE["june_summaries"].get(key)
        if cached is not None:
            return cached
    # Build every project's logged-hours detail concurrently instead of one-at-a-time.
    details = PROJECT_POOL.map(lambda p: get_june_detail(p["gid"], refresh=refresh, start=start, end=end), PROJECTS)
    out = [june_summary_from_detail(d) for d in details]
    with CACHE_LOCK:
        CACHE["june_summaries"][key] = out
    return out


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark">
<title>Asana Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { --blue:#6aa9e0; --blue-d:#9cc7f0; --green:#4cc085; --green-d:#6cd49d; --red:#e26b66;
          --bg:#12151a; --panel:#2a2f38; --panel2:#353b45; --border:#3c4350;
          --text:#edeff2; --muted:#a3aab4; --faint:#737b86; }
  body { font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0; background:var(--bg); color:var(--text); }
  .wrap { max-width:1040px; margin:32px auto; padding:0 20px; }
  h1 { font-size:22px; margin:0 0 2px; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 24px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:18px; }
  .card { background:var(--panel); border-radius:12px; padding:20px; box-shadow:0 1px 3px rgba(0,0,0,.4);
          cursor:pointer; transition:transform .08s, box-shadow .08s; border:1px solid var(--border); }
  .card:hover { transform:translateY(-2px); box-shadow:0 8px 22px rgba(0,0,0,.55); border-color:var(--blue); }
  .card h3 { margin:0 0 14px; font-size:16px; }
  .stats { display:flex; gap:24px; }
  .stat .n { font-size:24px; font-weight:600; color:var(--blue-d); }
  .stat .l { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .card .go { margin-top:14px; font-size:12px; color:var(--blue-d); }
  .head-right { display:flex; align-items:center; gap:12px; }
  .dash-updated { font-size:11px; color:var(--faint); white-space:nowrap; }
  .cap-bar { margin-top:14px; }
  .cap-bar .track { height:8px; background:var(--panel2); border-radius:5px; overflow:hidden; }
  .cap-bar .fill { height:100%; background:var(--green); border-radius:5px; }
  .cap-bar.over .fill { background:var(--red); }
  .cap-bar .lab { font-size:11px; color:var(--muted); margin-top:5px; }
  /* month filter toolbar */
  .toolbar { display:flex; align-items:center; gap:8px; margin:0 0 18px; font-size:13px; color:var(--muted); }
  .toolbar label { margin-left:6px; }
  .toolbar label:first-child { margin-left:0; }
  .toolbar select, .toolbar input[type=date] { background:var(--panel2); color:var(--text);
                    border:1px solid var(--border); border-radius:8px; padding:7px 10px; font-size:13px; cursor:pointer; }
  .toolbar input[type=date]::-webkit-calendar-picker-indicator { filter:invert(.8); cursor:pointer; }
  .toolbar.est-toolbar { flex-wrap:wrap; }
  .toolbar .tb-label { font-weight:600; color:var(--text); margin-left:0; }
  .toolbar .chk { display:inline-flex; align-items:center; gap:5px; margin-left:0; cursor:pointer; }
  .toolbar .chk input { cursor:pointer; margin:0; }
  .toolbar .tb-sep { width:1px; align-self:stretch; background:var(--border); margin:2px 4px; }
  /* dashboard layout: left nav + content */
  .layout { display:flex; gap:24px; align-items:flex-start; }
  .sidebar { flex:0 0 210px; position:sticky; top:32px; background:var(--panel); border-radius:12px;
             padding:14px 12px; box-shadow:0 1px 3px rgba(0,0,0,.4); border:1px solid var(--border); }
  .sidebar .brand { font-size:15px; font-weight:600; padding:6px 12px 14px; color:var(--text); }
  .nav-section { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.06em;
                 color:var(--faint); padding:14px 12px 6px; }
  .nav-section:first-of-type { padding-top:4px; }
  .nav-item { display:block; padding:10px 12px; margin-bottom:4px; border-radius:8px;
              font-size:14px; color:var(--muted); text-decoration:none; cursor:pointer; }
  .nav-item:hover { background:var(--panel2); color:var(--text); }
  .nav-item.active { background:var(--blue); color:#10141a; font-weight:600; }
  .content { flex:1; min-width:0; }
  .content .head { margin-bottom:4px; }
  .content h1 { font-size:20px; margin:0; }
  .section-h { font-size:16px; margin:30px 0 12px; padding-top:6px; border-top:1px solid var(--border); }
  .section-h.flush { margin-top:8px; padding-top:0; border-top:0; }   /* first heading under the filter */
  /* Tasks tab: total-hours summary banner */
  .summary-bar { display:flex; gap:24px; background:var(--panel2); border:1px solid var(--border);
                 border-radius:12px; padding:18px 24px; margin:6px 0 4px; }
  .summary-stat { display:flex; flex-direction:column; gap:4px; }
  .summary-stat .n { font-size:28px; font-weight:700; color:var(--green-d); font-variant-numeric:tabular-nums; }
  .summary-stat .l { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .card.june:hover { border-color:var(--green); }
  .card.june .stat .n, .card.june .go { color:var(--green-d); }
  .card.june .cap-bar { margin-top:22px; }   /* extra space between the big numbers and the bar */
  /* MSA/capacity cards: lighter panel + brighter border so they stand out at the top of the list */
  .card.cap { background:var(--panel2); border-color:#5c6b86; }
  .grp-card { grid-column: 1 / -1; }   /* combined buckets (e.g. CMD) span the whole row */
  /* combined budget-group card: per-project breakdown rows */
  .grp-tag { font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.05em;
             color:var(--muted); background:var(--panel2); border-radius:8px; padding:2px 7px; vertical-align:middle; }
  .grp-members { margin-top:16px; border-top:1px solid var(--border); padding-top:6px; }
  .grp-row { display:flex; justify-content:space-between; align-items:center; gap:10px;
             padding:7px 6px; border-radius:7px; font-size:13px; cursor:pointer; }
  .grp-row:hover { background:var(--panel2); }
  .grp-row .grp-name { color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .grp-row .grp-h { color:var(--green-d); font-variant-numeric:tabular-nums; white-space:nowrap; }
  /* detail */
  .head { display:flex; align-items:center; justify-content:space-between; gap:16px; }
  .left { display:flex; align-items:center; gap:14px; }
  .crumbs { font-size:15px; display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
  .crumb { color:var(--blue-d); text-decoration:none; cursor:pointer; }
  .crumb:hover { text-decoration:underline; }
  .crumb-sep { color:#5a616b; }
  .crumb-cur { font-weight:600; }
  button.btn { background:var(--blue); color:#10141a; border:0; border-radius:8px; padding:9px 16px; font-size:14px; font-weight:600; cursor:pointer; }
  button.btn:hover { background:var(--blue-d); }
  button.btn:disabled { background:#434a56; color:#8a929c; cursor:default; }
  .back { background:var(--panel2); color:var(--text); }
  .back:hover { background:#414854; }
  .panel { background:var(--panel); border-radius:12px; padding:26px 30px 36px; box-shadow:0 1px 3px rgba(0,0,0,.4); border:1px solid var(--border); }
  #updated { font-size:12px; color:var(--muted); }
  .chart-box { position:relative; height:480px; margin-top:10px; }
  .muted { color:var(--muted); }
  .hint { font-size:12px; color:var(--muted); margin-top:8px; }
  /* drill-down task list */
  .drill-head { display:flex; align-items:center; gap:14px; margin-bottom:14px; }
  .drill-head h2 { font-size:17px; margin:0; }
  .drill-total { font-size:13px; color:var(--muted); }
  table.tasks { width:100%; border-collapse:collapse; font-size:14px; }
  table.tasks th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.04em;
                   color:var(--muted); border-bottom:2px solid var(--border); padding:8px 10px; }
  table.tasks th.hours { text-align:right; }
  table.tasks td { padding:9px 10px; border-bottom:1px solid var(--border); vertical-align:top; }
  table.tasks tr.parent td { font-weight:600; }
  table.tasks tr.sub td { font-weight:400; color:var(--muted); }
  .proj-toggle { display:inline-flex; align-items:center; gap:8px; cursor:pointer; }
  .proj-toggle input { cursor:pointer; margin:0; flex:0 0 auto; }
  .badge { display:inline-block; font-size:11px; padding:2px 8px; border-radius:10px; background:var(--panel2); color:var(--text); }
  .sub-name { padding-left:22px; position:relative; }
  .sub-name::before { content:'↳'; position:absolute; left:6px; color:#5a616b; }
  .done { text-decoration:line-through; color:var(--faint); }
  .hours { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
  /* settings: per-person graph colors */
  .color-list { display:flex; flex-direction:column; gap:2px; }
  .color-row { display:flex; align-items:center; gap:12px; padding:9px 6px; border-bottom:1px solid var(--border); }
  .color-row:last-child { border-bottom:0; }
  .color-row .color-name { flex:1; font-size:14px; }
  .color-pick { width:40px; height:28px; padding:0; border:1px solid var(--border); border-radius:6px;
                background:var(--panel2); cursor:pointer; }
  .reset-one { background:none; border:0; color:var(--blue-d); font-size:12px; cursor:pointer; padding:4px 6px; }
  .reset-one:hover:not(:disabled) { text-decoration:underline; }
  .reset-one:disabled { color:var(--faint); cursor:default; }
  .color-actions { margin-top:18px; }
</style>
</head>
<body>
<div class="wrap" id="app"></div>
<script>
const app = document.getElementById('app');
let chart = null;

// Dark-mode chart defaults: light tick/legend text and faint gridlines.
Chart.defaults.color = '#9aa0a8';
Chart.defaults.borderColor = 'rgba(255,255,255,.08)';

// Draws a dashed horizontal target line (e.g. the 128 h per-assignee cap) on a bar chart.
// Enable per-chart via options.plugins.capLine = { value: <hours> }.
const capLinePlugin = {
  id: 'capLine',
  afterDatasetsDraw(c) {
    const cfg = c.options.plugins.capLine;
    if (!cfg || cfg.value == null) return;
    const y = c.scales.y.getPixelForValue(cfg.value);
    const { left, right } = c.chartArea, ctx = c.ctx;
    ctx.save();
    ctx.beginPath(); ctx.setLineDash([6, 4]); ctx.lineWidth = 2; ctx.strokeStyle = '#8fc0ee';
    ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
    ctx.setLineDash([]); ctx.fillStyle = '#8fc0ee'; ctx.font = '600 12px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText(h2(cfg.value) + ' h target', right - 6, y - 6);
    ctx.restore();
  }
};
Chart.register(capLinePlugin);

// Draws a per-bar monthly-budget marker: a short amber line across each billable bar that
// has a cap. Enable via options.plugins.capMarks = { caps: [<cap or null per bar>] }.
const capMarksPlugin = {
  id: 'capMarks',
  afterDatasetsDraw(c) {
    const cfg = c.options.plugins.capMarks;
    if (!cfg || !cfg.caps) return;
    const meta = c.getDatasetMeta(0), y = c.scales.y, ctx = c.ctx;
    ctx.save();
    cfg.caps.forEach((cap, i) => {
      const bar = meta.data[i];
      if (cap == null || !bar) return;
      const half = (bar.width || 18) / 2 + 2, py = y.getPixelForValue(cap);
      ctx.beginPath(); ctx.lineWidth = 2.5; ctx.strokeStyle = '#f0c674';
      ctx.moveTo(bar.x - half, py); ctx.lineTo(bar.x + half, py); ctx.stroke();
    });
    ctx.restore();
  }
};
Chart.register(capMarksPlugin);

function fmtErr(e){ return '<p class="muted">Error: '+e+'</p>'; }
function esc(s){ return (s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function h2(x){ return Number(x || 0).toFixed(2); }   // always two decimals: 40.00, 40.50, 40.75

// Stable per-person colors, shared across the estimated & actual per-person stacked charts,
// so the same employee keeps one color everywhere within a session.
// Well-separated categorical hues, one per color family, so stacked segments never blend.
// Deliberately NO orange/amber/gold — those get lost against the amber capacity marker (#f0c674).
const PERSON_PALETTE = [
  '#1f77b4', // blue
  '#d62728', // red
  '#2ca02c', // green
  '#9467bd', // purple
  '#17becf', // cyan
  '#e377c2', // pink
  '#8c564b', // brown
  '#7f7f7f', // gray
  '#393b79', // deep indigo
  '#a55194', // plum
];
// Fixed colors for specific people; everyone else auto-assigns a still-unused palette hue.
// Seeded into _personColors so an override is reserved up front and never clashes with an
// auto-assigned person, even if that person is colored first.
const PERSON_COLOR_OVERRIDES = { 'Miranda Osborn': '#9467bd', 'Unassigned': '#7f7f7f' };   // Miranda purple (was orange); Unassigned always grey
const _personColors = Object.assign({}, PERSON_COLOR_OVERRIDES);
let _personColorN = 0;
// User-picked colors (Settings tab), persisted per-browser. These win over the built-in
// defaults/auto-assignment so a chosen color applies everywhere the same person is drawn.
const PERSON_COLOR_STORE = 'personColors.v1';
function loadPersonColorConfig(){
  try { return JSON.parse(localStorage.getItem(PERSON_COLOR_STORE)) || {}; } catch (e) { return {}; }
}
function savePersonColorConfig(){
  try { localStorage.setItem(PERSON_COLOR_STORE, JSON.stringify(personColorConfig)); } catch (e) {}
}
let personColorConfig = loadPersonColorConfig();
// Every name we've been asked to color this session, so the Settings tab can list everyone
// (seeded with the known team + Unassigned so they always show even before any chart loads).
const _seenPeople = new Set([...TEAM_MEMBERS, 'Unassigned']);
function personColor(name){
  _seenPeople.add(name);
  if (name in personColorConfig) return personColorConfig[name];   // user choice wins
  if (name in _personColors) return _personColors[name];
  // Skip palette entries already taken (by an override, a user color, or an earlier person).
  const used = new Set([...Object.values(_personColors), ...Object.values(personColorConfig)]);
  while (used.size < PERSON_PALETTE.length && used.has(PERSON_PALETTE[_personColorN % PERSON_PALETTE.length])) _personColorN++;
  return (_personColors[name] = PERSON_PALETTE[_personColorN++ % PERSON_PALETTE.length]);
}
// Default color for a person, ignoring any user override (used by the Settings "Reset" action
// to show what they'd revert to).
function personDefaultColor(name){
  if (name in _personColors) return _personColors[name];
  const used = new Set([...Object.values(_personColors), ...Object.values(personColorConfig)]);
  while (used.size < PERSON_PALETTE.length && used.has(PERSON_PALETTE[_personColorN % PERSON_PALETTE.length])) _personColorN++;
  return (_personColors[name] = PERSON_PALETTE[_personColorN++ % PERSON_PALETTE.length]);
}
// Build stacked Chart.js datasets (one per person) from project rows. hoursOf(row) returns
// { person: hours } for that project; persons are ordered by grand total (biggest at the bottom).
function personStacks(rows, hoursOf){
  const maps = rows.map(hoursOf), totals = {};
  maps.forEach(m => Object.entries(m).forEach(([p, h]) => { totals[p] = (totals[p] || 0) + h; }));
  const persons = Object.keys(totals).sort((a, b) => totals[b] - totals[a]);
  return persons.map(p => ({ label: p, data: maps.map(m => Math.round((m[p] || 0) * 100) / 100),
    backgroundColor: personColor(p), borderColor: personColor(p), borderWidth: 0 }));
}
function capBar(hours, cap){
  if (cap == null) return '';   // only projects with a monthly capacity get a bar
  const pct = cap > 0 ? (hours / cap) * 100 : 0;
  const over = pct > 100;
  return `<div class="cap-bar${over ? ' over' : ''}">
      <div class="track"><div class="fill" style="width:${Math.min(pct,100)}%"></div></div>
      <div class="lab">${h2(hours)} / ${h2(cap)} h used · ${pct.toFixed(0)}%${over ? ' — over capacity' : ''}</div>
    </div>`;
}
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

// Which dashboard tab is active; remembered across renders/drill-ins.
let dashTab = 'team';   // Team Capacity is the default tab
// Estimated-hours Bar Chart filters, remembered across renders/tab switches.
let estStatusFilter = null;    // Set of enabled status columns; null = show all statuses
let estHideUnassigned = false; // when true, drop the Unassigned assignee from the chart
// Bar-chart drill-in: name of the budget group (e.g. "CMD") whose combined bucket has been
// clicked open into its member projects; null = show the combined bucket. One per chart.
let estDrillGroup = null;
let actualDrillGroup = null;
// Projects unchecked in the "By project" summary tables — hidden from the bar chart above only
// (the table still lists them so they can be re-checked). Keyed by the row's display name.
let estHiddenProjects = new Set();
let actualHiddenProjects = new Set();
// Team Capacity: hide the Unassigned bar by default; toggle remembered across renders.
let teamShowUnassigned = false;
// Task List: filter to a single assignee (person who logged the time); null = show everyone.
let itemFilterPerson = null;
// Selected date range (YYYY-MM-DD) for the "Actual Hours" view; shared by the tab and drill-in.
// Defaults to the current calendar month so the dashboard opens on "this month" every time.
function monthRange(now){ const d = now || new Date(), p = n => String(n).padStart(2, '0'),
  y = d.getFullYear(), m = d.getMonth(),
  first = `${y}-${p(m+1)}-01`, last = `${y}-${p(m+1)}-${p(new Date(y, m+1, 0).getDate())}`;
  return [first, last]; }
let [dateStart, dateEnd] = monthRange();
function fmtDate(d){ const [y,m,day]=d.split('-').map(Number); return new Date(y, m-1, day).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}); }
function rangeLabel(s, e){ return fmtDate(s) + ' – ' + fmtDate(e); }
function rangePicker(){
  return `<div class="toolbar">
      <label for="d-start">From</label><input type="date" id="d-start" value="${dateStart}">
      <label for="d-end">To</label><input type="date" id="d-end" value="${dateEnd}">
      <button class="btn" id="range-go">Search</button>
    </div>`;
}
const TABS = {
  team: { label:'Team Capacity', title:'Team Capacity' },
  actualproj: { label:'Bar Chart', title:'Bar Chart' },
  actualitems: { label:'Task List', title:'Task List' },
  estproj: { label:'Bar Chart', title:'Bar Chart' },
  estimated: { label:'Statistics', title:'Statistics' },
  capacity: { label:'MSA Project Capacity', title:'MSA Project Capacity' },
  settings: { label:'Graph Colors', title:'Graph Colors', sub:'Pick a color for each person — saved in this browser and applied to every per-person chart.' },
};
// Sidebar groups: estimated/planned views vs. logged-hours & progress views.
const NAV_SECTIONS = [
  { title: 'Estimated Hours', tabs: ['team', 'estproj', 'estimated'] },
  { title: 'Actual Hours', tabs: ['capacity', 'actualproj', 'actualitems'] },
  { title: 'Settings', tabs: ['settings'] },
];

// Shared left nav, reused by the dashboard and the drill-in detail pages so the
// sidebar always stays put. The active item tracks the last-opened tab (dashTab).
function sidebarHtml() {
  return `<nav class="sidebar">
      <div class="brand">Asana Dashboard</div>
      ${NAV_SECTIONS.map(sec =>
        `<div class="nav-section">${sec.title}</div>` +
        sec.tabs.map(k => `<a href="#" class="nav-item${k === dashTab ? ' active' : ''}" data-tab="${k}">${TABS[k].label}</a>`).join('')
      ).join('')}
    </nav>`;
}
// Wire nav clicks. onSwitch (dashboard) switches tab in place; on a detail page we
// set the tab and navigate home, where the dashboard renders it.
function wireSidebar(onSwitch) {
  document.querySelectorAll('.nav-item').forEach(a => {
    a.onclick = (e) => {
      e.preventDefault();
      dashTab = a.dataset.tab;
      if (onSwitch) onSwitch(); else location.hash = '';
    };
  });
}

function estCard(w) {
  const c = document.createElement('div');
  c.className = 'card';
  c.innerHTML = `<h3>${esc(w.name)}</h3>
    <div class="stats">
      <div class="stat"><div class="n">${h2(w.hours)}</div><div class="l">Est. Hours</div></div>
      <div class="stat"><div class="n">${w.ntasks}</div><div class="l">Tasks</div></div>
      ${w.cap != null ? `<div class="stat"><div class="n">${h2(w.cap)}</div><div class="l">Capacity h/mo</div></div>` : ''}
    </div>`;
  c.onclick = () => { location.hash = '#/p/' + w.gid; };
  return c;
}

function juneCard(w) {
  const c = document.createElement('div');
  c.className = 'card june';
  c.innerHTML = `<h3>${esc(w.name)}</h3>
    <div class="stats">
      <div class="stat"><div class="n">${h2(w.billable_hours)}</div><div class="l">Billable</div></div>
      <div class="stat"><div class="n">${h2(w.unbillable_hours)}</div><div class="l">Unbillable</div></div>
      <div class="stat"><div class="n">${w.completed}</div><div class="l">Completed Tasks</div></div>
    </div>`;
  c.onclick = () => { location.hash = '#/june/' + w.gid; };
  return c;
}

function capCard(w) {
  // Only billable hours count against a project's monthly capacity.
  const used = Number(w.billable_hours || 0), cap = Number(w.cap || 0);
  const unbill = Number(w.unbillable_hours || 0);
  const remaining = cap - used;
  const c = document.createElement('div');
  c.className = 'card june';
  c.innerHTML = `<h3>${esc(w.name)}</h3>
    <div class="stats">
      <div class="stat"><div class="n">${h2(w.cap)}</div><div class="l">Capacity h/mo</div></div>
      <div class="stat"><div class="n">${h2(used)}</div><div class="l">Billable used</div></div>
      <div class="stat"><div class="n"${remaining < 0 ? ' style="color:#e26b66"' : ''}>${h2(remaining)}</div><div class="l">${remaining < 0 ? 'Over' : 'Remaining'}</div></div>
    </div>
    ${capBar(used, cap)}
    ${unbill > 0 ? `<div class="cap-bar"><div class="lab">+ ${h2(unbill)} h unbillable (not counted)</div></div>` : ''}`;
  c.onclick = () => { location.hash = '#/june/' + w.gid; };
  return c;
}

// Roll up a budget group's member projects (from the current Hours-Logged data)
// into one combined bucket: summed billable/unbillable, plus the per-member rows.
function buildGroupSummary(g, jd) {
  const members = g.gids.map(gid => jd.find(w => w.gid === gid)).filter(Boolean);
  return {
    name: g.name, cap: g.cap, members,
    billable_hours: members.reduce((a, w) => a + (w.billable_hours || 0), 0),
    unbillable_hours: members.reduce((a, w) => a + (w.unbillable_hours || 0), 0),
    nentries: members.reduce((a, w) => a + (w.nentries || 0), 0),
    updated: members.length ? members[0].updated : '',
  };
}

function groupCard(g) {
  // Combined monthly bucket shared by several projects; billable hours only.
  const used = Number(g.billable_hours || 0), cap = Number(g.cap || 0);
  const unbill = Number(g.unbillable_hours || 0);
  const remaining = cap - used;
  const c = document.createElement('div');
  c.className = 'card june grp-card';
  const rows = g.members.map(m =>
    `<div class="grp-row" data-gid="${m.gid}" title="Open ${esc(m.name)}">
       <span class="grp-name">${esc(m.name)}</span>
       <span class="grp-h">${h2(m.billable_hours)} h</span>
     </div>`).join('') || '<div class="grp-row"><span class="grp-name muted">No member data in range.</span></div>';
  c.innerHTML = `<h3>${esc(g.name)} <span class="grp-tag">combined bucket</span></h3>
    <div class="stats">
      <div class="stat"><div class="n">${h2(g.cap)}</div><div class="l">Capacity h/mo</div></div>
      <div class="stat"><div class="n">${h2(used)}</div><div class="l">Billable used</div></div>
      <div class="stat"><div class="n"${remaining < 0 ? ' style="color:#e26b66"' : ''}>${h2(remaining)}</div><div class="l">${remaining < 0 ? 'Over' : 'Remaining'}</div></div>
    </div>
    ${capBar(used, cap)}
    ${unbill > 0 ? `<div class="cap-bar"><div class="lab">+ ${h2(unbill)} h unbillable (not counted)</div></div>` : ''}
    <div class="grp-members">${rows}</div>`;
  // Each member row opens that project's own Hours-Logged detail.
  c.querySelectorAll('.grp-row[data-gid]').forEach(r =>
    r.onclick = (e) => { e.stopPropagation(); location.hash = '#/june/' + r.dataset.gid; });
  return c;
}

async function renderDashboard() {
  if (chart) { chart.destroy(); chart = null; }
  app.innerHTML = `
    <div class="layout">
      ${sidebarHtml()}
      <main class="content">
        <div class="head">
          <h1 id="tab-title"></h1>
          <div class="head-right">
            <span id="dash-updated" class="dash-updated"></span>
            <button class="btn" id="dash-refresh">Refresh</button>
          </div>
        </div>
        <p class="sub" id="tab-sub"></p>
        <div id="tabview"><p class="muted">Loading widgets…</p></div>
      </main>
    </div>`;
  const view = document.getElementById('tabview');
  const btn = document.getElementById('dash-refresh');
  let estData = null, juneData = null, teamData = null;   // cached so switching tabs is instant
  let groupsConfig = null;   // budget-group definitions (loaded once); combined per current range
  let personStatsCache = {};   // Actual Hours per-person totals, keyed by 'start:end'
  let projPersonCache = {};    // per-project person split { gid: { person: {b,u} } }, keyed by 'start:end'
  let itemStatsCache = {};     // Actual Hours per-item (task) totals, keyed by 'start:end'
  let personLoading = {};      // in-flight guard so the chart + summary don't double-fetch

  // Wire the date-range Search button (if the current tab rendered one). Editing the date
  // fields does nothing until Search is clicked (or Enter is pressed in a field).
  function wireRangeSel() {
    const s = document.getElementById('d-start'), e = document.getElementById('d-end'),
          go = document.getElementById('range-go');
    if (!go) return;
    const apply = () => {
      if (s.value) dateStart = s.value;
      if (e.value) dateEnd = e.value;
      juneData = null; renderTab(); loadJune(false);
    };
    go.onclick = apply;
    [s, e].forEach(inp => inp.onkeydown = ev => { if (ev.key === 'Enter') apply(); });
  }

  function cardGrid(items, cardFn, empty, toolbar) {
    view.innerHTML = toolbar || '';
    if (!items.length) { view.insertAdjacentHTML('beforeend', `<p class="muted">${empty}</p>`); }
    else {
      const grid = document.createElement('div');
      grid.className = 'grid';
      items.forEach(w => grid.appendChild(cardFn(w)));
      view.appendChild(grid);
    }
    wireRangeSel();
  }

  function renderEstProj() {
    // Stacked bar chart of estimated hours per project, split by assignee, with filters for
    // the status column and for hiding Unassigned. Caps/gids come from estData; the per-person
    // split and per-status breakdown are inverted out of teamData.breakdown's task rows.
    if (!estData || !teamData) { view.innerHTML = '<p class="muted">Loading widgets…</p>'; return; }
    const NO_STATUS = '(No status)';
    // Every status column seen in the data → drives the filter checkboxes.
    const allStatuses = new Set();
    Object.values(teamData.breakdown).forEach(projs => (projs || []).forEach(p =>
      (p.tasks || []).forEach(t => allStatuses.add(t.status || NO_STATUS))));
    const statusList = [...allStatuses].sort((a, b) =>
      a === NO_STATUS ? 1 : b === NO_STATUS ? -1 : a.localeCompare(b));
    // Forget any remembered status that no longer exists; an empty set collapses back to "all".
    if (estStatusFilter) {
      estStatusFilter = new Set([...estStatusFilter].filter(s => allStatuses.has(s)));
      if (!estStatusFilter.size) estStatusFilter = null;
    }
    const statusOn = s => !estStatusFilter || estStatusFilter.has(s);

    // Toolbar: one checkbox per status column, then an "Exclude Unassigned" toggle.
    const toolbar =
      '<div class="toolbar est-toolbar">' +
        '<span class="tb-label">Status</span>' +
        statusList.map(s =>
          `<label class="chk"><input type="checkbox" class="est-status" value="${esc(s)}" ${statusOn(s) ? 'checked' : ''}>${esc(s)}</label>`
        ).join('') +
        '<span class="tb-sep"></span>' +
        `<label class="chk"><input type="checkbox" id="est-hide-unassigned" ${estHideUnassigned ? 'checked' : ''}>Exclude Unassigned</label>` +
      '</div>';
    const wireFilters = () => {
      view.querySelectorAll('.est-status').forEach(cb => cb.onchange = () => {
        const on = [...view.querySelectorAll('.est-status')].filter(c => c.checked).map(c => c.value);
        estStatusFilter = (on.length === statusList.length) ? null : new Set(on);
        renderEstProj();
      });
      const hu = view.querySelector('#est-hide-unassigned');
      if (hu) hu.onchange = () => { estHideUnassigned = hu.checked; renderEstProj(); };
    };

    // Aggregate estimated hours per project from the filtered task rows: total, item count,
    // and the per-person split for the stacked bars.
    const capOf = Object.fromEntries(estData.map(w => [w.name, w.cap == null ? null : w.cap]));
    const gidOf = Object.fromEntries(estData.map(w => [w.name, w.gid]));
    const agg = {};
    Object.entries(teamData.breakdown).forEach(([person, projs]) => {
      if (estHideUnassigned && person === 'Unassigned') return;
      (projs || []).forEach(p => (p.tasks || []).forEach(t => {
        if (!statusOn(t.status || NO_STATUS)) return;
        const a = agg[p.project] || (agg[p.project] = { hours: 0, ntasks: 0, persons: {} });
        a.hours += t.estimated || 0;
        a.ntasks += 1;
        a.persons[person] = (a.persons[person] || 0) + (t.estimated || 0);
      }));
    });
    const rows = Object.entries(agg)
      .map(([name, a]) => ({ name, gid: gidOf[name], cap: name in capOf ? capOf[name] : null,
        hours: Math.round(a.hours * 100) / 100, ntasks: a.ntasks, persons: a.persons }))
      .filter(w => w.hours > 0)
      .sort((a, b) => b.hours - a.hours);

    // Roll grouped projects (e.g. CMD) into one combined bucket; clicking that bucket drills in
    // and splits it back out into its member projects (each as its own bar). Non-grouped
    // projects always stay standalone.
    const groups = groupsConfig || [];
    const memberGids = new Set(groups.flatMap(g => g.gids));
    const drill = estDrillGroup ? groups.find(g => g.name === estDrillGroup) : null;
    let displayRows, backBar = '';
    if (drill) {
      const gset = new Set(drill.gids);
      displayRows = rows.filter(w => gset.has(w.gid)).sort((a, b) => b.hours - a.hours);
      if (!displayRows.length) { estDrillGroup = null; return renderEstProj(); }
      backBar = `<div class="drill-head"><button class="btn back" id="est-drill-back">← Back to all projects</button>` +
                `<h2>${esc(drill.name)} · split by project</h2></div>`;
    } else {
      const groupRows = groups.map(g => {
        const gset = new Set(g.gids);
        const members = rows.filter(w => gset.has(w.gid));
        const persons = {};
        members.forEach(m => Object.entries(m.persons).forEach(([p, v]) => { persons[p] = (persons[p] || 0) + v; }));
        return { name: g.name, gid: null, isGroup: true, group: g.name, cap: g.cap,
          hours: round2(members.reduce((a, m) => a + m.hours, 0)),
          ntasks: members.reduce((a, m) => a + m.ntasks, 0), persons, members };
      }).filter(g => g.hours > 0);
      const others = rows.filter(w => !memberGids.has(w.gid));
      displayRows = [...groupRows, ...others].sort((a, b) => b.hours - a.hours);
    }
    if (!displayRows.length) {
      view.innerHTML = toolbar + '<p class="muted">No estimated hours match the current filters.</p>';
      wireFilters(); return;
    }
    view.innerHTML = toolbar + backBar + '<div class="chart-box"><canvas id="chart"></canvas></div>' +
      '<div id="est-summary"></div>';
    wireFilters();
    const backBtn = document.getElementById('est-drill-back');
    if (backBtn) backBtn.onclick = () => { estDrillGroup = null; renderEstProj(); };
    if (chart) { chart.destroy(); chart = null; }
    const labels = displayRows.map(w => w.name);
    const gids = displayRows.map(w => w.gid), ntasks = displayRows.map(w => w.ntasks);
    const caps = displayRows.map(w => (w.cap == null ? null : w.cap));
    const datasets = personStacks(displayRows, w => w.persons);
    const top = Math.max(...displayRows.map(w => w.hours), ...caps.filter(c => c != null), 1);
    chart = new Chart(document.getElementById('chart'), {
      type: 'bar',
      data: { labels, datasets },
      options: { responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        // Combined buckets split into their projects; single projects open their detail.
        onClick: (evt, els) => { if (!els.length) return; const r = displayRows[els[0].index];
          if (r.isGroup) { estDrillGroup = r.group; renderEstProj(); }
          else if (r.gid) location.hash = '#/p/' + r.gid; },
        onHover: (evt, els) => { const r = els.length ? displayRows[els[0].index] : null;
          evt.native.target.style.cursor = (r && (r.isGroup || r.gid)) ? 'pointer' : 'default'; },
        scales: { x: { stacked: true, title: { display: true, text: 'Project' } },
                  y: { stacked: true, beginAtZero: true, suggestedMax: top * 1.05,
                       title: { display: true, text: 'Estimated hours' }, ticks: { callback: v => h2(v) } } },
        plugins: { legend: { display: true, position: 'bottom' }, capMarks: { caps },
          tooltip: { itemSort: (a, b) => b.parsed.y - a.parsed.y, filter: item => item.parsed.y > 0,
            callbacks: {
              label: ctx => `${ctx.dataset.label}: ${h2(ctx.parsed.y)} h`,
              afterBody: items => {
                const r = displayRows[items[0].dataIndex], lines = [`Total ${h2(r.hours)} h · ${r.ntasks} tasks`];
                if (r.cap != null) lines.push(`Budget ${h2(r.cap)} h/mo (${(r.hours / r.cap * 100).toFixed(0)}%)`);
                if (r.isGroup) {
                  lines.push('', 'By project:');
                  r.members.slice().sort((a, b) => b.hours - a.hours).forEach(m => {
                    lines.push(`  ${m.name} — ${h2(m.hours)} h`);
                    Object.entries(m.persons).filter(x => x[1] > 0).sort((a, b) => b[1] - a[1])
                      .forEach(([p, v]) => lines.push(`     ${p}: ${h2(v)} h`));
                  });
                  lines.push('', 'Click to split into projects');
                }
                return lines;
              } } } } }
    });
    renderEstSummary(displayRows);
  }

  // Bottom-of-page summary table for the Estimated Hours bar chart: totals per project.
  function renderEstSummary(rows) {
    const box = document.getElementById('est-summary');
    if (!box) return;
    const statRow = (name, hours, ntasks, cls) =>
      `<tr${cls ? ' class="' + cls + '"' : ''}><td>${esc(name)}</td>` +
      `<td class="hours">${h2(hours)} h</td><td class="hours">${ntasks}</td></tr>`;
    const totH = rows.reduce((a, w) => a + w.hours, 0);
    const totT = rows.reduce((a, w) => a + w.ntasks, 0);
    box.innerHTML =
      `<h2 class="section-h">By project</h2>
       <table class="tasks">
         <thead><tr><th>Project</th><th class="hours">Estimated</th><th class="hours">Tasks</th></tr></thead>
         <tbody>${rows.map(w => statRow(w.name, w.hours, w.ntasks)).join('')}
           ${statRow('All projects', totH, totT, 'parent')}</tbody>
       </table>`;
  }

  function renderActualProj() {
    // Stacked bar chart of logged hours per project for the selected date range, split by person.
    const picker = rangePicker();
    if (!juneData) { view.innerHTML = picker + '<p class="muted">Loading widgets…</p>'; wireRangeSel(); return; }
    // Roll grouped projects (e.g. CMD) into one combined bucket with the group's cap; clicking
    // that bucket drills in and splits it into its member projects. Every other project stays
    // on its own bar. Then drop anything with no hours.
    const memberGids = new Set((groupsConfig || []).flatMap(g => g.gids));
    const drill = actualDrillGroup ? (groupsConfig || []).find(g => g.name === actualDrillGroup) : null;
    let rows, backBar = '';
    if (drill) {
      const gset = new Set(drill.gids);
      rows = juneData.filter(w => gset.has(w.gid) && w.hours > 0)
        .map(w => Object.assign({ isGroup: false }, w))
        .sort((a, b) => b.hours - a.hours);
      if (!rows.length) { actualDrillGroup = null; return renderActualProj(); }
      backBar = `<div class="drill-head"><button class="btn back" id="actual-drill-back">← Back to all projects</button>` +
                `<h2>${esc(drill.name)} · split by project</h2></div>`;
    } else {
      const groupItems = (groupsConfig || []).map(g => {
        const s = buildGroupSummary(g, juneData);
        return { name: g.name, gid: null, isGroup: true, gids: g.gids, cap: g.cap, members: s.members,
          billable_hours: s.billable_hours, unbillable_hours: s.unbillable_hours,
          hours: s.billable_hours + s.unbillable_hours, nentries: s.nentries };
      });
      const projItems = juneData.filter(w => !memberGids.has(w.gid)).map(w => Object.assign({ isGroup: false }, w));
      rows = [...groupItems, ...projItems].filter(w => w.hours > 0).sort((a, b) => b.hours - a.hours);
    }
    if (!rows.length) {
      view.innerHTML = picker + `<p class="muted">No hours logged in ${rangeLabel(dateStart, dateEnd)}.</p>`;
      wireRangeSel(); return;
    }
    view.innerHTML = picker + backBar + '<div class="chart-box"><canvas id="chart"></canvas></div>' +
      '<div id="actual-summary"></div>';
    wireRangeSel();
    const backBtn = document.getElementById('actual-drill-back');
    if (backBtn) backBtn.onclick = () => { actualDrillGroup = null; renderActualProj(); };
    if (chart) { chart.destroy(); chart = null; }
    renderActualSummary(rows);
    // The per-person split needs each project's detail (loaded on demand). Show a placeholder
    // in the chart box until it lands, then re-render this whole tab.
    const key = dateStart + ':' + dateEnd, pcache = projPersonCache[key];
    if (!pcache) {
      const cb = view.querySelector('.chart-box');
      if (cb) cb.innerHTML = '<p class="muted" style="padding:24px">Loading per-person breakdown…</p>';
      loadPersonStats(key);
      return;
    }
    // Checkboxes in the By-project table hide projects from the chart only.
    const chartRows = rows.filter(w => !actualHiddenProjects.has(w.name));
    if (!chartRows.length) {
      const cbox = view.querySelector('.chart-box');
      if (cbox) cbox.innerHTML = '<p class="muted" style="padding:24px">All projects hidden — re-check a project below to show it in the chart.</p>';
      return;
    }
    const labels = chartRows.map(w => w.name);
    const gids = chartRows.map(w => w.gid), ents = chartRows.map(w => w.nentries);
    const caps = chartRows.map(w => (w.cap == null ? null : w.cap));
    // Total logged hours (billable + unbillable) per person for a row; groups sum their members.
    const rowPersons = (w) => {
      const src = w.isGroup ? (w.gids || []) : [w.gid], out = {};
      src.forEach(g => { const m = pcache[g]; if (m) Object.entries(m).forEach(([p, v]) => { out[p] = (out[p] || 0) + v.b + v.u; }); });
      return out;
    };
    const datasets = personStacks(chartRows, rowPersons);
    // Keep every budget marker visible even if it sits above the tallest bar.
    const top = Math.max(...chartRows.map(w => w.hours), ...caps.filter(c => c != null), 1);
    chart = new Chart(document.getElementById('chart'), {
      type: 'bar',
      data: { labels, datasets },
      options: { responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        // Combined buckets split into their projects; single projects open their detail.
        onClick: (evt, els) => { if (!els.length) return; const r = chartRows[els[0].index];
          if (r.isGroup) { actualDrillGroup = r.name; renderActualProj(); }
          else if (r.gid) location.hash = '#/june/' + r.gid; },
        onHover: (evt, els) => { const r = els.length ? chartRows[els[0].index] : null;
          evt.native.target.style.cursor = (r && (r.isGroup || r.gid)) ? 'pointer' : 'default'; },
        scales: { x: { stacked: true, title: { display: true, text: 'Project' } },
                  y: { stacked: true, beginAtZero: true, suggestedMax: top * 1.05,
                       title: { display: true, text: 'Hours logged' }, ticks: { callback: v => h2(v) } } },
        plugins: { legend: { display: true, position: 'bottom' }, capMarks: { caps },
          tooltip: { itemSort: (a, b) => b.parsed.y - a.parsed.y, filter: item => item.parsed.y > 0,
            callbacks: {
              label: ctx => `${ctx.dataset.label}: ${h2(ctx.parsed.y)} h`,
              afterBody: items => {
                const r = chartRows[items[0].dataIndex], lines = [`Total ${h2(r.hours)} h · ${r.nentries} entries`];
                if (r.cap != null) lines.push(`Budget ${h2(r.cap)} h billable`);
                if (r.isGroup) {
                  lines.push('', 'By project:');
                  (r.members || []).slice().sort((a, b) => b.hours - a.hours).forEach(m => {
                    lines.push(`  ${m.name} — ${h2(m.hours)} h`);
                    Object.entries(pcache[m.gid] || {}).map(([p, v]) => [p, v.b + v.u])
                      .filter(x => x[1] > 0).sort((a, b) => b[1] - a[1])
                      .forEach(([p, v]) => lines.push(`     ${p}: ${h2(v)} h`));
                  });
                  lines.push('', 'Click to split into projects');
                }
                return lines;
              } } } } }
    });
  }

  // Bottom-of-page summary tables for Actual Hours: totals per project and per person.
  // rows_ mirrors the bar chart's rows (combined buckets when not drilled in, member projects
  // when drilled in) so the "By project" table matches whatever the chart is showing.
  function renderActualSummary(rows_) {
    const box = document.getElementById('actual-summary');
    if (!box) return;
    const statRow = (name, b, u, cls) =>
      `<tr${cls ? ' class="' + cls + '"' : ''}><td>${esc(name)}</td>` +
      `<td class="hours">${h2(b)} h</td><td class="hours">${h2(u)} h</td><td class="hours">${h2(b + u)} h</td></tr>`;
    // Same as statRow but the Project cell leads with a checkbox that toggles the project's
    // visibility in the bar chart above (unchecking adds it to actualHiddenProjects).
    const projStatRow = (name, b, u) => {
      const checked = actualHiddenProjects.has(name) ? '' : ' checked';
      const attr = esc(name).replace(/"/g, '&quot;');
      return `<tr><td><label class="proj-toggle"><input type="checkbox" class="proj-check" data-proj="${attr}"${checked}> ${esc(name)}</label></td>` +
        `<td class="hours">${h2(b)} h</td><td class="hours">${h2(u)} h</td><td class="hours">${h2(b + u)} h</td></tr>`;
    };

    // By project — mirrors the chart's rows (combined buckets / drilled-in members).
    const projRows = (rows_ || juneData.filter(w => w.hours > 0)).slice().sort((a, b) => b.hours - a.hours);
    const pjB = projRows.reduce((a, w) => a + w.billable_hours, 0);
    const pjU = projRows.reduce((a, w) => a + w.unbillable_hours, 0);
    const projTable =
      `<h2 class="section-h">By project</h2>
       <table class="tasks">
         <thead><tr><th>Project</th><th class="hours">Billable</th><th class="hours">Unbillable</th><th class="hours">Total</th></tr></thead>
         <tbody>${projRows.map(w => projStatRow(w.name, w.billable_hours, w.unbillable_hours)).join('')}
           ${statRow('All projects', pjB, pjU, 'parent')}</tbody>
       </table>`;

    // By person — aggregated across every project's detail for this range (loaded on demand).
    // When drilled into a group, scope the per-person totals to that group's member projects.
    const key = dateStart + ':' + dateEnd;
    let people = personStatsCache[key];
    if (actualDrillGroup && projPersonCache[key]) {
      const pc = projPersonCache[key], g = (groupsConfig || []).find(x => x.name === actualDrillGroup), pa = {};
      (g ? g.gids : []).forEach(gid => Object.entries(pc[gid] || {}).forEach(([p, v]) => {
        const a = pa[p] || (pa[p] = { billable: 0, unbillable: 0 }); a.billable += v.b; a.unbillable += v.u; }));
      people = Object.keys(pa).map(name => ({ name, billable: pa[name].billable, unbillable: pa[name].unbillable }))
        .sort((a, b) => (b.billable + b.unbillable) - (a.billable + a.unbillable));
    }
    let personTable;
    if (!people) {
      personTable = '<h2 class="section-h">By person</h2><p class="muted" id="person-loading">Loading per-person totals…</p>';
    } else {
      const ppB = people.reduce((a, p) => a + p.billable, 0);
      const ppU = people.reduce((a, p) => a + p.unbillable, 0);
      personTable =
        `<h2 class="section-h">By person</h2>
         <table class="tasks">
           <thead><tr><th>Person</th><th class="hours">Billable</th><th class="hours">Unbillable</th><th class="hours">Total</th></tr></thead>
           <tbody>${people.map(p => statRow(p.name, p.billable, p.unbillable)).join('')}
             ${statRow('Everyone', ppB, ppU, 'parent')}</tbody>
         </table>`;
    }
    box.innerHTML = projTable + personTable;
    // Wire the By-project checkboxes: toggling one hides/shows that project in the chart
    // (kept in actualHiddenProjects) and re-renders the tab.
    box.querySelectorAll('.proj-check').forEach(cb => {
      cb.onchange = () => {
        const p = cb.getAttribute('data-proj');
        if (cb.checked) actualHiddenProjects.delete(p); else actualHiddenProjects.add(p);
        renderActualProj();
      };
    });
    if (!people) loadPersonStats(key);
  }

  // Pull each logged project's detail and split billable/unbillable per person — both globally
  // (summary table) and per project (the stacked chart) — then cache + re-render.
  async function loadPersonStats(key) {
    if (personLoading[key] || personStatsCache[key]) return;   // already loading / loaded
    personLoading[key] = true;
    const gids = juneData.filter(w => w.hours > 0).map(w => w.gid);
    let details;
    try {
      details = await Promise.all(gids.map(g =>
        fetch(`/api/june/${g}?start=${dateStart}&end=${dateEnd}`).then(r => r.json())));
    } catch (e) {
      const el = document.getElementById('person-loading');
      if (el) el.innerHTML = fmtErr(e);
      delete personLoading[key];
      return;
    }
    delete personLoading[key];
    if (key !== dateStart + ':' + dateEnd) return;   // range changed while loading
    const agg = {}, byGid = {}, items = {};
    gids.forEach((g, idx) => {
      const d = details[idx], pm = byGid[g] = {};
      (d.labels || []).forEach((name, i) => {
        const a = agg[name] || (agg[name] = { billable: 0, unbillable: 0 });
        a.billable += d.billable[i]; a.unbillable += d.unbillable[i];
        const p = pm[name] || (pm[name] = { b: 0, u: 0 });
        p.b += d.billable[i]; p.u += d.unbillable[i];
      });
      // Roll each project's time entries up by task AND the person who logged them, so the
      // Items tab can list every worked-on item with who logged the time and how much. Key by
      // project + task + person so identically named tasks / multiple loggers stay separate.
      (d.entries || []).forEach(e => {
        const person = e.by || 'Unknown';
        const ikey = (d.name || '') + ' | ' + e.task + ' | ' + person;
        const it = items[ikey] || (items[ikey] = { task: e.task, project: d.name || '', person: person, billable: 0, unbillable: 0, entries: 0 });
        if (e.billable) it.billable += e.hours; else it.unbillable += e.hours;
        it.entries += 1;
      });
    });
    projPersonCache[key] = byGid;
    personStatsCache[key] = Object.keys(agg)
      .map(name => ({ name, billable: agg[name].billable, unbillable: agg[name].unbillable }))
      .sort((a, b) => (b.billable + b.unbillable) - (a.billable + a.unbillable));
    itemStatsCache[key] = Object.values(items)
      // Group by project (A→Z), then heaviest items first within each project.
      .sort((a, b) => a.project.localeCompare(b.project)
        || (b.billable + b.unbillable) - (a.billable + a.unbillable)
        || a.task.localeCompare(b.task));
    // Re-render the whole tab so the chart (which needs the per-project split) draws too.
    if (dashTab === 'actualproj') renderActualProj();
    else if (dashTab === 'actualitems') renderActualItems();
  }

  // Actual Hours › Items: every task worked on in the selected range, with its logged
  // hours (billable / unbillable / total) and a grand total. Reuses the per-item rollup
  // that loadPersonStats builds from each project's time entries.
  function renderActualItems() {
    const picker = rangePicker();
    if (!juneData) { view.innerHTML = picker + '<p class="muted">Loading widgets…</p>'; wireRangeSel(); return; }
    if (!juneData.some(w => w.hours > 0)) {
      view.innerHTML = picker + `<p class="muted">No hours logged in ${rangeLabel(dateStart, dateEnd)}.</p>`;
      wireRangeSel(); return;
    }
    const key = dateStart + ':' + dateEnd, allItems = itemStatsCache[key];
    if (!allItems) {
      view.innerHTML = picker + '<p class="muted" id="items-loading">Loading items…</p>';
      wireRangeSel();
      loadPersonStats(key);
      return;
    }
    // Assignee filter: dropdown of everyone who logged time in this range. If the remembered
    // selection isn't present in this range, fall back to showing everyone.
    const people = [...new Set(allItems.map(it => it.person))].sort((a, b) => a.localeCompare(b));
    if (itemFilterPerson && !people.includes(itemFilterPerson)) itemFilterPerson = null;
    const items = itemFilterPerson ? allItems.filter(it => it.person === itemFilterPerson) : allItems;
    const filterBar = `<div class="toolbar">
      <label for="item-assignee">Assignee</label>
      <select id="item-assignee">
        <option value="">All assignees</option>
        ${people.map(p => `<option value="${esc(p)}"${p === itemFilterPerson ? ' selected' : ''}>${esc(p)}</option>`).join('')}
      </select>
    </div>`;
    const totB = items.reduce((a, it) => a + it.billable, 0);
    const totU = items.reduce((a, it) => a + it.unbillable, 0);
    const hoursCells = (b, u) =>
      `<td class="hours">${h2(b)} h</td><td class="hours">${h2(u)} h</td><td class="hours">${h2(b + u)} h</td>`;
    // Headline summary: total hours logged across every project for the selected range.
    const summary = `<div class="summary-bar">
      <div class="summary-stat"><span class="n">${h2(totB + totU)} h</span><span class="l">Total hours logged</span></div>
      <div class="summary-stat"><span class="n">${h2(totB)} h</span><span class="l">Billable</span></div>
      <div class="summary-stat"><span class="n">${h2(totU)} h</span><span class="l">Unbillable</span></div>
    </div>`;
    // One titled section per project: a heading, a table of that project's logged tasks
    // (with who logged them), and a project total row. `items` is already sorted by project,
    // so the keys keep project order.
    const byProject = {};
    items.forEach(it => (byProject[it.project] = byProject[it.project] || []).push(it));
    const sections = Object.keys(byProject).map(proj => {
      const list = byProject[proj];
      const pB = list.reduce((a, it) => a + it.billable, 0);
      const pU = list.reduce((a, it) => a + it.unbillable, 0);
      const body = list.map(it =>
        `<tr><td>${esc(it.task)}</td><td>${esc(it.person)}</td>${hoursCells(it.billable, it.unbillable)}</tr>`).join('');
      return `<h2 class="section-h">${esc(proj)}</h2>
        <table class="tasks">
          <thead><tr><th>Task</th><th>Logged by</th><th class="hours">Billable</th><th class="hours">Unbillable</th><th class="hours">Total</th></tr></thead>
          <tbody>${body}
            <tr class="parent"><td>Total</td><td></td>${hoursCells(pB, pU)}</tr></tbody>
        </table>`;
    }).join('');
    const noneMsg = items.length ? '' : `<p class="muted">No hours logged by ${esc(itemFilterPerson)} in ${rangeLabel(dateStart, dateEnd)}.</p>`;
    view.innerHTML = picker + filterBar + summary + (noneMsg || sections);
    wireRangeSel();
    const sel = document.getElementById('item-assignee');
    if (sel) sel.onchange = () => { itemFilterPerson = sel.value || null; renderActualItems(); };
  }

  function renderTeam() {
    // Bar chart of estimated hours per assignee, with a dashed line at the 128 h target.
    // Unassigned is hidden by default; the toolbar toggle brings it back.
    const src = teamData, hasU = src.labels.includes('Unassigned');
    const keep = src.labels.map((_, i) => i).filter(i => teamShowUnassigned || src.labels[i] !== 'Unassigned');
    const d = { cap: src.cap,
      labels: keep.map(i => src.labels[i]), hours: keep.map(i => src.hours[i]),
      estimated: keep.map(i => src.estimated[i]), actual: keep.map(i => src.actual[i]),
      counts: keep.map(i => src.counts[i]) };
    const toolbar = hasU
      ? `<div class="toolbar team-toolbar"><label class="chk"><input type="checkbox" id="team-show-unassigned" ${teamShowUnassigned ? 'checked' : ''}>Include Unassigned</label></div>`
      : '';
    view.innerHTML = toolbar + '<div class="chart-box"><canvas id="chart"></canvas></div>';
    const cb = document.getElementById('team-show-unassigned');
    if (cb) cb.onchange = () => { teamShowUnassigned = cb.checked; renderTeam(); };
    if (chart) { chart.destroy(); chart = null; }
    const colors = d.hours.map((h, i) => d.labels[i] === 'Unassigned' ? personColor('Unassigned') : (h > d.cap ? '#e26b66' : '#4cc085'));
    chart = new Chart(document.getElementById('chart'), {
      type: 'bar',
      data: { labels: d.labels, datasets: [{ label: 'Remaining hours', data: d.hours,
        backgroundColor: colors, borderColor: colors, borderWidth: 1,
        _counts: d.counts, _est: d.estimated, _act: d.actual }] },
      options: { responsive: true, maintainAspectRatio: false,
        onClick: (evt, els) => { if (els.length) showBreakdown(d.labels[els[0].index]); },
        onHover: (evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        scales: { x: { title: { display: true, text: 'Assignee' },
                       ticks: { callback: (v, i) => [d.labels[i], h2(d.hours[i]) + ' h'] } },
                  y: { beginAtZero: true, suggestedMax: Math.max(d.cap * 1.1, ...d.hours, 1),
                       title: { display: true, text: 'Remaining hours' }, ticks: { callback: v => h2(v) } } },
        plugins: { legend: { display: false }, capLine: { value: d.cap },
          tooltip: { callbacks: {
            label: ctx => `Remaining: ${h2(ctx.parsed.y)} h of ${h2(d.cap)} (${(ctx.parsed.y / d.cap * 100).toFixed(0)}%)`,
            afterLabel: ctx => `Est ${h2(ctx.dataset._est[ctx.dataIndex])} − actual ${h2(ctx.dataset._act[ctx.dataIndex])} · ${ctx.dataset._counts[ctx.dataIndex]} items` } } } }
    });
  }

  function showBreakdown(name) {
    if (chart) { chart.destroy(); chart = null; }
    const rows_ = (teamData.breakdown[name] || []).slice().sort((a, b) => b.remaining - a.remaining);
    const totEst = rows_.reduce((a, r) => a + r.estimated, 0);
    const totAct = rows_.reduce((a, r) => a + r.actual, 0);
    const totRem = totEst - totAct;
    const pct = (totRem / teamData.cap * 100).toFixed(0);
    // Each project is a bold header row, with that person's tasks/subtasks listed beneath it.
    let rows = rows_.map(r => {
      let body = `<tr class="parent"><td>${esc(r.project)}</td><td></td>` +
        `<td class="hours">${h2(r.estimated)} h</td>` +
        `<td class="hours">${h2(r.actual)} h</td>` +
        `<td class="hours">${h2(r.remaining)} h</td></tr>`;
      const tasks = r.tasks || [];
      if (!tasks.length) {
        body += `<tr class="sub"><td class="sub-name" colspan="5">No tasks.</td></tr>`;
      } else {
        body += tasks.map(t => {
          const ctx = t.context ? ` <span class="muted">(${esc(t.context)})</span>` : '';
          const indent = t.type === 'subtask' ? 'padding-left:34px' : 'padding-left:22px';
          return `<tr class="sub"><td class="${t.type === 'subtask' ? 'sub-name' : ''}" style="${indent}">${esc(t.name)}${ctx}</td>` +
            `<td>${t.status ? `<span class="badge">${esc(t.status)}</span>` : '—'}</td>` +
            `<td class="hours">${h2(t.estimated)} h</td>` +
            `<td class="hours">${h2(t.actual)} h</td>` +
            `<td class="hours">${h2(t.remaining)} h</td></tr>`;
        }).join('');
      }
      return body;
    }).join('');
    if (!rows_.length) rows = '<tr><td colspan="5" class="muted">No estimated or actual hours.</td></tr>';
    view.innerHTML =
      `<div class="drill-head">
         <button class="btn back" id="tochart">← Back to chart</button>
         <h2>${esc(name)}</h2>
       </div>
       <p class="drill-total">${h2(totRem)} h remaining of ${h2(teamData.cap)} (${pct}%) · ${h2(totEst)} est − ${h2(totAct)} actual · ${rows_.length} project(s)</p>
       <table class="tasks">
         <thead><tr><th>Project / Task</th><th>Status</th><th class="hours">Est.</th><th class="hours">Actual</th><th class="hours">Remaining</th></tr></thead>
         <tbody>${rows}</tbody>
       </table>`;
    document.getElementById('tochart').onclick = renderTeam;
  }

  // One central "Updated" label by the Refresh button, reflecting the data behind the
  // active tab (estimated pull for team/estimated; logged-hours pull for the rest).
  function setUpdatedLabel() {
    const el = document.getElementById('dash-updated');
    if (!el) return;
    let u = null;
    if (dashTab === 'settings') u = null;
    else if (dashTab === 'team') u = teamData && teamData.updated;
    else if (dashTab === 'estimated' || dashTab === 'estproj') u = estData && estData[0] && estData[0].updated;
    else u = juneData && juneData[0] && juneData[0].updated;
    el.textContent = u ? ('Updated ' + u) : '';
  }

  function renderTab() {
    if (chart) { chart.destroy(); chart = null; }   // leaving any chart tab
    estDrillGroup = null; actualDrillGroup = null;   // switching tabs collapses any drilled-in bucket
    document.querySelectorAll('.nav-item').forEach(a =>
      a.classList.toggle('active', a.dataset.tab === dashTab));
    setUpdatedLabel();
    // Titles stay clean: the From/To filter under range-based tabs is the single
    // place that shows the selected date range.
    document.getElementById('tab-title').textContent = TABS[dashTab].title;
    const sub = TABS[dashTab].sub || '', subEl = document.getElementById('tab-sub');
    subEl.textContent = sub; subEl.style.display = sub ? '' : 'none';
    const loading = '<p class="muted">Loading widgets…</p>';

    if (dashTab === 'team') {
      teamData ? renderTeam() : (view.innerHTML = loading);
    } else if (dashTab === 'estproj') {
      renderEstProj();
    } else if (dashTab === 'actualproj') {
      renderActualProj();
    } else if (dashTab === 'actualitems') {
      renderActualItems();
    } else if (dashTab === 'estimated') {
      estData ? cardGrid(estData, estCard, 'No projects.') : (view.innerHTML = loading);
    } else if (dashTab === 'capacity') {
      const picker = rangePicker();
      if (!juneData) { view.innerHTML = picker + loading; wireRangeSel(); }
      else {
        // MSA projects with a monthly capacity lead the list and are highlighted: combined
        // budget buckets first, then any standalone capped project (group members roll into
        // their bucket). Everything else that logged hours follows as plain statistics cards.
        const memberGids = new Set((groupsConfig || []).flatMap(g => g.gids));
        const groups = (groupsConfig || []).map(g => buildGroupSummary(g, juneData))
                         .filter(g => g.members.length);
        const standalone = juneData.filter(w => w.cap != null && !memberGids.has(w.gid))
                             .sort((a, b) => b.billable_hours - a.billable_hours);
        const others = juneData.filter(w => w.cap == null && !memberGids.has(w.gid))
                         .sort((a, b) => b.hours - a.hours);
        view.innerHTML = picker;
        const hasCap = groups.length || standalone.length;
        if (!hasCap && !others.length) {
          view.insertAdjacentHTML('beforeend', `<p class="muted">No hours logged in ${rangeLabel(dateStart, dateEnd)}.</p>`);
        } else {
          if (hasCap) {
            view.insertAdjacentHTML('beforeend', '<h2 class="section-h flush">MSA projects · monthly capacity</h2>');
            const capGrid = document.createElement('div');
            capGrid.className = 'grid';
            groups.forEach(g => { const c = groupCard(g); c.classList.add('cap'); capGrid.appendChild(c); });
            standalone.forEach(w => { const c = capCard(w); c.classList.add('cap'); capGrid.appendChild(c); });
            view.appendChild(capGrid);
          }
          if (others.length) {
            view.insertAdjacentHTML('beforeend', `<h2 class="section-h${hasCap ? '' : ' flush'}">Other projects</h2>`);
            const grid = document.createElement('div');
            grid.className = 'grid';
            others.forEach(w => grid.appendChild(juneCard(w)));
            view.appendChild(grid);
          }
        }
        wireRangeSel();
      }
    } else if (dashTab === 'settings') {
      renderSettings();
    }
  }

  // Settings: per-person graph colors. Choices persist to localStorage (see personColor)
  // and take effect on any per-person chart the next time it's drawn.
  function renderSettings() {
    // Everyone we might color: known team + Unassigned, plus anyone seen in a chart this
    // session, plus anyone who already has a saved color.
    const people = [...new Set([...TEAM_MEMBERS, 'Unassigned', ..._seenPeople, ...Object.keys(personColorConfig)])];
    const rows = people.map(p => {
      const custom = p in personColorConfig;
      return `<div class="color-row" data-person="${esc(p)}">
          <input type="color" class="color-pick" value="${personColor(p)}">
          <span class="color-name">${esc(p)}</span>
          <button class="reset-one"${custom ? '' : ' disabled'}>Reset</button>
        </div>`;
    }).join('');
    view.innerHTML = `<div class="panel">
        <div class="color-list">${rows}</div>
        <div class="color-actions"><button class="btn back" id="reset-all">Reset all to defaults</button></div>
        <p class="hint">Applies to the per-person stacked bar charts and the Unassigned bar on Team Capacity. Open a chart tab to see the change.</p>
      </div>`;
    view.querySelectorAll('.color-row').forEach(row => {
      const name = row.dataset.person;
      const pick = row.querySelector('.color-pick'), reset = row.querySelector('.reset-one');
      pick.oninput = () => { personColorConfig[name] = pick.value; savePersonColorConfig(); reset.disabled = false; };
      reset.onclick = () => { delete personColorConfig[name]; savePersonColorConfig(); pick.value = personDefaultColor(name); reset.disabled = true; };
    });
    document.getElementById('reset-all').onclick = () => { personColorConfig = {}; savePersonColorConfig(); renderSettings(); };
  }

  wireSidebar(renderTab);

  // Loads the selected range's "Actual Hours" widgets (used on its own when the range changes).
  async function loadJune(refresh) {
    const s = dateStart, e = dateEnd;   // capture so a fast re-pick doesn't paint stale data
    personStatsCache = {}; projPersonCache = {}; itemStatsCache = {}; personLoading = {};   // range/refresh changed — recompute per-person/per-item splits
    try {
      const url = `/api/june?start=${s}&end=${e}` + (refresh ? '&refresh=1' : '');
      const j = await fetch(url).then(r => r.json());
      if (s !== dateStart || e !== dateEnd) return;   // a newer range was picked mid-flight
      juneData = j;
    } catch (err) { view.innerHTML = fmtErr(err); return; }
    if (['capacity', 'actualproj', 'actualitems'].includes(dashTab)) renderTab();
  }

  async function loadAll(refresh) {
    btn.disabled = true; btn.textContent = refresh ? 'Refreshing…' : 'Refresh';
    const q = refresh ? '?refresh=1' : '';
    try {
      const [e, grp] = await Promise.all([
        fetch('/api/projects' + q).then(r => r.json()),
        groupsConfig ? Promise.resolve(groupsConfig) : fetch('/api/groups').then(r => r.json()),
        loadJune(refresh),
      ]);
      estData = e; groupsConfig = grp;
      // Team load derives from the per-project detail that /api/projects just (re)built,
      // so it reads the warm cache — no refresh flag, no duplicate Asana pulls.
      teamData = await fetch('/api/assignees').then(r => r.json());
    } catch (err) { view.innerHTML = fmtErr(err); }
    renderTab();
    btn.disabled = false; btn.textContent = 'Refresh';
  }
  btn.onclick = () => loadAll(true);
  renderTab();        // paint sidebar + active tab immediately (shows "Loading…")
  loadAll(false);     // navigation = cached
}

async function renderDetail(gid) {
  app.innerHTML = `
    <div class="layout">
      ${sidebarHtml()}
      <main class="content">
        <div class="head">
          <div id="crumbs" class="crumbs">Loading…</div>
          <div class="head-right">
            <span id="dash-updated" class="dash-updated"></span>
            <button class="btn" id="refresh">Refresh</button>
          </div>
        </div>
        <p class="sub" id="sub"></p>
        <div class="panel">
          <div id="view"></div>
        </div>
      </main>
    </div>`;
  wireSidebar(null);
  const toDash = () => { location.hash = ''; };
  const btn = document.getElementById('refresh');
  let detailData = null;

  function showChart() {
    const d = detailData;
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:d.name}]);
    document.getElementById('view').innerHTML =
      '<div class="chart-box"><canvas id="chart"></canvas></div>';
    if (chart) { chart.destroy(); chart = null; }
    chart = new Chart(document.getElementById('chart'), {
      type:'bar',
      data:{ labels:d.labels, datasets:[{ label:'Estimated hours', data:d.hours,
        backgroundColor:'#6aa9e0', borderColor:'#9cc7f0', borderWidth:1, _counts:d.counts }] },
      options:{ responsive:true, maintainAspectRatio:false,
        onClick:(evt, els) => { if (els.length) showTasks(d.labels[els[0].index]); },
        onHover:(evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins:{ legend:{display:false}, tooltip:{ callbacks:{
          label: ctx => `Estimated: ${h2(ctx.parsed.y)} h`,
          afterLabel: ctx => `Items: ${ctx.dataset._counts[ctx.dataIndex]}` } } },
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
         <button class="btn back" id="tochart">← Back to chart</button>
         <h2>${esc(assignee)}</h2>
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
      document.getElementById('dash-updated').textContent = detailData.updated ? ('Updated ' + detailData.updated) : '';
      showChart();
    } catch (e) { document.getElementById('view').innerHTML = fmtErr(e); }
    finally { btn.disabled = false; btn.textContent = 'Refresh'; }
  }
  btn.onclick = () => load(true);
  load(false);   // navigation = cached
}

async function renderJuneDetail(gid) {
  app.innerHTML = `
    <div class="layout">
      ${sidebarHtml()}
      <main class="content">
        <div class="head">
          <div id="crumbs" class="crumbs">Loading…</div>
          <div class="head-right">
            <span id="dash-updated" class="dash-updated"></span>
            <button class="btn" id="refresh">Refresh</button>
          </div>
        </div>
        <p class="sub" id="sub"></p>
        <div class="panel">
          <div id="view"></div>
        </div>
      </main>
    </div>`;
  wireSidebar(null);
  const toDash = () => { location.hash = ''; };
  const btn = document.getElementById('refresh');
  let data = null;

  function showChart() {
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:data.name}]);
    document.getElementById('view').innerHTML =
      '<div class="chart-box"><canvas id="chart"></canvas></div>';
    if (chart) { chart.destroy(); chart = null; }
    chart = new Chart(document.getElementById('chart'), {
      type:'bar',
      data:{ labels:data.labels, datasets:[
        { label:'Billable', data:data.billable, backgroundColor:'#4cc085', borderColor:'#6cd49d',
          borderWidth:1, _counts:data.counts },
        { label:'Unbillable', data:data.unbillable, backgroundColor:'#8a929c', borderColor:'#a3aab4',
          borderWidth:1 } ] },
      options:{ responsive:true, maintainAspectRatio:false,
        onClick:(evt, els) => { if (els.length) showEntries(data.labels[els[0].index]); },
        onHover:(evt, els) => { evt.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins:{ legend:{display:true}, tooltip:{ callbacks:{
          label: ctx => `${ctx.dataset.label}: ${h2(ctx.parsed.y)} h`,
          afterLabel: ctx => ctx.datasetIndex === 0 ? `Entries: ${data.counts[ctx.dataIndex]}` : '' } } },
        scales:{ x:{ title:{display:true,text:'Logged by'}, ticks:{ callback:(v,i) => [data.labels[i], h2(data.hours[i]) + ' h'] } },
                 y:{ beginAtZero:true, title:{display:true,text:'Hours logged'}, ticks:{ callback:v => h2(v) } } } }
    });
  }

  function showEntries(person) {
    if (chart) { chart.destroy(); chart = null; }
    const ml = rangeLabel(data.start, data.end);
    setCrumbs([{label:'Dashboard', fn:toDash}, {label:data.name, fn:showChart}, {label:person}]);
    const rows_ = data.entries.filter(e => e.by === person);
    const total = rows_.reduce((a, e) => a + e.hours, 0);
    const billTotal = rows_.reduce((a, e) => a + (e.billable ? e.hours : 0), 0);
    const unbillTotal = total - billTotal;
    let rows = '';
    rows_.forEach(e => {
      rows += `<tr><td>${esc(e.date)}</td><td>${esc(e.task)}</td>` +
        `<td>${e.billable ? '<span class="badge">Billable</span>' : '<span class="badge" style="color:#a3aab4">Unbillable</span>'}</td>` +
        `<td class="hours">${h2(e.hours)} h</td></tr>`;
    });
    if (!rows_.length) rows = '<tr><td colspan="4" class="muted">No entries.</td></tr>';
    document.getElementById('view').innerHTML =
      `<div class="drill-head">
         <button class="btn back" id="tochart">← Back to chart</button>
         <h2>${esc(person)}</h2>
       </div>
       <p class="drill-total">${rows_.length} entr${rows_.length === 1 ? 'y' : 'ies'} · ${total.toFixed(2)} h logged in ${ml} · ${h2(billTotal)} h billable / ${h2(unbillTotal)} h unbillable</p>
       <table class="tasks">
         <thead><tr><th>Date</th><th>Task</th><th>Billable?</th><th class="hours">Hours</th></tr></thead>
         <tbody>${rows}</tbody>
       </table>`;
    document.getElementById('tochart').onclick = showChart;
  }

  async function load(refresh) {
    btn.disabled = true; btn.textContent = refresh ? 'Refreshing…' : 'Refresh';
    try {
      data = await (await fetch('/api/june/' + gid + `?start=${dateStart}&end=${dateEnd}` + (refresh ? '&refresh=1' : ''))).json();
      document.getElementById('sub').textContent = `Hours logged ${rangeLabel(data.start, data.end)} per person · ${data.nentries} time entries · ${h2(data.billable_hours)} h billable / ${h2(data.unbillable_hours)} h unbillable`;
      document.getElementById('dash-updated').textContent = data.updated ? ('Updated ' + data.updated) : '';
      showChart();
    } catch (e) { document.getElementById('view').innerHTML = fmtErr(e); }
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


# The shared UI script references a few constants that the static build (build_static.py)
# injects via its PREPEND. When serving the page ourselves we inject the same values here,
# in a <script> that runs before the UI script, so identifiers like TEAM_MEMBERS are defined.
def render_page():
    boot = "<script>\nconst TEAM_MEMBERS = %s;\n</script>\n" % json.dumps(TEAM_MEMBERS)
    return PAGE.replace('<div class="wrap" id="app"></div>\n<script>',
                        '<div class="wrap" id="app"></div>\n' + boot + '<script>', 1)


PAGE_HTML = render_page()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = self.path.split("?")
        path = parts[0]
        query = urllib.parse.parse_qs(parts[1] if len(parts) > 1 else "")
        refresh = query.get("refresh", [""])[0] == "1"
        start = query.get("start", [DEFAULT_START])[0]
        end = query.get("end", [DEFAULT_END])[0]
        if start > end:                      # tolerate inverted ranges
            start, end = end, start
        try:
            if path == "/api/projects":
                return self._json(200, get_summaries(refresh=refresh))
            if path == "/api/june":
                return self._json(200, get_june_summaries(refresh=refresh, start=start, end=end))
            if path == "/api/assignees":
                return self._json(200, get_assignee_load(refresh=refresh))
            if path == "/api/groups":
                return self._json(200, GROUPS)
            if path.startswith("/api/june/"):
                gid = path.rsplit("/", 1)[-1]
                return self._json(200, get_june_detail(gid, refresh=refresh, start=start, end=end))
            if path.startswith("/api/project/"):
                gid = path.rsplit("/", 1)[-1]
                return self._json(200, get_detail(gid, refresh=refresh))
            if path == "/" or path.startswith("/index"):
                return self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode())
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
