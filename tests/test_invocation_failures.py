import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from run_codex import (
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    _default_runner,
    main,
)


def codex_stdout(final_message):
    events = [
        {"type": "thread.started", "thread_id": "T9"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": final_message},
        },
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class InvocationFailureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.prompts = self.root / "prompts"
        self.prompts.mkdir()
        for role in ("testing", "coordinator", "review"):
            (self.prompts / f"{role}.md").write_text(
                f"ROLE:{role}\n", encoding="utf-8"
            )
        self._git("init")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")

    def _git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            capture_output=True,
        )

    def argv(self, timeout="1800"):
        return [
            "--agent", "testing",
            "--workflow", "issue-9",
            "--repo", str(self.repo),
            "--task", "test failure handling",
            "--state-file", str(self.root / "state.json"),
            "--prompt-dir", str(self.prompts),
            "--timeout-seconds", timeout,
        ]

    def run_main_with(self, runner, *, timeout="1800"):
        stderr = io.StringIO()
        stdout = io.StringIO()
        with patch("run_codex._default_runner", side_effect=runner):
            with redirect_stderr(stderr), redirect_stdout(stdout):
                rc = main(self.argv(timeout))
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_default_timeout_is_1800_seconds(self):
        self.assertEqual(DEFAULT_AGENT_TIMEOUT_SECONDS, 1800)

    def test_default_runner_passes_timeout_to_subprocess(self):
        completed = subprocess.CompletedProcess(["codex"], 0, stdout="", stderr="")
        with patch("run_codex.subprocess.run", return_value=completed) as run:
            _default_runner(["codex"], self.repo, "prompt", timeout_seconds=37)
        self.assertEqual(run.call_args.kwargs["timeout"], 37)

    def test_timeout_is_failed_and_reports_unverified_artifacts(self):
        def runner(command, cwd, input_text, *, timeout_seconds):
            (Path(cwd) / "partial.txt").write_text("partial\n", encoding="utf-8")
            raise subprocess.TimeoutExpired(command, timeout_seconds)

        rc, _, stderr = self.run_main_with(runner, timeout="10")
        payload = json.loads(stderr)
        self.assertEqual(rc, 2)
        self.assertIn("timed out after 10 seconds", payload["error"])
        self.assertEqual(payload["unverified_artifacts"], ["?? partial.txt"])

    def test_nonzero_is_failed_and_reports_unverified_artifacts(self):
        def runner(command, cwd, input_text, *, timeout_seconds):
            (Path(cwd) / "failed.txt").write_text("partial\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")

        rc, _, stderr = self.run_main_with(runner)
        payload = json.loads(stderr)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["unverified_artifacts"], ["?? failed.txt"])

    def test_invalid_result_is_failed_and_reports_unverified_artifacts(self):
        def runner(command, cwd, input_text, *, timeout_seconds):
            (Path(cwd) / "invalid.txt").write_text("partial\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                command, 0, stdout=codex_stdout("no result"), stderr=""
            )

        rc, _, stderr = self.run_main_with(runner)
        payload = json.loads(stderr)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["unverified_artifacts"], ["?? invalid.txt"])

    def test_malformed_result_is_failed_and_reports_unverified_artifacts(self):
        def runner(command, cwd, input_text, *, timeout_seconds):
            (Path(cwd) / "malformed.txt").write_text("partial\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=codex_stdout('HERMES_RESULT={"status":'),
                stderr="",
            )

        rc, _, stderr = self.run_main_with(runner)
        payload = json.loads(stderr)
        self.assertEqual(rc, 2)
        self.assertIn("not valid JSON", payload["error"])
        self.assertEqual(payload["unverified_artifacts"], ["?? malformed.txt"])

    def test_explicit_timeout_reaches_default_runner(self):
        seen = {}

        def runner(command, cwd, input_text, *, timeout_seconds):
            seen["timeout_seconds"] = timeout_seconds
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=codex_stdout(
                    'HERMES_RESULT={"status":"RED_COMPLETE",'
                    '"test_command":"python -m unittest"}'
                ),
                stderr="",
            )

        rc, _, _ = self.run_main_with(runner, timeout="1234")
        self.assertEqual(rc, 0)
        self.assertEqual(seen["timeout_seconds"], 1234)


if __name__ == "__main__":
    unittest.main()
