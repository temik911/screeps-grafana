# CLAUDE.md

Guidance for Claude Code when working in this repo (a fork of screeps-grafana). It is the
telemetry backend for the **creeps-claude** Screeps bot (sibling repo): a poller reads the bot's
metrics segment from the Screeps API and ships them to Graphite → Grafana.

**Conventions in this file.** `$GRAFANA` is your Grafana base URL (`http://localhost:1337` locally, or
whatever your reverse proxy serves — see `docker-compose.proxy.yml`), and `$TOKEN` a Grafana API token.
Nothing here is tied to a particular host.

## Pipeline (how a metric gets on screen)

```
creeps-claude bot                 this repo (node poller)            grafana
─────────────────                 ──────────────────────            ───────
StatsProcess.writeStatsSegment    src/ScreepsStatsd.js               dashboard panels
  → RawMemory.segments[15]  ──►  GET /api/user/memory-segment  ──►  statsd :8125/UDP
     (JSON blob, per tick)          every ~10s, per shard              → graphite (whisper)
                                    report(): recursively flattens        → Grafana queries
                                    nested objects into gauges              stats.gauges.$shard.*
```

- **`src/ScreepsStatsd.js#report`** recursively walks the segment JSON and emits every **numeric
  leaf** as a StatsD gauge named `stats.gauges.<shard>.<dotted.path>`. There is **no whitelist** —
  any new field the bot adds to its segment (e.g. `snap.construction.sites`) automatically becomes
  `stats.gauges.<shard>.construction.sites` in Graphite. So **adding a metric never needs a poller
  change** — only a bot-side emit + a dashboard panel to display it.
- Metrics are namespaced per shard (`stats.gauges.<shard>.*`); the dashboard has a `$shard` template
  variable (dropdown). Multi-shard: `SCREEPS_SHARD=shard2,shard3` + parallel `SCREEPS_TOKEN` list
  (per-shard tokens — the segment endpoint is rate-limited 360/h **per token**).

## Two dashboards, only one of which lives in the repo

| uid | title | source of truth | tooling |
|---|---|---|---|
| `screeps-overview` | Screeps — Overview | `sampleDashboard.json` in this repo | `tools/restructure_dashboard.py` rebuilds the layout |
| `screeps-rooms` | Screeps — Rooms | **live Grafana only — there is no JSON for it here** | `tools/add_rooms_overview.py` (re)creates its top table |

So for `screeps-rooms` the "keep the repo file in sync" rule cannot apply: GET the live dashboard,
change what you mean to change, POST it back. Any script that edits it must be idempotent for the same
reason — `add_rooms_overview.py` removes its own previous panel and un-shifts the rest before
re-inserting, so re-running it never stacks copies.

Its layout: the overview table at the top (all rooms, one row each), then a `$room` row of detail
panels. The `$shard` and `$room` template variables are `query` variables wrapped in
`currentAbove(..., 0)` — see the comment in `add_rooms_overview.py#target` for why (Graphite's
wildcard returns every room we ever emitted, so a plain `rooms.*` lists long-dead prep-claims).

**Adding a column to that table is a two-step deploy, and doing it in one step produces a wrong
table rather than an empty one.** The column titles are assigned by POSITION after the join, so they
are only correct while every query returns data. A metric the bot has only just started emitting does
not exist in Graphite yet: its query comes back empty, the join yields one column fewer, and every
title shifts by one — RCL renders under «Контроллер %», the exchange counter under the new column, and
nothing about it looks like an error. Deploy the bot, wait for the series to show up in Graphite, and
only then run the script.

## Deploying the dashboard (do this yourself — don't ask)

The dashboard is **`sampleDashboard.json`** (already wrapped as the API payload:
`{dashboard, folderUid:"", overwrite:true}`, uid `screeps-overview`). The live Grafana
(v13) is updated **via its HTTP API with a service-account
token**, NOT by redeploying the docker stack.

The token is in the **macOS Keychain**. Read it straight into a variable and pipe into curl —
**never print it to stdout**:

