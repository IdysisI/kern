# Balanced A/B eval — 2026-09-10 (cfb722f vs 1ae7601+proc-fix)

2 repetitions, order alternated (r1: base->cur, r2: cur->base), sequential,
same 6 tasks (giant_output with per-run unpredictable secret), session ids recorded.

| model | rep | version | pass | provider incidents |
|---|---|---|---|---|
| deepseek-v4-flash | r1 | baseline | 6/6 | 0 |
| deepseek-v4-flash | r1 | current | 6/6 | 0 |
| deepseek-v4-flash | r2 | baseline | 6/6 | 0 |
| deepseek-v4-flash | r2 | current | 6/6 | 0 |
| glm-5.3 | r1 | baseline | 6/6 | 0 |
| glm-5.3 | r1 | current | 6/6 | 0 |
| glm-5.3 | r2 | baseline | 6/6 | 0 |
| glm-5.3 | r2 | current | 6/6 | 0 |
| MiniMax-M3 | r1 | both | 0/6 | 12 (upstream down: HTTP 504 "upstream stalled", confirmed externally) |

Interpretable result: 48/48 pass on both models, both versions, both reps —
no regression from the streaming/compaction/atomicity fixes; no measurable
difference either (tasks saturate for these models — harder tasks needed to
discriminate). MiniMax deferred until upstream returns.

Limit: 2 reps x 2 models is a bounded signal; provider incidents fully
account for all non-passes.
