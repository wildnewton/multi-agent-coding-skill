import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from run_codex import (
    AgentRepositoryMutationError,
    CodexInvocationError,
    InvalidAgentResult,
    invoke_agent,
)


def codex_stdout(thread_id, final_message):
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final_message}},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, command, cwd, input_text):
        self.calls.append((command, Path(cwd), input_text))
        return subprocess.CompletedProcess(command, 0, stdout=self.outputs.pop(0), stderr="")


TASK_REVIEW_TASK = "Review issue 13\nRequirement: add Task Review\nAC: gate implementation"
TASK_REVIEW_HANDOFF = "HERMES_RESULT=" + json.dumps(
    {
        "status": "HANDOFF",
        "next_agent": "task_review",
        "task": TASK_REVIEW_TASK,
        "reason": "Task must be independently validated before implementation",
    }
)
TASK_REVIEW_CLEAN = (
    'HERMES_RESULT={"status":"TASK_REVIEW_CLEAN",'
    '"evidence_and_root_cause":"The gap is confirmed in the runner.",'
    '"clearer_requirement":"Add a pre-implementation Task Review gate.",'
    '"acceptance_criteria":"Task Review must be clean before Testing or Review.",'
    '"simplest_approach":"Reuse the pending handoff and keep one clean checkpoint."}'
)
TASK_REVIEW_CHANGES = (
    'HERMES_RESULT={"status":"CHANGES_REQUIRED",'
    '"evidence_and_root_cause":"The task does not define stale-cert invalidation.",'
    '"clearer_requirement":"Invalidate prior certification on a new Task Review handoff.",'
    '"acceptance_criteria":"A new handoff clears the prior clean checkpoint.",'
    '"simplest_approach":"Clear the clean checkpoint when routing to Task Review."}'
)
TESTING_HANDOFF = (
    'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing",'
    '"task":"Add RED for the approved behavior",'
    '"reason":"The task contract is approved"}'
)
REVIEW_HANDOFF = (
    'HERMES_RESULT={"status":"HANDOFF","next_agent":"review",'
    '"task":"Review the implementation","reason":"GREEN is ready",'
    '"full_test_command":"python -m unittest discover -s tests"}'
)


