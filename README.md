# Multi-Agent Incident Response via Controlled Fault Injection

A full-stack implementation of the CSE 598 capstone proposal: a toy service with
deterministic fault injection, two diagnosis architectures compared head to head,
and a web dashboard to drive and inspect both.

The research question is whether a **multi-agent pipeline** (deterministic log
parsing → one LLM call → deterministic runbook lookup) diagnoses faults better
than a **single-agent baseline** (one LLM call on raw log text). Both conditions
make exactly one model call, so any difference is attributable to architecture
rather than to API budget.

---

## What this adds to the Phase 0 proposal

Phase 0 reported 100% accuracy for *both* conditions across 16 logs — a ceiling
effect that leaves the research question unanswerable. Four changes open it back up:

| Addition | Why |
|---|---|
| **Multi-provider** — Claude *and* Groq-hosted open models behind one interface | The capability axis. Opus solves this task from raw text either way; weaker models are where structured input should start to matter. Groq's free tier makes the sweep cost nothing. |
| **Noise mode** — one shared error name for database faults, unrelated warnings, red-herring transients, irregular cadence | Removes the error name that would identify the fault by itself, leaving the distribution of failures to reason about. Addresses §7 of the proposal. |
| **Evidence-citation scoring** | Checks that request ids, error types and latency figures cited in the explanation actually exist in the log. A correct label citing invented evidence is right by luck, and accuracy alone can't see that. |
| **Web tier** — FastAPI + React dashboard | Live agent trace, sweep runner, comparison charts, run history. |

The proposal's documented CLI commands work as written. This is a rebuild from the PDF, not a fork of the original repository, so its logs and outputs are not byte-identical to Phase 0.

---

## Quick start

No API key is needed to run everything — the `mock` provider is a deterministic
offline rule engine.

```bash
cd backend && pip install -r requirements.txt
```

```bash
cd frontend && npm install
```

Copy `backend/.env.example` to `backend/.env` and fill in whichever keys you have.

Then start both servers (two terminals):

```bash
python -m uvicorn --app-dir backend incident_response.api:app --port 8000
```

```bash
npm --prefix frontend run dev
```

Open http://localhost:5173. The **API keys** button in the header saves, tests,
switches and removes keys for each provider. The active key lives in `backend/.env`,
so the CLI uses the same one, and other saved keys live in `backend/data/keys.json`.
Both are plain text and gitignored, and the backend only answers on local hostnames.

---

## Command line

The proposal's documented invocations work as written:

```bash
python generate_logs.py --fault-type pool_exhausted --run-id 1
```

```bash
python diagnose.py --log logs/pool_exhausted_run1.log
```

```bash
python run_pipeline.py --log logs/pool_exhausted_run1.log
```

```bash
python evaluate.py --logs-dir logs/ --output outputs/comparison_results.csv
```

Plus the new flags:

```bash
python generate_logs.py --suite --runs-per-fault 10 --noise
```

```bash
python evaluate.py --arms anthropic:claude-opus-5-5,groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b
```

`--provider mock` on any command runs it offline.

---

## Architecture

```
backend/
  incident_response/
    toy_service.py          deterministic fault injection, request-id keyed
    log_generator.py        writes <scenario>.log + hidden <scenario>.meta.json
    taxonomy.py             fault types, descriptions, runbook table
    evidence.py             automatic evidence-citation scoring
    conditions.py           the two experimental arms, as event streams
    evaluation.py           sweep engine (log × model × condition)
    db.py                   SQLite persistence + aggregation
    api.py                  FastAPI, SSE endpoints
    agents/
      log_correlator.py     step 1 — deterministic regex parse
      hypothesis_agent.py   step 2 — THE single LLM call
      runbook_agent.py      step 3 — deterministic lookup
    providers/
      base.py               LLMClient protocol, tolerant JSON extraction
      anthropic_client.py   official anthropic SDK
      groq_client.py        openai SDK pointed at Groq
      mock_client.py        offline rule engine
  diagnose.py  run_pipeline.py  generate_logs.py  evaluate.py

frontend/
  src/components/  DiagnoseView · AgentTrace · VerdictCard · CompareView · RunsView
```

The CLI and the API both import the same core package, so the web tier cannot
drift from the graded command-line artifacts.

### Conditions

```
single_agent : raw log text ───────────────────────► [Hypothesis Agent · LLM] ──► verdict

pipeline     : raw log text ──► [Log Correlator]  ──► [Hypothesis Agent · LLM] ──► [Runbook] ──► verdict
                                 deterministic                 ONE call           deterministic
```

The Log Correlator and Runbook Agent are plain Python because parsing, counting
and table lookup are arithmetic, not reasoning. Spending LLM calls on them would
break the call-count parity that makes this a test of architecture.

---

## Methodology notes

Decisions that matter if you are reading the numbers:

**Effort is pinned, not just call count.** On current Claude models thinking is
always on and bills as output. Leaving effort at its default would let the
raw-log arm spend more reasoning than the structured-summary arm, so the two
would be matched on call count but not on compute. Both arms run at
`output_config: {effort: "low"}`.

