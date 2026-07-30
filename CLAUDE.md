# CLAUDE.md

Guidance for Claude Code when working in this repo (a fork of screeps-grafana). It is the
telemetry backend for the **creeps-claude** Screeps bot (sibling repo): a poller reads the bot's
metrics segment from the Screeps API and ships them to Graphite → Grafana.

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

## Deploying the dashboard (do this yourself — don't ask)

The dashboard is **`sampleDashboard.json`** (already wrapped as the API payload:
`{dashboard, folderUid:"", overwrite:true}`, uid `screeps-overview`). The live Grafana
(`https://example.com/screeps-grafana/`, v13) is updated **via its HTTP API with a service-account
token**, NOT by redeploying the docker stack.

The token is in the **macOS Keychain**. Read it straight into a variable and pipe into curl —
**never print it to stdout**:

```bash
TOKEN=$(security find-generic-password -s screeps-grafana-token -a grafana -w)

# Push the local dashboard to live (overwrite:true → replaces by uid, version conflicts ignored):
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data @sampleDashboard.json https://example.com/screeps-grafana/api/dashboards/db
# → {"status":"success", ... "version":N}

# Pull the LIVE dashboard (to diff against before editing — live is the source of truth, the repo
# file can drift):
curl -s -H "Authorization: Bearer $TOKEN" \
  https://example.com/screeps-grafana/api/dashboards/uid/screeps-overview
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
  "https://example.com/screeps-grafana/render/d-solo/<dash-uid>/?panelId=<id>&var-shard=shard1&from=now-6h&to=now&width=1400&height=560&theme=light"
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
  "https://example.com/screeps-grafana/api/datasources/proxy/uid/dfnetfig270g0c/render?target=<expr>&from=-20min&format=json"
```

Always print the number of points and the step you actually got before trusting a comparison, and do
not pass `maxDataPoints` — that is what triggered the worst of it (the whole 2-year archive at a 1-hour
step for a 40-minute request).

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
0 * * * *  /opt/screeps-grafana/tools/tg_rooms_digest.sh >>/var/log/screeps-digest.log 2>&1
```

**The schedule is NOT in the crontab.** Cron wakes the script hourly; the script compares the current
Moscow hour against `SEND_AT_MSK_HOURS` (currently `0,3,6,9,12,15,18,21`) and exits silently otherwise.
Reason: the box runs `Europe/Amsterdam` and its cron (vixie 3.0pl1) has **no `CRON_TZ`** support — the
binary has no such string and `man 5 crontab` does not mention it — so a crontab line written in local
time would slide an hour against Moscow at every DST switch. **To change the times, edit
`SEND_AT_MSK_HOURS` in the env file; leave the crontab alone.**

Config — `/etc/screeps-grafana-digest.env`, root-only `chmod 600`, deliberately outside git:

| key | meaning |
|---|---|
| `GRAFANA_TOKEN` | service account `sa-1-cron_viewer`, **Viewer** role — render + dashboard read only |
| `TG_BOT_TOKEN` | `@your_screeps_bot`, a bot dedicated to this (NOT the concierge bot) |
| `TG_CHAT_ID` | the owner's private chat |
| `SEND_AT_MSK_HOURS` | comma list of Moscow hours to send at |
| optional | `THEME` (dark — reads better in Telegram), `WIDTH`, `HEIGHT`, `FROM`, `CAPTION`, `SHARD`, `PANEL_TITLE`, `DASH_UID`, `BASE` |

Operational notes, each one a bug that was avoided or fixed:

- The panel is resolved **by title**, never by a pinned id: `add_rooms_overview.py` assigns a fresh
  panel id every run, so a hardcoded id starts rendering something else after the next dashboard edit.
- Grafana is addressed as `http://127.0.0.1:1337/screeps-grafana` (published port, not the public URL),
  so the digest survives a broken proxy or an expired certificate. The subpath is still required.
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

## Secrets — where each one lives

Nothing secret belongs in this repo. Read from the Keychain into a variable and pipe it onward; when a
value has to reach the VPS, send it on **stdin**, never as an argv element (argv is visible in `ps` and
lands in transcripts).

