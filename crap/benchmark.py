from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from .util import clean_env, ident, run_command, safe_file, write_json


class Benchmark:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = Path(cfg["benchmark"]["path"])
        self.commit = cfg["benchmark"]["commit"]

    def git(self, *args) -> bytes:
        return subprocess.check_output(["git", "-C", str(self.root), *args], timeout=120, stderr=subprocess.PIPE)

    def read(self, path: str) -> bytes:
        return self.git("show", f"{self.commit}:{path}")

    def catalog(self) -> list[dict]:
        return json.loads(self.read("generated/index.json"))

    def cache_helper(self) -> Path:
        destination = Path(self.cfg["state_dir"]) / "tools" / "cache-scripts" / self.commit
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("fetch_dependency_caches.sh", "lake_cache_curl.py"):
            p = destination / name
            p.write_bytes(self.read("scripts/" + name))
            p.chmod(0o755)
        return destination / "fetch_dependency_caches.sh"

    def extract(self, problem: str, destination: Path):
        ident(problem)
        if destination.exists():
            raise ValueError(f"Destination already exists: {destination}")
        prefix = f"generated/{problem}/"
        blob = self.git("archive", "--format=zip", self.commit, prefix)
        destination.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for entry in archive.infolist():
                if not entry.filename.startswith(prefix) or entry.is_dir():
                    continue
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Benchmark archive contains a symlink")
                relative = entry.filename[len(prefix):]
                path = safe_file(destination, relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(entry))
        for required in ("Submission.lean", "Challenge.lean", "Solution.lean", "WorkspaceTest.lean", "config.json", "lakefile.toml", "lean-toolchain"):
            if not (destination / required).is_file():
                raise ValueError(f"Incomplete generated workspace: {required}")

    def fetch(self, log: Path):
        self.root.parent.mkdir(parents=True, exist_ok=True)
        if not self.root.exists():
            self.root.mkdir()
            subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin", self.cfg["benchmark"]["url"]], check=True)
        try:
            self.git("cat-file", "-e", self.commit + "^{commit}")
        except subprocess.CalledProcessError:
            rc = run_command(["git", "fetch", "--depth=1", "origin", self.commit], self.root, log, 900)
            if rc:
                raise RuntimeError(f"Benchmark fetch failed; see {log}")
        # Checkout is intentionally limited to a newly created/detached managed checkout.
        head = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True, text=True)
        if head.returncode == 0 and head.stdout.strip() != self.commit:
            raise ValueError("Existing benchmark HEAD differs from pin; use a new benchmark path")
        if head.returncode != 0:
            self.git("checkout", "--detach", self.commit)
        return self.commit

    def restore_trusted(self, problem: str, work: Path) -> list[str]:
        """Restore accidental worker edits without touching solver-owned source."""
        changed = []
        with tempfile.TemporaryDirectory(prefix="trusted-", dir=work.parent) as temp:
            original = Path(temp) / "workspace"
            self.extract(problem, original)
            for path in original.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(original).as_posix()
                if relative == "Submission.lean" or relative.startswith("Submission/"):
                    continue
                dest = safe_file(work, relative)
                data = path.read_bytes()
                if not dest.exists() or dest.read_bytes() != data:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    changed.append(relative)
        instructions = (Path(__file__).parent / "prompts" / "worker.txt").read_bytes()
        agents = safe_file(work, "AGENTS.md")
        if not agents.exists() or agents.read_bytes() != instructions:
            agents.write_bytes(instructions)
            changed.append("AGENTS.md")
        return changed


def setup_lake(work: Path, logdir: Path, timeout: float = 3600, control=None, cache_helper: Path | None = None) -> None:
    end = time.monotonic() + timeout
    for name, command in (("update", ["lake", "update"]), ("cache", ["lake", "exe", "cache", "get"])):
        rc = run_command(command, work, logdir / f"{name}.log", max(0, end - time.monotonic()), env=clean_env(), tick=control)
        if rc:
            raise RuntimeError(f"Lake setup failed ({name}, exit {rc}); see {logdir}")
    if cache_helper is not None:
        rc = run_command(["bash", str(cache_helper), "--restore", str(work)], work, logdir / "dependency-cache.log",
                         max(0, end - time.monotonic()), env=clean_env(), tick=control)
        if rc:
            raise RuntimeError(f"Dependency cache restoration failed; see {logdir}")


def add_problem(cfg: dict, store, problem: str):
    ident(problem)
    if store.rows("SELECT id FROM problems WHERE id=?", (problem,)):
        raise ValueError(f"Problem already added: {problem}")
    bench = Benchmark(cfg)
    entry = next((p for p in bench.catalog() if p["id"] == problem), None)
    if entry is None:
        raise ValueError(f"Problem absent from pinned catalog: {problem}")
    root = Path(cfg["state_dir"]) / "problems" / problem
    work = root / "work"
    bench.extract(problem, work)
    (work / "research").mkdir()
    write_json(work / "research" / "dag.json", {"version": 1, "targets": [], "nodes": []})
    write_json(work / "research" / "sources.json", [])
    (work / "research" / "progress.md").write_text("# Research progress\n\nNo attempts yet.\n", encoding="utf-8")
    (work / "AGENTS.md").write_text((Path(__file__).parent / "prompts" / "worker.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (work / ".gitignore").write_text(".lake/\n*.olean\n*.ilean\n", encoding="utf-8")
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    metadata = {**entry, "benchmark_commit": bench.commit, "lean_toolchain": (work / "lean-toolchain").read_text().strip()}
    store.execute("INSERT INTO problems(id,metadata) VALUES(?,?)", (problem, json.dumps(metadata, ensure_ascii=False)))
    write_json(root / "metadata.json", metadata)
    checkpoint(work, "Initial benchmark workspace")
    return work


def checkpoint(work: Path, message: str) -> str:
    # Explicit paths prevent dependencies or credentials becoming checkpoint content.
    subprocess.run(["git", "-C", str(work), "add", "--", "Submission.lean", "Submission", "research", "AGENTS.md", ".gitignore"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.name=CRAP", "-c", "user.email=local@crap.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgSign=false", "commit", "--allow-empty", "-m", message],
                   check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()


def doctor(cfg: dict) -> dict:
    tools = {name: shutil.which(name) for name in ("git", "lake", "lean", "comparator", "landrun", "lean4export", "nanoda_bin")}
    tools["codex"] = shutil.which(cfg["codex"]["command"][0])
    import sys
    return {"platform": sys.platform, "linux_required_for_verification": True,
            "tools": tools, "ready": sys.platform == "linux" and all(tools.values()),
            "aristotle_enabled": cfg["aristotle"]["enabled"],
            "aristotle_command_found": bool(cfg["aristotle"]["command"] and shutil.which(cfg["aristotle"]["command"][0])),
            "benchmark_commit": cfg["benchmark"]["commit"]}
