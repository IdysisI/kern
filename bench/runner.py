"""kern bench — the scoreboard. Same tasks, same harness, every model.

Answers with data: which proxy models does kern make smart vs dumb?

  .venv/bin/python -m bench.runner [model ...]

Writes bench/RESULTS.md and prints the table.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kern.client import Client
from kern.engine import Engine
from kern.journal import create_session

KERN_HOME = Path.home() / ".kern"

TASKS = [
    {
        "id": "write_and_run",
        "prompt": "Create a file answer.py that prints the number 42, run it with python3, and confirm.",
        "check": "test -f answer.py && python3 answer.py | grep -q 42",
    },
    {
        "id": "edit_existing",
        "seed": {"buggy.py": "total = 0\nfor i in range(1, 101):\n    total += i\nprint(total - 1)  # BUG\n"},
        "prompt": "buggy.py should print the sum 1..100 = 5050 but has an off-by-one. Read it, fix it with edit(), and run it to prove it prints 5050.",
        "check": "python3 buggy.py | grep -q 5050",
    },
    {
        "id": "debug_test",
        "seed": {"calc.py": "def add(a, b):\n    return a - b  # BUG\n",
                 "test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"},
        "prompt": "test_calc.py fails. Find the bug, fix it, and make `python3 -m pytest test_calc.py -q` pass. Tell me the final pytest verdict.",
        "check": "python3 -m pytest test_calc.py -q 2>&1 | grep -Eq '1 passed|passed'",
    },
    {
        "id": "fetch_and_answer",
        "prompt": "Use the fetch tool on https://example.com and tell me the main heading of the page.",
        "check": None,  # judged by reply text
        "reply_contains": "example",
    },
    {
        "id": "plan_and_background",
        "prompt": ("Make a 3-step plan with todo(). Then write serve_check.py that starts an http.server on port 8734 "
                   "in the background (use exec background=true), verifies with curl that it answers, then kills it with proc(). "
                   "Confirm when done."),
        "check": "test -f serve_check.py",
        "min_steps": 4,   # todo + write + exec(background) + proc/curl...
    },
    {
        "id": "giant_output",
        "seed": {"bigdata.txt": "".join(f"line-{i:05d}: {'payload ' * 6}\n" for i in range(20000))},
        "prompt": ("bigdata.txt is large. Find the exact content of line 12345 and write ONLY that line's "
                   "number and text to found.txt, then verify found.txt matches bigdata.txt's line 12345."),
        "check": "test -f found.txt && grep -q 'line-12344' found.txt",
    },
]

DEFAULT_MODELS = ["gemini-3.8-flash-api", "claude-haiku-4-5-20251001",
                  "claude-sonnet-4-6", "gpt-5.4-mini", "deepseek-v4-flash"]

TIMEOUT = 300


async def run_one(client: Client, model: str, task: dict) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix=f"kernbench-{task['id']}-"))
    for name, content in (task.get("seed") or {}).items():
        (workdir / name).write_text(content)
    # unique port per run so parallel jobs never collide
    import random
    prompt = task["prompt"].replace("8734", str(random.randint(20000, 59000)))
    sess = create_session(cwd=str(workdir))
    eng = Engine(client, model, sess, str(workdir), approve=lambda *a: True)
    t0 = time.monotonic()
    err = ""
    reply = ""
    try:
        reply = await asyncio.wait_for(eng.chat(prompt), TIMEOUT)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
    dt = time.monotonic() - t0

    steps = sum(1 for e in sess.events if e["kind"] == "tool_result")
    ok = False
    if not err and steps >= task.get("min_steps", 1):   # no zero-tool "passes"
        if task.get("check"):
            r = subprocess.run(["bash", "-c", task["check"]], cwd=workdir,
                               capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0
        elif task.get("reply_contains"):
            ok = task["reply_contains"].lower() in reply.lower()
        else:
            ok = bool(reply) and "error" not in reply.lower()[:40]
    shutil.rmtree(workdir, ignore_errors=True)
    return {"model": model, "task": task["id"], "ok": ok, "seconds": round(dt, 1),
            "tok_in": eng.usage_in, "tok_out": eng.usage_out, "error": err,
            "steps": steps}


async def main():
    models = sys.argv[1:] or DEFAULT_MODELS
    client = Client()
    results = []
    sem = asyncio.Semaphore(1)

    async def guarded(model, task):
        async with sem:
            print(f"  … {model} × {task['id']}", flush=True)
            r = await run_one(client, model, task)
            print(f"  {'✓' if r['ok'] else '✗'} {model} × {task['id']} "
                  f"({r['seconds']}s, {r['steps']} steps) {r['error']}", flush=True)
            return r

    jobs = [guarded(m, t) for m in models for t in TASKS]
    results = await asyncio.gather(*jobs)

    # aggregate per model
    by_model: dict[str, list] = {}
    for r in results:
        by_model.setdefault(r["model"], []).append(r)

    lines = ["# kern bench — model scoreboard",
             "",
             f"run: {time.strftime('%Y-%m-%d %H:%M')} · {len(TASKS)} tasks · proxy http://127.0.0.1:8790",
             "",
             "| model | pass | avg s | tok in | tok out |",
             "|---|---|---|---|---|"]
    print("\n=== SCOREBOARD ===")
    for model, rs in sorted(by_model.items(), key=lambda kv: -sum(x["ok"] for x in kv[1])):
        passed = sum(x["ok"] for x in rs)
        avg_s = sum(x["seconds"] for x in rs) / len(rs)
        tin = sum(x["tok_in"] for x in rs)
        tout = sum(x["tok_out"] for x in rs)
        lines.append(f"| {model} | {passed}/{len(rs)} | {avg_s:.0f} | {tin:,} | {tout:,} |")
        print(f"{model:32} {passed}/{len(rs)}  avg {avg_s:.0f}s")
    out = "\n".join(lines) + "\n"
    (Path(__file__).parent / "RESULTS.md").write_text(out)
    (Path(__file__).parent / "results.json").write_text(json.dumps(results, indent=1))
    print("\nwrote bench/RESULTS.md")


if __name__ == "__main__":
    asyncio.run(main())
