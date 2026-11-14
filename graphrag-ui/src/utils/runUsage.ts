// Totals of one ingest, conversion or rebuild run, as the server returns them.
export interface RunUsage {
  kind: string;
  status: string;
  seconds: number;
  phases?: Record<string, number>;
  totals: {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    cost: number;
  };
}

const formatSeconds = (s: number): string => {
  if (s < 60) return `${Math.max(0, Math.round(s))} s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  if (m < 60) return rest ? `${m} min ${rest} s` : `${m} min`;
  const h = Math.floor(m / 60);
  return `${h} h ${m % 60} min`;
};

const formatCost = (cost: number): string =>
  cost >= 1 ? `$${cost.toFixed(2)}` : cost >= 0.01 ? `$${cost.toFixed(3)}` : `$${cost.toFixed(4)}`;

/**
 * One line with the time, model calls, tokens and cost of a run, for
 * example "Took 1 min 12 s · 46 model calls · 81,200 tokens · cost $0.42".
 * Returns "" when there is nothing to show.
 */
export function describeRunUsage(usage: unknown): string {
  const u = usage as RunUsage | undefined;
  if (!u || typeof u.seconds !== "number" || !u.totals) return "";
  const parts = [`Took ${formatSeconds(u.seconds)}`];
  const { calls, input_tokens, output_tokens, cost } = u.totals;
  if (calls > 0) {
    parts.push(`${calls.toLocaleString()} model call${calls === 1 ? "" : "s"}`);
    const tokens = (input_tokens || 0) + (output_tokens || 0);
    if (tokens > 0) parts.push(`${tokens.toLocaleString()} tokens`);
    if (cost > 0) parts.push(`cost ${formatCost(cost)}`);
  }
  return parts.join(" · ");
}
