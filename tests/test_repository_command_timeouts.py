import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from run_codex import (
    DEFAULT_REPOSITORY_COMMAND_TIMEOUT_SECONDS,
    _gh,
    _git,
    main,
)


class RepositoryCommandTimeoutTests(unittest.TestCase):
    def test_git_uses_repository_command_timeout(self):
        completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
        with patch("run_codex.subprocess.run", return_value=completed) as run:
            _git(Path("/tmp/example-repo"), "status", allow_failure=True)

        self.assertEqual(
            run.call_args.kwargs["timeout"], DEFAULT_REPOSITORY_COMMAND_TIMEOUT_SECONDS
        )

    def test_gh_uses_repository_command_timeout(self):
        completed = subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")
        with patch("run_codex.subprocess.run", return_value=completed) as run:
            _gh(Path("/tmp/example-repo"), "pr", "view")

        self.assertEqual(
            run.call_args.kwargs["timeout"], DEFAULT_REPOSITORY_COMMAND_TIMEOUT_SECONDS
        )

    def test_git_allow_failure_does_not_swallow_timeout(self):
        timeout = subprocess.TimeoutExpired(["git", "status"], 60)
        with patch("run_codex.subprocess.run", side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired):
                _git(Path("/tmp/example-repo"), "status", allow_failure=True)

    def test_post_agent_guard_timeout_preserves_pending_and_reports_artifacts(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            repo = root / "repo"
            repo.mkdir()
            state_file = root / "state.json"
            prompts = root / "prompts"
            prompts.mkdir()
            (prompts / "coordinator.md").write_text("ROLE:coordinator\n", encoding="utf-8")

            pending = {
                "from": "testing",
                "to": "coordinator",
                "payload": {
                    "status": "RED_COMPLETE",
                    "test_command": "python -m unittest",
                },
            }
            state_file.write_text(
                json.dumps(
                    {
                        "workflow_id": "issue-27",
                        "sessions": {"coordinator": "existing-session"},
                        "pending": pending,
                        "review_certification": None,
                        "task_review_clean_checkpoint": "approved",
                    }
                ),
                encoding="utf-8",
            )

            argv = [
                "--agent",
                "coordinator",
                "--workflow",
                "issue-27",
                "--repo",
                str(repo),
                "--task",
                "consume pending testing result",
                "--state-file",
                str(state_file),
                "--prompt-dir",
                str(prompts),
            ]
            completed = subprocess.CompletedProcess(["codex"], 0, stdout="", stderr="")
            timeout = subprocess.TimeoutExpired(["git", "ls-remote"], 60)

            stderr = io.StringIO()
            with (
                patch("run_codex._worktree_status", side_effect=[[], [" M scripts/nightly-market-data.sh"]]),
                patch(
                    "run_codex._capture_repository_guard",
                    return_value={
                        "head": "abc123",
                        "branch": "feature",
                        "staged": "",
                        "remote_checked": True,
                        "remote_head": "abc123",
                    },
                ),
                patch("run_codex._verify_dispatch_bridge"),
                patch("run_codex._publish_handoff_trace"),
                patch("run_codex._default_runner", return_value=completed),
                patch("run_codex._verify_agent_did_not_mutate_repository", side_effect=timeout),
                redirect_stderr(stderr),
            ):
                rc = main(argv)

            self.assertEqual(rc, 2)
            payload = json.loads(stderr.getvalue())
            self.assertEqual(
                payload["unverified_artifacts"], [" M scripts/nightly-market-data.sh"]
            )
            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["pending"], pending)


if __name__ == "__main__":
    unittest.main()
