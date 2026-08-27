import ScreepsStatsd from "./src/ScreepsStatsd.js";

// SCREEPS_SHARD may be a comma list (e.g. "shard2,shard3") — one poller per shard, each tagging
// metrics as stats.gauges.<shard>.*. SCREEPS_TOKEN may be a single token or a comma list aligned with
// SCREEPS_SHARD (token[i] -> shard[i]); a single token is reused for every shard.
//
// RATE LIMITING: the memory-segment endpoint allows 360 req/h and (empirically) the bucket is SHARED
// across an account's tokens — extra tokens do NOT add budget. We space each shard's poll at
// BASE_INTERVAL_MS × shardCount, so the TOTAL request rate is 3600/BASE per hour regardless of shard
// count (this is what stops 2 shards from doing 720/h and exhausting the cap). BASE=10s → 360/h total,
// i.e. right at the cap. The 429 handler below makes the poller self-recover if it ever trips.
// Pollers are also staggered so their requests don't fire in one simultaneous burst.
//
// ⚠️ THIS COMMENT USED TO RECOMMEND "bump SCREEPS_POLL_BASE_MS to 12000 for a safety margin", AND
// THAT CURE IS WORSE THAN THE DISEASE. Measured 27.08.2026:
//
//   * the poller is NOT over-polling. Sampling x-ratelimit-remaining once a minute with nothing
//     else of ours running gave 6 foreign requests per 60s window, TEN windows out of ten — exactly
//     360/h, exactly what BASE=10s configures. (An earlier 90-second window read 11 and extrapolated
//     to 435/h; that was a small denominator, not a rate. And do not re-derive this by dividing the
//     total by the script's elapsed time: the loop sleeps once more than it measures, so 60 requests
//     over "661s" prints 327/h for ten windows that were 60s each.)
//   * the visible cost today is 44 null points out of 8640 on stats.gauges.<shard>.tick over 24h —
//     0.5%, longest gap 80s. Over 7d at the 1m archive: 7 of 10080, longest 180s. (Nulls ARE
//     expressible by that query: a row known dead came back 100% null, which is the control.)
//   * BASE=12000 would give 300/h and 60/h of headroom — but graphite-conf/storage-schemas.conf
//     stores `stats.*` at 10s:1d, so writing every 12s leaves one 10s bucket in six EMPTY. That is
//     ~17% nulls at the finest resolution against today's 0.5%: the panel gets visibly worse in
//     order to make a limit we are not actually exceeding less tight.
//
// The real shape of it: the quota (360/h) and the finest retention (10s = 360 points/h) are EQUAL BY
// CONSTRUCTION. There is no interval with headroom that also fills every bucket, so this is not a
// misconfiguration to tune away — it is the price of sampling at the resolution we store. What the
// 0.5% actually measures is OTHER consumers of the account's segment quota: every manual read, every
// screeps-probe run and every colony snapshot costs the poller one live sample. If the gaps ever
// matter, the lever is to make those readers stop competing — not to slow the poller down.
const shards = (process.env.SCREEPS_SHARD || "shard3")
    .split(",").map((s) => s.trim()).filter(Boolean);
const tokens = (process.env.SCREEPS_TOKEN || "")
    .split(",").map((t) => t.trim()).filter(Boolean);

const BASE_INTERVAL_MS = Number(process.env.SCREEPS_POLL_BASE_MS) || 10_000;
const intervalMs = BASE_INTERVAL_MS * shards.length;

shards.forEach((shard, i) => {
    const token = tokens.length === shards.length ? tokens[i] : (tokens[0] || "");
    const poller = new ScreepsStatsd(
        process.env.SCREEPS_HOST,
        token,
        shard,
        process.env.SCREEPS_SEGMENT,
        process.env.GRAPHITE_PORT_8125_UDP_ADDR,
        intervalMs,
    );
    // Stagger starts by BASE so N shards' requests don't burst together within each window.
    setTimeout(() => poller.run(), i * BASE_INTERVAL_MS);
});
