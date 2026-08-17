import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import InvalidAgentResult, _current_pr_number, invoke_agent


def codex_stdout(thread_id, final_message):
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final_message}},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeRunner:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def __call__(self, command, cwd, input_text):
        self.calls.append(input_text)
        return subprocess.CompletedProcess(command, 0, stdout=self.output, stderr="")


class PrLookupAndReviewSnapshotTests(unittest.TestCase):
    def test_pr_lookup_failure_is_not_treated_as_no_pr(self):
        repo = Path("/tmp/example-repo")
        branch = subprocess.CompletedProcess(["git"], 0, stdout="feature\n", stderr="")
        failed = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="network failure")
        with (
            patch("run_codex._has_origin", return_value=True),
            patch("run_codex._git", return_value=branch),
            patch("run_codex.subprocess.run", return_value=failed),
        ):
            with self.assertRaisesRegex(InvalidAgentResult, "unable to determine current GitHub PR"):
                _current_pr_number(repo)

    def test_successful_empty_pr_lookup_is_no_pr(self):
        repo = Path("/tmp/example-repo")
        branch = subprocess.CompletedProcess(["git"], 0, stdout="feature\n", stderr="")
        empty = subprocess.CompletedProcess(["gh"], 0, stdout="[]\n", stderr="")
        with (
            patch("run_codex._has_origin", return_value=True),
            patch("run_codex._git", return_value=branch),
            patch("run_codex.subprocess.run", return_value=empty),
        ):
            self.assertIsNone(_current_pr_number(repo))

    def test_review_body_change_during_invocation_cannot_be_certified(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            repo = root / "repo"
            repo.mkdir()
            state_file = root / "state.json"
            prompts = root / "prompts"
            prompts.mkdir()
            for role in ("testing", "coordinator", "task_review", "review"):
                (prompts / f"{role}.md").write_text(f"ROLE:{role}\n", encoding="utf-8")

            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()

            pending = {
                "from": "coordinator",
                "to": "review",
                "payload": {
                    "status": "HANDOFF",
                    "next_agent": "review",
                    "task": "Review GREEN",
                    "reason": "GREEN is ready",
                    "full_test_command": "python -m unittest",
                },
            }
            state_file.write_text(
                json.dumps(
                    {
                        "workflow_id": "issue-21",
                        "sessions": {},
                        "pending": pending,
                        "task_review_clean_checkpoint": "approved",
                        "review_certification": None,
                    }
                ),
                encoding="utf-8",
            )
            runner = FakeRunner(
                codex_stdout("R21", 'HERMES_RESULT={"status":"REVIEW_CLEAN"}')
            )

            with (
                patch("run_codex._has_origin", return_value=True),
                patch("run_codex._current_pr_number", return_value=22),
                patch("run_codex._current_pr_head", return_value=head),
                patch("run_codex._current_pr_body_hash", side_effect=["body-v1", "body-v2"]),
                patch("run_codex._publish_handoff_trace"),
                patch("run_codex._publish_specialist_failure_trace") as failure_trace,
            ):
                with self.assertRaisesRegex(
                    InvalidAgentResult, "PR description changed during Review"
                ):
                    invoke_agent(
                        agent="review",
                        workflow_id="issue-21",
                        repo=repo,
                        task="external",
                        state_file=state_file,
                        prompt_dir=prompts,
                        runner=runner,
                    )

            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["pending"], pending)
            self.assertIsNone(state["review_certification"])
            self.assertEqual(len(runner.calls), 1)
            self.assertIn("PR description changed during Review", failure_trace.call_args.kwargs["reason"])


if __name__ == "__main__":
    unittest.main()
