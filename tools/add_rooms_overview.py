#!/usr/bin/env python3
"""Put an overview table at the top of the "Screeps — Rooms" dashboard: one row per room with RCL,
controller %, ETA to the next level, storage energy, the energy still needed to finish the room's
construction sites, and how much energy it gave to / took from the other rooms over the panel's time
range. Idempotent — re-running replaces the panel.

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
- Every reduced column is called `Last *`, and they can only be told apart AFTER the join, where
  Grafana disambiguates them as `Last * 1..N` — in DESCENDING query order (…,C,B,A). A refId-filtered
  `organize` placed before the join does nothing: reduce builds a new frame and drops the refId, so
  the filter matches no frames. This is also why a "delta" column is built by wrapping the QUERY
  (integral∘derivative) rather than by giving that one reduce a different reducer: a second reducer
  would name its column `Delta`/`Range` and renumber the `Last *` run, silently shifting every rename
  below it.

That last point makes the rename positional, which is the fragile part of this panel. It is also
loudly visible if it ever breaks: the units and the gauge are pinned to the final column names, so a
reordering shows up as a percent gauge over storage numbers rather than as quietly swapped data.
Re-render the panel after touching this (see RENDER_CHECK below)."""
import json
import os
import subprocess
import urllib.request

# Grafana base URL + API token. Both come from the environment so this script is not tied to one
# host: GRAFANA=http://localhost:1337 for a local stack. GRAFANA_TOKEN wins if set; otherwise we fall
# back to the macOS Keychain, which is where the author keeps it.
GRAFANA = os.environ.get("GRAFANA", "http://localhost:1337").rstrip("/")
BASE = f"{GRAFANA}/api"
UID = "screeps-rooms"
TITLE = "Комнаты — обзор"
# Panel height in grid units (~30px each); everything below shifts by this. 16 fitted ~14 rows and the
# colony passed that: at 16 rooms the table cut off in the Telegram picture. 20 units ≈ 18 rows — bump
# this AND `HEIGHT` in tools/tg_rooms_digest.sh together, or the render crops again.
H = 20
G = "stats.gauges.$shard"

# refId → (graphite metric, column title, kind). Order matters twice: the queries are emitted in this
# order, and the joined columns come back in the REVERSE of it.
#
# CAUTION when adding a column: the rename below is POSITIONAL, so it is only correct while every
# query returns data. A metric the bot has just started emitting does not exist in Graphite yet, its
# query comes back empty, the join produces one column fewer and EVERY title shifts by one — the table
# then shows RCL under "Контроллер %" and so on. Deploy the bot first, wait for the series to appear,
# and only then run this script (or re-run it once the data is there).
#
# kind "level" = show the value as it stands now. kind "signed-delta" = the metric is a cumulative
# counter and the column shows how much it moved over the panel's time range, sign included (see
# `target`). Every kind still ends up reduced with the same lastNotNull, which is what keeps the
# fragile positional rename below working.
COLUMNS = [
    ("A", "rcl", "RCL", "level"),
    ("B", "controllerProgressPct", "Контроллер %", "level"),
    ("C", "controllerEtaHours", "До апа, ч", "level"),
    ("D", "storage.energy", "Сторадж, энергия", "level"),
    ("E", "sitesRemaining", "Достроить, энергия", "level"),
    ("F", "share.energyNet", "Обмен, энергия", "signed-delta"),
    ("G", "planVersion", "План", "level"),
]

RENDER_CHECK = (
    f"{GRAFANA}/render/d-solo/screeps-rooms/"
    "?panelId={id}&var-shard=shard1&from=now-6h&to=now&width=1400&height=560&theme=light"
)

