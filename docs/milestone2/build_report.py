"""Build the Milestone 2 report from the course template and the recorded runs."""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
import sys
import zipfile
from xml.sax.saxutils import escape

from PIL import Image

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "backend"))

from incident_response.analysis import exact_mcnemar, wilson_interval  # noqa: E402

TEMPLATE = pathlib.Path(sys.argv[1])
OUTPUT = HERE / "Milestone2_Report.docx"
FIGURES = HERE / "figures"
COMMIT = sys.argv[2] if len(sys.argv) > 2 else "[[commit hash]]"
TEXT = json.loads((HERE / "report_results.json").read_text(encoding="utf-8"))

SWEEPS = TEXT["sweeps"]
MODELS = {
    "qwen/qwen3.8-27b": "Qwen3.8 27B",
    "openai/gpt-oss-120b": "gpt-oss-120b",
    "openai/gpt-oss-20b": "gpt-oss-20b",
}
DATASETS = {"noisy": "D1 Noisy", "clean": "D2 Clean", "stakeholder": "D3 Proposal"}

db = sqlite3.connect(str(REPO / "backend/data/runs.db"))
db.row_factory = sqlite3.Row

# ---- XML helpers ---------------------------------------------------------

images: list[tuple[str, pathlib.Path]] = []


def runs(text: str, size: int | None = None, bold: bool = False) -> str:
    out = []
    for part in re.split(r"(\*\*.+?\*\*|\*[^*]+?\*|`.+?`|\[\[.+?\]\])", text):
        if not part:
            continue
        props, body = [], part
        if part.startswith("**"):
            body, props = part[2:-2], ["<w:b/>"]
        elif part.startswith("*"):
            body, props = part[1:-1], ["<w:i/>"]
        elif part.startswith("`"):
            body, props = part[1:-1], ['<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>']
        elif part.startswith("[["):
            body, props = part[2:-2], ['<w:highlight w:val="yellow"/>']
        if bold and "<w:b/>" not in props:
            props.insert(0, "<w:b/>")
        if size:
            props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
        rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(body)}</w:t></w:r>')
    return "".join(out)


def p(text: str, style: str | None = None, align: str | None = None, size: int | None = None, keep: bool = False) -> str:
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if keep:
        props += "<w:keepNext/>"
    if align:
        props += f'<w:jc w:val="{align}"/>'
    ppr = f"<w:pPr>{props}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{runs(text, size)}</w:p>"


def h1(text: str) -> str:
    return p(text, "Heading1")


def h2(text: str) -> str:
    return p(text, "Heading2")


def bullet(text: str) -> str:
    return p(text, "ListBullet")


def table(rows: list[list[str]], widths: list[int], size: int = 17) -> str:
    border = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    body = []
    for index, row in enumerate(rows):
        cells = []
        for width, text in zip(widths, row):
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>' if index == 0 else ""
            margin = '<w:spacing w:before="20" w:after="20"/>'
            cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shade}</w:tcPr>'
                f"<w:p><w:pPr>{margin}</w:pPr>{runs(text, size, bold=index == 0)}</w:p></w:tc>"
            )
        header = "<w:cantSplit/><w:tblHeader/>" if index == 0 else "<w:cantSplit/>"
        body.append(f"<w:tr><w:trPr>{header}</w:trPr>{''.join(cells)}</w:tr>")
    total = sum(widths)
    return (
        f'<w:tbl><w:tblPr><w:tblW w:w="{total}" w:type="dxa"/><w:jc w:val="center"/><w:tblBorders>{border}</w:tblBorders>'
        f'<w:tblLayout w:type="fixed"/><w:tblCellMar><w:left w:w="50" w:type="dxa"/><w:right w:w="50" w:type="dxa"/></w:tblCellMar></w:tblPr><w:tblGrid>{grid}</w:tblGrid>{"".join(body)}</w:tbl>'
        '<w:p><w:pPr><w:spacing w:after="60"/></w:pPr></w:p>'
    )


