import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from run_codex import InvalidAgentResult, invoke_agent


def codex_stdout(message):
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "T-red"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
        )
    ) + "\n"


class FakeRunner:
    def __call__(self, command, cwd, input_text):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_stdout(
                'HERMES_RESULT={"status":"RED_COMPLETE","test_command":"true"}'
            ),
            stderr="",
        )


class RedVerificationTests(unittest.TestCase):
    def test_green_command_does_not_consume_testing_handoff(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

            prompts = root / "prompts"
            prompts.mkdir()
            (prompts / "testing.md").write_text("ROLE:testing\n", encoding="utf-8")
            state_file = root / "state.json"
            pending = {
                "from": "coordinator",
                "to": "testing",
                "payload": {
                    "status": "HANDOFF",
                    "next_agent": "testing",
                    "task": "Add RED",
                    "reason": "Need RED",
                },
            }
            state_file.write_text(
                json.dumps(
                    {
                        "workflow_id": "issue-red",
                        "sessions": {},
                        "pending": pending,
                        "task_review_clean_checkpoint": "clean",
                        "review_certification": None,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(InvalidAgentResult, "must still fail before GREEN"):
                invoke_agent(
                    agent="testing",
                    workflow_id="issue-red",
                    repo=repo,
                    task="ignored",
                    state_file=state_file,
                    prompt_dir=prompts,
                    runner=FakeRunner(),
                )

            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["pending"], pending)


if __name__ == "__main__":
    unittest.main()
