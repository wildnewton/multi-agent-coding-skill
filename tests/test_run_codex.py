import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from run_codex import InvalidAgentResult, invoke_agent


TESTING_RESULT = 'HERMES_RESULT={"status":"RED_COMPLETE","commit":"aaa111"}'
COORDINATOR_RESULT = 'HERMES_RESULT={"status":"GREEN_COMPLETE","commit":"bbb222"}'
REVIEW_RESULT = 'HERMES_RESULT={"status":"REVIEW_CLEAN"}'


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
        self.calls.append(
            {
                "command": command,
                "cwd": Path(cwd),
                "input_text": input_text,
            }
        )
        stdout = self.outputs.pop(0)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


class InvokeAgentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "target-repo"
        self.repo.mkdir()
        self.state_file = self.root / "workflow.json"
        self.prompts = self.root / "prompts"
        self.prompts.mkdir()
        for role in ("testing", "coordinator", "review"):
            (self.prompts / f"{role}.md").write_text(
                f"ROLE:{role}\n", encoding="utf-8"
            )

    def invoke(self, agent, runner, task="do the task"):
        return invoke_agent(
            agent=agent,
            workflow_id="issue-51",
            repo=self.repo,
            task=task,
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=runner,
        )

    def test_first_testing_invocation_starts_and_saves_session(self):
        runner = FakeRunner([codex_stdout("T52", TESTING_RESULT)])

        result = self.invoke("testing", runner)

        self.assertEqual(result["status"], "RED_COMPLETE")
        self.assertEqual(
            runner.calls[0]["command"], ["codex", "exec", "--json", "-"]
        )
        self.assertIn("ROLE:testing", runner.calls[0]["input_text"])
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["sessions"]["testing"], "T52")

    def test_second_testing_invocation_resumes_same_session(self):
        first = FakeRunner([codex_stdout("T52", TESTING_RESULT)])
        self.invoke("testing", first)
        second = FakeRunner([codex_stdout("T52", TESTING_RESULT)])

        self.invoke("testing", second, task="add missing coverage")

        self.assertEqual(
            second.calls[0]["command"],
            ["codex", "exec", "resume", "T52", "--json", "-"],
        )

    def test_coordinator_uses_session_separate_from_testing(self):
        runner = FakeRunner(
            [
                codex_stdout("T52", TESTING_RESULT),
                codex_stdout("C52", COORDINATOR_RESULT),
            ]
        )

        self.invoke("testing", runner)
        self.invoke("coordinator", runner)

        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["sessions"]["testing"], "T52")
        self.assertEqual(state["sessions"]["coordinator"], "C52")
        self.assertNotEqual(
            state["sessions"]["testing"], state["sessions"]["coordinator"]
        )

    def test_second_coordinator_invocation_resumes_coordinator_session(self):
        first = FakeRunner([codex_stdout("C52", COORDINATOR_RESULT)])
        self.invoke("coordinator", first)
        second = FakeRunner([codex_stdout("C52", COORDINATOR_RESULT)])

        self.invoke("coordinator", second, task="fix review finding")

        self.assertEqual(
            second.calls[0]["command"],
            ["codex", "exec", "resume", "C52", "--json", "-"],
        )

    def test_review_always_starts_fresh_session(self):
        runner = FakeRunner(
            [
                codex_stdout("R1", REVIEW_RESULT),
                codex_stdout("R2", REVIEW_RESULT),
            ]
        )

        self.invoke("review", runner)
        self.invoke("review", runner)

        self.assertEqual(
            runner.calls[0]["command"], ["codex", "exec", "--json", "-"]
        )
        self.assertEqual(
            runner.calls[1]["command"], ["codex", "exec", "--json", "-"]
        )
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertNotIn("review", state["sessions"])

    def test_result_contract_is_parsed_from_codex_json_stream(self):
        runner = FakeRunner(
            [
                codex_stdout(
                    "T52",
                    'notes before\nHERMES_RESULT={"status":"RED_COMPLETE",'
                    '"commit":"abc123","summary":"3 tests"}\nnotes after',
                )
            ]
        )

        result = self.invoke("testing", runner)

        self.assertEqual(
            result,
            {"status": "RED_COMPLETE", "commit": "abc123", "summary": "3 tests"},
        )

    def test_missing_or_invalid_result_contract_fails_closed(self):
        runner = FakeRunner([codex_stdout("T52", "RED is done")])

        with self.assertRaises(InvalidAgentResult):
            self.invoke("testing", runner)


if __name__ == "__main__":
    unittest.main()
