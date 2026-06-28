import ScreepsStatsd from "./src/ScreepsStatsd.js";

// SCREEPS_SHARD may be a comma list (e.g. "shard2,shard3") — one poller per shard, each tagging
// metrics as stats.gauges.<shard>.*. SCREEPS_TOKEN may be a single token or a comma list aligned with
// SCREEPS_SHARD (token[i] -> shard[i]); a single token is reused for every shard.
//
// RATE LIMITING: the memory-segment endpoint allows 360 req/h and (empirically) the bucket is SHARED
// across an account's tokens — extra tokens do NOT add budget. Polling at exactly 360/h (10s) leaves
// zero headroom, so any jitter trips a 429 (whose body is plain text, not JSON). We therefore space
// each shard's poll at BASE_INTERVAL_MS × shardCount, so the TOTAL request rate is 3600/BASE per hour
// regardless of shard count. BASE=12s → 300/h total, a safe ~83% of the cap. Pollers are also
// staggered so their requests don't fire in one simultaneous burst.
const shards = (process.env.SCREEPS_SHARD || "shard3")
    .split(",").map((s) => s.trim()).filter(Boolean);
const tokens = (process.env.SCREEPS_TOKEN || "")
    .split(",").map((t) => t.trim()).filter(Boolean);

const BASE_INTERVAL_MS = Number(process.env.SCREEPS_POLL_BASE_MS) || 12_000;
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
