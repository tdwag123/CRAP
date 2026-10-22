"""Exercise the pipeline with subprocess fixtures, without accounts or Lean.

Run from the repository root: python scripts/demo.py --output .test-artifacts/demo
All proof verdicts in this demonstration are explicitly simulated.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crap.benchmark import add_problem
from crap.store import Store
from crap.supervisor import Supervisor
from crap.util import write_json
from tests.support import SimulatedVerifier, fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    cfg = fixture(root)
    tests = Path(__file__).resolve().parents[1] / "tests"
    cfg["codex"]["command"] = [sys.executable, str(tests / "fake_codex.py")]
    cfg["aristotle"].update(enabled=True, command=[sys.executable, str(tests / "fake_mcp.py"), str(root / "remote")])
    store = Store(Path(cfg["state_dir"]))
    try:
        add_problem(cfg, store, "test_problem")
        supervisor = Supervisor(cfg, store, verifier=SimulatedVerifier())
        first = supervisor.step("test_problem")
        # Advance only the polling eligibility, not the per-problem deadline.
        store.execute("UPDATE jobs SET polled=0")
        store.close()
        # Reconstruct the supervisor to exercise recovery from persisted state.
        store = Store(Path(cfg["state_dir"]))
        store.recover()
        supervisor = Supervisor(cfg, store, verifier=SimulatedVerifier())
        second = supervisor.step("test_problem")
        report = {"simulated": True, "notice": "No Codex, Aristotle service, or Lean proof checker was called.",
                  "first_turn": first, "second_turn": second,
                  "problems": store.rows("SELECT * FROM problems"), "jobs": store.rows("SELECT * FROM jobs")}
        write_json(root / "SIMULATED-REPORT.json", report)
        print(json.dumps({"simulated": True, "first_turn": first, "second_turn": second,
                          "report": str(root / "SIMULATED-REPORT.json")}, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