class TaskReviewGateTests(unittest.TestCase):
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

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, text=True, capture_output=True)

    def invoke(self, agent, runner, task="do the task"):
        return invoke_agent(
            agent=agent,
            workflow_id="issue-13",
            repo=self.repo,
            task=task,
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=runner,
        )

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def write_state(self, *, clean=None, review_certification=None, pending=None):
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-13",
                    "sessions": {},
                    "pending": pending,
                    "task_review_clean_checkpoint": clean,
                    "review_certification": review_certification,
                }
            ),
            encoding="utf-8",
        )

    def handoff_task_review(self):
        runner = FakeRunner([codex_stdout("C13", TASK_REVIEW_HANDOFF)])
        result = self.invoke("coordinator", runner)
        self.assertEqual(result["next_agent"], "task_review")
        return runner

    def complete_clean_task_review(self):
        self.handoff_task_review()
        runner = FakeRunner([codex_stdout("TR13", TASK_REVIEW_CLEAN)])
        result = self.invoke("task_review", runner, task="ignored external task")
        self.assertEqual(result["status"], "TASK_REVIEW_CLEAN")
        return runner

    def test_pre_clean_coordinator_is_read_only(self):
        def editing_runner(command, cwd, input_text):
            (Path(cwd) / "production.py").write_text("changed\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=codex_stdout("C13", TASK_REVIEW_HANDOFF), stderr="")

        with self.assertRaises(AgentRepositoryMutationError):
            self.invoke("coordinator", editing_runner)

    def test_pre_clean_coordinator_cannot_bypass_to_testing_or_review(self):
        for output in (TESTING_HANDOFF, REVIEW_HANDOFF):
            with self.subTest(output=output):
                runner = FakeRunner([codex_stdout("C13", output)])
                with self.assertRaises(InvalidAgentResult):
                    self.invoke("coordinator", runner)
                if self.state_file.exists():
                    self.state_file.unlink()

    def test_task_review_handoff_is_single_pending_and_invalidates_certifications(self):
        self.write_state(
            clean="old-clean",
            review_certification={"head": "old-head", "pr_body_hash": "old-body"},
        )
        self.handoff_task_review()
        state = self.state()
        self.assertEqual(state["pending"]["to"], "task_review")
        self.assertEqual(state["pending"]["payload"]["task"], TASK_REVIEW_TASK)
        self.assertIsNone(state["task_review_clean_checkpoint"])
        self.assertIsNone(state["review_certification"])
        self.assertNotIn("pending_task_review_checkpoint", state)

    def test_task_review_clean_requires_all_review_fields(self):
        self.handoff_task_review()
        runner = FakeRunner([codex_stdout("TR13", 'HERMES_RESULT={"status":"TASK_REVIEW_CLEAN"}')])
        with self.assertRaises(InvalidAgentResult):
            self.invoke("task_review", runner)
        self.assertIsNone(self.state()["task_review_clean_checkpoint"])

    def test_changes_required_returns_to_coordinator_and_keeps_gate_closed(self):
        self.handoff_task_review()
        runner = FakeRunner([codex_stdout("TR13", TASK_REVIEW_CHANGES)])
        self.invoke("task_review", runner)
        state = self.state()
        self.assertEqual((state["pending"]["from"], state["pending"]["to"]), ("task_review", "coordinator"))
        self.assertIsNone(state["task_review_clean_checkpoint"])

        retry = "HERMES_RESULT=" + json.dumps(
            {
                "status": "HANDOFF",
                "next_agent": "task_review",
                "task": "Review revised issue 13",
                "reason": "Task was revised",
            }
        )
        coordinator = FakeRunner([codex_stdout("C13", retry)])
        self.invoke("coordinator", coordinator, task="manual copy must be ignored")
        self.assertEqual(self.state()["pending"]["to"], "task_review")

    def test_task_review_clean_unlocks_testing_handoff(self):
        self.complete_clean_task_review()
        self.assertIsNotNone(self.state()["task_review_clean_checkpoint"])
        coordinator = FakeRunner([codex_stdout("C13", TESTING_HANDOFF)])
        result = self.invoke("coordinator", coordinator, task="manual copy must be ignored")
        self.assertEqual(result["next_agent"], "testing")
        self.assertEqual(self.state()["pending"]["to"], "testing")

    def test_task_review_clean_unlocks_coordinator_edits(self):
        self.complete_clean_task_review()

        def editing_coordinator(command, cwd, input_text):
            (Path(cwd) / "production.py").write_text("implementation\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=codex_stdout("C13", REVIEW_HANDOFF), stderr="")

        result = self.invoke("coordinator", editing_coordinator)
        self.assertEqual(result["next_agent"], "review")
        self.assertTrue((self.repo / "production.py").exists())

    def test_existing_clean_coordinator_cannot_edit_while_handing_back_to_task_review(self):
        self.write_state(clean="old-clean")

        def editing_runner(command, cwd, input_text):
            (Path(cwd) / "production.py").write_text("premature change\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=codex_stdout("C13", TASK_REVIEW_HANDOFF), stderr="")

        with self.assertRaises(AgentRepositoryMutationError):
            self.invoke("coordinator", editing_runner)

    def test_task_review_is_fresh_every_time(self):
        self.handoff_task_review()
        first = FakeRunner([codex_stdout("TR1", TASK_REVIEW_CHANGES)])
        self.invoke("task_review", first)
        retry = "HERMES_RESULT=" + json.dumps(
            {"status": "HANDOFF", "next_agent": "task_review", "task": "revised task", "reason": "revise"}
        )
        coordinator = FakeRunner([codex_stdout("C13", retry)])
        self.invoke("coordinator", coordinator)
        second = FakeRunner([codex_stdout("TR2", TASK_REVIEW_CLEAN)])
        self.invoke("task_review", second)
        self.assertEqual(first.calls[0][0], ["codex", "exec", "--json", "-"])
        self.assertEqual(second.calls[0][0], ["codex", "exec", "--json", "-"])
        self.assertNotIn("task_review", self.state()["sessions"])

    def test_task_review_is_read_only(self):
        self.handoff_task_review()

        def editing_runner(command, cwd, input_text):
            (Path(cwd) / "review-edit.txt").write_text("changed\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=codex_stdout("TR13", TASK_REVIEW_CLEAN), stderr="")

        with self.assertRaises(AgentRepositoryMutationError):
            self.invoke("task_review", editing_runner)
        self.assertIsNone(self.state()["task_review_clean_checkpoint"])

    def test_task_review_blocked_keeps_original_pending_and_gate_closed(self):
        self.handoff_task_review()
        before = self.state()["pending"]
        runner = FakeRunner([codex_stdout("TR13", 'HERMES_RESULT={"status":"BLOCKED","summary":"Missing evidence"}')])
        result = self.invoke("task_review", runner)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(self.state()["pending"], before)
        self.assertIsNone(self.state()["task_review_clean_checkpoint"])

    def test_timeout_cannot_create_task_review_certification(self):
        self.handoff_task_review()
        before = self.state()["pending"]

        def timeout_runner(command, cwd, input_text):
            raise subprocess.TimeoutExpired(command, 10)

        with self.assertRaises(CodexInvocationError):
            self.invoke("task_review", timeout_runner)
        self.assertEqual(self.state()["pending"], before)
        self.assertIsNone(self.state()["task_review_clean_checkpoint"])

    def test_task_review_specialist_cannot_route(self):
        self.handoff_task_review()
        routed = TASK_REVIEW_CLEAN[:-1] + ',"next_agent":"testing"}'
        runner = FakeRunner([codex_stdout("TR13", routed)])
        with self.assertRaises(InvalidAgentResult):
            self.invoke("task_review", runner)


if __name__ == "__main__":
    unittest.main()
