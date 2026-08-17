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
        self.calls = []

    def __call__(self, command, cwd, input_text):
        self.calls.append(input_text)
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
        value = invoke_agent(
            agent=agent, workflow_id="issue-17", repo=self.repo, task=task,
            state_file=self.state_file, prompt_dir=self.prompts, runner=runner,
        )
        return value, runner

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def prime_task_review(self):
        self.invoke("coordinator", HANDOFF_RESULT)
        return self.state()["pending"]

    def test_coordinator_handoff_accepts_before_trace_dispatch(self):
        with patch("run_codex._publish_handoff_trace") as publish:
            self.invoke("coordinator", HANDOFF_RESULT)
        publish.assert_not_called()
        self.assertEqual(self.state()["pending"]["to"], "task_review")

    def test_dispatch_trace_failure_blocks_task_review_without_consuming_pending(self):
        original = self.prime_task_review()
        runner = FakeRunner(codex_stdout("TR17", "HERMES_RESULT=" + json.dumps(CLEAN_RESULT)))
        with patch("run_codex._publish_handoff_trace", side_effect=InvalidAgentResult("trace failed")):
            with self.assertRaises(InvalidAgentResult):
                invoke_agent(
                    agent="task_review", workflow_id="issue-17", repo=self.repo, task="external",
                    state_file=self.state_file, prompt_dir=self.prompts, runner=runner,
                )
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.state()["pending"], original)

    def test_clean_result_trace_carries_checkpoint_and_exact_result_at_coordinator_dispatch(self):
        self.prime_task_review()
        self.invoke("task_review", CLEAN_RESULT)
        with patch("run_codex._publish_handoff_trace") as publish:
            self.invoke("coordinator", {"status": "BLOCKED", "summary": "done"})
        handoff = publish.call_args.args[2]
        checkpoint = publish.call_args.kwargs["task_checkpoint"]
        self.assertEqual(handoff["from"], "task_review")
        self.assertEqual(handoff["to"], "coordinator")
        self.assertEqual(
            {key: value for key, value in handoff["payload"].items() if key != "task_review_checkpoint"},
            CLEAN_RESULT,
        )
        self.assertTrue(checkpoint)
        self.assertEqual(handoff["payload"]["task_review_checkpoint"], checkpoint)

    def test_changes_required_trace_carries_same_reviewed_checkpoint(self):
        self.prime_task_review()
        self.invoke("task_review", CHANGES_RESULT)
        with patch("run_codex._publish_handoff_trace") as publish:
            self.invoke("coordinator", {"status": "BLOCKED", "summary": "done"})
        handoff = publish.call_args.args[2]
        checkpoint = publish.call_args.kwargs["task_checkpoint"]
        self.assertEqual(
            {key: value for key, value in handoff["payload"].items() if key != "task_review_checkpoint"},
            CHANGES_RESULT,
        )
        self.assertTrue(checkpoint)
        self.assertEqual(handoff["payload"]["task_review_checkpoint"], checkpoint)

    def test_reverse_handoff_remains_accepted_when_dispatch_trace_fails(self):
        self.prime_task_review()
        self.invoke("task_review", CLEAN_RESULT)
        accepted = self.state()["pending"]
        runner = FakeRunner(codex_stdout("C17", 'HERMES_RESULT={"status":"BLOCKED","summary":"done"}'))
        with patch("run_codex._publish_handoff_trace", side_effect=InvalidAgentResult("trace failed")):
            with self.assertRaises(InvalidAgentResult):
                invoke_agent(
                    agent="coordinator", workflow_id="issue-17", repo=self.repo, task="external",
                    state_file=self.state_file, prompt_dir=self.prompts, runner=runner,
                )
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.state()["pending"], accepted)
        self.assertIsNotNone(self.state()["task_review_clean_checkpoint"])


if __name__ == "__main__":
    unittest.main()
