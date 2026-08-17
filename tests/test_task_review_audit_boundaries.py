import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import (
    InvalidAgentResult,
    _gh_env,
    _publish_handoff_trace,
    _publish_specialist_failure_trace,
)


class TaskReviewAuditBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path("/tmp/example-repo")
        self.handoff = {
            "from": "coordinator",
            "to": "task_review",
            "payload": {
                "status": "HANDOFF",
                "next_agent": "task_review",
                "task": "Review issue 17",
                "reason": "independent review",
            },
        }

    def _publish(self, handoff=None, pr_number=None):
        completed = subprocess.CompletedProcess(["gh"], 0, stdout="ok", stderr="")
        with (
            patch("run_codex._has_origin", return_value=True),
            patch("run_codex._current_pr_number", return_value=pr_number),
            patch("run_codex.subprocess.run", return_value=completed) as run,
        ):
            _publish_handoff_trace(
                self.repo,
                "issue-17",
                handoff or self.handoff,
                head="abc123",
                task_checkpoint="checkpoint-17" if (handoff or self.handoff).get("to") == "task_review" else None,
            )
        return run

    def test_gh_env_ignores_ambient_repo_override(self):
        with patch.dict("run_codex.os.environ", {"GH_REPO": "wrong/repo"}, clear=False):
            env = _gh_env()
        self.assertNotIn("GH_REPO", env)
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")

    def test_task_review_trace_always_uses_issue(self):
        run = self._publish(pr_number=22)
        self.assertEqual(run.call_args.args[0][:4], ["gh", "issue", "comment", "17"])
        self.assertIn("checkpoint-17", run.call_args.args[0][-1])

    def test_task_review_trace_requires_issue_workflow(self):
        with patch("run_codex._has_origin", return_value=True):
            with self.assertRaisesRegex(InvalidAgentResult, "requires an issue-<number> workflow"):
                _publish_handoff_trace(self.repo, "pr-22", self.handoff, head="abc")

    def test_non_task_review_trace_uses_issue_before_pr_exists(self):
        handoff = {"from": "coordinator", "to": "testing", "payload": {"status": "HANDOFF", "task": "RED"}}
        run = self._publish(handoff, pr_number=None)
        self.assertEqual(run.call_args.args[0][:4], ["gh", "issue", "comment", "17"])

    def test_non_task_review_trace_uses_pr_once_pr_exists(self):
        handoff = {"from": "testing", "to": "coordinator", "payload": {"status": "RED_COMPLETE"}}
        run = self._publish(handoff, pr_number=22)
        self.assertEqual(run.call_args.args[0][:4], ["gh", "pr", "comment", "22"])

    def test_task_review_failure_trace_stays_on_issue_even_when_pr_exists(self):
        completed = subprocess.CompletedProcess(["gh"], 0, stdout="ok", stderr="")
        with (
            patch("run_codex._has_origin", return_value=True),
            patch("run_codex._current_pr_number", return_value=22),
            patch("run_codex.subprocess.run", return_value=completed) as run,
        ):
            _publish_specialist_failure_trace(
                self.repo,
                "issue-17",
                self.handoff,
                head="abc123",
                reason="BLOCKED: missing evidence",
            )
        self.assertEqual(run.call_args.args[0][:4], ["gh", "issue", "comment", "17"])
        self.assertIn("BLOCKED: missing evidence", run.call_args.args[0][-1])

    def test_trace_publish_failure_fails_closed(self):
        failed = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="boom")
        with (
            patch("run_codex._has_origin", return_value=True),
            patch("run_codex._current_pr_number", return_value=None),
            patch("run_codex.subprocess.run", return_value=failed),
        ):
            with self.assertRaisesRegex(InvalidAgentResult, "unable to publish workflow handoff trace"):
                _publish_handoff_trace(self.repo, "issue-17", self.handoff, head="abc")


if __name__ == "__main__":
    unittest.main()