def figure(name: str, width_inches: float, caption: str) -> str:
    path = FIGURES / name
    images.append((name, path))
    index = len(images)
    with Image.open(path) as image:
        ratio = image.height / image.width
    cx, cy = int(width_inches * 914400), int(width_inches * ratio * 914400)
    drawing = (
        f'<w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{100 + index}" name="Figure {index}" descr="{escape(caption, {chr(34): "&quot;"})}"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:nvPicPr><pic:cNvPr id="{index}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:embed="rIdFig{index}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
    )
    return f'<w:p><w:pPr><w:keepNext/><w:jc w:val="center"/></w:pPr>{drawing}</w:p>' + p(f"**Figure {index}.** {caption}", align="center", size=18)


# ---- results -------------------------------------------------------------


def fetch(dataset: str, model: str, condition: str) -> list[sqlite3.Row]:
    sweep = SWEEPS[dataset][model]
    return db.execute(
        "select * from runs where sweep_id=? and model=? and condition=? order by id", (sweep, model, condition)
    ).fetchall()


def rate(rows: list, test) -> tuple[int, int]:
    return sum(1 for row in rows if test(row)), len(rows)


def pct(k: int, n: int) -> str:
    return f"{100 * k / n:.1f}%" if n else "n/a"


def result_row(dataset: str, label: str, model: str, condition: str, model_label: str, bold: bool = False) -> list[str]:
    all_rows = fetch(dataset, model, condition)
    answered = [row for row in all_rows if not row["api_error"]]
    k, n = rate(answered, lambda r: r["correct"] == 1)
    interval = wilson_interval(k, n)
    strict, _ = rate(answered, lambda r: r["correct"] == 1 and not r["malformed"])
    met, _ = rate(answered, lambda r: r["meets_proposal"] == 1)
    grounded, _ = rate(answered, lambda r: r["grounded"] == 1)
    fp_rows = [row for row in answered if row["true_fault"] == "no_fault"]
    fp, fn = rate(fp_rows, lambda r: r["predicted_fault"] not in (None, "no_fault"))
    tokens = "none" if model == "mock-heuristic" else (
        f"{sum(r['input_tokens'] for r in answered) / n:.0f} / {sum(r['output_tokens'] for r in answered) / n:.0f}" if n else "n/a"
    )
    latency = "<1" if model == "mock-heuristic" else (f"{sum(r['llm_latency_ms'] for r in answered) / n / 1000:.1f}" if n else "n/a")
    ci = f" [{interval[0]:.2f}, {interval[1]:.2f}]" if interval else ""
    # Rules produce no model output, so format and explanation scores do not apply.
    scored = ["n/a"] * 3 if model == "mock-heuristic" else [pct(strict, n), pct(met, n), pct(grounded, n)]
    row = [DATASETS[dataset], label, model_label, str(n), f"{pct(k, n)}{ci}", *scored, pct(fp, fn), tokens, latency]
    return [f"**{cell}**" for cell in row] if bold else row


def results_table() -> str:
    rows = [["Data", "Method", "Model", "n", "Accuracy [95% CI]", "Strict", "Proposal", "Grounded", "False alarm", "Tokens in/out", "Sec."]]
    for dataset in DATASETS:
        rows.append(result_row(dataset, "B: Rules", "mock-heuristic", "single_agent", "none"))
        for model, name in MODELS.items():
            if model not in SWEEPS[dataset] or not fetch(dataset, model, "pipeline"):
                continue
            rows.append(result_row(dataset, "A: Raw log", model, "single_agent", name))
            rows.append(result_row(dataset, "Pipeline", model, "pipeline", name, bold=True))
    return table(rows, [700, 880, 1150, 380, 1620, 680, 800, 860, 640, 1040, 500], size=15)


