export type FaultType = "dependency_down" | "slow_query" | "pool_exhausted" | "no_fault";
export type Condition = "single_agent" | "pipeline" | "pipeline_no_spacing" | "tool_agent";

export interface ProviderInfo {
  provider: string;
  label: string;
  available: boolean;
  env_key: string | null;
  default_model: string;
  models: string[];
  comparable: boolean;
  note: string;
}

export interface KeyEntry {
  id: string;
  label: string;
  masked: string;
  active: boolean;
}

export interface KeyProvider {
  provider: string;
  label: string;
  keys: KeyEntry[];
}

export interface LogInfo {
  name: string;
  true_fault: FaultType;
  noise: boolean;
  request_count: number;
}

export interface LogDetail extends LogInfo {
  text: string;
  meta: Record<string, unknown>;
}

export interface LogSummary {
  total_requests: number;
  ok_count: number;
  error_count: number;
  error_type_counts: Record<string, number>;
  error_request_ids: number[];
  sample_error_messages: string[];
  latency_ms_mean: number;
  latency_ms_max: number;
  latency_ms_p90: number;
  error_id_spacing: { gaps: number[]; mean_gap: number | null; max_contiguous_run: number };
  warning_count: number;
  sample_warnings: string[];
  unparsed_lines: number;
}

export interface EvidenceReport {
  claims_checked: number;
  claims_supported: number;
  score: number | null;
  unsupported: string[];
  // Absent on runs stored before error-count checking existed.
  count_claims_checked?: number;
  count_claims_supported?: number;
  count_unsupported?: string[];
}

export interface DiagnosisResult {
  condition: Condition;
  provider: string;
  model: string;
  predicted_fault: FaultType | null;
  explanation: string;
  confidence: number | null;
  remediation: string;
  ranked_alternatives: { fault_type: string; confidence: number }[];
  agent_call_log: string[];
  llm_call_count: number;
  malformed: boolean;
  api_error: boolean;
  parse_error: string | null;
  input_tokens: number;
  output_tokens: number;
  llm_latency_ms: number;
  total_latency_ms: number;
  log_summary: LogSummary | null;
  evidence: EvidenceReport | null;
}

export interface RecoveryResult {
  action: string | null;
  target: string | null;
  valid: boolean;
  healthy_after: boolean;
  reason: string;
}

export interface TraceEvent {
  type: "run_start" | "step_start" | "step_done" | "result" | "error";
  agent?: string;
  kind?: "deterministic" | "llm" | "tool";
  output?: Record<string, unknown>;
  input_preview?: string;
  result?: DiagnosisResult;
  run_id?: number;
  true_fault?: FaultType;
  correct?: boolean;
  recovery_result?: RecoveryResult | null;
  message?: string;
  condition?: Condition;
  label?: string;
  provider?: string;
  model?: string;
}

export interface SweepEvent {
  type: string;
  sweep_id?: string;
  arm?: string;
  total?: number;
  completed?: number;
  log_name?: string;
  true_fault?: FaultType;
  condition?: Condition;
  predicted_fault?: FaultType | null;
  correct?: boolean;
  confidence?: number | null;
  malformed?: boolean;
  api_error?: boolean;
  error?: string;
  message?: string;
}

export interface ArmStats {
  provider: string;
  model: string;
  condition: Condition;
  n: number;
  n_failed: number;
  n_correct: number;
  accuracy: number;
  strict_accuracy: number;
  mean_confidence: number | null;
  malformed_rate: number;
  mean_evidence_score: number | null;
  input_tokens: number;
  output_tokens: number;
  mean_latency_ms: number;
}

export interface FaultStats {
  provider: string;
  model: string;
  condition: Condition;
  true_fault: FaultType;
  n: number;
  accuracy: number;
  mean_confidence: number | null;
}

export interface DeltaRow {
  provider: string;
  model: string;
  single_agent_accuracy: number;
  pipeline_accuracy: number;
  delta: number;
  strict_delta: number;
  n_per_condition: number;
}

export interface RateStat {
  k: number;
  n: number;
  rate: number | null;
  ci95: [number, number] | null;
}

export interface CountStat extends RateStat {
  runs_scored: number;
  runs_with_wrong_count: number;
}

export interface ConditionAnalysis {
  accuracy: RateStat;
  strict: RateStat;
  meets_proposal: RateStat;
  grounded: RateStat;
  // Absent from servers older than count checking.
  counts?: CountStat;
  recovery: RateStat;
  invalid_after_correct: number;
  failure_labels: Record<string, number>;
  malformed: number;
  brier: number | null;
  ece: number | null;
  mean_input_tokens: number | null;
  mean_output_tokens: number | null;
}

export interface PairedStat {
  only_single: number;
  only_pipeline: number;
  p: number;
}

export interface OtherCondition {
  summary: ConditionAnalysis & { mean_llm_calls: number | null };
  n_pairs: number;
  vs_single: PairedStat;
}

