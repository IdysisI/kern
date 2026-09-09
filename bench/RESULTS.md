# Kern Model Scoreboard (vsllm-hub @ 127.0.0.1:8790)

Evaluated across 5 real agentic tasks:
1. `write_and_run`: Generate and verify a standalone Python script
2. `edit_existing`: Read code, locate an off-by-one bug, and surgically patch it with `edit()`
3. `debug_test`: Run failing pytest, inspect tracebacks, fix implementation, verify green test suite
4. `fetch_and_answer`: HTTP fetch documentation webpage, parse content, extract key header
5. `plan_and_background`: Multi-step `todo()` plan, start background server with `exec(background=true)`, probe with curl, terminate with `proc()`

*All tasks enforce hard assertions and minimum tool execution steps (no passing via zero-step conversational hallucination).*

| Model | Pass Rate | Avg Time | Prompt Tokens | Completion Tokens | Status / Recommendation |
|---|---|---|---|---|---|
| **deepseek-v4-flash** | **5/5** (100%) | **25s** | ~28,400 | ~1,850 | ⚡ **Fastest & Best Value.** Exceptional tool accuracy, instant streaming. |
| **claude-sonnet-4-6** | **5/5** (100%) | **32s** | ~29,100 | ~1,920 | 🧠 **Top Tier Reasoning.** Flawless diff generation and surgical edits. |
| **gemini-3.8-flash-api** | **4/5** (80%) | **33s** | ~24,800 | ~1,600 | 🚀 **Very Fast.** Great for quick tasks; occasionally over-iterates on multi-process tasks. |
| **claude-haiku-4-5-20251001** | **5/5** (100%) | **105s** | ~31,200 | ~2,100 | 🐢 **Accurate but Slow.** High latency through the local proxy endpoint. |
| **gpt-5.4-mini** | **0/5** (0%) | - | - | - | ❌ **Proxy Stalled.** Stream consistently hangs upstream without emitting chunks (caught by stall watchdog). |
