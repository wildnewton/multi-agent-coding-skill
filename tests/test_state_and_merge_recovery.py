import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import InvalidAgentResult, _load_state, invoke_agent


def codex_stdout(thread_id, final_message):
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final_message}},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeRunner:
    def __init__(self, final_message):
        self.final_message = final_message
        self.calls = []

    def __call__(self, command, cwd, input_text):
        self.calls.append(input_text)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_stdout("C-recovery", self.final_message),
            stderr="",
        )


class StateAndMergeRecoveryTests(unittest.TestCase):
    def test_legacy_state_is_rejected_instead_of_migrated(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_file = Path(tempdir) / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "workflow_id": "issue-21",
                        "sessions": {},
                        "pending_agent": None,
                        "review_clean_head": "legacy-head",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "legacy workflow state is unsupported"):
                _load_state(state_file, "issue-21")

    def test_stale_pr_body_releases_consumed_review_result_for_recovery(self):
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
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()

            prompts = root / "prompts"
            prompts.mkdir()
            (prompts / "coordinator.md").write_text("ROLE:coordinator\n", encoding="utf-8")
            state_file = root / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "workflow_id": "issue-21",
                        "sessions": {"coordinator": "C-recovery"},
                        "pending": {
                            "from": "review",
                            "to": "coordinator",
                            "payload": {"status": "REVIEW_CLEAN"},
                        },
                        "task_review_clean_checkpoint": "approved",
                        "review_certification": {
                            "head": head,
                            "pr_body_hash": "body-v1",
                        },
                    }
                ),
                encoding="utf-8",
            )

            merge_result = (
                'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
                f'"reviewed_head":"{head}","draft":false}}'
            )
            with patch("run_codex._current_pr_body_hash", return_value="body-v2"):
                with self.assertRaisesRegex(InvalidAgentResult, "PR description"):
                    invoke_agent(
                        agent="coordinator",
                        workflow_id="issue-21",
                        repo=repo,
                        task="ignored while consuming review result",
                        state_file=state_file,
                        prompt_dir=prompts,
                        runner=FakeRunner(merge_result),
                    )

            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIsNone(state["pending"])

            recovery = FakeRunner(
                'HERMES_RESULT={"status":"HANDOFF","next_agent":"review",'
                '"task":"Review the corrected PR description","reason":"PR body changed"}'
            )
            invoke_agent(
                agent="coordinator",
                workflow_id="issue-21",
                repo=repo,
                task="PR description changed after Review; request fresh Review",
                state_file=state_file,
                prompt_dir=prompts,
                runner=recovery,
            )
            self.assertIn("PR description changed after Review", recovery.calls[0])


if __name__ == "__main__":
    unittest.main()
