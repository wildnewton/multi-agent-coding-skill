import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from run_codex import CodexInvocationError, invoke_agent


TESTING_TASK = "Add focused RED coverage for issue 21"
TESTING_HANDOFF = "HERMES_RESULT=" + json.dumps(
    {
        "status": "HANDOFF",
        "next_agent": "testing",
        "task": TESTING_TASK,
        "reason": "The reviewed task needs RED coverage",
    }
)
TESTING_RESULT_DICT = {
    "status": "RED_COMPLETE",
    "test_command": "python -m unittest tests.test_executor_handoff",
    "summary": "RED coverage added",
}
TESTING_RESULT = "HERMES_RESULT=" + json.dumps(TESTING_RESULT_DICT)


def codex_stdout(thread_id, final_message):
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": final_message},
        },
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, command, cwd, input_text):
        self.calls.append((command, Path(cwd), input_text))
        return subprocess.CompletedProcess(
            command, 0, stdout=self.outputs.pop(0), stderr=""
        )


class ExecutorHandoffTests(unittest.TestCase):
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
            (self.prompts / f"{role}.md").write_text(
                f"ROLE:{role}\n", encoding="utf-8"
            )

        self._git("init")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")

        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-21",
                    "sessions": {},
                    "pending": None,
                    "task_review_clean_checkpoint": "fixture-approved",
                    "review_certification": None,
                }
            ),
            encoding="utf-8",
        )

    def _git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            capture_output=True,
        )

    def invoke(self, agent, runner, task="external task must not replace pending payload"):
        return invoke_agent(
            agent=agent,
            workflow_id="issue-21",
            repo=self.repo,
            task=task,
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=runner,
        )

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def handoff_testing(self):
        coordinator = FakeRunner([codex_stdout("C21", TESTING_HANDOFF)])
        result = self.invoke("coordinator", coordinator, task="implement issue 21")
        self.assertEqual(result["status"], "HANDOFF")
        return coordinator

    def test_coordinator_handoff_creates_single_pending_envelope(self):
        self.handoff_testing()

        state = self.state()
        self.assertEqual(
            state["pending"],
            {
                "from": "coordinator",
                "to": "testing",
                "payload": json.loads(TESTING_HANDOFF.split("=", 1)[1]),
            },
        )
        self.assertNotIn("pending_agent", state)
        self.assertNotIn("pending_result_ready", state)

    def test_specialist_invocation_uses_exact_pending_task(self):
        self.handoff_testing()
        testing = FakeRunner([codex_stdout("T21", TESTING_RESULT)])

        self.invoke("testing", testing, task="WRONG RECONSTRUCTED TASK")

        prompt = testing.calls[0][2]
        self.assertIn(TESTING_TASK, prompt)
        self.assertNotIn("WRONG RECONSTRUCTED TASK", prompt)

    def test_specialist_completion_flips_pending_back_to_coordinator(self):
        self.handoff_testing()
        testing = FakeRunner([codex_stdout("T21", TESTING_RESULT)])

        self.invoke("testing", testing)

        self.assertEqual(
            self.state()["pending"],
            {
                "from": "testing",
                "to": "coordinator",
                "payload": TESTING_RESULT_DICT,
            },
        )

    def test_coordinator_consumes_exact_specialist_result_without_completion_handshake(self):
        self.handoff_testing()
        testing = FakeRunner([codex_stdout("T21", TESTING_RESULT)])
        self.invoke("testing", testing)

        next_handoff = "HERMES_RESULT=" + json.dumps(
            {
                "status": "HANDOFF",
                "next_agent": "testing",
                "task": "Add the next missing RED case",
                "reason": "The first RED result exposed another required case",
            }
        )
        coordinator = FakeRunner([codex_stdout("C21", next_handoff)])
        self.invoke("coordinator", coordinator, task="WRONG MANUAL RESULT COPY")

        prompt = coordinator.calls[0][2]
        self.assertIn("RED_COMPLETE", prompt)
        self.assertIn(TESTING_RESULT_DICT["test_command"], prompt)
        self.assertNotIn("WRONG MANUAL RESULT COPY", prompt)
        self.assertEqual(self.state()["pending"]["to"], "testing")

    def test_specialist_timeout_keeps_original_pending_handoff(self):
        self.handoff_testing()
        before = self.state()["pending"]

        def timeout_runner(command, cwd, input_text):
            raise subprocess.TimeoutExpired(command, 10)

        with self.assertRaises(CodexInvocationError):
            self.invoke("testing", timeout_runner)

        self.assertEqual(self.state()["pending"], before)


if __name__ == "__main__":
    unittest.main()
