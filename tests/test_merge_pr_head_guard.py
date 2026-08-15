import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from run_codex import main


class MergePrHeadGuardTests(unittest.TestCase):
    def run_main(self, result, current_pr_head):
        with tempfile.TemporaryDirectory() as tempdir:
            repo = Path(tempdir).resolve()
            stdout = io.StringIO()
            stderr = io.StringIO()
            args = [
                "--agent",
                "coordinator",
                "--workflow",
                "issue-11",
                "--repo",
                str(repo),
                "--task",
                "check merge readiness",
            ]
            with (
                patch("run_codex.invoke_agent", return_value=result),
                patch("run_codex._current_pr_head", return_value=current_pr_head),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(args)
            return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_matching_pr_head_allows_merge_readiness(self):
        result = {
            "status": "AWAIT_USER_MERGE",
            "reviewed_head": "sha-a",
            "draft": False,
        }

        exit_code, stdout, stderr = self.run_main(result, "sha-a")

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout), result)
        self.assertEqual(stderr, "")

    def test_moved_pr_head_blocks_merge_readiness(self):
        result = {
            "status": "AWAIT_USER_MERGE",
            "reviewed_head": "sha-a",
            "draft": False,
        }

        exit_code, stdout, stderr = self.run_main(result, "sha-b")

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["error_code"], "MERGE_PR_HEAD_MISMATCH")
        self.assertEqual(error["reviewed_head"], "sha-a")
        self.assertEqual(error["current_pr_head"], "sha-b")


if __name__ == "__main__":
    unittest.main()
