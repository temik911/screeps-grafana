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
