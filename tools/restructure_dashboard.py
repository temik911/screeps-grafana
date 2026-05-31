#!/usr/bin/env python3
"""Restructure ../sampleDashboard.json: 8-panel status strip + collapsible rows.

Idempotent — re-run after adding new panels to the JSON and they'll be picked up
automatically AS LONG AS their ids are listed in `ROWS` below. New panels not
listed will be silently dropped (with a printed warning) — add them to a row
before re-running.

Usage:
    python3 tools/restructure_dashboard.py
    # Then deploy via Grafana API (see project memory for live URL + token).

Categorization rules:
  - Status strip (always-visible top row): ids 101-108 below, rebuilt from scratch
    each run. Each is a stat panel with traffic-light thresholds.
  - Domain rows: ids 200-207, each a Grafana "row" panel that groups detail
    panels by topic. Detail panels keep their original queries; only gridPos.y
    is recomputed based on the row they belong to.

To add a new panel:
  1. Append the panel definition (with its existing id) anywhere in the JSON.
  2. Add its id to the appropriate row in ROWS below.
  3. Re-run this script.

Layout:
  - Status strip:    y=0..3   (8 stats × w=3, side by side)
  - Each row header: 1 unit tall, full width
  - Detail panels:   reflow under their row, preserving original side-by-side
                     pairings via x=0 detection (panel starts new visual row
                     when its original x was 0, otherwise placed next to prior).
"""
import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(SCRIPT_DIR), 'sampleDashboard.json')


def stat_panel(pid, title, description, target, thresh_steps, unit='none', decimals=1, color_mode='value'):
    return {
        'id': pid,
        'type': 'stat',
        'title': title,
        'description': description,
        'gridPos': {'h': 4, 'w': 3, 'x': 0, 'y': 0},  # x filled in later
        'datasource': 'localGraphite',
        'targets': [{'target': target, 'refId': 'A'}],
        'fieldConfig': {
            'defaults': {
                'decimals': decimals,
                'unit': unit,
                'thresholds': {'mode': 'absolute', 'steps': thresh_steps},
                'color': {'mode': 'thresholds'},
            },
            'overrides': [],
        },
        'options': {
            'graphMode': 'area',
            'colorMode': color_mode,
            'textMode': 'auto',
            'justifyMode': 'auto',
            'orientation': 'auto',
            'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False},
        },
    }


def row_panel(pid, title, y, collapsed=False):
    return {
        'id': pid,
        'type': 'row',
        'title': title,
        'collapsed': collapsed,
        'gridPos': {'h': 1, 'w': 24, 'x': 0, 'y': y},
        'panels': [],
    }


# Status strip: 8 panels × w=3 = 24, h=4. Order = priority of glance.
STATUS_STRIP = [
    stat_panel(101, 'CPU / tick', 'Average CPU consumed per tick. limit=20 on shard3.',
               'aliasByNode(stats.gauges.cpu.used, 2)',
               [{'color': 'green', 'value': None}, {'color': 'yellow', 'value': 14}, {'color': 'red', 'value': 18}]),
    stat_panel(102, 'CPU bucket', 'Bucket = CPU savings buffer (max 10000). <2000 = burning into reserve. <500 = critical.',
               'aliasByNode(stats.gauges.cpu.bucket, 2)',
               [{'color': 'red', 'value': None}, {'color': 'yellow', 'value': 2000}, {'color': 'green', 'value': 7000}],
               decimals=0),
    stat_panel(103, 'GCL', 'Global Control Level — caps the number of rooms we can own.',
               'aliasByNode(stats.gauges.gcl.level, 2)',
               [{'color': 'blue', 'value': None}], decimals=0),
    stat_panel(104, 'Hostiles (colony+remotes)',
               'Total hostile creeps across home rooms + visible remotes. Any non-zero = check now.',
               'sumSeries(stats.gauges.rooms.*.hostiles, stats.gauges.remotes.*.hostiles)',
               [{'color': 'green', 'value': None}, {'color': 'orange', 'value': 1}, {'color': 'red', 'value': 3}],
               decimals=0),
    stat_panel(105, 'Storage Δ/tick',
               'Sum of net energy flow into storage across owned rooms (sum of throughput). Negative = drawing down.',
               'sumSeries(stats.gauges.rooms.*.storageRate)',
               [{'color': 'red', 'value': None}, {'color': 'orange', 'value': 0}, {'color': 'green', 'value': 1}],
               decimals=1),
    stat_panel(106, 'Spawn busy %',
               'Spawn utilization across owned rooms (average). 100% = spawn is the bottleneck.',
               'averageSeries(stats.gauges.rooms.*.spawnBusyPct)',
               [{'color': 'green', 'value': None}, {'color': 'yellow', 'value': 80}, {'color': 'orange', 'value': 95}],
               unit='percent', decimals=0),
    stat_panel(107, 'Haulers alive',
               'Live logistics creeps colony-wide. Compare to per-room target in the Logistics row.',
               'aliasByNode(stats.gauges.creepsByRole.logistics, 2)',
               [{'color': 'blue', 'value': None}], decimals=0),
    stat_panel(108, 'Memory bytes',
               'JSON.stringify(Memory).length, refreshed every 100 ticks. Soft limit ~2MB on shard3.',
               'aliasByNode(stats.gauges.memory.bytes, 2)',
               [{'color': 'green', 'value': None}, {'color': 'yellow', 'value': 1500000}, {'color': 'red', 'value': 1900000}],
               unit='bytes', decimals=0),
]
for i, p in enumerate(STATUS_STRIP):
    p['gridPos']['x'] = i * 3
    p['gridPos']['y'] = 0

