import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import InvalidAgentResult, invoke_agent


def codex_stdout(final_message):
    events = [
        {"type": "thread.started", "thread_id": "C-safety"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": final_message},
        },
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeRunner:
    def __init__(self, final_message, mutation=None):
        self.final_message = final_message
        self.mutation = mutation

    def __call__(self, command, cwd, input_text):
        if self.mutation is not None:
            self.mutation(Path(cwd))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_stdout(self.final_message),
            stderr="",
        )


class MergeRecoverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.prompts = self.root / "prompts"
        self.prompts.mkdir()
        (self.prompts / "coordinator.md").write_text(
            "ROLE:coordinator\n", encoding="utf-8"
        )
        self.state_file = self.root / "state.json"

        self._git("init")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            capture_output=True,
        )

    def write_state(self, pending):
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-21",
                    "sessions": {},
                    "pending": pending,
                    "task_review_clean_checkpoint": "approved",
                    "review_certification": {
                        "head": self.head,
                        "pr_body_hash": "body-v1",
                    },
                }
            ),
            encoding="utf-8",
        )

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def invoke(self, runner, task="recovery evidence"):
        return invoke_agent(
            agent="coordinator",
            workflow_id="issue-21",
            repo=self.repo,
            task=task,
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=runner,
        )

    def test_merge_readiness_rejects_new_unstaged_coordinator_edits(self):
        self.write_state(
            {
                "from": "review",
                "to": "coordinator",
                "payload": {"status": "REVIEW_CLEAN"},
            }
        )
        merge_result = (
            'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
            f'"reviewed_head":"{self.head}","draft":false}}'
        )

        def edit_worktree(repo):
            (repo / "production.py").write_text("unreviewed\n", encoding="utf-8")

        with self.assertRaisesRegex(InvalidAgentResult, "requires a clean worktree"):
            self.invoke(FakeRunner(merge_result, mutation=edit_worktree))

        self.assertIsNone(self.state()["pending"])
        self.assertTrue((self.repo / "production.py").exists())

    def test_recovery_coordinator_cannot_wait_on_user_while_specialist_is_unresolved(self):
        pending = {
            "from": "coordinator",
            "to": "testing",
            "payload": {
                "status": "HANDOFF",
                "next_agent": "testing",
                "task": "Add RED coverage",
                "reason": "Testing timed out",
            },
        }
        self.write_state(pending)
        decision = (
            'HERMES_RESULT={"status":"AWAIT_USER_DECISION",'
            '"question":"Should we change scope?"}'
        )

        with self.assertRaisesRegex(
            InvalidAgentResult,
            "cannot await user decision while a specialist handoff is unresolved",
        ):
            self.invoke(FakeRunner(decision))

        self.assertEqual(self.state()["pending"], pending)

    def test_draft_pr_cannot_become_user_merge_pending(self):
        self.write_state(
            {
                "from": "review",
                "to": "coordinator",
                "payload": {"status": "REVIEW_CLEAN"},
            }
        )
        merge_result = (
            'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
            f'"reviewed_head":"{self.head}","draft":false}}'
        )
        with (
            patch("run_codex._current_pr_body_hash", return_value="body-v1"),
            patch("run_codex._current_pr_head", return_value=self.head),
            patch("run_codex._current_pr_is_draft", return_value=True),
            patch("run_codex._publish_handoff_trace") as publish,
        ):
            with self.assertRaisesRegex(InvalidAgentResult, "actual GitHub PR to be ready"):
                self.invoke(FakeRunner(merge_result), task="ask for merge")

        self.assertIsNone(self.state()["pending"])
        publish.assert_called_once()
        self.assertEqual(
            (publish.call_args.args[2]["from"], publish.call_args.args[2]["to"]),
            ("review", "coordinator"),
        )

    def test_clean_merge_readiness_still_uses_existing_certification_checks(self):
        self.write_state(None)
        merge_result = (
            'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
            f'"reviewed_head":"{self.head}","draft":false}}'
        )
        with (
            patch("run_codex._current_pr_body_hash", return_value="body-v1"),
            patch("run_codex._current_pr_head", return_value=self.head),
            patch("run_codex._current_pr_is_draft", return_value=False),
            patch("run_codex._publish_handoff_trace"),
        ):
            result = self.invoke(FakeRunner(merge_result), task="ask for merge")

        self.assertEqual(result["status"], "AWAIT_USER_MERGE")
        self.assertEqual(self.state()["pending"]["to"], "user")


if __name__ == "__main__":
    unittest.main()