token = os.environ.get("GRAFANA_TOKEN") or subprocess.check_output(
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


def target(ref, metric, kind="level"):
    # aliasByNode(..., 4) makes the series name the ROOM name, which is what `reduce` then turns into
    # the `Field` column: stats(0).gauges(1).shard(2).rooms(3).<room>(4)
    #
    # kind "signed-delta": share counters are cumulative (a terminal send happens at most once per
    # cooldown, so the bot emits totals — a per-tick gauge would be missed by the ~10s poller).
    # integral(derivative(x)) re-accumulates the increments INSIDE the query window, so the last point
    # equals the movement over exactly that window, per series. Validated on the monotonic
    # `energySent` against both a hand-computed last−first and the nonNegativeDerivative chain: all
    # three agree to the unit (138379 over 3h), and that figure equals the sum of what the receiving
    # rooms took.
    #
    # It must be plain `derivative`: `energyNet` is the one share series that goes DOWN (every time
    # the room donates), and nonNegativeDerivative would silently drop exactly those decrements —
    # i.e. show donors as 0. The price is that a counter reset (wiped Memory) reads as one large
    # negative step in the window that contains it, instead of being swallowed as a gap.
    #
    # This is the shape it is because this backend is graphite-API, not graphite-web: `movingSum`,
    # `applyByNode` and `groupByNodes` are all absent (KeyError), so there is no fixed-3h rolling
    # window and no way to subtract two `rooms.*` series room by room — which is why the SIGNED net
    # is computed bot-side and arrives here as its own gauge. Hence the window is the PANEL's, which
    # is why the digest renders with from=now-3h (its own cadence) — see tools/tg_rooms_digest.sh.
    #
    # `removeEmptySeries` rather than `currentAbove` for the signed column: currentAbove would need a
    # threshold below every legitimate value, and any threshold ≥ a real donation silently hides the
    # donors — the very rooms the column exists to show. removeEmptySeries drops a series only when
    # it is all-null in the window, which is exactly the dead-room test (13 of 29 paths survive).
    #
    # currentAbove(..., 0) is not cosmetic: Graphite's wildcard returns every path within retention,
    # and every remote we ever prep-claimed emitted rooms.<name>.* for the few hundred ticks it was
    # briefly ours. Without the filter this table shows 29 rows for 13 live rooms, 16 of them blank
    # (verified live). The same trick guards the $room variable on this dashboard.
    #
    # The threshold is 0 and not -1 on purpose: this graphite-web compares NON-strictly, so a series
    # sitting at exactly 0 survives while a dead one (all nulls in the window) is dropped — checked
    # against rooms.*.hostiles, which is 0 in all 13 live rooms and still returned all 13. That is
    # what lets "Достроить, энергия" print a real 0 for a room with nothing under construction
    # instead of an empty cell. Re-check this if the Graphite image is ever upgraded.
    expr = f"{G}.rooms.*.{metric}"
    if kind == "signed-delta":
        return {"refId": ref,
                "target": f"aliasByNode(removeEmptySeries(integral(derivative({expr}))), 4)"}
    return {"refId": ref, "target": f"aliasByNode(currentAbove({expr}, 0), 4)"}


def steps(bands):
    """[(from, colour), …] → a Grafana threshold config. The first band's `from` must be None (base)."""
    return {"mode": "absolute",
            "steps": [{"value": value, "color": colour} for value, colour in bands]}


transformations = [
    {"id": "reduce", "options": {"reducers": ["lastNotNull"], "mode": "seriesToRows"},
     "filter": {"id": "byRefId", "options": ref}}
    for ref, _metric, _label, _kind in COLUMNS
]
transformations.append({"id": "joinByField", "options": {"byField": "Field", "mode": "outer"}})

# Joined position 1 is the LAST query — walk the columns backwards.
rename = {"Field": "Комната"}
index = {"Field": 0}
for i, (_ref, _metric, label, _kind) in enumerate(reversed(COLUMNS)):
    rename[f"Last * {i + 1}"] = label
    index[f"Last * {i + 1}"] = len(COLUMNS) - i
transformations.append({"id": "organize", "options": {
    "excludeByName": {}, "renameByName": rename, "indexByName": index}})

panel = {
    "id": next_id,
    "type": "table",
    "title": TITLE,
    "description": (
        "Одна строка на комнату: уровень, заполнение контроллера, оценка времени до следующего уровня, "
        "энергия в сторадже, сколько энергии осталось влить в стройку и по какому плану комната строится. "
        "Колонка «План»: «старый» — раскладка BasePlanner, «v2» — генератор бункера v2; переход идёт "
        "по одной комнате, флаг живёт в room.memory.bunkerV2. Список комнат растёт сам — "
        "запросы идут по wildcard rooms.*, так что новая "
        "комната появляется здесь на первой же записи телеметрии. Пустая клетка — это отсутствие метрики, "
        "а не ноль: у RCL8-комнаты нет «до апа» (апать некуда), у молодой комнаты ещё нет стораджа. "
        "«До апа» считается по фактической скорости апгрейда за последние снимки statsHistory, поэтому у "
        "комнаты, которая сейчас не апгрейдит, значение будет огромным. "
        "«Достроить» — сумма недостающего прогресса по стройкам В САМОЙ комнате (Σ progressTotal − "
        "progress), то есть сколько энергии строителям осталось внести; 0 — стройки нет. Стройки в "
        "ремоутах (дороги, контейнеры) сюда НЕ входят — они лежат в construction.byRoom. "
        "«Обмен» — энергия, переданная терминалом МЕЖДУ НАШИМИ комнатами ЗА ОКНО ПАНЕЛИ (а не всего): "
        "МИНУС и оранжевый — комната отдала, ПЛЮС и синий — приняла, 0 — обмена не было. Цвет здесь "
        "означает направление, а не «хорошо/плохо»: отдавать — работа комнаты на RCL8, принимать — "
        "нормальное состояние молодой. В картинке для Телеграма окно — 3 часа, ровно период между "
        "картинками, а на самом дашборде это выбранный сверху диапазон. Рыночные сделки сюда не "
        "входят, налог за пересылку тоже (он списывается с отправителя отдельно, метрика share.cost). "
        "Сумма колонки по всем комнатам обязана давать 0 — иначе потеряна метрика."
    ),
    "gridPos": {"h": H, "w": 24, "x": 0, "y": 0},
    "datasource": "localGraphite",
    "targets": [target(ref, metric, kind) for ref, metric, _label, kind in COLUMNS],
    "transformations": transformations,
    "fieldConfig": {
        "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}, "decimals": 0},
        "overrides": [
            {"matcher": {"id": "byName", "options": "Комната"},
             "properties": [{"id": "custom.width", "value": 130}]},
            {"matcher": {"id": "byName", "options": "RCL"},
             "properties": [
                 # Discrete levels, so a solid block per band rather than an interpolated gradient.
                 {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                 {"id": "color", "value": {"mode": "thresholds"}},
                 # Pinned narrow: a filled cell left to auto-width becomes a colour block wider than
                 # every other column and drags the eye away from the actual numbers.
                 {"id": "custom.width", "value": 70},
                 {"id": "custom.align", "value": "center"},
                 {"id": "thresholds", "value": steps([
                     (None, "semi-dark-red"),   # 1-3: no storage yet, still on harvesters
                     (4, "orange"),             # 4-5: storage/links arriving
                     (6, "yellow"),             # 6: terminal, extractor
                     (7, "light-green"),        # 7: labs, factory
                     (8, "dark-green"),         # 8: maxed
                 ])},
             ]},
            {"matcher": {"id": "byName", "options": "Контроллер %"},
             "properties": [
                 {"id": "unit", "value": "percent"},
                 {"id": "min", "value": 0}, {"id": "max", "value": 100},
                 {"id": "decimals", "value": 1},
                 {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
                 # Capped, or on a wide monitor the bar eats the slack of the whole row.
                 {"id": "custom.width", "value": 260},
                 # Progress is neither good nor bad, so the bar length carries the meaning and the
                 # colour only marks "about to level" — an interpolated gradient here would paint a
                 # nearly-full controller red, i.e. exactly backwards.
                 {"id": "color", "value": {"mode": "thresholds"}},
                 {"id": "thresholds", "value": steps([(None, "blue"), (90, "green")])},
             ]},
            {"matcher": {"id": "byName", "options": "До апа, ч"},
             "properties": [
                 {"id": "unit", "value": "h"}, {"id": "decimals", "value": 1},
                 # All six columns are pinned: with any of them left on auto, the leftover width of a
                 # wide screen is inserted BETWEEN columns and the row reads as disconnected islands.
                 {"id": "custom.width", "value": 150},
                 # Coloured text, not a filled cell: three painted columns out of five turns the table
                 # into a heatmap and stops being scannable.
                 {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                 {"id": "color", "value": {"mode": "thresholds"}},
                 {"id": "thresholds", "value": steps([
                     (None, "green"),           # < 1 day
                     # semi-dark, because plain "yellow" as TEXT is nearly white on the dark theme —
                     # the band stops reading as a warning at all (checked in both themes).
                     (24, "semi-dark-yellow"),
                     (72, "orange"),            # 3+ days
                     (168, "semi-dark-red"),    # a week or more: the room is barely upgrading
                 ])},
             ]},
            {"matcher": {"id": "byName", "options": "Сторадж, энергия"},
             "properties": [
                 {"id": "unit", "value": "short"},
                 {"id": "custom.width", "value": 170},
                 {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                 {"id": "color", "value": {"mode": "thresholds"}},
                 # Rough colony-wide bands, not per-RCL: 10k is the bar assistRoom uses to call a room
                 # self-sufficient, and a mature room sitting under ~50k is running its buffer down.
                 {"id": "thresholds", "value": steps([
                     (None, "semi-dark-red"),
                     (10000, "orange"),
                     (50000, "semi-dark-yellow"),
                     (150000, "green"),
                 ])},
             ]},
            {"matcher": {"id": "byName", "options": "Достроить, энергия"},
             "properties": [
                 {"id": "unit", "value": "short"},
                 {"id": "custom.width", "value": 170},
                 {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                 {"id": "color", "value": {"mode": "thresholds"}},
                 # The base band covers exactly 0 — "nothing to build" is neither good nor bad, so it
                 # stays the plain text colour instead of shouting green at every idle room. Above it
                 # the bands are sized against what a site backlog actually costs: a handful of roads
                 # and extensions is a few thousand, one storage/terminal-class building is 30-100k,
                 # and past ~300k the room is carrying a whole RCL's unlocks at once.
                 {"id": "thresholds", "value": steps([
                     (None, "text"),
                     (1, "green"),
                     (30000, "semi-dark-yellow"),
                     (100000, "orange"),
                     (300000, "semi-dark-red"),
                 ])},
             ]},
            {"matcher": {"id": "byName", "options": "План"},
             "properties": [
                 # 1 = the old BasePlanner layout, 2 = the bunker v2 generator. The migration runs room
                 # by room, so this column is the answer to "which of the two is this room building?".
                 # A mapping rather than the bare number: "v2" reads at a glance, "2" does not.
                 {"id": "mappings", "value": [{"type": "value", "options": {
                     "1": {"text": "старый", "index": 0},
                     "2": {"text": "v2", "index": 1}}}]},
                 {"id": "custom.width", "value": 90},
                 {"id": "custom.align", "value": "center"},
                 {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                 {"id": "color", "value": {"mode": "thresholds"}},
                 # v2 is the state we are migrating TOWARDS, so it is the one that stands out; a room
                 # still on the old plan is the normal majority and stays quiet.
                 {"id": "thresholds", "value": steps([(None, "text"), (2, "green")])},
             ]},
            {"matcher": {"id": "byName", "options": "Обмен, энергия"},
             "properties": [
                 {"id": "unit", "value": "short"},
                 {"id": "custom.width", "value": 175},
                 {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                 {"id": "color", "value": {"mode": "thresholds"}},
                 # Colour encodes DIRECTION, not quality — the minus sign already carries the
                 # direction, and the colour makes the two groups separable at a glance without
                 # reading the numbers. Deliberately not green/red: giving energy away is what a
                 # maxed room is FOR, and taking it is what a young room is for, so neither side is
                 # "bad". Warm = energy leaving, cool = energy arriving, plain text = no exchange,
                 # which keeps the idle majority of rows quiet.
                 {"id": "thresholds", "value": steps([
                     (None, "orange"),   # < 0 — donor, energy left the room
                     (0, "text"),        # exactly 0 — no exchange in this window
                     (1, "blue"),        # > 0 — recipient
                 ])},
             ]},
        ],
    },
    "options": {
        "showHeader": True,
        # Composite default sort — the table panel honours every key in this list, not just the first
        # (verified by render): level first, then how close that room is to the next one, so the rooms
        # about to level sit at the top of their band. Both keys reference the RENAMED column titles.
        "sortBy": [{"displayName": "RCL", "desc": True},
                   {"displayName": "Контроллер %", "desc": True}],
        "footer": {"show": False},
    },
}

panels.insert(0, panel)
res = api("/dashboards/db", {"dashboard": dash, "folderUid": "", "overwrite": True})
print(res["status"], "v" + str(res["version"]), "| panels:", len(panels))
print("render check:", RENDER_CHECK.format(id=next_id))
