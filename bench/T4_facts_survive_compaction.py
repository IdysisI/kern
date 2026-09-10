"""T4 — régression du cas réel: gros write() puis compaction puis reprise.
Le relevé mécanique (facts) doit indiquer l'écriture réussie indépendamment du
résumé LLM, et les détails restent consultables (compacted-*.jsonl)."""
import sys, json, os, pathlib, tempfile
sys.path.insert(0, "/home/marty/kern")
from kern.journal import create_session
from kern import pager

tmp = tempfile.mkdtemp()
sess = create_session(cwd=tmp)

# scenario as in 0899d0: assistant announces, writes a 4057-char file, analyzes
content = "x = \"kern\"" + "\n" + "# " * 2000 + "# " * 200
big_call = {"id": "c1", "name": "write",
            "arguments": {"path": "kern/pager.py", "content": content}}
assert len(json.dumps(big_call["arguments"])) >= 4057
sess.emit("user", text="refactor pager.py")
sess.emit("assistant", text="I'll rewrite pager.py", tool_calls=[big_call])
sess.emit("action", call_id="c1", name="write")
sess.emit("tool_result", call_id="c1", name="write",
          text=f"wrote kern/pager.py ({len(content)} bytes)")
sess.emit("assistant", text="pager.py rewritten. next: tests.")

# compact ALL of it (worst case: the summarizer would only see 300 chars of args)
facts = []
calls_by_id = {"c1": {"name": "write", "path": "kern/pager.py", "arglen": len(json.dumps(big_call["arguments"]))}}
for ev in sess.events:
    if ev["kind"] == "tool_result":
        t = str(ev.get("text", ""))
        st = "error" if t.startswith("error") else "denied" if t.startswith("denied") else "ok"
        facts.append(f"{ev.get('name')} kern/pager.py -> {st} (ev n={ev.get('n')})")
facts_text = "\n".join(facts)

# simulate the compaction WITH a BAD summary (as 0899d0 produced: "no code changes yet")
dropped = sess.compact_into(len(sess.events), "primary_intent: refactor\nfiles_touched: (none — no code changes yet)",
                            facts=facts_text)
assert dropped > 0

# reprise: materialize the post-compaction view
view = pager.materialize(sess.events, sess)
vt = " ".join(m.get("text", "") for m in view)
assert "<execution-facts" in vt, "FAIL: facts block missing from view"
assert "write kern/pager.py" in vt and "-> ok" in vt, "FAIL: successful write not in facts"
# the facts CONTRADICT the bad summary — and the contradiction is visible

# details still consultable: archived events exist and carry the full write
arch = sorted(pathlib.Path(sess.dir).glob("compacted-*.jsonl"))
assert arch, "FAIL: no compacted archive"
arch_events = [json.loads(l) for a in arch for l in open(a) if l.strip()]
writes = [e for e in arch_events if e["kind"] == "tool_result" and e.get("call_id") == "c1"]
assert writes and str(writes[0].get("text", "")).startswith("wrote kern/pager.py")
arg_call = [e for e in arch_events if e["kind"] == "assistant" for tc in e.get("tool_calls", []) if tc["id"] == "c1"]
assert arg_call, "FAIL: full write args not archived"
print(f"PASS T4: 4057-char write survives compaction in facts; bad LLM summary overridden; details in {arch[0].name}")
