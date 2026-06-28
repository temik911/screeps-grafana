import ScreepsStatsd from "./src/ScreepsStatsd.js";

// SCREEPS_SHARD may be a comma list (e.g. "shard2,shard3") — one poller per shard, each tagging
// metrics as stats.gauges.<shard>.*. The segment endpoint is rate-limited to 360 req/h PER TOKEN.
//
// SCREEPS_TOKEN may also be a comma list, ALIGNED with SCREEPS_SHARD (token[i] is used for shard[i]).
// With one token per shard, each shard has its own rate-limit bucket → polls at the full 10s rate.
// With a single shared token for N shards, the interval scales to 10s × N so the shared 360/h budget
// isn't exceeded (otherwise we'd halve effective freshness).
const shards = (process.env.SCREEPS_SHARD || "shard3")
    .split(",").map((s) => s.trim()).filter(Boolean);
const tokens = (process.env.SCREEPS_TOKEN || "")
    .split(",").map((t) => t.trim()).filter(Boolean);

const perShardToken = tokens.length === shards.length && tokens.length > 1;
const intervalMs = perShardToken ? 10_000 : 10_000 * shards.length;

shards.forEach((shard, i) => {
    const token = perShardToken ? tokens[i] : tokens[0];
    new ScreepsStatsd(
        process.env.SCREEPS_HOST,
        token,
        shard,
        process.env.SCREEPS_SEGMENT,
        process.env.GRAPHITE_PORT_8125_UDP_ADDR,
        intervalMs,
    ).run();
});