```bash
TOKEN=$(security find-generic-password -s screeps-grafana-token -a grafana -w)

# Push the local dashboard to live (overwrite:true → replaces by uid, version conflicts ignored):
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data @sampleDashboard.json "$GRAFANA"/api/dashboards/db
# → {"status":"success", ... "version":N}

# Pull the LIVE dashboard (to diff against before editing — live is the source of truth, the repo
# file can drift):
curl -s -H "Authorization: Bearer $TOKEN" \
  "$GRAFANA"/api/dashboards/uid/screeps-overview
```

**Curation principle:** treat the LIVE dashboard as authoritative — when in doubt, GET it, add only
the panel(s) you intend, and POST back (don't blindly overwrite live with a possibly-stale repo
file, and don't blast in dozens of panels at once). Keep `sampleDashboard.json` in sync with what
you POST so the repo stays deployable.

## Verify a panel by RENDERING it (an image renderer is running)

`/api/ds/query` only proves the QUERY returns data — **transformations, field overrides, units and
cell options all run in the browser**, so a panel can query perfectly and still render "No data" (this
is exactly how the Rooms overview table shipped broken). The stack now runs a
`grafana-image-renderer` sidecar, so render the panel and look at the PNG:

```bash
TOKEN=$(security find-generic-password -s screeps-grafana-token -a grafana -w)
curl -s -H "Authorization: Bearer $TOKEN" -o /tmp/panel.png \
  "$GRAFANA/render/d-solo/<dash-uid>/?panelId=<id>&var-shard=shard1&from=now-6h&to=now&width=1400&height=560&theme=light"
```

Template variables must be passed explicitly (`var-shard=`, `var-room=`) — the renderer has no UI
state to fall back on. When a transformation chain is in doubt, POST a throwaway dashboard with one
panel per candidate chain, render each, compare, then delete it (`DELETE /api/dashboards/uid/<uid>`);
that is how the working chain for the Rooms overview table was found.

A whole dashboard renders from `/render/d/<uid>/` (same params) — useful for checking layout after
panels shift. Add **`&kiosk=true`**: without it the render includes the nav rail and whatever
announcement modal Grafana currently shows, which covers the top panels. Panel height is easy to get
wrong this way — a table that shows every row at `height=560` in a `d-solo` render can still be
clipped inside the dashboard, where its `gridPos.h` is what decides.

## Reading exact values back: use Graphite's own /render, not /api/ds/query

Retention for `stats.*` (`storage-schemas.conf` in the graphite container) is
**`10s:1d, 1m:28d, 10m:1y, 1h:2y`** — the last day is at the poller's own resolution, older data is
rolled up by *average*.

`POST /api/ds/query` is fine for "does this query return anything", but it has answered **short ranges
from a much coarser archive**, ignoring the requested window: a `now-20min` query came back with a
single point covering hours. Rolled-up points are averages, so the values are quietly wrong rather than
missing — the tell that exposed it here was an RCL 8 room reporting `rcl` **7.4**, a bucket average
spanning the level-up, and an ETA that could not be reconciled with the rate next to it.

For exact numbers go through the datasource proxy to Graphite's native API, which honours `from`:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "$GRAFANA/api/datasources/proxy/uid/dfnetfig270g0c/render?target=<expr>&from=-20min&format=json"
```

Always print the number of points and the step you actually got before trusting a comparison, and do
not pass `maxDataPoints` — that is what triggered the worst of it (the whole 2-year archive at a 1-hour
step for a 40-minute request).

### The backend is graphite-web 1.1 (since 09.08.2026) — ask it what it supports

`GET /functions` through the datasource proxy returns the full list (154 entries). **Ask it instead of
guessing**; that endpoint is the main practical gain of the upgrade.

Until 09.08.2026 the container ran `graphite_api` on Python 2.7 — a fork frozen at the 0.9 function
set. Everything added in 1.1 answered 500 with an HTML **Python traceback** ending in
`KeyError: u'<funcname>'`, which is why a client expecting JSON just failed to parse: `aggregate`,
`groupByNodes`, `filterSeries`, `divideSeriesLists`, `pow`, `interpolate`, `delay`, `applyByNode`,
`sortBy`, `movingSum`. All of them work now — see `UPGRADE_GRAPHITE.md` for how the swap was done and
what was compared.

**`currentAbove(x, 0)` no longer keeps zeros — it now compares STRICTLY.** In `graphite_api` a series
sitting at exactly 0 survived, and the Rooms table leaned on that to print real zeros instead of blank
cells. Measured on a copy before switching: `rooms.*.sitesRemaining` returned **16 series on the old
backend and 0 on the new one**, because no room had construction pending. That is worse than a blank
column — a query returning no series at all costs the join a frame and shifts every column title by
one, silently. Use **`removeEmptySeries(x)`**: it drops a series only when the whole window is null
(the dead-room test), keeps zeros, and behaves identically on both backends, which is why the
dashboards were migrated to it *before* the upgrade rather than during it.

Two habits from the old backend that are no longer forced, but stay true:

- **Per-series arithmetic across a wildcard.** `diffSeries` still collapses a wildcard into ONE series;
  the escape hatch `applyByNode` now exists, so "value now minus value 3h ago, per room" is finally
  expressible server-side. The signed net is still computed bot-side, and that remains the better
  place for it.
- **Growth over the query window** = `integral(nonNegativeDerivative(counter))` — still correct for a
  cumulative counter, and the window is whatever the panel asks for. **But do not reach for
  `nonNegativeDerivative` on a gauge that legitimately falls**: the rampart shell's total hits drops
  every time the filler walks to storage (decay does not pause), and discarding those falls drew the
  wall growing twice as fast as it does. For those, `derivative` + `summarize(..., 'avg')`.

## Don't smooth a metric the bot already smooths

`controllerEtaHours` is computed from an upgrade rate the bot averages over 6000 ticks (~6.8 h). The
overview ETA chart used to wrap it in `movingAverage(…, '6h')` — added when the metric came off a
600-tick window and was too jumpy to read — which after the bot-side change smoothed an already-smooth
series and added hours of lag. Before adding a Graphite-side rolling function, check whether the metric
is already averaged in `StatsProcess` (`docs/TELEMETRY.md` in the bot repo says which are).

Three panels display that metric: `screeps-overview` #46 (timeseries, log axis — the values span hours
to weeks), and on `screeps-rooms` the overview table column «До апа, ч» plus the per-room stat panel
#103. All three read the gauge raw.

## Telegram digest — the Rooms table as a photo every 3 hours

`tools/tg_rooms_digest.sh` renders the "Комнаты — обзор" panel and posts it with `sendPhoto`. It runs
from **root's crontab on the VPS** (`cron` was not installed on that box — it was added for this).

```
0 * * * *  /path/to/screeps-grafana/tools/tg_rooms_digest.sh >>/var/log/screeps-digest.log 2>&1
```

**The schedule is NOT in the crontab.** Cron wakes the script hourly; the script compares the current
Moscow hour against `SEND_AT_MSK_HOURS` (currently `0,3,6,9,12,15,18,21`) and exits silently otherwise.
Reason: the box runs `Europe/Amsterdam` and its cron (vixie 3.0pl1) has **no `CRON_TZ`** support — the
binary has no such string and `man 5 crontab` does not mention it — so a crontab line written in local
time would slide an hour against Moscow at every DST switch. **To change the times, edit
`SEND_AT_MSK_HOURS` in the env file; leave the crontab alone.**

Config — an env file outside git, root-only `chmod 600` (the script reads `$CONF`, default
`/etc/screeps-grafana-digest.env`):

| key | meaning |
|---|---|
| `GRAFANA_TOKEN` | a service account with the **Viewer** role — render + dashboard read only |
| `TG_BOT_TOKEN` | a bot dedicated to this — not one you use for anything else |
| `TG_CHAT_ID` | the owner's private chat |
| `SEND_AT_MSK_HOURS` | comma list of Moscow hours to send at |
| optional | `THEME` (dark — reads better in Telegram), `WIDTH`, `HEIGHT`, `FROM`, `CAPTION`, `SHARD`, `PANEL_TITLE`, `DASH_UID`, `BASE` |

`FROM` defaults to **`now-3h`, and that is load-bearing, not cosmetic**: the table's «Обмен» column
shows how far the cumulative share counter MOVED over the rendered window, so the 3h window is what
makes it mean "since the previous picture". If `SEND_AT_MSK_HOURS` is ever changed to a different
cadence, `FROM` has to move with it or that column will span more than one digest.

**`WIDTH`/`HEIGHT` are sized to the panel and have to be re-checked whenever it grows.** Both failure
modes are silent — the render succeeds, the photo is just missing part of the table. Width follows the
pinned columns (1000 clipped the seventh, 1200 clipped the eighth «План», 1320 clipped the ninth
«Рампарт, мин HP» — the rampart columns added 09.08.2026 cost 260px between them; now **1440**, against
1385 of declared column widths); height follows the `H` grid units in `add_rooms_overview.py` (620 cut
the list off at 16 rooms; now **700**, which fits 16 rooms with a row to spare).
**Adding a column or an owned room means re-rendering the panel and looking at the PNG** — and this is
not hypothetical bookkeeping: the ninth column shipped in the morning and the digest was posting a
truncated table until the evening, with nothing anywhere reporting a problem.

Sum the `custom.width` overrides in the panel to get the number instead of guessing — that is what
1385 above is. Deliberately tight rather than generous: Telegram scales the image to the phone's
width, so every unused pixel makes the text smaller.

Operational notes, each one a bug that was avoided or fixed:

- The panel is resolved **by title**, never by a pinned id: `add_rooms_overview.py` assigns a fresh
  panel id every run, so a hardcoded id starts rendering something else after the next dashboard edit.
- Grafana is addressed as `http://127.0.0.1:1337` (published port, not the public URL),
  so the digest survives a broken proxy or an expired certificate.
- A renderer that is down or timing out answers **HTTP 200 with an HTML error page**, so the script
  checks the PNG magic bytes rather than trusting the status code.
- Test in cron's stripped environment, not just your shell:
  `env -i PATH=/usr/bin:/bin HOME=/root DRY_RUN=1 FORCE=1 tools/tg_rooms_digest.sh`.
- Manual run: `FORCE=1 …` (bypasses the hour gate), `DRY_RUN=1 FORCE=1 …` (renders, sends nothing).
- Log: `/var/log/screeps-digest.log`, one line per send; skipped hours write nothing.

**Why cron and not Grafana itself** (checked on the live instance, don't re-litigate): Telegram *is* a
native contact point (`bottoken` + `chatid` — a bot token is needed either way), but Grafana OSS has no
scheduler — `/api/reports/settings` returns **404**, reporting is Enterprise. The only native way to
get a periodic image would be an always-firing alert rule plus a notification `repeat_interval`, which
means a permanently red fake alert and alert-shaped messages. `GF_UNIFIED_ALERTING_SCREENSHOTS_CAPTURE`
would also have to be turned on (grafana restart + nginx reload).

## `tools/restructure_dashboard.py` — the dashboard is GENERATED, not hand-laid

`sampleDashboard.json`'s layout is produced by this script; **do not hand-place panels and expect
them to stick**. Each run REBUILDS the whole dashboard from two Python-side definitions:

- **`STATUS_STRIP`** — 8 always-visible `stat` panels (ids 101-108) across the top (y=0..3), with
  traffic-light thresholds. Edited in the script itself.
- **`ROWS`** — ordered list of `(title, [content-panel-ids in display order], collapsed)`. For each
  entry the script emits a `row` panel (ids 200+) and reflows the listed content panels under it,
  recomputing only `gridPos.y` (side-by-side pairings preserved from each panel's original `x`).

Rebuild logic (`main()`): strip all prior `row` panels + the status strip + row separators, index
the rest `by_id`, then re-emit status strip + each row with its content panels. **Any content panel
whose id is NOT in some `ROWS` entry and NOT in `DROP_IDS` is treated as an orphan and DROPPED** (a
`WARNING: panels with ids [...] are not in any row, will be DROPPED` line prints). Idempotent: same
input + same ROWS → same output.

### Adding a panel (the correct workflow)

1. Append the panel object(s) to `sampleDashboard.json` with a fresh content id (copy an existing
   timeseries panel as a template so `datasource:"localGraphite"`, `fieldConfig`, `options` match;
   set `title`, `targets` (Graphite `alias(stats.gauges.$shard.<path>, '…')`), and `fieldConfig`
   `unit`). A **row** you add by hand will be stripped — rows come from `ROWS`, not the JSON.
2. Add the content id(s) to the right `ROWS` entry (or a new `('Title', [ids], False)` tuple).
3. `python3 tools/restructure_dashboard.py` — reflows everything; check the printed panel count and
   that there's **no unexpected orphan warning**.
4. POST `sampleDashboard.json` to the API (above). Commit both the JSON and the script change.

> Pitfall (learned the hard way): appending a panel + `type:"row"` straight into the JSON and
> POSTing makes it show up **once**, but the next `restructure_dashboard.py` run silently drops it
> (orphan) and rebuilds the row from scratch. Always route new panels through `ROWS`.

**That pitfall had already fired — thirteen times — before anyone noticed (07.08.2026).** Adding the
remote-profitability panels meant running `restructure_dashboard.py`, and it warned that ids
`242, 243, 245-251, 253-256` were in no row and would be dropped: the six Power-creep panels, the four
inter-room-sharing ones, both Movement panels and `REGEN_MINERAL`. Two entire rows — *Power creeps*
and *Inter-room sharing* — existed in the live dashboard and in the JSON, but had never been declared
in `ROWS`. Nothing was broken yet only because nobody had run the script since; the next run for any
unrelated reason would have taken them out, and the POST after it would have made that permanent.

They are adopted now (`ROWS` has both rows, each panel at the position it already occupied) and a run
is clean: **94 panels in, 94 out, zero orphans**. Two habits follow:

- **Read the orphan warning even when it is about someone else's panels.** It prints on a run you
  started for your own reasons, and it is the only signal you get.
- **Pick a fresh id by scanning every id in the file, not by eyeballing a range.** 241-243 look free
  in the `<100` content block and are not — they belong to the controller, movement and route-policy
  panels. Taking them silently replaced three working panels; caught by `git diff`, reverted, moved to
  301-303. `git checkout sampleDashboard.json` is the undo, so commit before experimenting.

### `tools/add_remote_profit.py`

Adds the three remote-economics panels (ids 301-303, row *Remote mining*): profitability over time,
current standing as a bar gauge, and measured harvest beside them. Idempotent by id, like
`add_rooms_overview.py`. The bot must ship `remotes.*.profit` and `remotes.*.harvested` **first** —
a panel over a series Graphite has never seen renders empty (harmless here, unlike the rooms table
where a missing series shifts every column title).

Note on the bar gauge: it is **not** sorted worst-first, and the title no longer pretends otherwise.
Grafana sorts bar gauges by series name client-side, and this backend has no `sortBy` to do it
server-side (`sortByMinima` exists but returns the series in name order anyway). Colour carries the
signal instead — red is a remote costing more than it earns.

## Secrets

Nothing secret belongs in this repo, and nothing secret is in its history. `docker-compose.env`
(Screeps auth token, `RENDERER_TOKEN`) is gitignored; anything else a tool needs is read from the
environment or an OS keychain at call time.

Two Grafana limits worth knowing before planning work: an Editor-role token **cannot create service
accounts**, so a new scoped token has to be minted in the UI, and default `admin:admin` is disabled
once you change it, so there is no admin fallback.
