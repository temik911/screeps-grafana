# Setup: from zero to graphs

A complete walkthrough for running this stack yourself and getting your own bot's numbers into it.
Nothing here assumes you have seen the project before.

If you only want the short version: `cp docker-compose.env.example docker-compose.env`, put your
Screeps auth token in it, `docker compose -f docker-compose.local.yml --env-file docker-compose.env up -d`,
open <http://localhost:1337>, and make your bot write JSON to `RawMemory.segments[15]`.

---

## 1. What this actually is

Five pieces, each doing one thing:

```
  your bot                    this repo's poller           statsd → graphite            grafana
 ┌──────────────────┐        ┌─────────────────────┐      ┌───────────────────┐      ┌──────────┐
 │ writes JSON into │  HTTP  │ GET memory-segment  │ UDP  │ stores each number │      │  draws   │
 │ RawMemory        │ ─────► │ every 10s, walks it │ ───► │ as a time series   │ ───► │  panels  │
 │ .segments[15]    │        │ number by number    │      │                    │      │          │
 └──────────────────┘        └─────────────────────┘      └───────────────────┘      └──────────┘
```

The important consequence of that shape: **the bot and the monitoring never talk directly.** The bot
drops a JSON blob into a memory segment once per tick and forgets about it; the poller reads that
segment through the public Screeps API from wherever it happens to run. Your bot needs no network
code, no credentials, and no knowledge that any of this exists.

**Why a segment and not `Memory.stats`.** The API endpoint for the whole of `Memory` allows roughly
1440 requests per day — once a minute. The segment endpoint allows **360 per hour**, six times more
generous, which is what makes a 10-second refresh possible. Upstream's `stats.js` in this repo writes
`Memory.stats` and predates that change; ignore it and follow §4 below.

---

## 2. Prerequisites

- **Docker** with the Compose plugin (`docker compose version` should print something).
- **A Screeps auth token** — <https://screeps.com/a/#!/account/auth-tokens>. Create one with the
  *memory segments* permission. This is the only credential the stack needs, and it is read-only as
  far as your colony is concerned.
- Somewhere to run it. A laptop is fine to try it out, but note the poller has to be **running** to
  collect anything — there is no backfill. For continuous graphs you want a machine that stays up.

---

## 3. Run the stack

```bash
git clone <this repo>
cd screeps-grafana

cp docker-compose.env.example docker-compose.env
$EDITOR docker-compose.env
```

Two values must be filled in:

| variable | what to put |
|---|---|
| `SCREEPS_TOKEN` | your auth token |
| `RENDERER_TOKEN` | any random string — `openssl rand -hex 24`. **Grafana refuses to start** if this is left at the placeholder |

and one you probably want to check:

| variable | default | note |
|---|---|---|
| `SCREEPS_SHARD` | `shard3` | your shard, or a comma list for several — `shard2,shard3` |
| `SCREEPS_SEGMENT` | `15` | any segment 0–99; must match what your bot writes to |
| `SCREEPS_HOST` | `https://screeps.com` | change for a private server |

Then:

```bash
docker compose -f docker-compose.local.yml --env-file docker-compose.env up -d
docker compose -f docker-compose.local.yml logs -f node
```

The poller prints one line per successful fetch. Grafana is on <http://localhost:1337>, initial
login `admin` / `admin`.

> **Use `docker-compose.local.yml`, not `docker-compose.yml`.** The latter is wired for the author's
> VPS — it joins an external network from another compose project and serves Grafana from a subpath
> on a domain you don't own. It will fail to start on your machine, and if you patch around that,
> every link Grafana emits will point at the wrong host.

### Connect Grafana to Graphite

Once, in the UI: **Connections → Data sources → Add → Graphite**, URL `http://graphite:8000`, Save &
test. Then **Dashboards → Import** and paste `sampleDashboard.json` from this repo.

---

## 4. Getting your own metrics in

This is the part that is actually about your code.

### The contract

Write a JSON object to the segment. Every **number** anywhere in it becomes a time series; the series
name is the path to it, dotted, prefixed with `stats.gauges.<shard>.`:

```js
RawMemory.segments[15] = JSON.stringify({
  tick: Game.time,
  cpu: { used: Game.cpu.getUsed(), bucket: Game.cpu.bucket },
  rooms: { W1N1: { rcl: 4, energy: 12345 } },
})
```

gives you

```
stats.gauges.shard3.tick
stats.gauges.shard3.cpu.used
stats.gauges.shard3.cpu.bucket
stats.gauges.shard3.rooms.W1N1.rcl
stats.gauges.shard3.rooms.W1N1.energy
```

Three rules follow from `report()` in `src/ScreepsStatsd.js`, and they are worth internalising
before you design your blob:

