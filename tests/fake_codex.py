"""Subprocess fixture implementing the small Codex surface used by our runner."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from tests.support import graph
from crap.util import write_json

work = pathlib.Path.cwd()
prompt = sys.stdin.read()
output = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "simulated-session-1"}), flush=True)
first = not (work / "research" / "proof.md").exists()
if first:
    graph(work)
    (work / "research" / "proof.md").write_text("Synthetic proof sketch for orchestration only.\n")
    (work / "research" / "search.md").write_text("Synthetic search record; no actual literature search.\n")
else:
    results = list((work / "research" / "aristotle").glob("*/candidate.lean"))
    if results:
        (work / "Submission.lean").write_text(results[0].read_text(), encoding="utf-8")
        data = json.loads((work / "research" / "dag.json").read_text())
        data["nodes"][1].update(check=True, delegate=False, owner="codex")
        write_json(work / "research" / "dag.json", data)
(work / "research" / "progress.md").write_text("SIMULATED workflow. No real mathematical verification occurred.\n")
ready = "sorry" not in (work / "Submission.lean").read_text()
write_json(output, {"summary": "SIMULATED research turn", "research_complete": True,
                    "candidate_ready": ready, "blocked": not first and not ready, "next_action": "Integrate remote candidate"})
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 10}}), flush=True)
