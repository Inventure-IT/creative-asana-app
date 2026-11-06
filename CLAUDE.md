# Creative Team Hours Dashboard

## What this is

A dashboard for the Creative Team showing hours billed to projects, pulled live from Asana.
The audience is the **project manager and leadership** — so the bias is always toward *more
statistics, clearly presented*: capacity vs. usage, per-person load, per-project rollups,
over/under-budget signals. When adding a view, ask "what decision does this let a PM make?"

Two deploy targets, one UI:

| File | Role |
| --- | --- |
| `creative_asana_app.py` | The local app **and the single source of truth for all front-end HTML/CSS/JS** (the `PAGE` string, ~line 509 onward). Serves `/api/*` from Python, keeping the PAT server-side. |
| `build_static.py` | Generates `index.html` for GitHub Pages. Reimplements the Python aggregation in JS and shims `fetch()` so the same `/api/*` routes work browser-side, with a PAT login screen. |
| `index.html` | **Generated artifact — never hand-edit.** |

## The one rule that matters most

**Edit the UI only in `creative_asana_app.py`, then run `python build_static.py` to regenerate
`index.html`.** The build needs no Asana token and takes about a second. Hand-editing
`index.html` silently drifts the two apart, and the next build wipes it.

If you change data logic (aggregation, filters, new fields), you usually have to change it
**twice**: the Python implementation in `creative_asana_app.py` *and* the mirrored JS
implementation in `build_static.py`'s `PREPEND`. Keep the two functions named alike
(`project_detail` / `projectDetail`, `get_assignee_load` / `getAssigneeLoad`) so the pairing
stays obvious.

**The UI script may only use identifiers it defines itself, plus `TEAM_MEMBERS`,
`PROJECT_ROSTER`** (the configured project names, in `PROJECTS` order), **`ARCHIVED_GIDS`,
`FIREBASE_CONFIG`, `PLANNING_DOMAINS` and `TEAM_CAPACITY_EXCLUDE`**. `PREPEND` runs before
it in the static build but *not* in the local app, which injects only those — if the UI
script ever needs another config value, add it to **both** `render_page()`'s boot script and
`PREPEND`. Reaching for a `PREPEND` helper (`round2`, `sum`, `asanaGet`, …) from the UI
script works on GitHub Pages and throws a `ReferenceError` locally. The UI script has its own
`r2()` for rounding — use that.

## Configuration lives at the top of `creative_asana_app.py`

`PROJECTS`, `GROUPS`, `ARCHIVED_GIDS`, `EST_FIELD`, `EXCLUDE_SECTIONS`, `ASSIGNEE_HOURS_CAP`, `TEAM_MEMBERS`,
`DEFAULT_START`/`DEFAULT_END`. `build_static.py` imports these and injects them into the static
build, so **adding a project or team member is a one-line change plus a rebuild.** Never
duplicate a project list or a team roster anywhere else.

