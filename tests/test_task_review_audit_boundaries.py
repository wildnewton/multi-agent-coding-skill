import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from run_codex import InvalidAgentResult, _fetch_issue_comment, main


class TaskReviewAuditBoundaryTests(unittest.TestCase):
    def test_fetch_issue_comment_uses_expected_gh_api_endpoint(self):
        repo = Path("/tmp/example-repo")
        payload = {
            "issue_url": "https://api.github.com/repos/example/repo/issues/17",
            "body": "Task checkpoint: `abc`\nVerdict: `TASK_REVIEW_CLEAN`\n",
        }
        completed = subprocess.CompletedProcess(
            ["gh"], 0, stdout=json.dumps(payload), stderr=""
        )

        with (
            patch.dict("run_codex.os.environ", {"GH_REPO": "wrong/repo"}),
            patch("run_codex.subprocess.run", return_value=completed) as run,
        ):
            result = _fetch_issue_comment(repo, 123)

        self.assertEqual(result, payload)
        self.assertEqual(
            run.call_args.args[0],
            ["gh", "api", "repos/{owner}/{repo}/issues/comments/123"],
        )
        self.assertEqual(run.call_args.kwargs["cwd"], repo)
        self.assertEqual(run.call_args.kwargs["env"]["GH_PROMPT_DISABLED"], "1")
        self.assertNotIn("GH_REPO", run.call_args.kwargs["env"])

    def test_fetch_issue_comment_nonzero_fails_closed(self):
        completed = subprocess.CompletedProcess(
            ["gh"], 1, stdout="", stderr="not found"
        )

        with patch("run_codex.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(
                InvalidAgentResult,
                "unable to verify Task Review audit comment 123",
            ):
                _fetch_issue_comment(Path("/tmp/example-repo"), 123)

    def test_cli_forwards_task_review_comment_id(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            args = [
                "--agent",
                "coordinator",
                "--workflow",
                "issue-17",
                "--repo",
                tempdir,
                "--task",
                "resume after Task Review",
                "--completed-agent",
                "task_review",
                "--task-review-comment-id",
                "123",
            ]
            with (
                patch(
                    "run_codex.invoke_agent",
                    return_value={
                        "status": "AWAIT_USER_DECISION",
                        "question": "continue?",
                    },
                ) as invoke,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                rc = main(args)

        self.assertEqual(rc, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "AWAIT_USER_DECISION")
        self.assertEqual(invoke.call_args.kwargs["completed_agent"], "task_review")
        self.assertEqual(invoke.call_args.kwargs["task_review_comment_id"], 123)


if __name__ == "__main__":
    unittest.main()
