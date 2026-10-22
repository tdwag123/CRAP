from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from . import dag
from .aristotle import Aristotle
from .benchmark import Benchmark, add_problem, doctor
from .config import DEFAULT, load
from .setup import activate, install_tools, preflight, require_preflight
from .store import Store
from .supervisor import Supervisor
from .util import ident, supervisor_lock, write_json


def parser():
    p = argparse.ArgumentParser(prog="crap", description="CRAP: Can't Reasonably Articulate Proofs — research, plan, and verify Lean proofs")
    p.add_argument("--config", type=Path, default=Path("pipeline.local.json"))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create local configuration; no network or agent calls")
    boot = sub.add_parser("bootstrap", help="Fetch the pinned benchmark")
    boot.add_argument("--install-tools", action="store_true", help="Build pinned Linux verification tools")
    sub.add_parser("doctor", help="Report tool availability")
    sub.add_parser("preflight", help="Run real positive/negative and sandbox verification checks")
    cat = sub.add_parser("catalog", help="List pinned benchmark problems")
    cat.add_argument("--all", action="store_true", help="Include hidden, draft and archived entries")
    cat.add_argument("--group")
    for name in ("add", "prepare", "pause", "resume", "handoff", "dag"):
        s = sub.add_parser(name)
        s.add_argument("problem")
        if name == "resume":
            s.add_argument("--fresh-session", action="store_true", help="Recover from invalid CLI session IDs")
    runner = sub.add_parser("run", help="Work through selected problems with persistent deadlines")
    runner.add_argument("--problem")
    runner.add_argument("--once", action="store_true", help="Perform one bounded turn")
    sub.add_parser("stop", help="Request a graceful stop, including the running Codex turn")
    sub.add_parser("status", help="Print problem states, node states and remote jobs")
    ar = sub.add_parser("aristotle", help="Inspect connection or reconcile an uncertain submission")
    ar.add_argument("action", choices=("probe", "jobs", "attach", "abandon"))
    ar.add_argument("job", nargs="?")
    ar.add_argument("remote_id", nargs="?")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        if args.command == "init":
            if args.config.exists():
                raise ValueError(f"Configuration already exists: {args.config}")
            write_json(args.config, DEFAULT)
            print(f"Created {args.config}. Edit the model and Aristotle command before running.")
            return 0
        cfg = load(args.config)
        activate(cfg)
        root = Path(cfg["state_dir"])
        store = Store(root)
        supervisor = Supervisor(cfg, store)
        if hasattr(args, "problem") and args.problem:
            ident(args.problem)
        if args.command == "doctor":
            print(json.dumps(doctor(cfg), indent=2))
            return 0
        if args.command == "status":
            result = {"problems": store.rows("SELECT * FROM problems ORDER BY rowid"),
                      "nodes": store.rows("SELECT problem,id,status,detail FROM nodes"),
                      "jobs": store.rows("SELECT id,problem,node,status,remote_id FROM jobs"),
                      "stop_requested": (root / "control" / "stop").exists()}
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command in ("stop", "pause"):
            if args.command == "pause":
                store.problem(args.problem)
            path = root / "control" / ("stop" if args.command == "stop" else "pause-" + args.problem)
            path.parent.mkdir(exist_ok=True)
            path.touch()
            print("Stop requested." if args.command == "stop" else "Pause requested; the original deadline remains in force.")
            return 0
        with supervisor_lock(root):
            if args.command == "bootstrap":
                print("Pinned benchmark:", Benchmark(cfg).fetch(root / "bootstrap.log"))
                if args.install_tools:
                    install_tools(cfg)
            elif args.command == "preflight":
                print(json.dumps(preflight(cfg), indent=2))
            elif args.command == "catalog":
                for entry in Benchmark(cfg).catalog():
                    if not args.all and not (entry.get("visible") and entry.get("status") == "active"):
                        continue
                    if args.group and entry.get("group") != args.group:
                        continue
                    print(f"{entry['id']}\t{entry['status']}\t{entry['title']}")
            elif args.command == "add":
                print(add_problem(cfg, store, args.problem))
            elif args.command == "prepare":
                supervisor.prepare(args.problem)
                print("Dependencies prepared; the research clock has not started.")
            elif args.command == "resume":
                state = store.problem(args.problem)
                if state["status"] in ("solved", "expired") or (state["deadline"] and state["deadline"] <= time.time()):
                    raise ValueError("A solved or expired problem cannot be resumed")
                fields = {"status": "queued", "errors": 0}
                if args.fresh_session:
                    fields["session"] = None
                store.update(args.problem, **fields)
                (root / "control" / ("pause-" + args.problem)).unlink(missing_ok=True)
                print("Queued with the original deadline. Use run to continue.")
            elif args.command == "run":
                require_preflight(cfg)
                if cfg["aristotle"]["enabled"]:
                    Aristotle(cfg, store).probe()
                (root / "control" / "stop").unlink(missing_ok=True)
                def interrupt(signum, frame):
                    raise KeyboardInterrupt
                signal.signal(signal.SIGTERM, interrupt)
                supervisor.run(args.problem, args.once)
            elif args.command == "handoff":
                print(supervisor.handoff(args.problem))
            elif args.command == "dag":
                graph = dag.sync(store, args.problem, supervisor.work(args.problem))
                print(json.dumps(graph, indent=2, ensure_ascii=False))
            elif args.command == "aristotle":
                ar = Aristotle(cfg, store)
                if args.action == "probe":
                    print(json.dumps(ar.probe(), indent=2))
                elif args.action == "jobs":
                    print(json.dumps(store.rows("SELECT * FROM jobs"), indent=2, ensure_ascii=False))
                elif args.action == "attach":
                    if not args.job or not args.remote_id:
                        raise ValueError("attach requires JOB_ID REMOTE_PROJECT_ID")
                    ar.attach(args.job, args.remote_id)
                elif args.action == "abandon":
                    rows = store.rows("SELECT * FROM jobs WHERE id=?", (args.job,))
                    if not rows or rows[0]["status"] != "unknown":
                        raise ValueError("Only unknown jobs can be abandoned")
                    store.execute("UPDATE jobs SET status='abandoned' WHERE id=?", (args.job,))
                    print("Local job abandoned. This does not cancel a possible remote job.")
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Saved state and the original deadlines remain available.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
