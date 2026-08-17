import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import AgentRepositoryMutationError, InvalidAgentResult, invoke_agent


def codex_stdout(message):
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "T-red"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
        )
    ) + "\n"


class FakeRunner:
    def __init__(self, test_command):
        self.test_command = test_command

    def __call__(self, command, cwd, input_text):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_stdout(
                "HERMES_RESULT="
                + json.dumps(
                    {
                        "status": "RED_COMPLETE",
                        "test_command": self.test_command,
                    }
                )
            ),
            stderr="",
        )


class RedVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)

        self.prompts = root / "prompts"
        self.prompts.mkdir()
        (self.prompts / "testing.md").write_text("ROLE:testing\n", encoding="utf-8")
        self.state_file = root / "state.json"
        self.pending = {
            "from": "coordinator",
            "to": "testing",
            "payload": {
                "status": "HANDOFF",
                "next_agent": "testing",
                "task": "Add RED",
                "reason": "Need RED",
            },
        }
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-red",
                    "sessions": {},
                    "pending": self.pending,
                    "task_review_clean_checkpoint": "clean",
                    "review_certification": None,
                }
            ),
            encoding="utf-8",
        )

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def invoke(self, test_command, timeout_seconds=30):
        return invoke_agent(
            agent="testing",
            workflow_id="issue-red",
            repo=self.repo,
            task="ignored",
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=FakeRunner(test_command),
            timeout_seconds=timeout_seconds,
        )

    def test_green_command_does_not_consume_testing_handoff(self):
        with patch("run_codex._publish_specialist_failure_trace") as failure_trace:
            with self.assertRaisesRegex(InvalidAgentResult, "must still fail before GREEN"):
                self.invoke("true")

        self.assertEqual(self.state()["pending"], self.pending)
        self.assertEqual(failure_trace.call_count, 1)
        self.assertIn(
            "Testing RED_COMPLETE test_command must still fail before GREEN",
            failure_trace.call_args.kwargs["reason"],
        )

    def test_red_verification_timeout_does_not_consume_testing_handoff(self):
        command = "python -c \"import time; time.sleep(2); raise SystemExit(1)\""
        with patch("run_codex._publish_specialist_failure_trace") as failure_trace:
            with self.assertRaisesRegex(InvalidAgentResult, "timed out after 1 seconds"):
                self.invoke(command, timeout_seconds=1)

        self.assertEqual(self.state()["pending"], self.pending)
        self.assertEqual(failure_trace.call_count, 1)

    def test_red_verification_cannot_mutate_repository(self):
        command = (
            "python -c \"from pathlib import Path; "
            "Path('generated.txt').write_text('x'); raise SystemExit(1)\""
        )
        with patch("run_codex._publish_specialist_failure_trace") as failure_trace:
            with self.assertRaisesRegex(
                AgentRepositoryMutationError,
                "RED verification command modified the worktree",
            ):
                self.invoke(command)

        self.assertEqual(self.state()["pending"], self.pending)
        self.assertEqual(failure_trace.call_count, 1)
        self.assertTrue((self.repo / "generated.txt").exists())


if __name__ == "__main__":
    unittest.main()