1. **Only numbers are emitted.** Strings and booleans are walked past in silence — a status you want
   to see has to be encoded as a number (`0`/`1`, or an index into a list of states).
2. **Nesting is free and is how you group.** `rooms.<name>.<metric>` gives you a wildcard
   (`rooms.*.rcl`) you can graph as one query across every room, and it makes new rooms appear on
   their own without touching the dashboard.
3. **Everything is a gauge — a snapshot, not a rate.** For anything cumulative (resources sold,
   creeps spawned) emit a **running total** and let Graphite derive the rate. A per-tick counter
   would be sampled once every ~30 ticks and mostly read zero.

### Writing the segment

A segment must be *active* before you can write to it, and `setActiveSegments` takes effect on the
**following** tick. So call it every tick and write whenever the segment is available:

```js
module.exports.loop = function () {
  RawMemory.setActiveSegments([15])      // takes effect next tick; cheap, call it every tick

  // ... your main loop ...

  if (RawMemory.segments[15] !== undefined) {   // undefined on the first tick after a global reset
    RawMemory.segments[15] = JSON.stringify(collectStats())
  }
}
```

Two practical notes:

- A segment holds **100 KB**. That is a lot of numbers, but a blob that grows per room per remote can
  reach it; if the write silently does nothing, check the length first.
- Write it **last**, after everything else in the tick has run. CPU measured at the top of the loop
  misses the loop.

### Verify it end to end

```bash
# 1. is the bot actually writing it?
curl -s -H "X-Token: $TOKEN" \
  "https://screeps.com/api/user/memory-segment?segment=15&shard=shard3" | head -c 300

# 2. is the poller reading it?
docker compose -f docker-compose.local.yml logs --tail 20 node

# 3. did it reach graphite?
curl -s "http://localhost:1337/api/datasources/proxy/1/metrics/find?query=stats.gauges.*"
```

---

## 5. Things that will bite you

Each of these cost someone real time.

**Empty graphs, no errors anywhere.** statsd receives over **UDP**: if that container is down or was
recreated with a new IP, the poller's sends disappear without a single log line on either side. After
recreating statsd, restart the poller too — it caches the resolved address.
`docker compose -f docker-compose.local.yml restart node`.

**HTTP 429 from the API.** The segment endpoint allows 360 requests/hour and the budget is **shared
across all of your account's tokens** — adding tokens does not add budget. The default 10 s interval
sits exactly at the cap with no headroom; with several shards the poller automatically spaces itself
so the *total* stays at 360/h. If you see 429s, set `SCREEPS_POLL_BASE_MS=12000` (300/h).

**Old data looks smoothed.** Retention is `10s:1d, 1m:28d, 10m:1y, 1h:2y` and older points are rolled
up by **averaging**. Values do not disappear, they quietly become averages — an RCL8 room can read
`rcl 7.4` across a level-up. For exact numbers query a short recent window through Graphite directly
rather than reading a wide dashboard panel.

**Grafana won't start.** Almost always `RENDERER_TOKEN` left at the placeholder; the log says
`failed to start rendering service`.

**A metric never appears.** It is not a number (see rule 1), or the room stopped emitting and you are
looking at a stale series: Graphite's `metrics/find` returns every path within retention, including
ones nothing has written to for weeks. Wrap variable queries in `currentAbove(<expr>, 0)` to list only
series with a fresh value.

---

## 6. What in this repo is mine and not yours

Written down so you know what to ignore or replace:

- **`docker-compose.yml`, `docker-compose.prod.yml`** — my VPS: external nginx network, a subpath, the
  domain `example.com`. Use `docker-compose.local.yml`.
- **`CLAUDE.md`** — my working notes for an AI assistant. Accurate but full of my hostnames, panel ids
  and incident history. Interesting as a field report, useless as instructions for you.
- **`tools/add_rooms_overview.py`** — builds a per-room table panel. Hardcodes my metric names and
  reads a Grafana token from **macOS Keychain**; you would need to change both.
- **`tools/tg_rooms_digest.sh`** — posts a panel screenshot to Telegram on a schedule. Reads its
  config from `/etc/screeps-grafana-digest.env` on my server.
- **`roles/`, `playbook.yml`, `inventory`** — upstream's Ansible, unused and stale.
- **`stats.js`** — upstream's `Memory.stats` example, superseded by the segment approach in §4.

No credentials are committed anywhere; `docker-compose.env` is gitignored and has never been in the
history.

---

## 7. Credit

Forked from [bkconrad/screeps-grafana](https://github.com/bkconrad/screeps-grafana), MIT. The fork
adds multi-shard polling, the segment-based transport, rate-limit handling, image rendering, and the
dashboard tooling under `tools/`.
