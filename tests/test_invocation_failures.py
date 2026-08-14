import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import (
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    CodexInvocationError,
    InvalidAgentResult,
    _default_runner,
    invoke_agent,
    main,
)


def codex_stdout(thread_id, final_message):
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": final_message},
        },
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FailureRunner:
    def __init__(self, *, output="", returncode=0, timeout=False, edit=None):
        self.output = output
        self.returncode = returncode
        self.timeout = timeout
        self.edit = edit

    def __call__(self, command, cwd, input_text):
        cwd = Path(cwd)
        if self.edit:
            target = cwd / self.edit
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("unverified\n", encoding="utf-8")
        if self.timeout:
            raise subprocess.TimeoutExpired(command, 10)
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            stdout=self.output,
            stderr="failed" if self.returncode else "",
        )


class InvocationFailureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state_file = self.root / "state.json"
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

    def invoke(self, runner, *, timeout_seconds=DEFAULT_AGENT_TIMEOUT_SECONDS):
        return invoke_agent(
            agent="testing",
            workflow_id="issue-9",
            repo=self.repo,
            task="test timeout handling",
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )

    def test_default_timeout_is_1800_seconds(self):
        self.assertEqual(DEFAULT_AGENT_TIMEOUT_SECONDS, 1800)

    def test_default_runner_passes_timeout_to_subprocess(self):
        completed = subprocess.CompletedProcess(["codex"], 0, stdout="", stderr="")
        with patch("run_codex.subprocess.run", return_value=completed) as run:
            result = _default_runner(
                ["codex"], self.repo, "prompt", timeout_seconds=37
            )

        self.assertIs(result, completed)
        self.assertEqual(run.call_args.kwargs["timeout"], 37)

    def test_timeout_reports_unverified_worktree_artifacts(self):
        runner = FailureRunner(timeout=True, edit="tests/new_test.py")

        with self.assertRaises(CodexInvocationError) as caught:
            self.invoke(runner, timeout_seconds=10)

        self.assertEqual(
            caught.exception.unverified_artifacts,
            ["?? tests/new_test.py"],
        )
        self.assertIn("timed out after 10 seconds", str(caught.exception))

    def test_nonzero_exit_reports_unverified_worktree_artifacts(self):
        runner = FailureRunner(returncode=1, edit="failed.txt")

        with self.assertRaises(CodexInvocationError) as caught:
            self.invoke(runner)

        self.assertEqual(caught.exception.unverified_artifacts, ["?? failed.txt"])

    def test_invalid_result_reports_unverified_worktree_artifacts(self):
        runner = FailureRunner(
            output=codex_stdout("T9", "no machine result"),
            edit="partial.txt",
        )

        with self.assertRaises(InvalidAgentResult) as caught:
            self.invoke(runner)

        self.assertEqual(caught.exception.unverified_artifacts, ["?? partial.txt"])

    def test_main_passes_explicit_timeout_to_invoke_agent(self):
        with patch("run_codex.invoke_agent", return_value={"status": "BLOCKED"}) as invoke:
            rc = main(
                [
                    "--agent",
                    "testing",
                    "--workflow",
                    "issue-9",
                    "--repo",
                    str(self.repo),
                    "--task",
                    "task",
                    "--state-file",
                    str(self.state_file),
                    "--prompt-dir",
                    str(self.prompts),
                    "--timeout-seconds",
                    "1234",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(invoke.call_args.kwargs["timeout_seconds"], 1234)


if __name__ == "__main__":
    unittest.main()