def rcaeval_table() -> str:
    outputs = REPO / "backend" / "outputs"
    load = lambda name: [json.loads(line) for line in (outputs / f"rcaeval_{name}.jsonl").read_text(encoding="utf-8").splitlines()]
    rows = [["Method", "Model", "n", "Top-1", "Top-1 95% CI", "Top-3", "Tokens in/out", "Only raw right", "Only summary right", "Exact p"]]
    data = load("rules")
    hits, top3 = sum(r["top1"] for r in data), sum(r["top3"] for r in data)
    interval = wilson_interval(hits, len(data))
    rows.append(["B: Rules", "none", str(len(data)), f"{hits}/{len(data)}", f"[{interval[0]:.2f}, {interval[1]:.2f}]", f"{top3}/{len(data)}", "none", "", "", ""])
    for key, name in (("groq_qwen_qwen3.8-27b", "Qwen3.8 27B"), ("groq_openai_gpt-oss-120b", "gpt-oss-120b")):
        data = load(key)
        by_case = {condition: {r["case_id"]: r for r in data if r["condition"] == condition} for condition in ("raw_sample", "summary")}
        only_raw = sum(by_case["raw_sample"][c]["top1"] and not by_case["summary"][c]["top1"] for c in by_case["raw_sample"])
        only_summary = sum(by_case["summary"][c]["top1"] and not by_case["raw_sample"][c]["top1"] for c in by_case["raw_sample"])
        for condition, label, bold in (("raw_sample", "A: Raw sample", False), ("summary", "Pipeline summary", True)):
            sub = list(by_case[condition].values())
            n = len(sub)
            hits, top3 = sum(r["top1"] for r in sub), sum(r["top3"] for r in sub)
            interval = wilson_interval(hits, n)
            tokens = f"{sum(r['input_tokens'] for r in sub) / n:.0f} / {sum(r['output_tokens'] for r in sub) / n:.0f}"
            row = [label, name, str(n), f"{hits}/{n}", f"[{interval[0]:.2f}, {interval[1]:.2f}]", f"{top3}/{n}", tokens]
            row += [str(only_raw), str(only_summary), f"{exact_mcnemar(only_raw, only_summary):.3f}"] if bold else ["", "", ""]
            rows.append([f"**{cell}**" if cell else "" for cell in row] if bold else row)
    return table(rows, [1250, 1050, 340, 640, 1080, 640, 1120, 780, 960, 620], size=15)


def paired_table() -> str:
    rows = [["Data", "Model", "Pairs", "Only baseline A right", "Only pipeline right", "Exact McNemar p"]]
    for dataset in DATASETS:
        for model, name in MODELS.items():
            if model not in SWEEPS[dataset] or not fetch(dataset, model, "pipeline"):
                continue
            single = {r["log_name"]: r for r in fetch(dataset, model, "single_agent") if not r["api_error"]}
            pipe = {r["log_name"]: r for r in fetch(dataset, model, "pipeline") if not r["api_error"]}
            common = single.keys() & pipe.keys()
            only_a = sum(1 for key in common if single[key]["correct"] == 1 and pipe[key]["correct"] != 1)
            only_p = sum(1 for key in common if single[key]["correct"] != 1 and pipe[key]["correct"] == 1)
            rows.append([DATASETS[dataset], name, str(len(common)), str(only_a), str(only_p), f"{exact_mcnemar(only_a, only_p):.3f}"])
    return table(rows, [1100, 1900, 800, 2000, 1900, 1660])


# ---- report --------------------------------------------------------------

B = []
B.append(p("Capstone Project Milestone 2 Report", "Title"))
B.append(p("**Project title:** Multi-Agent Incident Response via Controlled Fault Injection"))
B.append(h1("Basic Information"))
B.append(
    table(
        [["Student name", "Email"]]
        + [[f"[[Student {i} name]]", "[[email]]"] for i in range(1, 6)],
        [4680, 4680],
        size=20,
    )
)
B.append(p("**Stakeholder name:** Krithika Hegde     **Meeting date:** November 8, 2026 [[confirm after the meeting]]"))

B.append(h2("Highlights"))
B.append(bullet("**A complete system:** a Python agent core, FastAPI and SQLite backend, React dashboard with live agent traces, and a tool-using diagnosis agent, packaged with automated tests, continuous integration and Docker."))
B.append(bullet("**Evaluated on four datasets,** including the stakeholder's own logs and RCAEval RE3, a public benchmark of faults in deployed microservice systems."))
B.append(bullet("**A strong result on the public benchmark:** with Qwen3.8 27B, our per-service summary identified the root-cause service in 19 of 30 cases, against 11 of 30 from raw logs and 8 of 30 from rules (paired p = 0.039), using 44% fewer input tokens."))
B.append(bullet("**Frontier-level accuracy from a small open model on the stakeholder's logs:** Qwen3.8 27B scored 16 of 16, matching the Claude Opus result published in the proposal."))
B.append(h1("Section 1  Stakeholder Follow Up and Final Deliverables (10 points)"))
B.append(p("[[Complete after the stakeholder meeting. Nothing in this section may be filled in before it takes place.]]"))
B.append(p("**Participants:** [[names]]. **Meeting notes:** [[brief notes, here or in an appendix]]."))
B.append(p("**Presented:** [[confirm what was actually shown. Planned: a live diagnosis in the dashboard comparing both conditions, the results in Table 1, and the failure cases in Section 5.]]"))
B.append(p("**Useful results and unmet needs:** [[stakeholder's view]]."))
B.append(
    table(
        [["Feedback", "Team response", "Next action"]] + [["[[feedback]]", "[[what changed, or why not]]", "[[action]]"] for _ in range(3)],
        [3120, 3120, 3120],
        size=19,
    )
)
B.append(p("**Agreed final deliverables, functionality and success criteria:** [[as confirmed with the stakeholder]]."))

