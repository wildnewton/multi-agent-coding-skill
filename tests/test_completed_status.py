import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import AgentRepositoryMutationError, InvalidAgentResult, invoke_agent


def codex_stdout(thread_id, final_message):
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final_message}},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeRunner:
    def __init__(self, final_message, *, edit=None):
        self.final_message = final_message
        self.edit = edit

    def __call__(self, command, cwd, input_text):
        if self.edit is not None:
            (Path(cwd) / self.edit).write_text("changed\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_stdout("C-completed", self.final_message),
            stderr="",
        )


COMPLETED = 'HERMES_RESULT={"status":"COMPLETED","report":"No implementation is required."}'
TASK_REVIEW_HANDOFF = (
    'HERMES_RESULT={"status":"HANDOFF","next_agent":"task_review",'
    '"task":"Verify whether the issue still requires implementation",'
    '"reason":"Need independent task validation"}'
)
TASK_REVIEW_CHANGES = (
    'HERMES_RESULT={"status":"CHANGES_REQUIRED",'
    '"evidence_and_root_cause":"Current behavior shows the reported issue no longer exists.",'
    '"clearer_requirement":"Do not implement a redundant fix.",'
    '"acceptance_criteria":"Close with verified no-change evidence.",'
    '"simplest_approach":"Return the finding to Coordinator without implementation."}'
)
TASK_REVIEW_CLEAN = (
    'HERMES_RESULT={"status":"TASK_REVIEW_CLEAN",'
    '"evidence_and_root_cause":"The issue is confirmed.",'
    '"clearer_requirement":"Implement the confirmed fix.",'
    '"acceptance_criteria":"The confirmed behavior is corrected.",'
    '"simplest_approach":"Make the smallest implementation change."}'
)


class CompletedStatusTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state_file = self.root / "state.json"
        self.prompts = self.root / "prompts"
        self.prompts.mkdir()
        for role in ("testing", "coordinator", "task_review", "review"):
            (self.prompts / f"{role}.md").write_text(f"ROLE:{role}\n", encoding="utf-8")
        self._git("init")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")
        self.write_state()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True
        )

    def write_state(self, *, clean=None, pending=None, sessions=None):
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-23",
                    "sessions": sessions or {},
                    "pending": pending,
                    "task_review_clean_checkpoint": clean,
                    "review_certification": None,
                }
            ),
            encoding="utf-8",
        )

    def invoke(self, agent, final_message, *, task="do the task", edit=None):
        return invoke_agent(
            agent=agent,
            workflow_id="issue-23",
            repo=self.repo,
            task=task,
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=FakeRunner(final_message, edit=edit),
        )

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_direct_completion_returns_report_and_clears_pending(self):
        result = self.invoke("coordinator", COMPLETED)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["report"], "No implementation is required.")
        self.assertIsNone(self.state()["pending"])

    def test_completion_requires_report(self):
        with self.assertRaisesRegex(InvalidAgentResult, "non-empty report"):
            self.invoke("coordinator", 'HERMES_RESULT={"status":"COMPLETED"}')

    def test_user_decision_can_resume_to_completion(self):
        self.invoke(
            "coordinator",
            'HERMES_RESULT={"status":"AWAIT_USER_DECISION","question":"Accept evidence?","summary":"Evidence is sufficient."}',
        )
        self.assertEqual(self.state()["pending"]["to"], "user")

        result = self.invoke("coordinator", COMPLETED, task="Accept the evidence")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertIsNone(self.state()["pending"])

    def test_task_review_changes_required_can_return_to_completion(self):
        self.invoke("coordinator", TASK_REVIEW_HANDOFF)
        self.invoke("task_review", TASK_REVIEW_CHANGES)
        self.assertEqual(self.state()["pending"]["from"], "task_review")
        self.assertIsNone(self.state()["task_review_clean_checkpoint"])

        result = self.invoke("coordinator", COMPLETED)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertIsNone(self.state()["pending"])

    def test_unresolved_specialist_handoff_blocks_completion(self):
        self.invoke("coordinator", TASK_REVIEW_HANDOFF)
        with self.assertRaisesRegex(InvalidAgentResult, "unresolved specialist"):
            self.invoke("coordinator", COMPLETED, task="recovery")

    def test_task_review_clean_checkpoint_blocks_completion(self):
        self.invoke("coordinator", TASK_REVIEW_HANDOFF)
        self.invoke("task_review", TASK_REVIEW_CLEAN)
        self.assertIsNotNone(self.state()["task_review_clean_checkpoint"])

        with self.assertRaisesRegex(InvalidAgentResult, "TASK_REVIEW_CLEAN"):
            self.invoke("coordinator", COMPLETED)

    def test_current_pr_blocks_completion(self):
        with patch("run_codex._current_pr_number", return_value=99):
            with self.assertRaisesRegex(InvalidAgentResult, "implementation-stage PR"):
                self.invoke("coordinator", COMPLETED)

    def test_repository_edit_blocks_completion(self):
        with self.assertRaises(AgentRepositoryMutationError):
            self.invoke("coordinator", COMPLETED, edit="unexpected.txt")


if __name__ == "__main__":
    unittest.main()
