import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import InvalidAgentResult, _task_review_checkpoint, invoke_agent


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

    def _assert_pending_recoverable(self, checkpoint, *, clean):
        state = self._state()
        self.assertEqual(state["pending_agent"], "task_review")
        self.assertTrue(state["pending_result_ready"])
        self.assertEqual(state["pending_task_review_checkpoint"], checkpoint)
        if clean:
            self.assertEqual(state["task_review_clean_checkpoint"], checkpoint)
        else:
            self.assertIsNone(state["task_review_clean_checkpoint"])

    def test_completed_task_review_returns_checkpoint_for_issue_comment(self):
        task = "Review issue 17 audit requirement"
        checkpoint = _task_review_checkpoint(task)
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-17",
                    "sessions": {},
                    "pending_agent": "task_review",
                    "pending_result_ready": False,
                    "review_clean_head": None,
                    "pending_task_review_checkpoint": checkpoint,
                    "task_review_clean_checkpoint": None,
                }
            ),
            encoding="utf-8",
        )
        review_result = {
            "status": "TASK_REVIEW_CLEAN",
            "evidence_and_root_cause": "The missing audit trace is confirmed.",
            "clearer_requirement": "Require a verified Issue comment before completion.",
            "acceptance_criteria": "Completion fails closed without matching evidence.",
            "simplest_approach": "Reuse the existing checkpoint and completion handshake.",
        }

        def task_review_runner(command, cwd, input_text):
            payload = {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "HERMES_RESULT=" + json.dumps(review_result),
                },
            }
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(payload) + "\n", stderr=""
            )

        result = invoke_agent(
            agent="task_review",
            workflow_id="issue-17",
            repo=self.repo,
            task=task,
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=task_review_runner,
        )

        self.assertEqual(result["task_review_checkpoint"], checkpoint)

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
        checkpoint = self._prime_pending(clean=True)
        with self.assertRaises(InvalidAgentResult):
            self._invoke()
        self._assert_pending_recoverable(checkpoint, clean=True)

    def test_nonexistent_comment_fails_closed(self):
        checkpoint = self._prime_pending(clean=True)
        with patch(
            "run_codex._fetch_issue_comment",
            side_effect=InvalidAgentResult("Task Review audit comment was not found"),
        ):
            with self.assertRaises(InvalidAgentResult):
                self._invoke(comment_id=103)
        self._assert_pending_recoverable(checkpoint, clean=True)

    def test_wrong_issue_fails_closed(self):
        checkpoint = self._prime_pending(clean=True)
        with patch(
            "run_codex._fetch_issue_comment",
            return_value=self._comment(checkpoint, "TASK_REVIEW_CLEAN", issue=18),
        ):
            with self.assertRaises(InvalidAgentResult):
                self._invoke(comment_id=104)
        self._assert_pending_recoverable(checkpoint, clean=True)

    def test_wrong_checkpoint_fails_closed(self):
        checkpoint = self._prime_pending(clean=True)
        with patch(
            "run_codex._fetch_issue_comment",
            return_value=self._comment("stale-checkpoint", "TASK_REVIEW_CLEAN"),
        ):
            with self.assertRaises(InvalidAgentResult):
                self._invoke(comment_id=105)
        self._assert_pending_recoverable(checkpoint, clean=True)

    def test_wrong_verdict_fails_closed(self):
        checkpoint = self._prime_pending(clean=True)
        with patch(
            "run_codex._fetch_issue_comment",
            return_value=self._comment(checkpoint, "CHANGES_REQUIRED"),
        ):
            with self.assertRaises(InvalidAgentResult):
                self._invoke(comment_id=106)
        self._assert_pending_recoverable(checkpoint, clean=True)


if __name__ == "__main__":
    unittest.main()
