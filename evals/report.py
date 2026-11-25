"""Turn run summaries into a Markdown table and a self-contained HTML page with bar charts."""
import json
from html import escape
from pathlib import Path

COLORS = {"full_history": "#d9822b", "summary_only": "#8a63d2", "memory": "#2a9d6f"}
NOTES = [
    "Scoring is deterministic string matching (no LLM judge), so results are reproducible.",
    "'Stale' = the answer states an outdated value as current (for example the old budget).",
    "Latency is end to end: for `memory` it includes embedding the question and the database search.",
    "Setup tokens are the one-off cost of building summaries or memories; answer tokens are per question.",
    "Small dataset: results show direction, not proof. Add scenarios in evals/fixtures.py.",
]


def _fmt(value, suffix=""):
    return "-" if value is None else f"{value}{suffix}"


def markdown(summaries: dict[str, dict]) -> str:
    head = "| Strategy | Accuracy | Stale answers | Avg prompt tokens | p50 latency | p95 latency | Answer cost (USD) | Setup tokens | Errors |"
    lines = ["# ClinicMind memory evals", "", head, "|" + "---|" * 9]
    for name, s in summaries.items():
        lines.append(
            f"| {name} | {s['accuracy']:.0%} ({s['correct']}/{s['questions']}) | {s['stale_rate']:.0%} | "
            f"{_fmt(s['avg_prompt_tokens'])} | {_fmt(s['p50_latency_ms'], ' ms')} | {_fmt(s['p95_latency_ms'], ' ms')} | "
            f"{s['answer_cost_usd']:.4f} | {s['setup_prompt_tokens'] + s['setup_completion_tokens']} | {s['errors']} |"
        )
    tags = sorted({t for s in summaries.values() for t in s["accuracy_by_tag"]})
    if tags:
        lines += ["", "## Accuracy by question type", "", "| Strategy | " + " | ".join(tags) + " |", "|" + "---|" * (len(tags) + 1)]
        for name, s in summaries.items():
            lines.append(f"| {name} | " + " | ".join(f"{s['accuracy_by_tag'][t]:.0%}" if t in s["accuracy_by_tag"] else "-" for t in tags) + " |")
    return "\n".join(lines + ["", "## Notes", ""] + [f"- {n}" for n in NOTES]) + "\n"


def _bars(title: str, values: dict[str, float | None], unit: str, scale_max: float | None = None) -> str:
    shown = {k: v for k, v in values.items() if v is not None}
    top = scale_max or max(shown.values(), default=1) or 1
    rows, y = [], 34
    for name, value in values.items():
        width = 0 if value is None else max(2, int(360 * value / top))
        label = "-" if value is None else f"{value:g}{unit}"
        rows.append(
            f'<text x="0" y="{y + 15}" class="lbl">{escape(name)}</text>'
            f'<rect x="120" y="{y}" width="{width}" height="22" rx="3" fill="{COLORS.get(name, "#4a7fd6")}"/>'
            f'<text x="{128 + width}" y="{y + 16}" class="val">{escape(label)}</text>'
        )
        y += 32
    return f'<h3>{escape(title)}</h3><svg viewBox="0 0 620 {y}" role="img" aria-label="{escape(title)}">{"".join(rows)}</svg>'


def html(summaries: dict[str, dict]) -> str:
    charts = "".join([
        _bars("Accuracy (higher is better)", {k: round(s["accuracy"] * 100, 1) for k, s in summaries.items()}, "%", 100),
        _bars("Average prompt tokens per question (lower is cheaper)", {k: s["avg_prompt_tokens"] for k, s in summaries.items()}, ""),
        _bars("p95 latency (lower is faster)", {k: s["p95_latency_ms"] for k, s in summaries.items()}, " ms"),
        _bars("Stale answers (lower is better)", {k: round(s["stale_rate"] * 100, 1) for k, s in summaries.items()}, "%", 100),
    ])
    table = "<pre>" + escape(markdown(summaries)) + "</pre>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>ClinicMind evals</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#fff;--fg:#1d2330;--muted:#5d6678}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151c;--fg:#e6e9f0;--muted:#99a1b3}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem}}
svg{{width:100%;height:auto}} .lbl,.val{{font:13px system-ui,sans-serif;fill:var(--fg)}} .val{{fill:var(--muted)}}
pre{{white-space:pre-wrap;color:var(--muted);font-size:12px}}
</style></head><body><h1>ClinicMind memory evals</h1>{charts}<h3>Details</h3>{table}</body></html>"""


def write_report(out_dir: Path, summaries: dict[str, dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(markdown(summaries), encoding="utf-8")
    (out_dir / "report.html").write_text(html(summaries), encoding="utf-8")
    (out_dir / "results.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