B.append(h1("Section 2  Implemented Agentic System (25 points)"))
B.append(
    p(
        "The system turns a raw service log into an actionable diagnosis: a fault class, an explanation citing evidence, a confidence score, ranked alternatives and a remediation step. "
        "Figure 1 shows the implemented workflow and the planned extensions. A deterministic toy service injects a known fault into each run and records the ground truth in a sidecar file no model sees, which gives every result a verifiable answer. "
        "In the proposed pipeline, a parser distills the raw log into a structured summary, one LLM call reasons over it, and a lookup table returns the remediation. Baseline A sends the raw log to the same prompt. "
        "Both make exactly one LLM call, so differences reflect the design of the input rather than extra compute."
    )
)
B.append(figure("architecture.png", 4.8, "Implemented workflow, baselines, scoring and planned components."))
B.append(
    table(
        [
            ["Mechanism", "When used", "Reads and updates", "Effect on later steps"],
            ["Log Correlator (deterministic agent)", "First pipeline step", "Reads raw log. Produces counts, failing ids, spacing and latency statistics, and filters unrelated warnings.", "Its summary is the only input the LLM sees."],
            ["Hypothesis Agent (LLM reasoning)", "The single model call, in both LLM conditions", "Reads the taxonomy and either the summary or the raw log. Produces the fault, explanation, confidence and ranking.", "The label selects the runbook entry and is what gets scored."],
            ["Runbook Agent (deterministic agent)", "After the hypothesis", "Reads the label. Returns a fixed remediation.", "Ends the run."],
            ["Coordination and state", "Every run", "A fixed sequential orchestrator emits a trace event per step. Each run is stored in SQLite with its inputs, usage and scores.", "Drives the live dashboard trace and all later analysis."],
            ["Verification", "After each run", "Reads the explanation, log and hidden ground truth. Checks cited evidence, grounding and, in recovery mode, replays the proposed fix on the toy service.", "Affects scoring only. It does not change the agent's answer."],
            ["Error handling and recovery", "On failure", "Tolerant JSON parsing, recovery of the fault label from broken JSON, retries with backoff on rate limits, and stopping a model at its daily quota.", "Separates API failures from model failures in the results."],
        ],
        [1950, 1450, 3560, 2400],
    )
)
B.append(
    p(
        "**Design justification.** We matched each responsibility to the right tool. Parsing, counting and table lookup are deterministic, which makes them fast, auditable and free of model error, and it holds LLM calls equal across conditions so the comparison is rigorous. "
        "The locked Milestone 2 pipeline uses this fixed, auditable coordination, with verification applied after each answer. We then extended the system with a tool-using agent on branch `final-work`: the model chooses read-only log tools, decides when it has enough evidence, and gets one chance to correct citations an automatic check cannot confirm. "
        "In a pilot on D3 with gpt-oss-20b it scored 15 of 16 against 14 of 16 for the fixed pipeline in the same run, using 2.5 LLM calls and 2,222 input tokens per log against 1 call and 922 tokens. It chose tools per log, calling `list_errors` and `measure_spacing` only where the evidence warranted it, and evaluation across all datasets is planned in Section 6."
    )
)
B.append(
    p(
        "**Models and services.** Groq-hosted `openai/gpt-oss-120b`, `openai/gpt-oss-20b` and `qwen/qwen3.8-27b` through the OpenAI SDK. "
        "A Claude client is implemented, and its live evaluation is planned in Section 6. The application uses FastAPI, SQLite, React with Vite, and Recharts."
    )
)
B.append(
    p(
        "**Changes from the proposal.** We substantially extended the original scope. The evaluation now spans several hosted models and four datasets, including a public benchmark. Noise mode gives both database faults the same error name, so a "
        "single string cannot identify the fault. The summary gained failure-spacing fields. Evidence checking, grounded scoring, simulated recovery, "
        "failure labels and a web interface were added. [[Confirm and extend against the Milestone 1 report.]]"
    )
)

