#!/usr/bin/env bash
# Render the "Комнаты — обзор" panel and push it to Telegram as a photo. Driven by cron on the VPS:
#
#   0 9 * * *  /opt/screeps-grafana/tools/tg_rooms_digest.sh >>/var/log/screeps-digest.log 2>&1
#
# Secrets live in /etc/screeps-grafana-digest.env (root-only, NOT in git):
#   GRAFANA_TOKEN=...   service-account token, only needs render/dashboard read
#   TG_BOT_TOKEN=...    from @BotFather
#   TG_CHAT_ID=...      target chat
# Everything else is optional and overridable from that file (PANEL_TITLE, THEME, WIDTH, …).
#
# DRY_RUN=1 renders and reports the size without sending anything. FORCE=1 ignores the hour gate.
set -euo pipefail

CONF=${CONF:-/etc/screeps-grafana-digest.env}
if [ ! -r "$CONF" ]; then
    echo "$(date '+%F %T') config $CONF is missing or unreadable" >&2
    exit 1
fi
# shellcheck disable=SC1090
. "$CONF"

# Which Moscow hours to send at. The gate lives HERE rather than in the crontab because this box runs
# on Europe/Amsterdam and its cron (vixie 3.0pl1) has no CRON_TZ: a crontab line spelled in local time
# would slide by an hour against Moscow at every DST switch. So cron fires hourly and we decide.
SEND_AT_MSK_HOURS=${SEND_AT_MSK_HOURS:-}
if [ -n "$SEND_AT_MSK_HOURS" ] && [ -z "${FORCE:-}" ]; then
    hours=",${SEND_AT_MSK_HOURS// /},"
    now=$(TZ=Europe/Moscow date '+%-H')
    # Silent exit, not a log line: a skipped hour is the normal case, 16 times a day.
    case "$hours" in
        *",$now,"*) ;;
        *) exit 0 ;;
    esac
fi

: "${GRAFANA_TOKEN:?GRAFANA_TOKEN not set in $CONF}"
if [ -z "${DRY_RUN:-}" ]; then
    : "${TG_BOT_TOKEN:?TG_BOT_TOKEN not set in $CONF}" "${TG_CHAT_ID:?TG_CHAT_ID not set in $CONF}"
fi

# Grafana is addressed through its published host port rather than the public URL: the digest then
# keeps working when the reverse proxy or the certificate is having a bad day. The /screeps-grafana
# prefix is still required — SERVE_FROM_SUB_PATH makes it the real internal path.
BASE=${BASE:-http://127.0.0.1:1337/screeps-grafana}
DASH_UID=${DASH_UID:-screeps-rooms}
PANEL_TITLE=${PANEL_TITLE:-Комнаты — обзор}
SHARD=${SHARD:-shard1}
# 1280 because the table is eight pinned columns wide (~1210px); at the old 1000 the last one was
# clipped. Telegram scales the photo down to fit anyway, and taps open it at full size.
WIDTH=${WIDTH:-1280}
HEIGHT=${HEIGHT:-620}
THEME=${THEME:-dark}
# Matches SEND_AT_MSK_HOURS' 3-hour cadence, and it is not just cosmetic: the «Отправил/Принял»
# columns show the counters' growth over THIS window, so 3h is what makes them read "since the
# previous picture". Widen the window and those two columns start covering more than one digest.
FROM=${FROM:-now-3h}
CAPTION=${CAPTION:-Комнаты — обзор}

auth=(-H "Authorization: Bearer $GRAFANA_TOKEN")

# Resolve the panel by TITLE, never by a pinned id: tools/add_rooms_overview.py assigns a fresh id
# every time it re-creates the panel, so a hardcoded id silently starts rendering a different panel
# (or nothing) after the next dashboard edit.
panel_id=$(curl -sS -m 30 "${auth[@]}" "$BASE/api/dashboards/uid/$DASH_UID" | PANEL_TITLE="$PANEL_TITLE" python3 -c '
import json, os, sys
title = os.environ["PANEL_TITLE"]
panels = json.load(sys.stdin)["dashboard"]["panels"]
match = [p["id"] for p in panels if p.get("title") == title]
if not match:
    sys.exit(f"no panel titled {title!r} on this dashboard")
print(match[0])
')

png=$(mktemp /tmp/screeps-rooms-XXXXXX.png)
trap 'rm -f "$png"' EXIT

code=$(curl -sS -m 180 -o "$png" -w '%{http_code}' "${auth[@]}" \
    "$BASE/render/d-solo/$DASH_UID/?panelId=$panel_id&var-shard=$SHARD&from=$FROM&to=now&width=$WIDTH&height=$HEIGHT&theme=$THEME&tz=Europe%2FMoscow")
if [ "$code" != "200" ]; then
    echo "$(date '+%F %T') render failed: HTTP $code" >&2
    exit 1
fi
# A renderer that is down or timing out still answers 200, with an HTML error page in the body — so
# check the PNG magic bytes instead of trusting the status code.
if [ "$(head -c 4 "$png" | od -An -tx1 | tr -d ' \n')" != "89504e47" ]; then
    echo "$(date '+%F %T') render returned $(wc -c <"$png") bytes that are not a PNG" >&2
    exit 1
fi

if [ -n "${DRY_RUN:-}" ]; then
    echo "$(date '+%F %T') dry run: panel $panel_id rendered, $(wc -c <"$png") bytes, not sent"
    exit 0
fi

caption="$CAPTION · $(TZ=Europe/Moscow date '+%d.%m %H:%M') МСК"
response=$(curl -sS -m 120 -X POST "https://api.telegram.org/bot$TG_BOT_TOKEN/sendPhoto" \
    -F "chat_id=$TG_CHAT_ID" -F "caption=$caption" -F "photo=@$png;type=image/png")
case "$response" in
    *'"ok":true'*)
        echo "$(date '+%F %T') sent panel $panel_id ($(wc -c <"$png") bytes)"
        ;;
    *)
        # Telegram echoes the bot token nowhere in its errors, so this is safe to log verbatim.
        echo "$(date '+%F %T') telegram rejected the photo: $response" >&2
        exit 1
        ;;
esac
