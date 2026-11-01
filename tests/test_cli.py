"""Offline CLI checks using temporary inputs and mocked job/model services."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from job_matcher import cli, persistence
from job_matcher.matcher import MatchResult


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stderr = io.StringIO()
        self.stack.enter_context(redirect_stderr(self.stderr))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.fetch = self.stack.enter_context(patch.object(cli, "fetch_all", return_value=[]))
        self.client = self.stack.enter_context(patch.object(cli.genai, "Client"))
        self.match = self.stack.enter_context(
            patch.object(cli, "match_job", side_effect=AssertionError("Unexpected model call"))
        )
        self.stack.enter_context(patch.object(cli, "write_github_summary"))
        self.stack.enter_context(patch.object(cli, "write_candidates_summary"))
        self.stack.enter_context(
            patch.object(cli, "load_config", side_effect=lambda: persistence.load_config(self.root / "config.json"))
        )
        self.stack.enter_context(
            patch.object(cli, "load_resume", side_effect=lambda: persistence.load_resume(self.root / "resume.txt"))
        )
        self.stack.enter_context(
            patch.object(cli, "load_seen", side_effect=lambda: persistence.load_seen(self.root / "seen_jobs.json"))
        )
        self.stack.enter_context(
            patch.object(cli, "save_seen", side_effect=lambda seen: persistence.save_seen(seen, self.root / "seen_jobs.json"))
        )
        self.stack.enter_context(
            patch.object(cli, "clear_candidates", side_effect=lambda: persistence.clear_candidates(self.root / "candidates.json"))
        )
        self.stack.enter_context(
            patch.object(cli, "write_candidates", side_effect=lambda jobs: persistence.write_candidates(jobs, self.root / "candidates.json"))
        )

    def write_json(self, filename: str, value: object) -> None:
        (self.root / filename).write_text(json.dumps(value), encoding="utf-8")

    def assert_no_remote_work(self) -> None:
        self.fetch.assert_not_called()
        self.client.assert_not_called()
        self.match.assert_not_called()

    def test_missing_config_explains_setup_before_fetching(self) -> None:
        self.assertEqual(cli.main([]), 1)
        self.assertIn("Copy config.example.json to config.json", self.stderr.getvalue())
        self.assert_no_remote_work()

    def test_missing_resume_is_reported_before_missing_api_key(self) -> None:
        self.write_json("config.json", {})
        self.assertEqual(cli.main([]), 1)
        self.assertIn("Copy resume.example.txt to resume.txt", self.stderr.getvalue())
        self.assertNotIn("GEMINI_API_KEY", self.stderr.getvalue())
        self.assert_no_remote_work()

    def test_invalid_config_exits_without_fetching(self) -> None:
        for text in ("{invalid", "[]"):
            with self.subTest(config=text):
                (self.root / "config.json").write_text(text, encoding="utf-8")
                self.assertEqual(cli.main(["--deterministic-only"]), 1)
        self.assertIn("not valid JSON", self.stderr.getvalue())
        self.assertIn("must contain a JSON object", self.stderr.getvalue())
        self.assert_no_remote_work()

    def test_invalid_state_is_not_silently_reset(self) -> None:
        self.write_json("config.json", {})
        path = self.root / "seen_jobs.json"
        path.write_text("{invalid", encoding="utf-8")
        self.assertEqual(cli.main(["--deterministic-only"]), 1)
        self.assertIn("preserve your previously processed job IDs", self.stderr.getvalue())
        self.assertEqual(path.read_text(encoding="utf-8"), "{invalid")
        self.assert_no_remote_work()

    def test_empty_or_filtered_results_never_become_sample_jobs(self) -> None:
        self.write_json("config.json", {"titles": ["Software Engineer"]})
        for jobs in ([], [{"id": "test:filtered", "title": "Accountant", "location": ""}]):
            with self.subTest(jobs=jobs):
                self.fetch.return_value = jobs
                self.assertEqual(cli.main(["--deterministic-only"]), 0)
                candidates = json.loads((self.root / "candidates.json").read_text(encoding="utf-8"))
                self.assertEqual(candidates, [])
                self.assertFalse((self.root / "seen_jobs.json").exists())
        self.client.assert_not_called()
        self.match.assert_not_called()

    def test_seen_ids_are_preserved_and_not_scored_again(self) -> None:
        self.write_json("config.json", {})
        (self.root / "resume.txt").write_text("Example Python experience.\n", encoding="utf-8")
        existing = {"matched": False, "seen_at": "2020-01-01T00:00:00Z"}
        self.write_json("seen_jobs.json", {"test:old": existing})
        self.fetch.return_value = [
            {"id": "test:old", "title": "Old role", "company": "Example"},
            {"id": "test:new", "title": "New role", "company": "Example"},
        ]
        self.match.side_effect = None
        self.match.return_value = MatchResult(is_match=False, reason="Test result.")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "offline-test-key", "GEMINI_REQUEST_INTERVAL_SEC": "0"}):
            self.assertEqual(cli.main([]), 0)
            self.match.assert_called_once()
            self.assertEqual(self.match.call_args.args[0]["id"], "test:new")
            saved = json.loads((self.root / "seen_jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(set(saved), {"test:old", "test:new"})
            self.assertEqual(saved["test:old"], existing)

            self.match.reset_mock()
            self.assertEqual(cli.main([]), 0)
            self.match.assert_not_called()
            self.assertEqual(json.loads((self.root / "seen_jobs.json").read_text(encoding="utf-8")), saved)


if __name__ == "__main__":
    unittest.main()
