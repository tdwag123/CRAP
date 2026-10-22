from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .benchmark import Benchmark, doctor, setup_lake
from .util import clean_env, digest, run_command, write_json
from .verify import Verifier

PINS = {
    "landrun": ("zouuup/landrun", "5ed4a3db3a4ad930d577215c6b9abaa19df7f99f"),
    "lean4export": ("leanprover/lean4export", "076e8e57707e813375e8f9da8bf989799ace9680"),
    "comparator": ("leanprover/comparator", "d03acab154d269c06e60e4de7e4cc85deebff94b"),
    "nanoda": ("robsimmons/nanoda_lib", "68d5ca9db226849b41a6fff59d796ff19d0a8840"),
}


def activate(cfg):
    binary = Path(cfg["state_dir"]) / "tools" / "bin"
    os.environ["PATH"] = str(binary) + os.pathsep + os.environ.get("PATH", "")


def install_tools(cfg):
    if sys.platform != "linux":
        raise RuntimeError("Verifier tool installation requires Linux; run this on the server")
    for name in ("git", "go", "cargo", "lake"):
        if not shutil.which(name):
            raise RuntimeError(f"Install {name} first")
    bench = Benchmark(cfg)
    policy = bench.read("SECURITY.md").decode()
    for _, pin in PINS.values():
        if pin not in policy:
            raise RuntimeError("Pinned verifier versions differ from benchmark SECURITY.md; update setup.py")
    root = Path(cfg["state_dir"]) / "tools"
    binary = root / "bin"
    binary.mkdir(parents=True, exist_ok=True)
    logs = root / "logs"

    def execute(argv, cwd, name, env=None):
        rc = run_command(argv, cwd, logs / (name + ".log"), 7200, env=env or clean_env())
        if rc:
            raise RuntimeError(f"Tool build failed: {name}; see {logs}")

    env = clean_env()
    env["GOBIN"] = str(binary)
    execute(["go", "install", "github.com/zouuup/landrun/cmd/landrun@" + PINS["landrun"][1]], root, "landrun", env)
    for name in ("lean4export", "comparator", "nanoda"):
        repo, pin = PINS[name]
        source = root / (name + "-" + pin[:12])
        if not source.exists():
            source.mkdir()
            execute(["git", "init"], source, name + "-init")
            execute(["git", "remote", "add", "origin", "https://github.com/" + repo + ".git"], source, name + "-remote")
            execute(["git", "fetch", "--depth=1", "origin", pin], source, name + "-fetch")
            execute(["git", "checkout", "--detach", pin], source, name + "-checkout")
        actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if actual != pin:
            raise ValueError(f"Wrong tool source revision: {source}")
        if name == "lean4export":
            (source / "lean-toolchain").write_bytes(bench.read("lean-toolchain"))
        if name == "nanoda":
            execute(["cargo", "build", "--release", "--locked"], source, name + "-build")
            executable = source / "target" / "release" / "nanoda_bin"
        else:
            execute(["lake", "build", name], source, name + "-build")
            executable = source / ".lake" / "build" / "bin" / name
        shutil.copy2(executable, binary / executable.name)
    write_json(root / "installed.json", {"benchmark_commit": bench.commit, "pins": PINS})


def environment_fingerprint(cfg):
    report = doctor(cfg)
    paths = report["tools"]
    binaries = {k: {"path": str(Path(v).resolve()), "sha256": digest(Path(v).read_bytes())} for k, v in paths.items() if v}
    return {"benchmark_commit": cfg["benchmark"]["commit"], "binaries": binaries}


def preflight(cfg):
    report = doctor(cfg)
    if not report["ready"] or sys.platform != "linux":
        raise RuntimeError("Linux and all verifier tools are required; run doctor for missing tools")
    bench = Benchmark(cfg)
    root = Path(cfg["state_dir"])
    logs = root / "preflight"
    logs.mkdir(exist_ok=True)
    # Upstream's installation check tests comparator, exporter, sandbox and nanoda together.
    if bench.git("rev-parse", "HEAD").decode().strip() != bench.commit or bench.git("diff", "HEAD", "--"):
        raise RuntimeError("Preflight requires a clean benchmark checkout at the configured pin")
    setup_lake(bench.root, logs / "setup", 7200, cache_helper=bench.cache_helper())
    rc = run_command(["lake", "exe", "lean-eval", "check-comparator-installation"], bench.root,
                     logs / "installation.log", 7200, env=clean_env())
    if rc:
        raise RuntimeError(f"Official comparator installation check failed; see {logs}")
    rc = run_command([sys.executable, "scripts/sandbox_engaged_probe.py"], bench.root,
                     logs / "sandbox.log", 1800, env=clean_env())
    if rc:
        raise RuntimeError(f"Official sandbox engagement probe failed; see {logs}")
    with tempfile.TemporaryDirectory(prefix="preflight-", dir=root) as temp:
        work = Path(temp) / "work"
        bench.extract("two_plus_two", work)
        verifier = Verifier(cfg)
        negative = verifier.final("two_plus_two", work, time.time() + 7200)
        if negative["status"] != "rejected":
            raise RuntimeError(f"Unfinished starter was not rejected: {negative}")
        submission = work / "Submission.lean"
        text = submission.read_text(encoding="utf-8")
        if text.count("sorry") != 1:
            raise ValueError("Unexpected smoke-test starter; review the pinned benchmark")
        submission.write_text(text.replace("sorry", "decide"), encoding="utf-8")
        positive = verifier.final("two_plus_two", work, time.time() + 7200)
        if positive["status"] != "accepted":
            raise RuntimeError(f"Known valid proof did not pass: {positive}")
    receipt = {"fingerprint": environment_fingerprint(cfg), "passed_at": time.time(),
               "positive": positive, "negative": negative}
    write_json(root / "preflight.json", receipt)
    return receipt


def require_preflight(cfg):
    marker = Path(cfg["state_dir"]) / "preflight.json"
    if not marker.exists():
        raise RuntimeError("Run `crap preflight` before starting research")
    receipt = json.loads(marker.read_text(encoding="utf-8"))
    if not doctor(cfg)["ready"] or receipt["fingerprint"] != environment_fingerprint(cfg):
        raise RuntimeError("Verifier environment changed; rerun preflight")