export interface RealLogPreview {
  redactions: Record<string, number>;
  summary: { total_lines: number; error_lines: number; line_formats: Record<string, number> };
  sample: string[];
}

export interface RealLogResult {
  error?: string;
  needs_human?: boolean;
  hypotheses?: {
    label: string;
    cause: string;
    confidence: number | null;
    evidence: { quote: string; found_in_log: boolean }[];
  }[];
  next_checks?: string[];
}

export interface AnalysisArm {
  others: Record<string, OtherCondition>;
  provider: string;
  model: string;
  request_count: number | null;
  n_pairs: number;
  single_agent: ConditionAnalysis;
  pipeline: ConditionAnalysis;
  paired: { correct: PairedStat; meets_proposal: PairedStat; grounded: PairedStat; recovery_valid: PairedStat };
}

export interface StatsResponse {
  sweep_id: string | null;
  pooled: boolean;
  by_arm: ArmStats[];
  by_fault: FaultStats[];
  false_positive_rate: Record<string, number>;
  deltas: DeltaRow[];
}

export interface RunRow {
  id: number;
  created_at: string;
  sweep_id: string | null;
  log_name: string;
  true_fault: FaultType;
  noise: number;
  condition: Condition;
  provider: string;
  model: string;
  predicted_fault: FaultType | null;
  correct: number | null;
  confidence: number | null;
  malformed: number;
  api_error: number;
  evidence_score: number | null;
  evidence_checked: number | null;
  input_tokens: number;
  output_tokens: number;
  llm_latency_ms: number;
  explanation: string | null;
  remediation: string | null;
  recovery_action: string | null;
  recovery_target: string | null;
  recovery_valid: number | null;
  failure_labels: string | null;
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status}: ${detail.slice(0, 300)}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  providers: (live = false) =>
    json<{ providers: ProviderInfo[] }>(`/api/providers?live=${live}`),
  taxonomy: () => json<any>("/api/taxonomy"),
  logs: () => json<{ logs: LogInfo[] }>("/api/logs"),
  log: (name: string) => json<LogDetail>(`/api/logs/${encodeURIComponent(name)}`),
  generate: (body: Record<string, unknown>) =>
    json<{ generated: LogInfo[] }>("/api/logs/generate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  startSweep: (body: Record<string, unknown>) =>
    json<{ sweep_id: string; total: number; arms: string[]; conditions: string[] }>("/api/sweep", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  stats: (sweepId?: string, pooled = false) =>
    json<StatsResponse>(`/api/stats?pooled=${pooled}${sweepId ? `&sweep_id=${sweepId}` : ""}`),
  runs: (params: Record<string, string | number | undefined> = {}) => {
    const query = Object.entries(params)
      .filter(([, value]) => value !== undefined && value !== "")
      .map(([key, value]) => `${key}=${encodeURIComponent(String(value))}`)
      .join("&");
    return json<{ runs: RunRow[] }>(`/api/runs${query ? `?${query}` : ""}`);
  },
  sweeps: () => json<{ sweeps: any[] }>("/api/sweeps"),
  analysis: (sweepId?: string) =>
    json<{ sweep_ids: string[]; arms: AnalysisArm[] }>(
      `/api/analysis${sweepId ? `?sweep_ids=${sweepId}` : ""}`,
    ),
  realLogPreview: (text: string) =>
    json<RealLogPreview>("/api/real-log/preview", { method: "POST", body: JSON.stringify({ text }) }),
  realLogDiagnose: (text: string, provider: string, model: string) =>
    json<RealLogResult>("/api/real-log/diagnose", {
      method: "POST",
      body: JSON.stringify({ text, provider, model }),
    }),
  keys: () => json<{ providers: KeyProvider[] }>("/api/keys"),
  addKey: (body: { provider: string; label: string; value: string; activate: boolean }) =>
    json<{ providers: KeyProvider[] }>("/api/keys", { method: "POST", body: JSON.stringify(body) }),
  activateKey: (provider: string, id: string) =>
    json<{ providers: KeyProvider[] }>(`/api/keys/${provider}/${id}/activate`, { method: "POST" }),
  removeKey: (provider: string, id: string) =>
    json<{ providers: KeyProvider[] }>(`/api/keys/${provider}/${id}`, { method: "DELETE" }),
  testKey: (provider: string, id: string) =>
    json<{ ok: boolean; message: string }>(`/api/keys/${provider}/${id}/test`, { method: "POST" }),
};

/** Read a text/event-stream response, invoking `onEvent` per `data:` frame. */
export async function streamSSE<T>(
  url: string,
  init: RequestInit,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(url, {
    ...init,
    signal,
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  if (!response.ok) {
    throw new Error(`${response.status}: ${(await response.text()).slice(0, 300)}`);
  }
  if (!response.body) throw new Error("this browser cannot read a streaming response");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("data: ")) {
          try {
            onEvent(JSON.parse(line.slice(6)) as T);
          } catch {
            /* a partial frame is not fatal; the next read completes it */
          }
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
}
