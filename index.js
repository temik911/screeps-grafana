import ScreepsStatsd from "./src/ScreepsStatsd.js";

// SCREEPS_SHARD may be a comma-separated list (e.g. "shard2,shard3") — one poller per shard, each
// tagging its metrics with the shard name (stats.gauges.<shard>.*). The segment endpoint is rate-
// limited to 360 req/h PER TOKEN, so per-shard poll interval scales with shard count to stay under it.
const shards = (process.env.SCREEPS_SHARD || "shard3")
    .split(",").map((s) => s.trim()).filter(Boolean);
const intervalMs = 10_000 * shards.length;

for (const shard of shards) {
    new ScreepsStatsd(
        process.env.SCREEPS_HOST,
        process.env.SCREEPS_TOKEN,
        shard,
        process.env.SCREEPS_SEGMENT,
        process.env.GRAPHITE_PORT_8125_UDP_ADDR,
        intervalMs,
    ).run();
}
