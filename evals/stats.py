"""Summary statistics for one eval run."""
import math


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (simple and predictable for small samples)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(round(pct * len(ordered), 6) / 100))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(results: list[dict], setup: dict | None = None) -> dict:
    """results: dicts with correct, stale, tags, error, prompt_tokens, completion_tokens, latency_ms, cost_usd."""
    answered = [r for r in results if not r.get("error")]
    n, ok = len(results), sum(1 for r in results if r["correct"])
    by_tag: dict[str, list[bool]] = {}
    for r in results:
        for tag in r.get("tags") or []:
            by_tag.setdefault(tag, []).append(bool(r["correct"]))
    latencies = [r["latency_ms"] for r in answered if r.get("latency_ms") is not None]
    prompts = [r["prompt_tokens"] for r in answered if r.get("prompt_tokens") is not None]
    setup = setup or {}
    answer_cost = sum(float(r.get("cost_usd") or 0) for r in results)
    return {
        "questions": n,
        "correct": ok,
        "accuracy": round(ok / n, 4) if n else 0.0,
        "stale_rate": round(sum(1 for r in results if r["stale"]) / n, 4) if n else 0.0,
        "errors": n - len(answered),
        "avg_prompt_tokens": round(sum(prompts) / len(prompts), 1) if prompts else None,
        "p50_latency_ms": percentile(latencies, 50),
        "p95_latency_ms": percentile(latencies, 95),
        "answer_cost_usd": round(answer_cost, 6),
        "setup_prompt_tokens": setup.get("prompt_tokens", 0),
        "setup_completion_tokens": setup.get("completion_tokens", 0),
        "setup_calls": setup.get("calls", 0),
        "accuracy_by_tag": {t: round(sum(v) / len(v), 4) for t, v in sorted(by_tag.items())},
    }