| what | where | used for |
|---|---|---|
| Grafana token `claude` (Editor) | macOS Keychain `-s screeps-grafana-token -a grafana` | editing dashboards from a laptop |
| Grafana token `cron_viewer` (Viewer) | Keychain `-s screeps-grafana-viewer -a grafana` **and** `/etc/screeps-grafana-digest.env` on the VPS | the Telegram digest's render call |
| Telegram bot token | Keychain `-s screeps-tg-bot -a token` **and** the same env file | `sendPhoto` |
| `RENDERER_TOKEN` | `/opt/screeps-grafana/docker-compose.env` (gitignored) | grafana ↔ renderer |
| Screeps auth token | same `docker-compose.env` (`SCREEPS_TOKEN`) | the poller |

```bash
# add or replace a Keychain item WITHOUT the value touching history: -w last, prompts hidden
security add-generic-password -s screeps-tg-bot -a token -U -w
```

Two limits worth knowing before planning work: the `claude` token **cannot create service accounts**
(`serviceaccounts:create` denied), so a new scoped token has to be minted in the UI; and default
`admin:admin` is disabled (401), so there is no admin fallback.

A Telegram bot cannot message a chat that never wrote to it first — the owner must `/start` the bot,
then `getUpdates` yields the `chat_id` (there is no webhook set; `getWebhookInfo` returns an empty url).

## VPS ops (rarely needed — dashboard changes don't require this)

The stack already runs on the VPS; changing the dashboard needs only the API POST above. Only touch
the box for pipeline/poller changes.

- SSH `root@example.com`; stack in `/opt/screeps-grafana` (git clone of `youruser/screeps-grafana`,
  branch `master`, HTTPS origin — `git pull` works without a key).
- Brought up manually (NOT Ansible — the `roles/`/`inventory` are stale):
  `cd /opt/screeps-grafana && docker compose --env-file docker-compose.env up -d` (uses
  `docker-compose.yml`, not `.prod.yml`). Containers `screeps-grafana-{node,statsd,graphite,grafana}-1`.
- `node` ships to `statsd` over **UDP**, so a dead statsd loses metrics silently (node log:
  `EAI_AGAIN statsd`). If you recreate statsd, `docker compose restart node` (node caches its IP).
- Containers: `screeps-grafana-{node,statsd,graphite,grafana,renderer}-1`. `renderer` is the
  image-renderer sidecar (Chromium; the Grafana image has none) — grafana reaches it at
  `http://renderer:8081/render` and it fetches panels back through `GF_RENDERING_CALLBACK_URL`, which
  must carry the `/screeps-grafana/` subpath because `SERVE_FROM_SUB_PATH` makes that the real
  internal path. Both sides share `RENDERER_TOKEN` from `docker-compose.env`: **grafana refuses to
  boot** with the built-in default token, so a fresh clone must set it (`openssl rand -hex 24`).
- **Recreating grafana used to break the site in two ways; both are now fixed in `docker-compose.yml`,
  so don't reintroduce them.** (1) The `GF_SERVER_*` subpath env lived only on the running container —
  a recreate dropped it and Grafana served links from the domain root. (2) The reverse proxy
  (`proxy-nginx`, a *different* compose project) proxies to the literal host `grafana`, which it
  can only resolve on its own network `proxy_default`; the container had been attached to it
  by hand, so a recreate detached it and the site answered **502**. The compose file now declares that
  network as external and joins grafana to it with the `grafana` alias.
- After grafana's container IP changes, nginx still holds the old one — `docker exec proxy-nginx
  nginx -s reload`. If reload reports `host not found in upstream "grafana"`, grafana is not on
  `proxy_default` (check `docker inspect ... .NetworkSettings.Networks`), and reloading
  cannot fix it until it is.
- A poller/shard/token change is: VPS `git pull` + edit `docker-compose.env`
  (`SCREEPS_SHARD` + `SCREEPS_TOKEN` parallel lists) + `docker compose restart node`, THEN POST the
  dashboard (paths change with shards → panels go blank until re-POSTed).
- Not everything on the box is ours: `countdown-*` (site + nginx + certbot), `concierge-*` (a separate
  Telegram bot with its own `TELEGRAM_TOKEN`), `amnezia-*` and `dante-proxy` are unrelated services
  sharing the host. The only cross-project coupling is nginx, above. `cron` is now installed and
  enabled — root's crontab holds exactly one entry, the digest.
- The box lives in `Europe/Amsterdam` (not UTC, not Moscow). Anything scheduled in Moscow time has to
  convert or self-gate; don't change the system timezone, other services depend on it.
