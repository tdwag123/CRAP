from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import dag
from .aristotle import Aristotle
from .benchmark import Benchmark, checkpoint, setup_lake
from .codex import Codex
from .store import Store
from .util import digest, safe_file, source_digest, source_files, write_json
from .verify import Verifier

TERMINAL = {"solved", "expired", "paused", "needs_attention"}


class StopRequested(Exception):
    pass


def phase_for(state: dict, cfg: dict, now: float) -> str:
    if now >= state["deadline"] - cfg["budget"]["integration_hours"] * 3600:
        return "integrate"
    if state["phase"] == "research" and now < state["started"] + cfg["budget"]["search_hours"] * 3600:
        return "research"
    return "prove"


class Supervisor:
    def __init__(self, cfg: dict, store: Store, *, worker=None, verifier=None, aristotle=None):
        self.cfg, self.store = cfg, store
        self.root = Path(cfg["state_dir"])
        self.worker = worker or Codex(cfg, store)
        self.verifier = verifier or Verifier(cfg)
        self.aristotle = aristotle or Aristotle(cfg, store)

    def work(self, problem):
        return self.root / "problems" / problem / "work"

    def control(self, problem):
        if (self.root / "control" / "stop").exists() or (self.root / "control" / ("pause-" + problem)).exists():
            raise StopRequested(problem)

    def prepare(self, problem):
        work = self.work(problem)
        marker = work.parent / "prepared.json"
        metadata = json.loads(self.store.problem(problem)["metadata"])
        if metadata["benchmark_commit"] != self.cfg["benchmark"]["commit"]:
            raise ValueError("Problem pin differs from configured benchmark; use its original config")
        Benchmark(self.cfg).restore_trusted(problem, work)
        if not marker.exists():
            setup_lake(work, work.parent / "setup", control=lambda: self.control(problem), cache_helper=Benchmark(self.cfg).cache_helper())
            write_json(marker, {"benchmark_commit": metadata["benchmark_commit"], "prepared_at": time.time()})

    def handoff(self, problem):
        work = self.work(problem)
        state = self.store.problem(problem)
        root = work.parent / "handoff"
        root.mkdir(exist_ok=True)
        data = {"problem": state, "nodes": self.store.rows("SELECT * FROM nodes WHERE problem=?", (problem,)),
                "jobs": self.store.rows("SELECT * FROM jobs WHERE problem=?", (problem,)),
                "attempts": self.store.rows("SELECT * FROM attempts WHERE problem=?", (problem,))}
        write_json(root / "report.json", data)
        graph = safe_file(work, "research/dag.json")
        if graph.exists():
            shutil.copyfile(graph, root / "dag.json")
        progress = safe_file(work, "research/progress.md")
        notes = progress.read_text(encoding="utf-8") if progress.exists() else "No research notes available."
        (root / "HANDOFF.md").write_text(
            f"# {problem}\n\nStatus: {state['status']}\n\n{state['detail']}\n\n"
            "Source remains in ../work; immutable snapshots are in the checkpoint history.\n"
            "Remote jobs listed as pending or unknown may still be running. Expiry stops\n"
            "local work; this MCP contract has no remote cancellation tool.\n\n" + notes, encoding="utf-8")
        sources = source_files(work)
        source_root = root / "source"
        if source_root.exists():
            for previous in source_root.rglob("*.lean"):
                relative = previous.relative_to(source_root).as_posix()
                if relative not in sources:
                    safe_file(source_root, relative).unlink()
        for path, content in sources.items():
            dest = root / "source" / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        return root

    def expire(self, problem):
        self.store.update(problem, status="expired", detail="48-hour research budget exhausted (or configured problem budget).")
        self.store.event(problem, "expired", {})
        self.handoff(problem)

    def snapshot(self, problem, label):
        work = self.work(problem)
        sha = checkpoint(work, label)
        self.store.event(problem, "checkpoint", {"commit": sha, "label": label})
        self.handoff(problem)

    def step(self, problem: str):
        self.verifier.control = lambda: self.control(problem)
        state = self.store.problem(problem)
        if state["status"] in TERMINAL:
            return state["status"]
        self.control(problem)
        state = self.store.start(problem, self.cfg["budget"]["problem_hours"])
        if time.time() >= state["deadline"]:
            self.expire(problem)
            return "expired"
        work = self.work(problem)
        phase = phase_for(state, self.cfg, time.time())
        self.store.update(problem, phase=phase)
        self.aristotle.poll(problem, work, state["deadline"])
        timeout = min(self.cfg["budget"]["turn_minutes"] * 60, state["deadline"] - time.time())
        if phase == "research":
            timeout = min(timeout, state["started"] + self.cfg["budget"]["search_hours"] * 3600 - time.time())
        elif phase == "prove":
            timeout = min(timeout, state["deadline"] - self.cfg["budget"]["integration_hours"] * 3600 - time.time())
        if timeout <= 0:
            if time.time() >= state["deadline"]:
                self.expire(problem)
                return "expired"
            return "running"
        result = self.worker.run(problem, work, phase, timeout, control=lambda: self.control(problem))
        restored = Benchmark(self.cfg).restore_trusted(problem, work)
        if restored:
            self.store.event(problem, "trusted_files_restored", restored)
        self.snapshot(problem, f"{phase} turn: {result['status']}")
        if time.time() >= state["deadline"]:
            self.expire(problem)
            return "expired"
        answer = result.get("answer") or {}
        if result["status"] == "error":
            raise RuntimeError(f"Codex failed; see {result['directory']}")
        # Even a timed-out turn may have saved useful DAG/source changes.
        graph = dag.sync(self.store, problem, work)
        if answer.get("research_complete") and phase == "research":
            if graph["nodes"] and all(safe_file(work, "research/" + p).exists() for p in ("proof.md", "search.md")):
                self.store.update(problem, phase="prove")
        feedback = answer.get("summary", "Turn timed out; continue from saved files.")
        if restored:
            feedback += " Trusted file edits were restored: " + ", ".join(restored) + ". Edit only solver-owned files."
        self.store.update(problem, detail=feedback)
        rows = {r["id"]: r for r in self.store.rows("SELECT * FROM nodes WHERE problem=?", (problem,))}
        requested = [n for n in graph["nodes"] if n.get("check") and rows[n["id"]]["status"] not in ("checked", "conditional", "failed")
                     and all(n.get(k) for k in ("lean_file", "declaration", "lean_type"))]
        if requested and time.time() < state["deadline"]:
            checked = self.verifier.nodes(problem, work, state["deadline"], requested)
            self.store.event(problem, "node_verification", checked)
            if checked["status"] == "infrastructure_error":
                raise RuntimeError(f"Verifier infrastructure error: {checked.get('detail')}; {checked['log_dir']}")
            for key, verdict in checked["nodes"].items():
                dag.set_status(self.store, problem, key, verdict["status"], verdict["detail"] + " | " + checked["log_dir"])
            dag.publish(self.store, problem, work)
        phase = phase_for(self.store.problem(problem), self.cfg, time.time())
        if phase != "integrate" and time.time() < state["deadline"]:
            self.aristotle.submit_ready(problem, work, state["deadline"] - self.cfg["budget"]["integration_hours"] * 3600)
        candidate = source_digest(work)
        if answer.get("candidate_ready") and candidate != state["last_candidate"] and time.time() < state["deadline"]:
            verdict = self.verifier.final(problem, work, state["deadline"])
            self.store.event(problem, "final_verification", verdict)
            if verdict["status"] == "infrastructure_error":
                raise RuntimeError(f"Verifier infrastructure error: {verdict.get('detail')}; {verdict['log_dir']}")
            if verdict["status"] != "timeout":
                self.store.update(problem, last_candidate=candidate)
            self.store.update(problem, detail=json.dumps(verdict))
            if verdict["status"] == "accepted" and time.time() <= state["deadline"]:
                self.store.update(problem, status="solved", errors=0)
                self.snapshot(problem, "Official comparator and independent kernel accepted")
                return "solved"
        self.store.update(problem, errors=0)
        self.handoff(problem)
        if time.time() >= state["deadline"]:
            self.expire(problem)
            return "expired"
        return "waiting" if answer.get("blocked") else "running"

    def run(self, problem: str | None = None, once=False):
        self.store.recover()
        while True:
            if problem:
                candidates = [self.store.problem(problem)]
            else:
                candidates = self.store.rows("SELECT * FROM problems WHERE status IN ('queued','running') ORDER BY rowid")
            candidates = [p for p in candidates if p["status"] not in TERMINAL]
            if not candidates or (self.root / "control" / "stop").exists():
                return
            current = candidates[0]["id"]
            try:
                # Expired campaigns never spend time on preparation or remote work.
                state = self.store.problem(current)
                if state["deadline"] is not None and time.time() >= state["deadline"]:
                    self.expire(current)
                    if once:
                        return
                    continue
                self.control(current)
                self.prepare(current)
                print(f"[{current}] {self.store.problem(current)['phase']}", flush=True)
                outcome = self.step(current)
                print(f"[{current}] {outcome}", flush=True)
            except StopRequested:
                if (self.root / "control" / ("pause-" + current)).exists():
                    self.store.update(current, status="paused", detail="Paused by user; original deadline is retained.")
                self.snapshot(current, "Stopped with resumable progress")
                return
            except KeyboardInterrupt:
                self.snapshot(current, "Interrupted with resumable progress")
                raise
            except Exception as exc:
                state = self.store.problem(current)
                errors = state["errors"] + 1
                self.store.update(current, errors=errors, detail=str(exc))
                self.store.event(current, "error", {"error": str(exc)})
                print(f"[{current}] error: {exc}", flush=True)
                if errors >= self.cfg["budget"]["max_consecutive_errors"]:
                    self.store.update(current, status="needs_attention")
                    self.handoff(current)
                outcome = "waiting"
            if once:
                return
            if outcome == "waiting":
                end = time.time() + self.cfg["budget"]["retry_seconds"]
                deadline = self.store.problem(current)["deadline"]
                if deadline:
                    end = min(end, deadline)
                while time.time() < end:
                    if (self.root / "control" / "stop").exists() or (self.root / "control" / ("pause-" + current)).exists():
                        break
                    time.sleep(max(0, min(1, end - time.time())))
