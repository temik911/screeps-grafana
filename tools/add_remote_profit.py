#!/usr/bin/env python3
"""Add the remote-profitability panels to sampleDashboard.json.

Idempotent: removes its own panels by id before re-inserting, so re-running never stacks copies
(same rule as add_rooms_overview.py). Panels are appended to the JSON and registered in
`restructure_dashboard.py`'s ROWS — a panel that is not in some ROWS entry gets dropped as an
orphan on the next restructure run, which is the pitfall documented in CLAUDE.md.

Order matters: deploy the bot first, let `remotes.<room>.profit` show up in Graphite, and only then
run this + restructure + POST. A panel querying a series that does not exist yet renders empty,
which is harmless here (unlike the rooms table, where a missing series shifts every column title).

usage: python3 tools/add_remote_profit.py [path/to/sampleDashboard.json]
"""
import json
import sys

PROFIT_ID = 301
PROFIT_NOW_ID = 302
HARVEST_ID = 303
OUR_IDS = {PROFIT_ID, PROFIT_NOW_ID, HARVEST_ID}


def timeseries(pid, title, description, target, unit, x, y, extra_defaults=None):
    defaults = {
        "unit": unit,
        "decimals": 1,
        "custom": {
            "drawStyle": "line",
            "lineWidth": 2,
            "fillOpacity": 10,
            "spanNulls": True,
            "showPoints": "never",
        },
    }
    if extra_defaults:
        defaults.update(extra_defaults)
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "description": description,
        "gridPos": {"h": 8, "w": 12, "x": x, "y": y},
        "datasource": "localGraphite",
        "targets": [{"target": target, "refId": "A"}],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def build_panels():
    profit = timeseries(
        PROFIT_ID,
        "Remote — profitability (energy/tick)",
        "Net energy per tick per remote: income (10/tick per reserved source, 5 unreserved) minus "
        "miner/hauler/reserver bodies, container upkeep, road wear, defence — and minus what is "
        "decaying on the ground. Model: docs/in-progress/REMOTE_PROFITABILITY.md. "
        "Distance alone almost never makes a remote negative; a line dropping below zero means "
        "energy is being mined and left to rot, not that the room is too far.",
        "aliasByNode(stats.gauges.$shard.remotes.*.profit, 4)",
        "none",
        x=0,
        y=378,
        extra_defaults={
            "thresholds": {
                "mode": "absolute",
                "steps": [
                    {"color": "red", "value": None},
                    {"color": "yellow", "value": 0},
                    {"color": "green", "value": 5},
                ],
            }
        },
    )
    harvest = timeseries(
        HARVEST_ID,
        "Remote — measured harvest (energy/tick)",
        "Energy actually harvested in each remote last tick, straight from the engine's event log "
        "(EVENT_HARVEST, minerals excluded). This is the measured counterpart to the modelled "
        "income in the profitability panel: a gap between them means miners are not keeping the "
        "sources drained — dead miner, invader pause, or a body too small for a reserved source "
        "(5 WORK needed). Gaps in the line are ticks without vision, not zero harvest.",
        "aliasByNode(stats.gauges.$shard.remotes.*.harvested, 4)",
        "none",
        x=12,
        y=378,
    )
    profit_now = {
        "id": PROFIT_NOW_ID,
        "type": "bargauge",
        "title": "Remote — profitability now (red = losing energy)",
        "description": "Current net energy/tick per remote. Colour is the signal, not order: red means the remote costs more than it earns right now (Grafana sorts bar gauges by series name, and this Graphite backend has no sortBy, so ordering it by value is not available).  Cross-check with the container fill and the "
        "measured-harvest panels: a negative remote is nearly always a full container with a pile "
        "rotting beside it, not an expensive one.",
        "gridPos": {"h": 9, "w": 24, "x": 0, "y": 386},
        "datasource": "localGraphite",
        "targets": [{"target": "aliasByNode(stats.gauges.$shard.remotes.*.profit, 4)", "refId": "A"}],
        "fieldConfig": {
            "defaults": {
                "unit": "none",
                "decimals": 1,
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "red", "value": None},
                        {"color": "orange", "value": 0},
                        {"color": "yellow", "value": 5},
                        {"color": "green", "value": 10},
                    ],
                },
            },
            "overrides": [],
        },
        "options": {
            "displayMode": "gradient",
            "orientation": "horizontal",
            "showUnfilled": True,
            "valueMode": "color",
            "sortBy": "Value",
            "sortOrder": "asc",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }
    return [profit, harvest, profit_now]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "sampleDashboard.json"
    with open(path) as fh:
        doc = json.load(fh)
    dash = doc.get("dashboard", doc)

    before = len(dash["panels"])
    dash["panels"] = [p for p in dash["panels"] if p.get("id") not in OUR_IDS]
    removed = before - len(dash["panels"])

    dash["panels"].extend(build_panels())
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    print(f"panels: {before} → {len(dash['panels'])} (removed {removed} of ours, added 3)")
    print("next: add ids", sorted(OUR_IDS), "to the 'Remote mining' ROWS entry, then run")
    print("      python3 tools/restructure_dashboard.py && POST the JSON")


if __name__ == "__main__":
    main()