- `TEAM_CAPACITY_EXCLUDE` lists projects (gid, or lower-cased name for a portfolio project
  whose gid isn't known yet) left out of **both Team Capacity views** only — currently the
  Archive Board. Estimated skips them in `get_assignee_load` / `getAssigneeLoad`; Logged charts
  the per-person `teamHours` / `teamUnbillable` that `loadPersonStats` totals without them, and
  filters them out of the donuts and the drill-in (`teamCapacityExcluded`). Every other tab still
  reports them.

- **Projects also come from the Asana portfolio `PORTFOLIO_GID`**, read on each load and on
  Refresh (`sync_portfolio` / `syncPortfolio`, which rebuild `PROJECTS`, `EST_PROJECTS`,
  `ARCHIVED_GIDS` and the name/cap/kind maps *in place*). A new portfolio project appears with
  defaults; add it to `PROJECTS` only to give it settings. Archived-in-Asana projects are
  archived automatically, and summary rows carry `archived` so the page (whose injected
  `ARCHIVED_GIDS` is fixed at load) still splits them off.
- A project's `cap` is its **monthly hour capacity**; omit it for projects with no budget.
- A project's optional `kind` classifies the engagement (e.g. `"Support"`); it rides on the
  logged-hours rows and shows as a tag on the project's MSA Project Capacity card.
- `GROUPS` are several projects sharing one combined monthly cap (e.g. CMD). Members still
  appear individually in every other tab.
- `CARVE_OUTS` cut a budget out of another board by task name (e.g. Update Tuesday from
  Internal IIT Backlog): capacity = the matching open tasks' estimated hours, usage = hours
  logged to them and their subtasks. They add a card to MSA Project Capacity (and count in its
  summary bar) via `/api/carveouts`; the source project still reports the tasks everywhere else
  — except **both Team Capacity views**, which show a name-matched carve-out as its own slice,
  using the same `carve_out_hits` rule. Logged: `logged_detail` tags each entry `carve`, and
  `loadPersonStats` builds `teamSliceCache` from it. Estimated: `carve_splits` /
  `carveSplits` split `get_assignee_load`'s breakdown, the board keeping the rest, with each
  entry carrying `board` so Bar Chart · Estimated still groups per project. Whole-board
  carve-outs (empty match) aren't split.
- `UNBILLABLE_GIDS` marks projects whose logged time is unbillable; a carve-out's tasks on
  such a board stay billable. Each logged entry carries `billable`, and Team Capacity · Logged
  stacks billable (solid) under unbillable (faded) per person.
- `ARCHIVED_GIDS` is the set of archived project gids. They still appear on the card/list
  tabs (Project Cards, MSA Project Capacity), but under an **Archived projects** section
  below the active work and outside the summary stats — the UI splits them off via the
  injected `ARCHIVED_GIDS` / `isArchived()`. They are excluded from the aggregate estimated
  views: `EST_PROJECTS` is `PROJECTS` minus that set, and `get_assignee_load` /
  `getAssigneeLoad` map over it (which is what Team Capacity · Estimated and Bar Chart ·
  Estimated are built from). Every other endpoint keeps all of `PROJECTS`.

## Running it

```
setx ASANA_PAT "your_token"      # once; also read from the HKCU Environment registry key
python creative_asana_app.py     # serves http://localhost:8765 and opens a browser
python build_static.py           # regenerate index.html for Pages
```

**Sign-in (both builds)** lives in the UI script (`bootAuth` / `renderLogin`): Google on
`PLANNING_DOMAINS`, then the user's Asana token from Firestore `users/{uid}.asanaPat` (prompted for
and saved on first sign-in, or when Asana rejects the saved one). The token is handed over through
`useToken()`: the static build defines `window.__useToken` (token stays in the browser); locally it
is POSTed to `/api/token`, which validates it and switches the server's `TOKEN` via `set_token()`.
`ASANA_PAT` is optional — only used until someone signs in. A "token without Google" link remains
as a fallback.

## UI conventions — follow these exactly

The dashboard is **dark-mode only** and must read as one system. Do not introduce a second
visual language for a new tab.

### The page skeleton every tab follows

In order, top to bottom — do not reorder, and do not skip a step:

1. `<h1>` page title (from `TABS[key].title`) + Refresh button, then the one-line `sub`.
   Every tab has both. Titles are qualified (`Team Capacity · Estimated` vs `· Logged`)
   because several tabs share a short sidebar label.
2. A `summaryBar([...])` of headline stats — the numbers a PM should get without scrolling.
3. **One** `.toolbar`. Never stack two. Range-based tabs call `rangePicker(extra)` and pass
   their own controls in as `extra`; each control group starts with a `<span class="tb-label">`.
4. The chart (`.chart-box`) and/or the tables, with `.section-h` headings (`.flush` on the
   first one, which has no chart above it to sit under).

Detail pages (`#/p/…`, `#/logged/…`) use the same skeleton, with breadcrumbs in a `.crumbs`
row *above* the `<h1>`. They render straight onto the page background — `.panel` is only for
the Settings form.

### Color

Never hardcode a hex for chrome — use the CSS variables in `:root`:

`--bg` page · `--panel` cards · `--panel2` inset/raised surfaces & inputs · `--border` ·
`--text` · `--muted` secondary · `--faint` tertiary · `--blue`/`--blue-d` (estimated-hours
accent, buttons, active nav) · `--green`/`--green-d` (actual/logged-hours accent) ·
`--red` (over budget).

The blue/green split is semantic and load-bearing: **blue = estimated/planned, green =
actual/logged.** A "hours logged" card is `.card.logged`; an estimated card is plain `.card`.

