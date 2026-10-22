import unittest
from unittest.mock import patch

from crap.publication import check_paths, public_path
from scripts.check_public import outgoing_commits


class PublicationTest(unittest.TestCase):
    def test_pipeline_sources_allowed(self):
        for path in ("README.md", "crap/supervisor.py", "crap/prompts/worker.txt", "tests/test_pipeline.py",
                     "deploy/crap.service.example", ".github/workflows/tests.yml", ".githooks/pre-push"):
            self.assertTrue(public_path(path), path)

    def test_private_artifacts_rejected_even_inside_source_directories(self):
        for path in (".crap/state.sqlite3", "proofs/Main.lean", "tests/Submission.lean", "docs/proof.md",
                     "crap/events.jsonl", "tests/research/notes.txt", "pipeline.local.json", "tests/.env",
                     "tests/result.json", "tests/notes.zip", "../README.md", "/README.md"):
            self.assertFalse(public_path(path), path)

    def test_removed_private_file_still_rejected_in_outgoing_history(self):
        with patch("scripts.check_public.git", return_value="earlier\nlater\n") as git:
            self.assertEqual(["earlier", "later"], outgoing_commits(["refs/heads/main abc refs/heads/main def"]))
            git.assert_called_once_with("rev-list", "def..abc")
        self.assertEqual(["proofs/Main.lean"], check_paths(["README.md", "proofs/Main.lean"]))

    def test_first_push_checks_all_history_and_deletion_checks_none(self):
        zero = "0" * 40
        with patch("scripts.check_public.git", return_value="first\n") as git:
            self.assertEqual(["first"], outgoing_commits([f"refs/heads/main abc refs/heads/main {zero}"]))
            git.assert_called_once_with("rev-list", "abc")
        with patch("scripts.check_public.git") as git:
            self.assertEqual([], outgoing_commits([f"(delete) {zero} refs/heads/main abc"]))
            git.assert_not_called()
