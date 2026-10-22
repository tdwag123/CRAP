from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path

DEFAULT = {
    "state_dir": ".crap",
    "benchmark": {
        "url": "https://github.com/leanprover/lean-eval.git",
        "commit": "78f8a2412277f48187d3d8772039b2e245f99d0d",
        "path": ".crap/benchmark",
    },
    "budget": {
        "problem_hours": 48,
        "search_hours": 2,
        "integration_hours": 4,
        "turn_minutes": 30,
        "verify_minutes": 30,
        "max_consecutive_errors": 3,
        "retry_seconds": 60,
    },
    "codex": {"command": ["codex"], "model": None, "reasoning_effort": "high"},
    "aristotle": {
        "enabled": False,
        "command": [],
        "env_names": ["ARISTOTLE_API_KEY"],
        "max_pending": 2,
        "max_jobs_per_problem": 30,
        "max_attempts_per_node": 2,
        "poll_seconds": 120,
        "rpc_timeout_seconds": 90,
        "submit_tool": "prove",
        "poll_tool": "check_proof",
    },
}


def merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key not in base:
            raise ValueError(f"Unknown configuration key: {key}")
        result[key] = merge(base[key], value) if isinstance(base[key], dict) else value
    return result


def load(path: Path) -> dict:
    cfg = merge(DEFAULT, json.loads(path.read_text(encoding="utf-8")))
    base = path.resolve().parent
    cfg["state_dir"] = str((base / cfg["state_dir"]).resolve())
    cfg["benchmark"]["path"] = str((base / cfg["benchmark"]["path"]).resolve())
    if not re.fullmatch(r"[0-9a-f]{40}", cfg["benchmark"]["commit"]):
        raise ValueError("benchmark.commit must be an immutable 40-character SHA")
    for key, value in cfg["budget"].items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"budget.{key} must be a positive finite number")
    if cfg["budget"]["search_hours"] + cfg["budget"]["integration_hours"] >= cfg["budget"]["problem_hours"]:
        raise ValueError("Search and integration reserves must fit inside the problem budget")
    for section in ("codex", "aristotle"):
        cmd = cfg[section]["command"]
        if not isinstance(cmd, list) or not all(isinstance(s, str) and s for s in cmd):
            raise ValueError(f"{section}.command must be an argv array")
    if not cfg["codex"]["command"]:
        raise ValueError("codex.command is required")
    if cfg["aristotle"]["enabled"] and not cfg["aristotle"]["command"]:
        raise ValueError("Configure aristotle.command before enabling Aristotle")
    for key in ("max_pending", "max_jobs_per_problem", "max_attempts_per_node", "poll_seconds", "rpc_timeout_seconds"):
        value = cfg["aristotle"][key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"aristotle.{key} must be a positive integer")
    return cfg
