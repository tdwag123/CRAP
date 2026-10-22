from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

from crap.config import DEFAULT
from crap.util import write_json


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE, text=True).strip()


def fixture(root: Path):
    """A synthetic benchmark for orchestration tests. Never a real proof verdict."""
    repo = root / "benchmark"
    repo.mkdir()
    git(repo, "init")
    work = repo / "generated" / "test_problem"
    (work / "Submission").mkdir(parents=True)
    files = {
        "Submission.lean": "import Submission.Base\nnamespace Submission\ntheorem main : (2 : Nat) + 2 = 4 := by sorry\nend Submission\n",
        "Submission/Base.lean": "namespace Submission\ntheorem base : (1 : Nat) + 1 = 2 := by decide\nend Submission\n",
        "Challenge.lean": "-- Trusted test challenge\n", "Solution.lean": "-- Trusted test bridge\n",
        "WorkspaceTest.lean": "-- Synthetic test harness; not executable Lean\n",
        "config.json": "{}\n", "lakefile.toml": 'name = "test_problem"\n',
        "lean-toolchain": "leanprover/lean4:v4.35.0-rc3\n",
    }
    for name, content in files.items():
        (work / name).write_text(content, encoding="utf-8")
    write_json(repo / "generated" / "index.json", [{"id": "test_problem", "title": "Synthetic test only", "status": "active", "visible": True, "statement_revision": 1}])
    (repo / "scripts").mkdir()
    (repo / "scripts" / "fetch_dependency_caches.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "scripts" / "lake_cache_curl.py").write_text("# fixture\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgSign=false", "commit", "-m", "Fixture")
    cfg = copy.deepcopy(DEFAULT)
    cfg["state_dir"] = str(root / "state")
    cfg["benchmark"]["path"] = str(repo)
    cfg["benchmark"]["commit"] = git(repo, "rev-parse", "HEAD")
    return cfg


def graph(work: Path):
    value = {"version": 1, "targets": ["main"], "nodes": [
        {"id": "base", "statement": "One plus one is two", "depends_on": [], "lean_file": "Submission/Base.lean",
         "declaration": "Submission.base", "lean_type": "(1 : Nat) + 1 = 2", "owner": "codex", "check": True},
        {"id": "main", "statement": "Two plus two is four", "depends_on": ["base"], "lean_file": "Submission.lean",
         "declaration": "Submission.main", "lean_type": "(2 : Nat) + 2 = 4", "proof_sketch": "Use decide",
         "owner": "aristotle", "delegate": True, "check": False},
    ]}
    write_json(work / "research" / "dag.json", value)
    return value


class SimulatedVerifier:
    """Only for tests and the explicitly labeled demo; no Lean is invoked."""
    def nodes(self, problem, work, deadline, nodes):
        return {"status": "checked", "log_dir": "SIMULATED", "nodes": {
            n["id"]: {"status": "checked", "detail": "SIMULATED check, not a Lean proof"} for n in nodes}}

    def final(self, problem, work, deadline):
        code = (work / "Submission.lean").read_text(encoding="utf-8")
        return {"status": "rejected" if "sorry" in code else "accepted", "log_dir": "SIMULATED", "simulated": True}
