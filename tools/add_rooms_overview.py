#!/usr/bin/env python3
"""Put an overview table at the top of the "Screeps — Rooms" dashboard: one row per room with RCL,
controller %, ETA to the next level and storage energy. Idempotent — re-running replaces the panel.

Reads the live dashboard from the Grafana API, inserts the panel at y=0, shifts everything else down,
and POSTs it back."""
import json
import os
import subprocess
import urllib.request

BASE = "https://example.com/screeps-grafana/api"
UID = "screeps-rooms"
TITLE = "Комнаты — обзор"
H = 9  # panel height; everything below shifts by this
G = "stats.gauges.$shard"

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
    # aliasByNode(..., 4) makes the series name the ROOM name: stats(0).gauges(1).shard(2).rooms(3).<room>(4)
    #
    # currentAbove(..., 0) is not cosmetic: Graphite's wildcard returns every path within retention,
    # and every remote we ever prep-claimed emitted rooms.<name>.* for the few hundred ticks it was
    # briefly ours. Without the filter this table shows 29 rows for 13 live rooms, 16 of them blank
    # (verified live). The same trick guards the $room variable on this dashboard.
    return {
        "refId": ref,
        "target": f"aliasByNode(currentAbove({G}.rooms.*.{metric}, 0), 4)",
        "format": "table",
    }


# Column naming after `timeSeriesTable` isn't something the HTTP API can show us (transformations run
# in the browser, and this Grafana has no image renderer — /render 500s), so every rename/exclude below
# lists the plausible names for the same column. A name that doesn't exist is silently ignored by the
# organize transform, so the panel is correct whichever variant this version produces.
rename = {
    "Field": "Комната", "Metric": "Комната", "Name": "Комната",
    "A": "RCL", "A-series": "RCL", "rcl": "RCL",
    "B": "Контроллер %", "B-series": "Контроллер %", "controllerProgressPct": "Контроллер %",
    "C": "До апа, ч", "C-series": "До апа, ч", "controllerEtaHours": "До апа, ч",
    "D": "Сторадж, энергия", "D-series": "Сторадж, энергия", "energy": "Сторадж, энергия",
}
exclude = {f"Trend #{r}": True for r in "ABCD"}
exclude["Trend"] = True
exclude["Time"] = True

panel = {
    "id": next_id,
    "type": "table",
    "title": TITLE,
    "description": (
        "Одна строка на комнату: уровень, заполнение контроллера, оценка времени до следующего уровня и "
        "энергия в сторадже. Список комнат растёт сам — запросы идут по wildcard rooms.*, так что новая "
        "комната появляется здесь на первой же записи телеметрии. «До апа» считается по фактической "
        "скорости апгрейда за последние снимки statsHistory, поэтому у комнаты, которая сейчас не "
        "апгрейдит, значение будет огромным или пустым — это не ошибка, а буквальный ответ."
    ),
    "gridPos": {"h": H, "w": 24, "x": 0, "y": 0},
    "datasource": "localGraphite",
    "targets": [
        target("A", "rcl"),
        target("B", "controllerProgressPct"),
        target("C", "controllerEtaHours"),
        target("D", "storage.energy"),
    ],
    "transformations": [
        # Series → rows, one column per query, each reduced to its latest value.
        {"id": "timeSeriesTable",
         "options": {r: {"stat": "lastNotNull"} for r in "ABCD"}},
        # Merge the per-query frames into a single table keyed by the room name.
        {"id": "joinByField", "options": {"byField": "Field", "mode": "outer"}},
        {"id": "organize", "options": {"excludeByName": exclude, "renameByName": rename, "indexByName": {}}},
    ],
    "fieldConfig": {
        "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}, "decimals": 0},
        "overrides": [
            {"matcher": {"id": "byRegexp", "options": ".*Контроллер.*"},
             "properties": [{"id": "unit", "value": "percent"},
                            {"id": "custom.cellOptions",
                             "value": {"type": "gauge", "mode": "gradient"}},
                            {"id": "min", "value": 0}, {"id": "max", "value": 100}]},
            {"matcher": {"id": "byRegexp", "options": ".*До апа.*"},
             "properties": [{"id": "unit", "value": "h"}, {"id": "decimals", "value": 1}]},
            {"matcher": {"id": "byRegexp", "options": ".*Сторадж.*"},
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