B.append(h1("Section 3  Preliminary Evaluation Results and Analysis (30 points)"))
B.append(h2("Datasets"))
B.append(
    table(
        [
            ["Dataset", "Source and generation", "Size", "What it tests"],
            ["D1 Noisy", "Generated by the toy service with unrelated warnings, 0 to 2 red-herring transient errors, irregular fault timing and one shared error name for database faults.", "40 logs: 10 per fault class, 30 requests each", "Whether a system can separate faults by failure pattern when error names do not reveal them."],
            ["D2 Clean", "Generated by the toy service as described in the proposal, with a distinct error name per fault and regular fault timing.", "40 logs: 10 per fault class, 30 requests each", "The proposal's original task and conditions."],
            ["D4 RE3", "A public benchmark: RCAEval RE3, faults injected into three deployed microservice systems. The task is different: name the service that caused the failure, using logs and the incident start time.", "30 of 90 cases: 10 per system, chosen by a fixed spacing rule", "Real logs from deployed systems, with about 85,000 lines in a typical case and no fixed answer pattern."],
            ["D3 Proposal", "The stakeholder's own published logs and labels, from the proposal repository. Field order and level markers were normalized; no values changed.", "16 logs: 4 per fault class, 30 requests each", "Performance on stakeholder-provided data, and the closest comparison with the proposal's Claude Opus results."],
        ],
        [1000, 4200, 1860, 2300],
    )
)
B.append(
    p(
        "Ground truth is recorded when each log is generated. Generation is deterministic given a fault type and run number, and no train/test split applies "
        "because no component is trained. D1 and D2 come from our rebuilt generator, D3 comes from the proposal's generator, and D4 is an external public benchmark."
    )
)
B.append(h2("Baselines and evaluation conditions"))
B.append(bullet("**Baseline A, raw-log LLM:** the same LLM and system prompt, given the unprocessed log. It tests the value of preprocessing at equal LLM calls."))
B.append(bullet("**Baseline B, rules only:** hand-written string and pattern rules with no LLM. It tests whether an LLM is needed at all. Written with knowledge of the generator, it serves as an upper-bound control on D1 to D3 and as an independent reference on D4."))
B.append(
    p(
        "All compared systems received the same logs. LLM runs used temperature 0, a 2,048-token output limit, one call per diagnosis, no tools, and prompt-based JSON "
        "with one shared parser for every provider. Reasoning effort was not separately controlled on Groq. Rate-limited calls were retried, and calls that still failed are "
        "excluded and reported."
    )
)
B.append(h2("Metrics"))
B.append(
    p(
        "**Accuracy** counts the fault the model named. **Strict** also requires valid JSON. **Proposal criterion** requires the correct fault and no cited evidence missing "
        "from the log, matching the proposal's definition of success. **Grounded** also requires citing the real cause: an injected request or failure spacing, or for "
        "`slow_query` a measured latency. **False alarm** is the share of `no_fault` logs called a fault. Operational measures are mean tokens and mean model latency. "
        "Evidence checks use deterministic text rules developed on D1, and their further validation is planned in Section 6."
    )
)
B.append(h2("Results"))
B.append(p("**Table 1.** Measured results. Bold rows are the proposed system. Proposal is the proposal criterion. Sec. is mean model latency in seconds.", keep=True))
B.append(results_table())
B.append(p("**Table 2.** Paired comparison of the pipeline and Baseline A on identical logs.", keep=True))
B.append(paired_table())
B.append(p("**Table 3.** D4 pilot on RCAEval RE3: naming the root-cause service across 30 cases. Bold rows are the proposed system.", keep=True))
B.append(rcaeval_table())
for paragraph in TEXT["analysis"]:
    B.append(p(paragraph))

