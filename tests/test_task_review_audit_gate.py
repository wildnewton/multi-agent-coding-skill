import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import InvalidAgentResult, invoke_agent


class FakeRunner:
    def __call__(self, command, cwd, input_text):
        events = [
            {"type": "thread.started", "thread_id": "C17"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": 'HERMES_RESULT={"status":"AWAIT_USER_DECISION","question":"continue?"}',
                },
            },
        ]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(json.dumps(event) for event in events) + "\n",
            stderr="",
        )


class TaskReviewAuditGateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.state_file = root / "state.json"
        self.prompts = root / "prompts"
        self.prompts.mkdir()
        for role in ("testing", "coordinator", "task_review", "review"):
            (self.prompts / f"{role}.md").write_text(
                f"ROLE:{role}\n", encoding="utf-8"
            )
        self._git("init")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self.runner = FakeRunner()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True
        )

    def _prime_pending(self, *, clean):
        checkpoint = "checkpoint-17"
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-17",
                    "sessions": {},
                    "pending_agent": "task_review",
                    "pending_result_ready": True,
                    "review_clean_head": None,
                    "pending_task_review_checkpoint": checkpoint,
                    "task_review_clean_checkpoint": checkpoint if clean else None,
                }
            ),
            encoding="utf-8",
        )
        return checkpoint

    def _invoke(self, *, comment_id=None):
        return invoke_agent(
            agent="coordinator",
            workflow_id="issue-17",
            repo=self.repo,
            task="resume after Task Review",
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=self.runner,
            completed_agent="task_review",
            task_review_comment_id=comment_id,
        )

    def _comment(self, checkpoint, verdict, issue=17):
        return {
            "issue_url": f"https://api.github.com/repos/example/repo/issues/{issue}",
            "body": (
                f"### Task Review — {verdict}\n\n"
                f"Task checkpoint: `{checkpoint}`\n"
                f"Verdict: `{verdict}`\n"
            ),
        }

    def _state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_clean_completion_requires_matching_issue_comment(self):
        checkpoint = self._prime_pending(clean=True)
        with patch(
            "run_codex._fetch_issue_comment",
            return_value=self._comment(checkpoint, "TASK_REVIEW_CLEAN"),
        ):
            result = self._invoke(comment_id=101)
        self.assertEqual(result["status"], "AWAIT_USER_DECISION")
        self.assertIsNone(self._state()["pending_agent"])

    def test_changes_required_completion_requires_matching_issue_comment(self):
        checkpoint = self._prime_pending(clean=False)
        with patch(
            "run_codex._fetch_issue_comment",
            return_value=self._comment(checkpoint, "CHANGES_REQUIRED"),
        ):
            self._invoke(comment_id=102)
        self.assertIsNone(self._state()["pending_agent"])

    def test_missing_comment_id_fails_closed(self):
        self._prime_pending(clean=True)
        with self.assertRaises(InvalidAgentResult):
            self._invoke()
        self.assertEqual(self._state()["pending_agent"], "task_review")

    def test_nonexistent_comment_fails_closed(self):
        self._prime_pending(clean=True)
        with patch(
            "run_codex._fetch_issue_comment",
            side_effect=InvalidAgentResult("Task Review audit comment was not found"),
        ):
            with self.assertRaises(InvalidAgentResult):
                self._invoke(comment_id=103)
        self.assertEqual(self._state()["pending_agent"], "task_review")

    def test_wrong_issue_fails_closed(self):
        checkpoint = self._prime_pending(clean=True)
        with patch(
            "run_codex._fetch_issue_comment",
            return_value=self._comment(checkpoint, "TASK_REVIEW_CLEAN", issue=18),
        ):
            with self.assertRaises(InvalidAgentResult):
                self._invoke(comment_id=104)
        self.assertEqual(self._state()["pending_agent"], "task_review")

    def test_wrong_checkpoint_fails_closed(self):
        self._prime_pending(clean=True)
        with patch(
            "run_codex._fetch_issue_comment",
            return_value=self._comment("stale-checkpoint", "TASK_REVIEW_CLEAN"),
        ):
            with self.assertRaises(InvalidAgentResult):
                self._invoke(comment_id=105)
        self.assertEqual(self._state()["pending_agent"], "task_review")

    def test_wrong_verdict_fails_closed(self):
        checkpoint = self._prime_pending(clean=True)
        with patch(
            "run_codex._fetch_issue_comment",
            return_value=self._comment(checkpoint, "CHANGES_REQUIRED"),
        ):
            with self.assertRaises(InvalidAgentResult):
                self._invoke(comment_id=106)
        self.assertEqual(self._state()["pending_agent"], "task_review")


if __name__ == "__main__":
    unittest.main()
