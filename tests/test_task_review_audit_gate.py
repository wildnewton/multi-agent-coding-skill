import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import InvalidAgentResult, invoke_agent


def codex_stdout(thread_id, final_message):
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final_message}},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeRunner:
    def __init__(self, output):
        self.output = output
    def __call__(self, command, cwd, input_text):
        return subprocess.CompletedProcess(command, 0, stdout=self.output, stderr="")


TASK = "Review issue 17"
HANDOFF_RESULT = {
    "status": "HANDOFF",
    "next_agent": "task_review",
    "task": TASK,
    "reason": "independent review",
}
CLEAN_RESULT = {
    "status": "TASK_REVIEW_CLEAN",
    "evidence_and_root_cause": "confirmed",
    "clearer_requirement": "clear",
    "acceptance_criteria": "testable",
    "simplest_approach": "minimal",
}
CHANGES_RESULT = {**CLEAN_RESULT, "status": "CHANGES_REQUIRED"}


class TaskReviewAuditGateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.repo = root / "repo"; self.repo.mkdir()
        self.state_file = root / "state.json"
        self.prompts = root / "prompts"; self.prompts.mkdir()
        for role in ("testing", "coordinator", "task_review", "review"):
            (self.prompts / f"{role}.md").write_text(f"ROLE:{role}\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)
        self.state_file.write_text(json.dumps({
            "workflow_id": "issue-17", "sessions": {}, "pending": None,
            "task_review_clean_checkpoint": None, "review_certification": None,
        }), encoding="utf-8")

    def invoke(self, agent, result, task="external"):
        runner = FakeRunner(codex_stdout("T17", "HERMES_RESULT=" + json.dumps(result)))
        return invoke_agent(
            agent=agent, workflow_id="issue-17", repo=self.repo, task=task,
            state_file=self.state_file, prompt_dir=self.prompts, runner=runner,
        )

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def prime_task_review(self):
        self.invoke("coordinator", HANDOFF_RESULT)
        return self.state()["pending"]

    def test_coordinator_handoff_is_not_accepted_when_trace_publish_fails(self):
        before = self.state()
        with patch("run_codex._publish_handoff_trace", side_effect=InvalidAgentResult("trace failed")):
            with self.assertRaises(InvalidAgentResult):
                self.invoke("coordinator", HANDOFF_RESULT)
        self.assertEqual(self.state(), before)

    def test_clean_result_trace_carries_checkpoint_and_exact_result(self):
        self.prime_task_review()
        with patch("run_codex._publish_handoff_trace") as publish:
            self.invoke("task_review", CLEAN_RESULT)
        handoff = publish.call_args.args[2]
        self.assertEqual(handoff["from"], "task_review")
        self.assertEqual(handoff["to"], "coordinator")
        self.assertEqual(handoff["payload"], CLEAN_RESULT)
        self.assertTrue(publish.call_args.kwargs["task_checkpoint"])

    def test_changes_required_is_a_completed_traced_handoff(self):
        self.prime_task_review()
        with patch("run_codex._publish_handoff_trace") as publish:
            self.invoke("task_review", CHANGES_RESULT)
        self.assertEqual(self.state()["pending"]["payload"]["status"], "CHANGES_REQUIRED")
        self.assertEqual(publish.call_args.args[2]["payload"], CHANGES_RESULT)

    def test_specialist_result_is_not_accepted_when_trace_publish_fails(self):
        original = self.prime_task_review()
        with patch("run_codex._publish_handoff_trace", side_effect=InvalidAgentResult("trace failed")):
            with self.assertRaises(InvalidAgentResult):
                self.invoke("task_review", CLEAN_RESULT)
        self.assertEqual(self.state()["pending"], original)
        self.assertIsNone(self.state()["task_review_clean_checkpoint"])


if __name__ == "__main__":
    unittest.main()
