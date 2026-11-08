/** Chart colour assignments.
 *
 * Colour encodes the *entity*, never its rank: `single_agent` is always slot 1
 * and `pipeline` is always slot 2, in every chart on every screen, so filtering
 * or re-sorting never repaints a series.
 */

export const SERIES = {
  single_agent: "#2a78d6",
  pipeline: "#eb6834",
} as const;

export const STATUS = {
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
} as const;

export const FAULT_COLORS: Record<string, string> = {
  dependency_down: "#2a78d6",
  slow_query: "#eda100",
  pool_exhausted: "#eb6834",
  no_fault: "#1baf7a",
};

export const CONDITION_LABELS: Record<string, string> = {
  single_agent: "Single-agent baseline",
  pipeline: "Multi-agent pipeline",
};

export const FAULT_LABELS: Record<string, string> = {
  dependency_down: "dependency down",
  slow_query: "slow query",
  pool_exhausted: "pool exhausted",
  no_fault: "no fault",
};

export function pct(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}
