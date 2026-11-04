## 2026-11-04 (re-run)
- gemini-3.1-flash-lite: 0.8s, 5.4s
- gemini-3.5-flash-lite: 0.6s, 0.7s, 1.0s
- gemini-2.5-flash: 404 (not available to new keys)
Finding: on the free tier, model latency ranking flipped within ~9 days (3.5 was 27-30s on 2026-10-26).
Decision: keep 3.1 as primary, 3.5 as fallback, benchmark again before the final evals.