B.append(h1("Section 4  Demo or Case Study (15 points)"))
B.append(
    p(
        "**Task and expected outcome.** An actual run on `pool_exhausted_run1_noisy`, a 30-request log from D1, using Qwen3.8 27B with recovery scoring on. "
        "The injected fault is a connection-pool burst on requests 12 to 18. Requests 9 and 11 are red-herring `TimeoutError` transients, and the log contains unrelated warnings. "
        "Expected output: `pool_exhausted`, evidence from the log, and a fix that restores the service."
    )
)
B.append(
    p(
        "**Intermediate actions.** The Log Correlator read the log and produced the summary in Figure 3: 21 successes, 9 errors, failing ids 9 and 11 to 18, longest contiguous run 8, "
        "mean latency 48.1 ms, with 6 unrelated warnings filtered out. That summary was the only input to the Hypothesis Agent, which is the coordination step in Figure 1. "
        "The agent's label then selected the runbook entry."
    )
)
B.append(figure("demo_log_and_baseline_trace.jpg", 4.7, "The raw log shown to Baseline A, followed by its agent trace."))
B.append(figure("demo_pipeline_trace.jpg", 4.7, "Pipeline trace: the deterministic summary and the steps that followed it."))
B.append(figure("demo_pipeline_verdict.jpg", 4.7, "Final pipeline output with confidence, alternatives, recovery replay and evidence check."))
B.append(
    p(
        "**Final output.** The pipeline returned `pool_exhausted` at 0.95 confidence and chose `raise_pool_limit` on `connection-pool`. The toy service was replayed with that fix and was healthy "
        "afterward, and 4 of 4 checkable claims appeared in the log. It used 1,131 input and 249 output tokens. Baseline A also answered correctly on this log, using 2,439 input and 283 output tokens. "
        "The task was fully satisfied, and the end-to-end trace is detailed enough to support the refinement discussed in Section 5."
    )
)

B.append(h1("Section 5  Failure Analysis and Current Limitations (10 points)"))
B.append(
    p(
        "**Case 1: false alarm from a red herring (reasoning).** On `no_fault_run3_noisy`, gpt-oss-20b with the pipeline predicted `dependency_down` at 0.90 confidence. "
        "*Observed:* the log's only error is one `TimeoutError` on request 2, and the explanation cites it as a dependency timeout, although the prompt says isolated transients are not a fault. "
        "*Suspected cause:* the summary reports errors without separating isolated ones, and the model treats any error as a signal. "
        "*Proposed fix:* add an isolated-error count to the summary, or a minimum-evidence rule. *Check:* the false-alarm rate on `no_fault` logs in a fresh sweep."
    )
)
B.append(
    p(
        "**Case 2: right diagnosis, imprecise boundary (parsing).** In Section 4 the pipeline reached the right answer and described the burst as requests 11 to 18, with 9 errors and a run of 8. "
        "*Observed:* the injected burst is 12 to 18, and request 11 is a red herring next to it. Baseline A, reading the raw log, correctly identified 12 to 18. "
        "*Diagnosis:* the correlator measures spacing across all error types, so a transient error beside the burst extends it. "
        "*Proposed fix:* compute spacing per error type. *Check:* a test that the reported run matches injected ids on D1."
    )
)
B.append(
    p(
        "**Case 3: unusable output (output handling).** Four of 116 raw-log answers on D1 could not be used, against 0 of 116 pipeline answers. Two gpt-oss-120b answers contained "
        "`\"confidence\": 0. nine`, which is invalid JSON after a correct fault label. Two gpt-oss-20b answers used all 2,048 output tokens and returned no visible answer. "
        "The first is observed model behavior. The second suggests reasoning consumed the token budget on the longer input, but that is not confirmed. "
        "*Proposed fix:* provider-level structured output as a separately reported condition, and a larger token limit where rate limits allow. *Check:* the malformed and empty-answer counts."
    )
)
B.append(
    p(
        "**Case 4: false alarms on stakeholder logs (prompt).** On D3, gpt-oss-20b labeled 3 of 4 `no_fault` logs as `slow_query` in both conditions. "
        "*Observed:* on `no_fault_run2` the explanation calls a mean of 48.8 ms elevated against the ~45 ms baseline in the prompt, while slow-query logs in D3 average about 600 ms. "
        "*Suspected cause:* the prompt's single baseline figure, taken from our generator, anchors the model. "
        "*Proposed fix:* give a healthy range or let the model compare against the run's own distribution. *Check:* the false-alarm rate on D3."
    )
)
B.append(
    p(
        "**Scope and next steps.** D1 to D3 provide clean ground truth for controlled comparison across four known classes, and D4 extends the evaluation to deployed systems with logs as the evidence source. "
        "Because the parser was developed alongside D1 to D3, D4 serves as the independent test, and its summary method was not changed after results were seen. "
        "Larger evaluations and a live Claude run are planned. A real-log mode with automatic redaction is available on branch `final-work`, and Section 6 prioritizes its evaluation alongside the full benchmark."
    )
)