Canvas can't read CSS variables, so the same palette is mirrored in the `C` object
(`C.blue`, `C.green`, `C.red`, `C.panel2`, `C.amber`, …). Use `C.*` for anything drawn on a
chart and the CSS variables for everything else — never a bare hex in either place, and keep
the two definitions in lockstep.

**Themes** (Settings › Theme) are per person. A `THEMES` entry retints every surface (`surf`:
bg, panel, panel2, border, text, muted, faint) and swaps the `--blue`/`--blue-d` (`est`) and
`--green`/`--green-d` (`act`) pairs. `themeTones()` mixes the in-between greys (`--hover`,
`--border-h`, `--sep`, `--cap-border`, `--disabled`, `--disabled-t`, `--blk-off`, chart ticks)
unless a theme pins them (Classic does, so it matches `:root` exactly). `applyTheme()` sets the
CSS variables *and* mutates `C`, so read colors at draw time from `C.*` or the variables — never
cache one in a constant, and never hardcode a grey in the CSS (add a variable, then set it in
`applyTheme`). Every theme stays dark, keeps estimated and logged clearly distinct, and avoids
red, orange, amber and gold. The Theme tab orders cards by panel hue (`themeOrderKey`: neutrals
first, then teal → pink), so a new preset needs no manual placement. A person can also build
their own **Custom** theme (id `custom`) from the eight `CUSTOM_FIELDS` colors
(`customThemeEntry` mixes the rest); colors pass `cleanCustom` (strict `#rrggbb`) because they
land in style attributes, and live edits repaint only the Custom card so the open picker isn't
detached.

**Per-person preferences** live on the user's own Firestore doc `users/{uid}` (the one holding
`asanaPat`), written through `saveUserPrefs()`, which replaces whole fields (`updateDoc`) so a
reset value really goes away: `accentTheme` (`setTheme`), `customTheme` (`updateCustomTheme`) and `graphColors` = `{ people, projects }`
(`queueGraphColorsSave`, debounced, called by `savePersonColorConfig` / `saveProjectColorConfig`).
`savedPat` applies both on sign-in (`adoptGraphColors`). Both are also cached in `localStorage`,
which is all a token-only sign-in gets.

Chart data colors come from helpers, never literals:
- `personColor(name)` — stable per-person hue, shared across every per-person chart, and
  user-overridable in the Graph Colors tab (saved per person, see above). Use it anywhere a
  series represents a person.
- `projectColor(name)` — stable per-project hue for the donuts, off its own `PROJECT_PALETTE`
  (longer than the project roster, so no two projects ever share a color). Colors are claimed
  for the whole `PROJECT_ROSTER` up front, so a project looks the same on the Estimated and
  Logged donuts. Pin one by hand in `PROJECT_COLORS` (Georgia Grown is green) — those hues are
  reserved and never auto-assigned. People override them per person in Graph Colors › Projects
  (the donuts have no legend of their own), which lists the roster, the carve-outs, and every
  name `projectColor` has drawn (`_seenProjects`).
- `PERSON_PALETTE` deliberately contains **no orange/amber/gold**, because `#f0c674` is
  reserved for the per-bar capacity marker. Keep it that way. `Unassigned` is always grey
  (seeded into `_personColors`); real people get their color from the Graph Colors tab.
- `capColor(hours, cap)` — Team Capacity · **Logged**: green within `CAP_TOLERANCE` (15 h) of
  target, red outside it. Red means "needs attention" (over *or* under booked), not merely "over".
- `capColorEst(hours)` — Team Capacity · **Estimated**: green above `CAP_GREEN_MIN` (80 h)
  remaining, red at or below it (that person has room and needs work assigned).

### Layout & components

- Every page is `.layout` = sticky `.sidebar` + `.content`. Detail pages keep the same
  sidebar so nav never jumps.
- Nav is data-driven: add a tab to `TABS` (label + title, optional `sub`) and list its key
  in the right `NAV_SECTIONS` group ("Estimated Hours" / "Actual Hours" / "Settings").
  Don't hand-write nav markup.
- Card grids are `.grid` with `.card`; reuse `estCard` / `loggedCard` / `capCard` / `groupCard`
  rather than writing new card HTML. Stats inside a card are `.stats > .stat > .n` + `.l`
  (uppercase micro-label). A number that has gone the wrong way gets `.neg`.