# Row layout: (title, [panel_ids in display order], collapsed_default)
# Drop old status-strip ids (1,2,3,4) — replaced by NEW status strip above.
# Drop memory bytes (22) — now in status strip.
ROWS = [
    ('Health & CPU', [5, 6, 7, 23], False),
    ('Per-room economy', [8, 9, 10, 11, 25, 26, 29, 45, 46], False),
    ('Throughput', [12, 13, 14], False),
    ('Construction', [15, 28], False),
    ('Movement & Traffic', [24, 30, 31, 32], False),
    ('Logistics (HaulerScheduler)', [33, 34, 35], False),
    ('Remote mining', [16, 17, 18, 19, 20, 21], False),
    ('Hostiles & Defense', [27, 36, 37], False),
    ('Scout & Routing', [44, 38, 39, 40, 41, 42, 43], False),
]
DROP_IDS = {1, 2, 3, 4, 22}


def layout_panels_in_section(panels_in_order, y_start):
    """Re-layout panels within a section, preserving side-by-side groupings inferred from
    original x positions (panels with x==0 start a new visual row; x>0 sit beside the prior).
    Returns (laid_out_panels, new_y_cursor)."""
    y = y_start
    out = []
    current_row_h = 0
    last_x_end = 0
    for p in panels_in_order:
        gp = p['gridPos']
        w = gp['w']
        h = gp['h']
        if gp.get('_orig_x', gp['x']) == 0 or last_x_end + w > 24:
            y += current_row_h
            current_row_h = 0
            last_x_end = 0
        p['gridPos'] = {'h': h, 'w': w, 'x': last_x_end, 'y': y}
        out.append(p)
        last_x_end += w
        current_row_h = max(current_row_h, h)
    y += current_row_h
    return out, y


def main():
    with open(SRC) as f:
        dash = json.load(f)

    orig_panels = dash['dashboard']['panels']
    # Strip out any previous row/status-strip artifacts so re-runs are idempotent.
    orig_panels = [p for p in orig_panels if p.get('type') != 'row' and p['id'] not in {x.id for x in []}]
    orig_panels = [p for p in orig_panels if not (100 <= p['id'] < 110)]   # drop prior status strip
    orig_panels = [p for p in orig_panels if not (200 <= p['id'] < 210)]   # drop prior row separators
    by_id = {p['id']: p for p in orig_panels}

    for p in orig_panels:
        p.setdefault('_orig_x', p['gridPos']['x'])

    new_panels = []
    new_panels.extend(STATUS_STRIP)

    y_cursor = 4
    next_row_id = 200
    for title, ids, collapsed in ROWS:
        row = row_panel(next_row_id, title, y_cursor, collapsed)
        new_panels.append(row)
        next_row_id += 1
        y_cursor += 1

        section_panels = [by_id[pid] for pid in ids if pid in by_id]
        laid_out, y_cursor = layout_panels_in_section(section_panels, y_cursor)
        new_panels.extend(laid_out)

    for p in new_panels:
        p.pop('_orig_x', None)

    # Warn about orphaned panels (existed in JSON but not assigned to any row).
    present_ids = {p['id'] for p in orig_panels}
    referenced_ids = set()
    for _, ids, _ in ROWS:
        referenced_ids.update(ids)
    orphans = present_ids - referenced_ids - DROP_IDS
    if orphans:
        print(f'WARNING: panels with ids {sorted(orphans)} are not in any row, '
              f'will be DROPPED. Add them to ROWS in the script if you want to keep.')

    dash['dashboard']['panels'] = new_panels

    with open(SRC, 'w') as f:
        json.dump(dash, f, indent=2)

    print(f'Wrote {len(new_panels)} panels: '
          f'{len(STATUS_STRIP)} status + {len(ROWS)} rows + '
          f'{len(new_panels) - len(STATUS_STRIP) - len(ROWS)} content panels')


if __name__ == '__main__':
    main()
