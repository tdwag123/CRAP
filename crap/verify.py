from __future__ import annotations

import json
import re
import tempfile
import time
import uuid
from pathlib import Path

from .benchmark import Benchmark, setup_lake
from .util import clean_env, digest, run_command, source_files, write_json

ALLOWED_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}


def axiom_result(text: str, declaration: str) -> tuple[bool, str]:
    escaped = re.escape(declaration)
    empty = re.search(r"'" + escaped + r"' does not depend on any axioms", text)
    found = re.search(r"'" + escaped + r"' depends on axioms:\s*\[([^\]]*)\]", text, re.S)
    if empty:
        return True, "No axioms"
    if not found:
        return False, "No recognizable axiom report; failing closed"
    axioms = {a.strip() for a in found.group(1).split(",") if a.strip()}
    unexpected = axioms - ALLOWED_AXIOMS
    return not unexpected, "Axioms: " + ", ".join(sorted(axioms))


class Verifier:
    """Only this component assigns proof verdicts. Always reconstruct trusted files."""
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = Path(cfg["state_dir"])
        self.benchmark = Benchmark(cfg)
        self.control = None

    def _execute(self, problem: str, work: Path, deadline: float, nodes: list[dict] | None):
        runid = uuid.uuid4().hex
        logs = self.root / "checks" / problem / runid
        logs.mkdir(parents=True)
        snapshot = source_files(work)
        write_json(logs / "source-hashes.json", {p: digest(data) for p, data in snapshot.items()})
        for p, data in snapshot.items():
            target = logs / "source" / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        receipt = self.root / "preflight.json"
        fingerprint = json.loads(receipt.read_text(encoding="utf-8")).get("fingerprint") if receipt.exists() else None
        write_json(logs / "provenance.json", {"benchmark_commit": self.benchmark.commit, "started": time.time(),
                                              "kind": "nodes" if nodes is not None else "final", "preflight_fingerprint": fingerprint})
        temp_root = self.root / "tmp"
        temp_root.mkdir(exist_ok=True)
        end = min(deadline, time.time() + self.cfg["budget"]["verify_minutes"] * 60)
        result = {"status": "infrastructure_error", "log_dir": str(logs), "nodes": {}}
        try:
            with tempfile.TemporaryDirectory(prefix="verify-", dir=temp_root) as temp:
                clean = Path(temp) / "workspace"
                self.benchmark.extract(problem, clean)
                # Dependency setup runs against the pristine starter, before candidate code is copied.
                setup_lake(clean, logs / "setup", max(0, end - time.time()), self.control, self.benchmark.cache_helper())
                # Remove starter-owned helpers absent from the candidate, then copy only Lean sources.
                for p in (clean / "Submission").rglob("*.lean"):
                    if p.relative_to(clean).as_posix() not in snapshot:
                        p.unlink()
                for p, data in snapshot.items():
                    target = clean / p
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                if nodes is None:
                    rc = run_command(["lake", "test"], clean, logs / "comparator.log", max(0, end - time.time()), env=clean_env(), tick=self.control)
                    result["status"] = "accepted" if rc == 0 else ("timeout" if rc == 124 else "rejected")
                    result["exit_code"] = rc
                    output = (logs / "comparator.log").read_text(encoding="utf-8", errors="replace") if (logs / "comparator.log").exists() else ""
                    if rc not in (0, 124) and ("Failed to run comparator via" in output or "Cannot find trusted tool" in output):
                        result["status"] = "infrastructure_error"
                        result["detail"] = "Verifier tool launch failed; inspect comparator.log"
                else:
                    for node in nodes:
                        if time.time() >= end:
                            result["nodes"][node["id"]] = {"status": "timeout", "detail": "Verification budget exhausted"}
                            continue
                        module = node["lean_file"][:-5].replace("/", ".")
                        rc = run_command(["lake", "build", module], clean, logs / f"{node['id']}-build.log", max(0, end - time.time()), env=clean_env(), tick=self.control)
                        if rc:
                            result["nodes"][node["id"]] = {"status": "failed", "detail": f"Module build failed: exit {rc}"}
                            continue
                        # A typed use of the exact declaration detects changed or weakened interfaces.
                        audit = (f"import {module}\nset_option autoImplicit false\n"
                                 f"example : {node['lean_type']} := @{node['declaration']}\n"
                                 f"#print axioms {node['declaration']}\n")
                        (clean / "PipelineAudit.lean").write_text(audit, encoding="utf-8")
                        out = logs / f"{node['id']}-audit.log"
                        rc = run_command(["lake", "env", "lean", "PipelineAudit.lean"], clean, out, max(0, end - time.time()), env=clean_env(), tick=self.control)
                        passed, detail = axiom_result(out.read_text(encoding="utf-8", errors="replace"), node["declaration"])
                        result["nodes"][node["id"]] = {"status": "checked" if rc == 0 and passed else "conditional",
                                                          "detail": detail if rc == 0 else f"Type/axiom audit failed: exit {rc}"}
                    result["status"] = "checked"
        except (OSError, RuntimeError, ValueError) as exc:
            result["detail"] = str(exc)
        write_json(logs / "result.json", result)
        return result

    def final(self, problem, work, deadline):
        return self._execute(problem, work, deadline, None)

    def nodes(self, problem, work, deadline, nodes):
        return self._execute(problem, work, deadline, nodes)