- Filter bars are `.toolbar` (`.tb-label`, `.chk`, `.tb-sep`). Charts go in `.chart-box`.
- Drill-down tables are `table.tasks` with `tr.parent` / `tr.sub`; numeric cells use `.hours`,
  nesting uses `.lvl1` / `.lvl2` (never an inline `padding-left`).
- Loading and empty states go through `note()` / `noteBox()` (the latter fills a `.chart-box`)
  with the shared `LOADING` string — never a bespoke `<p class="muted">Loading…`.
- Breadcrumbs via `setCrumbs()`. Routes are hash-based: `#/p/<gid>` (estimated detail),
  `#/logged/<gid>` (logged-hours detail), `#/grp/<name>` (budget group), `#/carve/<name>`
  (carve-out: only its matching tasks); anything else is the dashboard.

### Numbers & text

- Always format hours with `h2()` — two decimals, every time (`40.00`, not `40`). No bare
  `.toFixed(2)`; percentages are `.toFixed(0)`.
- Counts read out in prose use `plural(n, 'task')` — never `3 task(s)` or a hand-rolled ternary.
- Any column of numbers gets `font-variant-numeric: tabular-nums` (`.hours`, `.stat .n`).
- Escape all Asana-sourced strings with `esc()` before interpolating into HTML.
- Unassigned work is the literal string `Unassigned` (always grey); tasks in no status
  column are `NO_STATUS` (`No status`), rendered as a `<span class="badge none">`.
- People axes are titled `Assignee` on estimated charts and `Logged by` on actual ones;
  value axes are `Remaining hours`, `Estimated hours`, or `Hours logged`.
- Date ranges render through `rangeLabel()` / `rangePicker()`; don't roll a new date UI.

### Charts

Chart.js 4 via CDN, dark defaults set once at the top of the script. Every render path must
call `destroyCharts()` first (the router does this) — new charts must be pushed into
`donutCharts` or assigned to `chart` so they get torn down. Custom plugins live next to the
existing `capLinePlugin` / `capMarksPlugin` and are enabled per-chart through
`options.plugins`.

## Data conventions

- Estimated hours come from the `Estimated time` custom field (**stored in minutes**);
  actual hours from `actual_time_minutes` and per-task time entries. Convert to hours at the
  edge, round with `r2` / `round2`.
- Tasks in an excluded status column (`EXCLUDE_SECTIONS`, currently `completed`) don't count
  toward estimated-hours totals.
- Everything is cached in memory (`CACHE`) until the user clicks **Refresh** (`?refresh=1`).
  Navigation must never re-hit the Asana API. Respect the two thread pools — `LEAF_POOL` for
  small per-task calls, `PROJECT_POOL` for whole projects — and never create ad-hoc pools;
  Asana rate-limits hard.
- **Every Asana call goes through `api_get()` / `api_pages()` / `api_batch()`** (`asanaGet` /
  `asanaBatch` in the static build). They keep one connection alive per worker thread, ask for
  gzip, retry 429/5xx, and follow pagination. Never hand-roll a `urllib.request` call or a
  `while next_page` loop.
- **Many small reads of the same shape go through `api_batch()`**, which posts them to Asana's
  `/batch_requests` `BATCH_MAX` (10) at a time — the per-task time-entry reads are ~90% of a
  load. Batching is an optimization only: a rejected batch, a failed action, or a response with
  a `next_page` falls back to a plain GET for that path, so it can cost requests but must never
  lose rows. Keep that fallback if you touch it. **Note: `/batch_requests` returns 404 on this
  workspace**, so in practice the fallback is the live path; the code probes once, warns, and
  disables batching for the session. Console prints per-route call counts
  (`/api/logged  48 Asana calls`) — use it to check a change actually reduced requests.
- **The binding constraint is Asana's per-token rate limit** (1500 requests/minute on paid
  plans, 150 on free), not connections or thread count. Every call passes a token bucket
  (`_take_token`: a 300-call burst, then 20/sec ≈ 1200/min — burst + a minute at the rate = 1500) that halves itself and stops bursting on a 429. So the only way to make a load
  meaningfully faster is to **make fewer calls** — raising concurrency past the limit just
  earns 429s.
