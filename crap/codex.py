from __future__ import annotations

import json
import time
from pathlib import Path

from .util import run_command, write_json

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"}, "research_complete": {"type": "boolean"},
        "candidate_ready": {"type": "boolean"}, "blocked": {"type": "boolean"},
        "next_action": {"type": "string"},
    },
    "required": ["summary", "research_complete", "candidate_ready", "blocked", "next_action"],
}


def command(cfg: dict, session: str | None, schema: Path, output: Path) -> list[str]:
    cmd = list(cfg["codex"]["command"])
    cmd += ["--no-daemon", "--search", "-a", "never", "-s", "workspace-write",
            "-c", "model_reasoning_effort=" + json.dumps(cfg["codex"]["reasoning_effort"])]
    if cfg["codex"]["model"]:
        cmd += ["-m", cfg["codex"]["model"]]
    cmd += ["exec"]
    if session:
        cmd += ["resume", session]
    cmd += ["--json", "--output-schema", str(schema), "-o", str(output), "-"]
    return cmd


class Codex:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store

    def run(self, problem: str, work: Path, phase: str, timeout: float, control=None):
        state = self.store.problem(problem)
        attempts_root = Path(self.cfg["state_dir"]) / "attempts" / problem
        attempts_root.mkdir(parents=True, exist_ok=True)
        attempt = self.store.execute("INSERT INTO attempts(problem,started,status,directory) VALUES(?,?,'running','')",
                                     (problem, time.time())).lastrowid
        directory = attempts_root / str(attempt)
        directory.mkdir()
        self.store.execute("UPDATE attempts SET directory=? WHERE id=?", (str(directory), attempt))
        write_json(directory / "settings.json", {"codex": self.cfg["codex"], "budget": self.cfg["budget"],
                                                 "benchmark_commit": self.cfg["benchmark"]["commit"]})
        write_json(directory / "schema.json", SCHEMA)
        statuses = self.store.rows("SELECT id,status,detail FROM nodes WHERE problem=?", (problem,))
        jobs = self.store.rows("SELECT id,node,status,remote_id FROM jobs WHERE problem=?", (problem,))
        prompt = (Path(__file__).parent / "prompts" / "turn.txt").read_text(encoding="utf-8")
        context = {"problem": problem, "phase": phase, "deadline_utc_epoch": state["deadline"],
                   "seconds_this_turn": timeout, "previous_feedback": state["detail"], "nodes": statuses,
                   "aristotle_enabled": self.cfg["aristotle"]["enabled"], "aristotle_jobs": jobs}
        prompt += "\nSUPERVISOR CONTEXT\n" + json.dumps(context, ensure_ascii=False, indent=2)
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        logfile = directory / "events.jsonl"
        position, partial, session, usage = 0, "", state["session"], {}

        def ingest(check_control=True):
            nonlocal position, partial, session, usage
            if control and check_control:
                control()
            if not logfile.exists():
                return
            with logfile.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(position)
                data = partial + stream.read()
                position = stream.tell()
            lines = data.split("\n")
            partial = lines.pop()
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    session = event["thread_id"]
                    self.store.update(problem, session=session)
                    self.store.execute("UPDATE attempts SET session=? WHERE id=?", (session, attempt))
                if event.get("type") == "turn.completed":
                    usage = event.get("usage", {})
                    self.store.execute("UPDATE attempts SET usage=? WHERE id=?", (json.dumps(usage), attempt))

        try:
            rc = run_command(command(self.cfg, session, directory / "schema.json", directory / "answer.json"),
                             work, logfile, timeout, stdin=prompt, tick=ingest)
            ingest(False)
            answer = None
            if rc == 0 and (directory / "answer.json").exists():
                answer = json.loads((directory / "answer.json").read_text(encoding="utf-8"))
                if (not isinstance(answer, dict) or set(answer) != set(SCHEMA["required"]) or
                        any(not isinstance(answer[k], bool) for k in ("research_complete", "candidate_ready", "blocked")) or
                        any(not isinstance(answer[k], str) for k in ("summary", "next_action"))):
                    raise ValueError("Codex result does not match the requested schema")
            status = "complete" if rc == 0 and answer is not None else ("timeout" if rc == 124 else "error")
            self.store.execute("UPDATE attempts SET status=?,finished=?,usage=?,detail=? WHERE id=?",
                               (status, time.time(), json.dumps(usage), json.dumps(answer), attempt))
            return {"status": status, "answer": answer, "directory": str(directory), "exit_code": rc}
        except BaseException as exc:
            ingest(False)
            self.store.execute("UPDATE attempts SET status='interrupted',finished=?,detail=? WHERE id=?", (time.time(), str(exc), attempt))
            raise
