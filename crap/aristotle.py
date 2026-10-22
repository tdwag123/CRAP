from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .mcp import MCPError, StdioMCP
from .util import clean_env, safe_file, write_json


class Aristotle:
    def __init__(self, cfg: dict, store):
        self.cfg, self.store = cfg, store
        self.options = cfg["aristotle"]
        self.root = Path(cfg["state_dir"])

    def connect(self, deadline=None):
        env = clean_env()
        for name in self.options["env_names"]:
            if name in os.environ:
                env[name] = os.environ[name]
        timeout = self.options["rpc_timeout_seconds"]
        if deadline is not None:
            timeout = min(timeout, max(0.1, (deadline - time.time()) / 3))
        return StdioMCP(self.options["command"], self.root, env, self.root / "aristotle-mcp.log", timeout)

    def probe(self):
        if not self.options["enabled"]:
            return {"enabled": False, "ready": False}
        with self.connect() as client:
            tools = {t["name"]: t for t in client.tools()}
        required = {self.options["submit_tool"]: {"code", "wait"}, self.options["poll_tool"]: {"project_id"}}
        for name, fields in required.items():
            if name not in tools or not fields <= tools[name].get("inputSchema", {}).get("properties", {}).keys():
                raise MCPError(f"MCP tool {name} does not implement the expected contract")
        return {"enabled": True, "ready": True, "tools": list(tools)}

    def save_result(self, job, response: dict, work: Path):
        status = response.get("status")
        remote = response.get("project_id") or job.get("remote_id")
        if status in ("submitted", "in_progress", "queued"):
            if not remote:
                raise MCPError("Async submission returned no project_id")
            state = "pending"
        elif status in ("proved", "partial", "failed", "error", "counterexample"):
            state = "returned" if status in ("proved", "partial") else "failed"
        else:
            raise MCPError(f"Unknown Aristotle status: {status!r}")
        dest = safe_file(work, "research/aristotle/" + job["id"])
        dest.mkdir(parents=True, exist_ok=True)
        write_json(dest / "result.json", response)
        if isinstance(response.get("code"), str):
            (dest / "candidate.lean").write_text(response["code"], encoding="utf-8")
        if state == "returned" and not isinstance(response.get("code"), str):
            state = "failed"
            response["pipeline_error"] = "No proof source in completed result"
        self.store.execute("UPDATE jobs SET status=?,remote_id=?,polled=?,result=? WHERE id=?",
                           (state, remote, time.time(), json.dumps(response, ensure_ascii=False), job["id"]))
        self.store.event(job["problem"], "aristotle_result", {"job": job["id"], "state": state, "remote_id": remote})

    def poll(self, problem: str, work: Path, deadline: float):
        if not self.options["enabled"] or time.time() >= deadline:
            return
        jobs = self.store.rows("SELECT * FROM jobs WHERE problem=? AND status='pending' AND polled<=?",
                               (problem, time.time() - self.options["poll_seconds"]))
        for job in jobs:
            if time.time() >= deadline:
                break
            try:
                with self.connect(deadline) as client:
                    response = client.call(self.options["poll_tool"], {"project_id": job["remote_id"]})
                self.save_result(job, response, work)
            except (MCPError, OSError) as exc:
                self.store.execute("UPDATE jobs SET polled=? WHERE id=?", (time.time(), job["id"]))
                self.store.event(problem, "aristotle_poll_error", {"job": job["id"], "error": str(exc)})

    def submit_ready(self, problem: str, work: Path, deadline: float):
        if not self.options["enabled"] or time.time() >= deadline:
            return
        rows = self.store.rows("SELECT * FROM nodes WHERE problem=?", (problem,))
        states = {r["id"]: r["status"] for r in rows}
        for row in rows:
            node = json.loads(row["payload"])
            if not node.get("delegate") or node.get("owner") != "aristotle" or row["status"] == "checked":
                continue
            if not all(states.get(dep) == "checked" for dep in node.get("depends_on", [])):
                continue
            if not all(node.get(k) for k in ("lean_file", "lean_type", "declaration")):
                continue
            jobs = self.store.rows("SELECT * FROM jobs WHERE problem=?", (problem,))
            if len(jobs) >= self.options["max_jobs_per_problem"]:
                return
            if sum(j["status"] in ("submitting", "pending", "unknown") for j in jobs) >= self.options["max_pending"]:
                return
            own = [j for j in jobs if j["node"] == row["id"]]
            if (len(own) >= self.options["max_attempts_per_node"] or
                    any(j["status"] in ("submitting", "pending", "unknown") or j["fingerprint"] == row["fingerprint"] for j in own)):
                continue
            if time.time() >= deadline:
                return
            # Self-contained node file plus all local helpers; Mathlib is provided by Aristotle.
            # The worker is instructed to package local imports and explain version differences.
            code = safe_file(work, node["lean_file"]).read_text(encoding="utf-8")
            by_id = {r["id"]: json.loads(r["payload"]) for r in rows}
            closure = set()
            def include(key):
                if key not in closure:
                    closure.add(key)
                    for dep in by_id[key].get("depends_on", []):
                        include(dep)
            for dependency in node.get("depends_on", []):
                include(dependency)
            context = [str(safe_file(work, by_id[key]["lean_file"]).resolve()) for key in sorted(closure)
                       if by_id[key].get("lean_file")]
            job = {"id": uuid.uuid4().hex, "problem": problem, "node": row["id"], "fingerprint": row["fingerprint"]}
            args = {"code": code, "wait": False, "hint": node.get("proof_sketch", ""), "context_files": sorted(set(context))}
            request_dir = self.root / "aristotle" / job["id"]
            write_json(request_dir / "request.json", {"node": node, "arguments": args, "fingerprint": row["fingerprint"]})
            self.store.execute("INSERT INTO jobs(id,problem,node,fingerprint,status,created) VALUES(?,?,?,?,?,?)",
                               (job["id"], problem, row["id"], row["fingerprint"], "submitting", time.time()))
            try:
                with self.connect(deadline) as client:
                    response = client.call(self.options["submit_tool"], args)
                # Persist raw reply before normalization, so an unusual response is recoverable.
                write_json(request_dir / "response.json", response)
                self.save_result(job, response, work)
            except (MCPError, OSError) as exc:
                self.store.execute("UPDATE jobs SET status='unknown',result=? WHERE id=?", (json.dumps({"error": str(exc)}), job["id"]))
                self.store.event(problem, "aristotle_submit_uncertain", {"job": job["id"], "error": str(exc)})
                # An unavailable integration must not consume a second submission slot.
                return

    def attach(self, job_id: str, remote_id: str):
        rows = self.store.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows or rows[0]["status"] != "unknown":
            raise ValueError("Only an uncertain job may have its remote ID attached")
        if not remote_id.strip():
            raise ValueError("Remote ID cannot be empty")
        self.store.execute("UPDATE jobs SET remote_id=?,status='pending',polled=0 WHERE id=?", (remote_id, job_id))
