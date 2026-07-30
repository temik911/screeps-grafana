#!/usr/bin/env python3
"""Put an overview table at the top of the "Screeps — Rooms" dashboard: one row per room with RCL,
controller %, ETA to the next level and storage energy. Idempotent — re-running replaces the panel.

Reads the live dashboard from the Grafana API, inserts the panel at y=0, shifts everything else down,
and POSTs it back.

## Why the transformation chain looks like this

Building a per-room table out of Graphite wildcard queries is not the documented happy path, and the
first two obvious chains produce an EMPTY panel. What actually happens (each variant was rendered and
looked at through the image renderer, Grafana 13.0.1):

- Graphite frames carry **no labels and no frame name** — the room lands in
  `fields[1].config.displayNameFromDS`. So `timeSeriesTable`, whose whole job is turning labels into
  columns, emits just a value column plus `Trend #A` and NO room column: join finds nothing to key on
  and the panel says "No data".
- `reduce`/`seriesToRows` is the chain that works with Graphite: it yields exactly two columns,
  `Field` (the room, from the display name) and `Last *`.
- One reduce per query, joined on `Field`, gives one row per room with the values aligned by room —
  which matters, because a maxed room has no ETA series and a young room has no storage series, and an
  index-based merge (`concatenate`) would silently shift those columns by a row.
- The four reduced columns are all called `Last *`, and they can only be told apart AFTER the join,
  where Grafana disambiguates them as `Last * 1..4` — in DESCENDING query order (D,C,B,A). A
  refId-filtered `organize` placed before the join does nothing: reduce builds a new frame and drops
  the refId, so the filter matches no frames.

That last point makes the rename positional, which is the fragile part of this panel. It is also
loudly visible if it ever breaks: the units and the gauge are pinned to the final column names, so a
reordering shows up as a percent gauge over storage numbers rather than as quietly swapped data.
Re-render the panel after touching this (see RENDER_CHECK below)."""
import json
import subprocess
import urllib.request

BASE = "https://example.com/screeps-grafana/api"
UID = "screeps-rooms"
TITLE = "Комнаты — обзор"
# Panel height in grid units (~30px each); everything below shifts by this. 16 fits ~14 rows without
# an inner scrollbar — the colony is 13 rooms and expanding, so bump this when rows start hiding.
H = 16
G = "stats.gauges.$shard"

# refId → (graphite metric, column title). Order matters twice: the queries are emitted in this order,
# and the joined columns come back in the REVERSE of it.
COLUMNS = [
    ("A", "rcl", "RCL"),
    ("B", "controllerProgressPct", "Контроллер %"),
    ("C", "controllerEtaHours", "До апа, ч"),
    ("D", "storage.energy", "Сторадж, энергия"),
]

RENDER_CHECK = (
    "https://example.com/screeps-grafana/render/d-solo/screeps-rooms/"
    "?panelId={id}&var-shard=shard1&from=now-6h&to=now&width=1400&height=560&theme=light"
)

token = subprocess.check_output(
    ["security", "find-generic-password", "-s", "screeps-grafana-token", "-a", "grafana", "-w"]
).decode().strip()


def api(path, body=None):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    return json.load(urllib.request.urlopen(req, timeout=30))


doc = api(f"/dashboards/uid/{UID}")
dash = doc["dashboard"]
panels = dash["panels"]

# Drop a previous version of this panel and un-shift, so re-runs don't stack.
existing = next((p for p in panels if p.get("title") == TITLE), None)
if existing is not None:
    shift = existing["gridPos"]["h"]
    panels.remove(existing)
    for p in panels:
        if p["gridPos"]["y"] >= existing["gridPos"]["y"]:
            p["gridPos"]["y"] -= shift

for p in panels:
    p["gridPos"]["y"] += H

next_id = max([p.get("id", 0) for p in panels] + [0]) + 1


def target(ref, metric):
    # aliasByNode(..., 4) makes the series name the ROOM name, which is what `reduce` then turns into
    # the `Field` column: stats(0).gauges(1).shard(2).rooms(3).<room>(4)
    #
    # currentAbove(..., 0) is not cosmetic: Graphite's wildcard returns every path within retention,
    # and every remote we ever prep-claimed emitted rooms.<name>.* for the few hundred ticks it was
    # briefly ours. Without the filter this table shows 29 rows for 13 live rooms, 16 of them blank
    # (verified live). The same trick guards the $room variable on this dashboard.
    return {"refId": ref, "target": f"aliasByNode(currentAbove({G}.rooms.*.{metric}, 0), 4)"}


transformations = [
    {"id": "reduce", "options": {"reducers": ["lastNotNull"], "mode": "seriesToRows"},
     "filter": {"id": "byRefId", "options": ref}}
    for ref, _metric, _label in COLUMNS
]
transformations.append({"id": "joinByField", "options": {"byField": "Field", "mode": "outer"}})

# Joined position 1 is the LAST query — walk the columns backwards.
rename = {"Field": "Комната"}
index = {"Field": 0}
for i, (_ref, _metric, label) in enumerate(reversed(COLUMNS)):
    rename[f"Last * {i + 1}"] = label
    index[f"Last * {i + 1}"] = len(COLUMNS) - i
transformations.append({"id": "organize", "options": {
    "excludeByName": {}, "renameByName": rename, "indexByName": index}})

panel = {
    "id": next_id,
    "type": "table",
    "title": TITLE,
    "description": (
        "Одна строка на комнату: уровень, заполнение контроллера, оценка времени до следующего уровня и "
        "энергия в сторадже. Список комнат растёт сам — запросы идут по wildcard rooms.*, так что новая "
        "комната появляется здесь на первой же записи телеметрии. Пустая клетка — это отсутствие метрики, "
        "а не ноль: у RCL8-комнаты нет «до апа» (апать некуда), у молодой комнаты ещё нет стораджа. "
        "«До апа» считается по фактической скорости апгрейда за последние снимки statsHistory, поэтому у "
        "комнаты, которая сейчас не апгрейдит, значение будет огромным."
    ),
    "gridPos": {"h": H, "w": 24, "x": 0, "y": 0},
    "datasource": "localGraphite",
    "targets": [target(ref, metric) for ref, metric, _label in COLUMNS],
    "transformations": transformations,
    "fieldConfig": {
        "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}, "decimals": 0},
        "overrides": [
            {"matcher": {"id": "byName", "options": "Контроллер %"},
             "properties": [
                 {"id": "unit", "value": "percent"},
                 {"id": "min", "value": 0}, {"id": "max", "value": 100},
                 {"id": "decimals", "value": 1},
                 # A gradient bar paints a nearly-full controller red, which reads as a problem when it
                 # is the opposite — keep one flat colour and let the length carry the meaning.
                 {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
                 {"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}},
             ]},
            {"matcher": {"id": "byName", "options": "До апа, ч"},
             "properties": [{"id": "unit", "value": "h"}, {"id": "decimals", "value": 1}]},
            {"matcher": {"id": "byName", "options": "Сторадж, энергия"},
             "properties": [{"id": "unit", "value": "short"}]},
        ],
    },
    "options": {
        "showHeader": True,
        "sortBy": [{"displayName": "RCL", "desc": True}],
        "footer": {"show": False},
    },
}

panels.insert(0, panel)
res = api("/dashboards/db", {"dashboard": dash, "folderUid": "", "overwrite": True})
print(res["status"], "v" + str(res["version"]), "| panels:", len(panels))
print("render check:", RENDER_CHECK.format(id=next_id))
