import fetch from 'node-fetch';
import StatsD from 'node-statsd';

const FETCH_INTERVAL_MS = 10_000;

export default class ScreepsStatsd {
  constructor(host, token, shard, segment, graphite) {
    this._host = host;
    this._token = token;
    this._shard = shard;
    this._segment = Number(segment);
    this._client = new StatsD({host: graphite});
  }

  run() {
    setInterval(() => this.getMemory(), FETCH_INTERVAL_MS);
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
      const body = await response.json();
      if (body.error || body.ok !== 1) throw new Error(body.error ?? `bad response: ${JSON.stringify(body)}`);
      if (!body.data) return;
      const data = JSON.parse(body.data);
      this.report(data);
    } catch (e) {
      console.error(e);
    }
  }

  report(data, prefix = "") {
    if (prefix === '') console.log("Pushing to gauges -", new Date());
    for (const [k, v] of Object.entries(data)) {
      if (v && typeof v === 'object') {
        this.report(v, prefix + k + '.');
      } else if (typeof v === 'number') {
        this._client.gauge(prefix + k, v);
      }
    }
  }
}