**JSON is extracted by prompt, not by provider machinery.** Anthropic's
`output_config.format` and OpenAI's `response_format` differ in strength. Using
each provider's best enforcement would make the malformed-output rate a
measurement of the enforcement layer rather than of the model, so every provider
gets plain prompting and the same tolerant parser. Malformed answers are
recorded as a metric, never retried away. (Rate-limit failures *are* retried
with backoff — that is transport, not model behaviour.)

**Both conditions get the same facts.** The healthy-latency baseline lives in the
shared system prompt. The summary payload adds only notes describing its own
fields.

**Accuracy is reported two ways.** `accuracy` scores the fault the model named,
even if the rest of its JSON was broken. `strict` also requires valid JSON. If the
pipeline wins only on `strict`, its advantage is formatting rather than diagnosis.
API failures are excluded from both and counted as `failed`.

**The mock provider is a rules-only baseline, not a model.** It pattern-matches
strings, was written with knowledge of the log generator, and also lets the dashboard
and harness be developed offline. The API rejects
sweeps that mix it with real models and leaves it out of the pooled view.

---

## Analysis

```bash
python analyze.py
```

Reports, per model and log length: accuracy, whether each run meets the proposal's
success criterion (right fault and no invented evidence), whether it is grounded
(cites the real cause), exact McNemar p-values on the paired logs, Brier and
calibration error, and token use. The dashboard's Compare tab shows the same table.

To test log length, generate a set for each length with `--requests 75 --logs-dir
logs/r75`, run one sweep per set, then pass all the ids to `analyze.py --sweeps`.
Non-default lengths get a `_r75` suffix so they can coexist.

---

## Recovery and failure labels

`--recovery` on `evaluate.py`, `diagnose.py` or `run_pipeline.py` (or the checkbox in
the dashboard) also asks the model for one action and target, inside its single call.
The plan is applied to the toy service, the scenario is replayed, and the new log is
checked for health. Nothing real is executed. Because this changes the prompt,
runs with it are not comparable with runs without it.

Every run also gets rule-based problem labels, such as `false_alarm`,
`blamed_red_herring` or `right_but_ungrounded`, shown in the Compare and Runs tabs.

---

## Experiment variants

Each is opt-in, so earlier sweeps stay comparable.

| Option | What it does |
|---|---|
| `--conditions single_agent,pipeline,pipeline_no_spacing` | Adds a pipeline given only the proposal's summary fields, to test whether gains come from structure or from the spacing feature |
| `--conditions ...,tool_agent` | Adds an agent that chooses read-only log tools, up to 5 rounds, and gets one chance to correct citations an automatic check could not confirm. It makes several LLM calls |
| `--no-hint` | Removes the spread-versus-clustered rule from the prompt |
| `--compound` with `generate_logs.py --suite --compound` | Adds two-fault runs, scored as an exact set |

The summary measures failure spacing on the most common error type and lists other
error types separately, so an unrelated transient cannot extend a burst.

## Real logs

The **Real log** tab accepts JSON lines, key=value or plain text up to 5 MB. Secrets and
personal data are removed first, you can review exactly what will be sent, and the model
sees only a summary and a sample of lines. Results are advisory, low-confidence or
unconfirmed answers ask for a person to investigate, and nothing is stored. Automatic
redaction can miss things, so review the sample before sending logs from a real system.

## Public benchmark: RCAEval RE3

`evaluate_rcaeval.py` asks which service caused each failure, using the
[RCAEval](https://zenodo.org/records/14590730) RE3 archives placed in `backend/external/rcaeval/`
(CC-BY-4.0, not included in this repository). It compares a rules control, a sample of raw log
lines, and a per-service summary, scored by Top-1 and Top-3 accuracy.

```bash
python evaluate_rcaeval.py --per-system 10 --arm groq:qwen/qwen3.8-27b
```

## Docker

```bash
docker compose up --build
```

Then open http://localhost:8080. Put keys in `backend/.env` before starting.

## Tests

Offline checks that make no API calls. Run all of them with one command (Python 3.11+, from any directory):

```bash
python backend/tests/run_all.py
```

It runs every `backend/tests/check_*.py`, prints a pass/fail summary per script and exits
non-zero if any fail. For `check_end_to_end.py` it starts the API on port 8123 from a
temporary copy of `backend/` with no `.env`, data, logs or `*_API_KEY` variables, then shuts
it down, so your own keys and run history are never touched. Port 8123 must be free.

Pass part of a name to run a subset, and `-v` to see every check line:

```bash
python backend/tests/run_all.py scoring end_to_end -v
```

---

## Cost

Per full sweep of 16 logs × 2 conditions = 32 calls, roughly 1.1k input / 450
output tokens each:

| Model | Per sweep |
|---|---|
| `claude-opus-5-5` | ~$0.45 (higher with thinking tokens) |
| `claude-sonnet-5-5` | ~$0.21 |
| `claude-haiku-5-5` | ~$0.01 |
| Groq free tier | free |

Scaling to n=50 runs per fault type across four models is still under $15, and
free if the Claude arms are left out.

---

## What to run to get an actual result

```bash
python generate_logs.py --suite --runs-per-fault 10 --noise
```

```bash
python evaluate.py --arms groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b,anthropic:claude-haiku-5-5
```

Read the **Pipeline minus baseline** block at the end. The hypothesis is that
the delta is ~0 on strong models and positive on weak ones. A flat delta across
every capability level is a real negative result, and a stronger one than 100%
vs 100% on sixteen logs.