B.append(h1("Section 6  Remaining Work Before the Final Submission (10 points)"))
B.append(
    table(
        [
            ["Priority", "Current gap", "Planned improvement", "Owner", "Done when"],
            ["Required", "Confirm the D4 result at larger scale", "Freeze the scorers, then run more logs per model", "[[names]]", "Paired tests reported on fresh logs"],
            ["Required", "Stress-test D1 to D3 under harder conditions", "Run 75 and 150-request logs, remove the spread-versus-cluster hint, and add a parser without spacing fields", "[[names]]", "Results show where structure helps most"],
            ["Required", "Public benchmark covers a 30-case pilot", "Run all 90 RE3 cases on more models", "[[names]]", "Full RE3 results reported"],
            ["Required", "Tool-using agent has only a 16-log pilot", "Evaluate it across all datasets, and decide with the stakeholder whether it becomes the main system", "[[names]]", "Agent results reported on every dataset"],
            ["Required", "Refinements identified in Section 5", "Apply and verify the fixes", "[[names]]", "Failure cases rechecked"],
            ["Required", "Stakeholder sign-off, final report, presentation and demo", "Follow up on the deliverables in Section 1 and prepare the final materials", "[[names]]", "Submitted and rehearsed"],
            ["Optional", "Real-log use", "Log upload, flexible parsing, secret redaction and an unknown class", "[[names]]", "An uploaded log returns a cited diagnosis"],
            ["Optional", "Coverage and packaging", "Live Claude run, compound faults, recovery sweep, Docker and CI", "[[names]]", "Each runs and is reported"],
        ],
        [950, 2250, 3010, 800, 2350],
    )
)

B.append(h1("Section 7  GitHub Repository and Team Contributions"))
B.append(
    p(
        f"**Repository:** https://github.com/EroNinja/agent-av-planner  **Branch:** `main`  **Commit:** {COMMIT}. "
        "The D4 adapter and results were added after that commit, at `4879977` on branch `final-work`. Setup and run instructions are in `README.md`. [[Confirm the grader has access before submitting.]]"
    )
)
B.append(
    table(
        [["Name", "GitHub username", "Actual contributions", "Commits or pull requests"]]
        + [[f"[[Student {i}]]", "[[username]]", "[[summary, including work not reflected in commits]]", "[[links]]"] for i in range(1, 6)],
        [1900, 1600, 3960, 1900],
    )
)
B.append(p("[[Describe dataset preparation, evaluation, stakeholder communication and writing. If the course requires disclosure of AI assistance, state it here.]]"))

# ---- assemble ------------------------------------------------------------

zin = zipfile.ZipFile(TEMPLATE)
document = zin.read("word/document.xml").decode("utf-8")
head = document[: document.index("<w:body>") + len("<w:body>")]
sect = re.search(r"<w:sectPr.*?</w:sectPr>", document, re.S).group(0)
body = "".join(B)
rels = zin.read("word/_rels/document.xml.rels").decode("utf-8")
types = zin.read("[Content_Types].xml").decode("utf-8")

links = "".join(
    f'<Relationship Id="rIdFig{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{name}"/>'
    for i, (name, _) in enumerate(images, 1)
)
rels = rels.replace("</Relationships>", links + "</Relationships>")
for extension, mime in (("jpg", "image/jpeg"), ("png", "image/png")):
    if f'Extension="{extension}"' not in types:
        types = types.replace("<Override", f'<Default Extension="{extension}" ContentType="{mime}"/><Override', 1)

with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.namelist():
        if item.endswith("/"):
            continue
        if item == "word/document.xml":
            zout.writestr(item, head + body + sect + "</w:body></w:document>")
        elif item == "word/_rels/document.xml.rels":
            zout.writestr(item, rels)
        elif item == "[Content_Types].xml":
            zout.writestr(item, types)
        else:
            zout.writestr(item, zin.read(item))
    for name, path in images:
        zout.write(path, f"word/media/{name}")

print(OUTPUT)
