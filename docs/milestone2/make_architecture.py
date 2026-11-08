"""Draw the Milestone 2 architecture figure."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Patch

OUT = Path(__file__).parent / "figures" / "architecture.png"

STYLES = {
    "core": ("#E6F1FB", "#185FA5", "solid"),
    "llm": ("#FAECE7", "#993C1D", "solid"),
    "new": ("#E1F5EE", "#0F6E56", "solid"),
    "planned": ("#FFFFFF", "#888780", (0, (4, 3))),
}

fig, ax = plt.subplots(figsize=(6.9, 5.5), dpi=220)
ax.set_xlim(0, 100)
ax.set_ylim(0, 80)
ax.axis("off")


def box(x, y, w, h, text, kind="core", size=6.8):
    fill, edge, line = STYLES[kind]
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0,rounding_size=1", facecolor=fill, edgecolor=edge,
            linewidth=0.9, linestyle=line,
        )
    )
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size, color="#222222", linespacing=1.15)


def arrow(a, b):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", mutation_scale=7, linewidth=0.8, color="#555555", shrinkA=0, shrinkB=0))


def lane_label(y, text):
    ax.text(0, y, text, fontsize=6.6, color="#444444", va="center", ha="left", fontweight="bold")


# Input
box(16, 71, 36, 8, "Generated logs (clean and noisy)\nground truth hidden in .meta.json")
box(62, 71, 37, 8, "Toy service\ndeterministic fault injection")
arrow((62, 75), (52, 75))
ax.plot([16, 14, 14], [75, 75, 38], color="#555555", linewidth=0.8)
for lane_y in (62, 50, 38):
    arrow((14, lane_y), (16, lane_y))

# Lanes
X = [16, 39, 62, 83]
W = [20, 20, 18, 16]
lanes = [(58, "Proposed\npipeline"), (46, "Baseline A"), (34, "Baseline B")]
for y, label in lanes:
    lane_label(y + 4, label)

box(X[0], 58, W[0], 8, "Log Correlator\nparse, count, spacing")
box(X[1], 58, W[1], 8, "Hypothesis Agent\nthe one LLM call", "llm")
box(X[2], 58, W[2], 8, "Runbook Agent\nfixed lookup")
box(X[3], 58, W[3], 8, "Verdict\nfault, evidence,\nconfidence, fix", size=6.3)
box(X[0], 46, W[0], 8, "Raw log text\nno preprocessing")
box(X[1], 46, W[1], 8, "Hypothesis Agent\nthe one LLM call", "llm")
box(X[2], 46, W[2], 8, "Runbook Agent\nfixed lookup")
box(X[3], 46, W[3], 8, "Verdict")
box(X[0], 34, W[0] + W[1] + 3, 8, "Hand-written rules on the raw log (no LLM)", "new")
box(X[2], 34, W[2], 8, "Runbook Agent\nfixed lookup")
box(X[3], 34, W[3], 8, "Verdict")

for y in (62, 50):
    arrow((X[0] + W[0], y), (X[1], y))
    arrow((X[1] + W[1], y), (X[2], y))
    arrow((X[2] + W[2], y), (X[3], y))
arrow((X[1] + W[1], 38), (X[2], 38))
arrow((X[2] + W[2], 38), (X[3], 38))

# Provider note
ax.text(49, 68.4, "LLM paths: same system prompt, same token limit, one call per diagnosis", fontsize=5.8, color="#993C1D", ha="center", style="italic")

# Scoring
lane_label(23, "Scoring\n(model\nnever sees)")
box(16, 19, 19, 8, "Evidence check\ncited claims vs log", "new")
box(37, 19, 19, 8, "Grounded scoring\ncites the real cause", "new")
box(58, 19, 19, 8, "Recovery replay\nsimulated fix", "new")
box(79, 19, 20, 8, "Failure labels\nrule-based", "new")

# Storage and interface
lane_label(11, "Results")
box(16, 7, 25, 8, "SQLite run store\npaired statistics", "new")
box(43, 7, 27, 8, "FastAPI backend\nlive trace, sweeps, API keys", "new")
box(72, 7, 27, 8, "React dashboard and CLI\nGroq tested, Claude not yet", "new")
arrow((41, 11), (43, 11))
arrow((70, 11), (72, 11))
arrow((91, 58), (91, 54))
arrow((91, 46), (91, 42))
arrow((91, 34), (91, 27))
ax.text(89.5, 30.5, "every verdict", fontsize=5.8, color="#555555", ha="right", va="center", style="italic")
arrow((28, 19), (28, 15))

# Planned
lane_label(2.5, "Planned")
box(10, 0, 16, 5, "Log upload", "planned", 6.2)
box(28, 0, 21, 5, "Tool-using agent loop", "planned", 6.2)
box(51, 0, 22, 5, "Public benchmark data", "planned", 6.2)
box(75, 0, 24, 5, "Compound faults", "planned", 6.2)

ax.legend(
    handles=[
        Patch(facecolor=STYLES["core"][0], edgecolor=STYLES["core"][1], label="In the proposal, implemented"),
        Patch(facecolor=STYLES["llm"][0], edgecolor=STYLES["llm"][1], label="LLM call"),
        Patch(facecolor=STYLES["new"][0], edgecolor=STYLES["new"][1], label="Added beyond the proposal"),
        Patch(facecolor="white", edgecolor=STYLES["planned"][1], linestyle="--", label="Planned, not built"),
    ],
    loc="upper center",
    bbox_to_anchor=(0.5, -0.005),
    ncol=4,
    fontsize=6,
    frameon=False,
    handlelength=1.2,
    columnspacing=1.2,
)

fig.savefig(OUT, bbox_inches="tight", pad_inches=0.05, facecolor="white")
print(OUT)
