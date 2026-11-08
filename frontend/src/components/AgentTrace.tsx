import type { TraceEvent } from "../lib/api";

interface StepState {
  agent: string;
  kind: "deterministic" | "llm" | "tool";
  status: "running" | "done";
  output?: Record<string, unknown>;
}

const AGENT_BLURB: Record<string, string> = {
  log_correlator: "regex parse → structured summary",
  hypothesis_agent: "the single model call",
  runbook_agent: "fixed remediation lookup",
};

function blurbFor(agent: string): string {
  if (agent.startsWith("agent_decision")) return "the model decides what to do next";
  if (agent.startsWith("tool_")) return "read-only log tool";
  if (agent.startsWith("self_check")) return "citations compared with the log";
  return AGENT_BLURB[agent] ?? "";
}

export function deriveSteps(events: TraceEvent[]): StepState[] {
  const steps: StepState[] = [];
  for (const event of events) {
    if (event.type === "step_start" && event.agent) {
      steps.push({
        agent: event.agent,
        kind: event.kind ?? "deterministic",
        status: "running",
      });
    } else if (event.type === "step_done" && event.agent) {
      const step = steps.find((candidate) => candidate.agent === event.agent);
      if (step) {
        step.status = "done";
        step.output = event.output;
      }
    }
  }
  return steps;
}

function SummaryPreview({ output }: { output: Record<string, unknown> }) {
  const errorIds = (output.error_request_ids as number[]) ?? [];
  const spacing = output.error_id_spacing as { mean_gap: number | null; max_contiguous_run: number } | undefined;
  const types = (output.error_type_counts as Record<string, number>) ?? {};

  return (
    <div className="stack" style={{ gap: 8 }}>
      <dl className="kv">
        <dt>requests</dt>
        <dd>
          {String(output.total_requests)} ({String(output.ok_count)} ok / {String(output.error_count)} error)
        </dd>
        <dt>error types</dt>
        <dd className="mono">
          {Object.keys(types).length
            ? Object.entries(types).map(([name, count]) => `${name} ×${count}`).join(", ")
            : "none"}
        </dd>
        <dt>failing ids</dt>
        <dd style={{ fontFamily: "var(--mono)", fontSize: 11.5 }}>
          {errorIds.length ? errorIds.join(", ") : "none"}
        </dd>
        <dt>spacing</dt>
        <dd>
          mean gap {spacing?.mean_gap ?? "—"} · longest run {spacing?.max_contiguous_run ?? 0}
        </dd>
        <dt>latency</dt>
        <dd>
          mean {String(output.latency_ms_mean)}ms · p90 {String(output.latency_ms_p90)}ms · max{" "}
          {String(output.latency_ms_max)}ms
        </dd>
      </dl>
      {Number(output.warning_count) > 0 && (
        <p className="muted" style={{ margin: 0 }}>
          {String(output.warning_count)} unrelated warning lines filtered out
        </p>
      )}
    </div>
  );
}

export default function AgentTrace({ events }: { events: TraceEvent[] }) {
  const steps = deriveSteps(events);
  if (!steps.length) {
    return <p className="muted" style={{ margin: 0 }}>Waiting for the first agent…</p>;
  }

  return (
    <div className="trace">
      {steps.map((step) => (
        <div
          key={step.agent}
          className={`step ${step.status === "done" ? "done" : "active"}`}
        >
          <div className="step-head">
            <span className="step-name">{step.agent}</span>
            <span className={`step-kind ${step.kind}`}>{step.kind}</span>
            <span className="step-status">
              {step.status === "running" ? <span className="spinner" /> : "done"}
            </span>
          </div>
          <p className="muted" style={{ margin: "3px 0 0" }}>{blurbFor(step.agent)}</p>

          {step.status === "done" && step.output && step.agent === "log_correlator" && (
            <div className="step-body">
              <SummaryPreview output={step.output} />
            </div>
          )}
          {step.status === "done" && step.output && /^(tool_|agent_decision|self_check)/.test(step.agent) && (
            <div className="step-body">
              <pre className="json">{JSON.stringify(step.output, null, 2)}</pre>
            </div>
          )}
          {step.status === "done" && step.output && step.agent === "runbook_agent" && (
            <div className="step-body">
              <p className="note">{String(step.output.remediation ?? "")}</p>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