- **Fetch a project's tasks once.** `fetch_tree(gid)` returns `{tasks, subs}` and is shared by
  the estimated and logged-hours paths (single-flighted per project, so the two concurrent
  startup requests can't duplicate it); `entries_for_task(gid)` caches time entries, which are
  range-independent, so changing the date range costs no API calls. **Subtasks come nested in
  the task-list read** (`TREE_FIELDS` = `TASK_FIELDS` + `subtasks.<SUBTASK_FIELDS>`), so a tree
  costs only its task-list pages; only a task whose nested list is shorter than its
  `num_subtasks` falls back to a `/subtasks` read. `TASK_FIELDS` / `SUBTASK_FIELDS` are the
  union both paths need — add a field there (it flows into `TREE_FIELDS`) rather than making a
  second request for it.
- **Don't read history no view needs.** `fetch_tasks` / `fetchTasks` ask for open tasks plus those
  completed *or modified* since `tree_since(range start)` (`HISTORY_SLACK_DAYS`, 30, before it) —
  two filtered reads, not the whole board. `fetch_tree(gid, since=…)` serves any caller needing no
  deeper history; an older range fetches deeper, reusing known subtask lists. The estimated side
  passes no `since` (open tasks are all it needs). Time entries are read only for items whose
  `modified_at` is on/after `touched_cutoff(start)` (`is_touched` / `isTouched`): logging time
  always bumps the item's *own* modified_at (verified against live data), so an untouched item has
  nothing in the range — and a carve-out counts its whole tracked total as "before the range".
  Subtask activity does **not** reliably bump the parent, which is what the 30-day slack covers.
- **Startup paints from a saved snapshot.** After a load where both halves are fresh, for the
  default (this-month) range, `saveSnapshot()` writes the loaded data (plus the per-person splits
  and Time Audit details) to `localStorage` `dashSnapshot.v1`; `renderDashboard` opens on it and
  `loadAll` swaps fresh data in as each half lands (`freshEst` / `freshLogged`; `loggedGen` stops
  a per-person load started on old data from overwriting new). Cleared on sign-out. Anything new
  loaded at startup that a tab needs to paint should be added to the snapshot.
- `/api/me` is the PAT owner's display name, used to default the Daily Log's assignee filter to
  "you". Anything that defaults to the signed-in user should read `currentUser`.

## Don'ts

- Don't commit a PAT, and don't add one to `index.html` (the static build asks the user).
- Don't add a build step, framework, or npm dependency. This stays a zero-install,
  two-Python-file app with one CDN script. The one exception is the Firebase SDK, imported
  lazily (ES modules from gstatic) by MSA Project Capacity's "Not this month" planning.

## "Not this month" planning (Firestore)

On MSA Project Capacity, signed-in users drag capacity cards into a **Not this month** section;
those cards leave every total on that tab. Moves are shared live through Firestore, doc
`capacity/{YYYY-MM}` (month of the range start) = `{ out: { <card id>: { by, email, at } } }`,
card ids `grp:<group>`, `p:<gid>`, `carve:<name>`. `FIREBASE_CONFIG` / `PLANNING_DOMAINS` live
at the top of `creative_asana_app.py`; the web config is public by design — access is enforced
by Firestore rules allowing only verified Google accounts on that domain. Card **order** within each
section is shared too (not per month): doc `layout/capacity` = `{ order: [card id, …] }`, applied by
`planSort()`, saved by `planSaveOrder()`. **Capacity overrides** (the "Edit capacity" button) live in doc `settings/capacity` = `{ caps: {
'p:<gid>' | 'grp:<group>': hours } }`; `applyCapOverrides()` lays them over the loaded rows before
every render (configured value kept as `cfgCap`), so every tab's budgets agree. **Holidays** (Settings › Holidays) live in doc `settings/holidays` = `{ days: [{ date, name }] }`,
edited by anyone signed in (atomic `arrayUnion`/`arrayRemove` to add or remove; an edit is a
`runTransaction` swap in `planHolidayReplace`); `DEFAULT_HOLIDAYS` is only the
starting list until that doc exists. `setHolidays()` refills the `HOLIDAYS` set `workdayProgress` reads. The `plan` object and
`plan*` helpers hold the state; a change re-renders from cached data (never re-hits Asana).
- Don't add light-mode styling or a third accent color without a reason that survives the
  blue/green semantics above.
- Don't leave `index.html` out of sync with the Python — rebuild before committing.
