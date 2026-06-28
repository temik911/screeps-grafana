import fetch from 'node-fetch';
import StatsD from 'node-statsd';

const DEFAULT_FETCH_INTERVAL_MS = 10_000;

export default class ScreepsStatsd {
  constructor(host, token, shard, segment, graphite, intervalMs = DEFAULT_FETCH_INTERVAL_MS) {
    this._host = host;
    this._token = token;
    this._shard = shard;
    this._segment = Number(segment);
    this._intervalMs = intervalMs;
    this._client = new StatsD({host: graphite});
  }

  run() {
    setInterval(() => this.getMemory(), this._intervalMs);
  }

  async getMemory() {
    try {
      const url = `${this._host}/api/user/memory-segment?segment=${this._segment}&shard=${this._shard}`;
      const response = await fetch(url, {
        method: 'GET',
        headers: {
          'X-Token': this._token,
          'content-type': 'application/json',
        },
      });
      // Handle non-2xx BEFORE response.json() — error bodies are plain text (e.g. 429 "Rate limit
      // exceeded, retry after …"), so JSON.parse would throw and spam a stack trace. Log one concise
      // line and skip this cycle; the next interval retries.
      if (response.status === 429) {
        const retry = response.headers.get('retry-after');
        console.warn(`[${this._shard}] rate limited (360/h shared) — retry-after ${retry}s; skipping`);
        return;
      }
      if (!response.ok) {
        console.warn(`[${this._shard}] memory-segment HTTP ${response.status}; skipping`);
        return;
      }
      const body = await response.json();
      if (body.error || body.ok !== 1) {
        console.warn(`[${this._shard}] bad response: ${body.error ?? JSON.stringify(body)}`);
        return;
      }
      if (!body.data) return;
      const data = JSON.parse(body.data);
      // Namespace every metric under its shard: stats.gauges.<shard>.* — so multiple shards don't
      // collide on the same series. The dashboard templates on $shard to pick which shard to view.
      this.report(data, this._shard + '.');
    } catch (e) {
      console.error(`[${this._shard}]`, e);
    }
  }

  report(data, prefix = "") {
    if (prefix === this._shard + '.') console.log(`Pushing ${this._shard} gauges -`, new Date());
    for (const [k, v] of Object.entries(data)) {
      if (v && typeof v === 'object') {
        this.report(v, prefix + k + '.');
      } else if (typeof v === 'number') {
        this._client.gauge(prefix + k, v);
      }
    }
  }
}
