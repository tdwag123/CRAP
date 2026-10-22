from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from crap import dag
from crap.aristotle import Aristotle
from crap.benchmark import Benchmark, add_problem
from crap.codex import Codex, command
from crap.config import DEFAULT, load
from crap.mcp import MCPError
from crap.store import Store
from crap.supervisor import StopRequested, Supervisor, phase_for
from crap.util import run_command, safe_file, source_files, supervisor_lock, write_json
from crap.verify import Verifier, axiom_result
from tests.support import SimulatedVerifier, fixture, graph

HERE = Path(__file__).resolve().parent


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = fixture(self.root)
        self.store = Store(Path(self.cfg["state_dir"]))
        self.work = add_problem(self.cfg, self.store, "test_problem")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def aristotle(self, mode="normal"):
        self.cfg["aristotle"].update(enabled=True, command=[sys.executable, str(HERE / "fake_mcp.py"), str(self.root / "remote"), mode])
        graph(self.work)
        dag.sync(self.store, "test_problem", self.work)
        dag.set_status(self.store, "test_problem", "base", "checked")
        return Aristotle(self.cfg, self.store)

    def test_deadline_survives_restart_and_configuration_change(self):
        first = self.store.start("test_problem", 48, now=100)
        self.store.close()
        self.store = Store(Path(self.cfg["state_dir"]))
        later = self.store.start("test_problem", 500, now=500)
        self.assertEqual(first["deadline"], later["deadline"])
        self.assertEqual(100, later["started"])

    def test_cycle_is_rejected(self):
        value = graph(self.work)
        value["nodes"][0]["depends_on"] = ["main"]
        write_json(self.work / "research/dag.json", value)
        with self.assertRaisesRegex(ValueError, "cycle"):
            dag.sync(self.store, "test_problem", self.work)

    def test_missing_dependency_is_rejected(self):
        value = graph(self.work)
        value["nodes"][1]["depends_on"] = ["absent"]
        write_json(self.work / "research/dag.json", value)
        with self.assertRaisesRegex(ValueError, "Unknown dependency"):
            dag.read_graph(self.work)

    def test_source_change_invalidates_transitive_checks(self):
        graph(self.work)
        dag.sync(self.store, "test_problem", self.work)
        for name in ("base", "main"):
            dag.set_status(self.store, "test_problem", name, "checked")
        p = self.work / "Submission/Base.lean"
        p.write_text(p.read_text() + "\n-- changed\n")
        dag.sync(self.store, "test_problem", self.work)
        self.assertEqual({"stale"}, {r["status"] for r in self.store.rows("SELECT status FROM nodes")})

    def test_contract_change_invalidates_descendants(self):
        value = graph(self.work)
        dag.sync(self.store, "test_problem", self.work)
        for name in ("base", "main"):
            dag.set_status(self.store, "test_problem", name, "checked")
        value["nodes"][0]["lean_type"] = "False"
        write_json(self.work / "research/dag.json", value)
        dag.sync(self.store, "test_problem", self.work)
        self.assertEqual({"stale"}, {r["status"] for r in self.store.rows("SELECT status FROM nodes")})

    def test_agent_cannot_assign_checked_status(self):
        value = graph(self.work)
        value["nodes"][0]["status"] = "checked"
        write_json(self.work / "research/dag.json", value)
        dag.sync(self.store, "test_problem", self.work)
        self.assertEqual({"planned"}, {r["status"] for r in self.store.rows("SELECT status FROM nodes")})

    def test_source_allowlist_excludes_trusted_edits(self):
        (self.work / "Challenge.lean").write_text("False")
        (self.work / "lakefile.lean").write_text("malicious build script")
        self.assertEqual({"Submission.lean", "Submission/Base.lean"}, set(source_files(self.work)))

    def test_benchmark_extraction_ignores_dirty_checkout(self):
        repo = Path(self.cfg["benchmark"]["path"])
        (repo / "generated/test_problem/Challenge.lean").write_text("changed")
        out = self.root / "fresh"
        Benchmark(self.cfg).extract("test_problem", out)
        self.assertEqual("-- Trusted test challenge\n", (out / "Challenge.lean").read_text())

    def test_aristotle_async_jobs_survive_reconnect(self):
        ar = self.aristotle()
        self.assertTrue(ar.probe()["ready"])
        ar.submit_ready("test_problem", self.work, time.time() + 60)
        job = self.store.rows("SELECT * FROM jobs")[0]
        self.assertEqual("pending", job["status"])
        self.store.execute("UPDATE jobs SET polled=0")
        Aristotle(self.cfg, self.store).poll("test_problem", self.work, time.time() + 60)
        self.assertEqual("returned", self.store.rows("SELECT * FROM jobs")[0]["status"])
        self.assertTrue((self.work / "research/aristotle" / job["id"] / "candidate.lean").exists())
        self.assertIn("sorry", (self.work / "Submission.lean").read_text())

    def test_aristotle_does_not_resubmit_pending_or_same_input(self):
        ar = self.aristotle()
        ar.submit_ready("test_problem", self.work, time.time() + 60)
        ar.submit_ready("test_problem", self.work, time.time() + 60)
        self.assertEqual(1, len(self.store.rows("SELECT * FROM jobs")))

    def test_lost_remote_response_is_unknown_not_retried(self):
        ar = self.aristotle("drop")
        ar.submit_ready("test_problem", self.work, time.time() + 60)
        ar.submit_ready("test_problem", self.work, time.time() + 60)
        jobs = self.store.rows("SELECT * FROM jobs")
        self.assertEqual(1, len(jobs))
        self.assertEqual("unknown", jobs[0]["status"])
        self.assertEqual(1, len(list((self.root / "remote").glob("*.json"))))
        ar.attach(jobs[0]["id"], "remote-1")
        self.assertEqual("pending", self.store.rows("SELECT * FROM jobs")[0]["status"])

    def test_dependencies_gate_delegation(self):
        ar = self.aristotle()
        dag.set_status(self.store, "test_problem", "base", "conditional")
        ar.submit_ready("test_problem", self.work, time.time() + 60)
        self.assertFalse(self.store.rows("SELECT * FROM jobs"))

    def test_expired_problem_does_not_call_worker(self):
        self.store.start("test_problem", 0.001, now=0)
        class Never:
            def run(self, *args, **kwargs):
                raise AssertionError("Worker was called after expiry")
        supervisor = Supervisor(self.cfg, self.store, worker=Never(), verifier=SimulatedVerifier())
        self.assertEqual("expired", supervisor.step("test_problem"))
        self.assertTrue((self.work.parent / "handoff/HANDOFF.md").exists())

    def test_recover_uncertain_submit_and_interrupted_turn(self):
        self.store.execute("INSERT INTO attempts(problem,started,status,directory) VALUES('test_problem',0,'running','x')")
        self.store.execute("INSERT INTO jobs(id,problem,node,fingerprint,status,created) VALUES('x','test_problem','main','hash','submitting',0)")
        self.store.recover()
        self.assertEqual("interrupted", self.store.rows("SELECT * FROM attempts")[0]["status"])
        self.assertEqual("unknown", self.store.rows("SELECT * FROM jobs")[0]["status"])

    def test_codex_subprocess_records_session_and_usage(self):
        self.cfg["codex"]["command"] = [sys.executable, str(HERE / "fake_codex.py")]
        self.store.start("test_problem", 48)
        result = Codex(self.cfg, self.store).run("test_problem", self.work, "research", 20)
        self.assertEqual("complete", result["status"])
        self.assertEqual("simulated-session-1", self.store.problem("test_problem")["session"])
        self.assertEqual(10, json.loads(self.store.rows("SELECT * FROM attempts")[0]["usage"])["output_tokens"])

    def test_full_simulated_research_delegate_integrate_pipeline(self):
        self.cfg["codex"]["command"] = [sys.executable, str(HERE / "fake_codex.py")]
        self.cfg["aristotle"].update(enabled=True, command=[sys.executable, str(HERE / "fake_mcp.py"), str(self.root / "remote")])
        supervisor = Supervisor(self.cfg, self.store, verifier=SimulatedVerifier())
        self.assertEqual("running", supervisor.step("test_problem"))
        self.assertEqual("prove", self.store.problem("test_problem")["phase"])
        self.store.execute("UPDATE jobs SET polled=0")
        self.assertEqual("solved", supervisor.step("test_problem"))
        self.assertEqual(2, len(self.store.rows("SELECT * FROM attempts")))
        self.assertEqual("returned", self.store.rows("SELECT * FROM jobs")[0]["status"])

    def test_model_success_report_cannot_override_rejection(self):
        class Liar:
            def run(self, *args, **kwargs):
                return {"status": "complete", "answer": {"summary": "Solved!", "candidate_ready": True}, "directory": "fake"}
        supervisor = Supervisor(self.cfg, self.store, worker=Liar(), verifier=SimulatedVerifier())
        self.assertEqual("running", supervisor.step("test_problem"))
        self.assertEqual("running", self.store.problem("test_problem")["status"])

    def test_final_verifier_copies_only_submission_and_preserves_trust(self):
        (self.work / "Challenge.lean").write_text("tampered")
        (self.work / "lakefile.lean").write_text("tampered")
        def fake_run(argv, clean, log, timeout, **kwargs):
            self.assertEqual("-- Trusted test challenge\n", (clean / "Challenge.lean").read_text())
            self.assertFalse((clean / "lakefile.lean").exists())
            self.assertIn("sorry", (clean / "Submission.lean").read_text())
            return 1
        with patch("crap.verify.setup_lake"), patch("crap.verify.run_command", side_effect=fake_run):
            result = Verifier(self.cfg).final("test_problem", self.work, time.time() + 60)
        self.assertEqual("rejected", result["status"])

    def test_pause_stops_before_worker(self):
        p = Path(self.cfg["state_dir"]) / "control/pause-test_problem"
        p.parent.mkdir()
        p.touch()
        with self.assertRaises(StopRequested):
            Supervisor(self.cfg, self.store).step("test_problem")

    def test_handoff_removes_deleted_helpers(self):
        supervisor = Supervisor(self.cfg, self.store)
        root = supervisor.handoff("test_problem")
        self.assertTrue((root / "source/Submission/Base.lean").exists())
        (self.work / "Submission/Base.lean").unlink()
        supervisor.handoff("test_problem")
        self.assertFalse((root / "source/Submission/Base.lean").exists())

    def test_infrastructure_errors_stop_after_configured_limit(self):
        self.cfg["budget"]["retry_seconds"] = 0.001
        class Broken:
            def run(self, *args, **kwargs):
                raise RuntimeError("Synthetic infrastructure failure")
        supervisor = Supervisor(self.cfg, self.store, worker=Broken())
        with patch.object(supervisor, "prepare"):
            supervisor.run("test_problem")
        state = self.store.problem("test_problem")
        self.assertEqual("needs_attention", state["status"])
        self.assertEqual(3, state["errors"])

    def test_remote_submission_caps(self):
        ar = self.aristotle()
        self.cfg["aristotle"]["max_jobs_per_problem"] = 1
        self.store.execute("INSERT INTO jobs(id,problem,node,fingerprint,status,created) VALUES('old','test_problem','other','h','failed',0)")
        ar.submit_ready("test_problem", self.work, time.time() + 60)
        self.assertEqual(1, len(self.store.rows("SELECT * FROM jobs")))


class UnitTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Linux process-group test")
    def test_parent_death_kills_worker_group(self):
        import signal
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = "import os,time,pathlib; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(120)"
            outer_script = root / "outer.py"
            outer_script.write_text(
                "import sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(HERE.parent)!r})\n"
                "from crap.util import run_command\n"
                f"run_command([sys.executable,'-c',{payload!r}], Path({str(root)!r}), Path({str(root / 'log')!r}), 120)\n")
            proc = subprocess.Popen([sys.executable, str(outer_script)])
            child_pid = None
            try:
                end = time.time() + 10
                while not (root / "pid").exists() and time.time() < end:
                    time.sleep(0.05)
                self.assertTrue((root / "pid").exists())
                child_pid = int((root / "pid").read_text())
                proc.kill()
                proc.wait(timeout=5)
                end = time.time() + 5
                while time.time() < end:
                    stat = Path(f"/proc/{child_pid}/stat")
                    if not stat.exists() or stat.read_text().split(") ", 1)[1].startswith("Z"):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("Worker survived the death of its supervisor")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if child_pid:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_axiom_audit(self):
        for text, good in [
            ("'Submission.x' does not depend on any axioms", True),
            ("'Submission.x' depends on axioms: [propext, Classical.choice, Quot.sound]", True),
            ("'Submission.x' depends on axioms: [sorryAx]", False),
            ("'Submission.x' depends on axioms: [Lean.ofReduceBool]", False),
            ("'Submission.x' depends on axioms: [invented_axiom]", False),
            ("success", False),
            ("'Submission.y' does not depend on any axioms", False),
        ]:
            self.assertEqual(good, axiom_result(text, "Submission.x")[0], text)

    def test_phase_boundaries(self):
        state = {"phase": "research", "started": 0, "deadline": 48 * 3600}
        self.assertEqual("research", phase_for(state, DEFAULT, 0))
        self.assertEqual("prove", phase_for(state, DEFAULT, 2 * 3600))
        self.assertEqual("integrate", phase_for(state, DEFAULT, 44 * 3600))

    def test_resume_uses_explicit_session(self):
        argv = command(DEFAULT, "abc", Path("schema.json"), Path("result.json"))
        self.assertIn("resume", argv)
        self.assertIn("abc", argv)
        self.assertNotIn("--last", argv)
        self.assertIn("workspace-write", argv)
        self.assertNotIn("--full-auto", argv)

    def test_paths_reject_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("../secret", ".git/config", ".lake/hidden"):
                with self.assertRaises(ValueError):
                    safe_file(Path(temp), name)

    def test_symlink_source_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "outside").write_text("secret")
            try:
                (root / "Submission.lean").symlink_to(root / "outside")
            except OSError:
                self.skipTest("Host does not permit unprivileged symlinks")
            with self.assertRaises(ValueError):
                source_files(root)

    def test_process_timeout_and_control(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rc = run_command([sys.executable, "-c", "import time; time.sleep(30)"], root, root / "out", 0.2)
            self.assertEqual(124, rc)
            def stop():
                raise StopRequested()
            with self.assertRaises(StopRequested):
                run_command([sys.executable, "-c", "import time; time.sleep(30)"], root, root / "out2", 10, tick=stop)

    def test_no_launch_after_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(124, run_command(["nonexistent-program"], root, root / "out", 0))

    def test_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            with supervisor_lock(Path(temp)):
                with self.assertRaises(RuntimeError):
                    with supervisor_lock(Path(temp)):
                        pass
            with supervisor_lock(Path(temp)):
                pass

    def test_config_rejects_mutable_pin_and_bad_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cfg.json"
            write_json(path, {"benchmark": {"commit": "main"}})
            with self.assertRaises(ValueError):
                load(path)
            write_json(path, {"budget": {"turn_minutes": -1}})
            with self.assertRaises(ValueError):
                load(path)
            write_json(path, {"budget": {"problem_hours": 2}})
            with self.assertRaises(ValueError):
                load(path)


if __name__ == "__main__":
    unittest.main()
