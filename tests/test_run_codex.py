import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from run_codex import InvalidAgentResult, invoke_agent


TESTING_RESULT = 'HERMES_RESULT={"status":"RED_COMPLETE","commit":"aaa111"}'
COORDINATOR_RESULT = (
    'HERMES_RESULT={"status":"HANDOFF","next_agent":"review",'
    '"task":"Review the verified GREEN implementation","commit":"bbb222",'
    '"test_command":"python -m unittest tests.test_feature",'
    '"full_test_command":"python -m unittest discover -s tests"}'
)
COORDINATOR_TESTING_RESULT = (
    'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing",'
    '"task":"Add RED coverage for AC3"}'
)
COORDINATOR_USER_RESULT = (
    'HERMES_RESULT={"status":"AWAIT_USER_MERGE","summary":"Ready to merge",'
    '"reviewed_head":"bbb222"}'
)
COORDINATOR_DECISION_RESULT = (
    'HERMES_RESULT={"status":"AWAIT_USER_DECISION",'
    '"question":"Should AC3 include archived records?"}'
)
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
        second = FakeRunner([codex_stdout("C52", COORDINATOR_TESTING_RESULT)])

        self.invoke("coordinator", second, task="route review finding")

        self.assertEqual(
            second.calls[0]["command"],
            ["codex", "exec", "resume", "C52", "--json", "-"],
        )

    def test_coordinator_can_handoff_to_testing(self):
        runner = FakeRunner([codex_stdout("C52", COORDINATOR_TESTING_RESULT)])

        result = self.invoke("coordinator", runner)

        self.assertEqual(result["status"], "HANDOFF")
        self.assertEqual(result["next_agent"], "testing")

    def test_coordinator_can_handoff_to_review(self):
        runner = FakeRunner([codex_stdout("C52", COORDINATOR_RESULT)])

        result = self.invoke("coordinator", runner)

        self.assertEqual(result["status"], "HANDOFF")
        self.assertEqual(result["next_agent"], "review")

    def test_coordinator_can_await_user_merge(self):
        runner = FakeRunner([codex_stdout("C52", COORDINATOR_USER_RESULT)])

        result = self.invoke("coordinator", runner)

        self.assertEqual(result["status"], "AWAIT_USER_MERGE")
        self.assertEqual(result["reviewed_head"], "bbb222")

    def test_coordinator_can_await_user_decision(self):
        runner = FakeRunner([codex_stdout("C52", COORDINATOR_DECISION_RESULT)])

        result = self.invoke("coordinator", runner)

        self.assertEqual(result["status"], "AWAIT_USER_DECISION")

    def test_coordinator_rejects_invalid_handoff_target(self):
        runner = FakeRunner(
            [
                codex_stdout(
                    "C52",
                    'HERMES_RESULT={"status":"HANDOFF","next_agent":"user",'
                    '"task":"ask a question"}',
                )
            ]
        )

        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", runner)

    def test_coordinator_handoff_requires_task(self):
        runner = FakeRunner(
            [
                codex_stdout(
                    "C52",
                    'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing"}',
                )
            ]
        )

        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", runner)

    def test_review_handoff_requires_green_evidence(self):
        runner = FakeRunner(
            [
                codex_stdout(
                    "C52",
                    'HERMES_RESULT={"status":"HANDOFF","next_agent":"review",'
                    '"task":"Review this"}',
                )
            ]
        )

        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", runner)

    def test_review_handoff_accepts_full_test_unavailable_reason(self):
        runner = FakeRunner(
            [
                codex_stdout(
                    "C52",
                    'HERMES_RESULT={"status":"HANDOFF","next_agent":"review",'
                    '"task":"Review this","commit":"bbb222",'
                    '"test_command":"python -m unittest tests.test_feature",'
                    '"full_test_unavailable_reason":"No full suite is configured"}',
                )
            ]
        )

        result = self.invoke("coordinator", runner)

        self.assertEqual(result["status"], "HANDOFF")
        self.assertEqual(result["next_agent"], "review")

    def test_review_handoff_rejects_both_full_test_fields(self):
        runner = FakeRunner(
            [
                codex_stdout(
                    "C52",
                    'HERMES_RESULT={"status":"HANDOFF","next_agent":"review",'
                    '"task":"Review this","commit":"bbb222",'
                    '"test_command":"python -m unittest tests.test_feature",'
                    '"full_test_command":"python -m unittest discover -s tests",'
                    '"full_test_unavailable_reason":"No full suite is configured"}',
                )
            ]
        )

        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", runner)

    def test_await_user_merge_requires_reviewed_head(self):
        runner = FakeRunner(
            [
                codex_stdout(
                    "C52",
                    'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
                    '"summary":"Ready"}',
                )
            ]
        )

        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", runner)

    def test_await_user_decision_requires_question(self):
        runner = FakeRunner(
            [
                codex_stdout(
                    "C52",
                    'HERMES_RESULT={"status":"AWAIT_USER_DECISION"}',
                )
            ]
        )

        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", runner)

    def test_specialists_cannot_choose_next_agent(self):
        cases = (
            (
                "testing",
                'HERMES_RESULT={"status":"RED_COMPLETE","commit":"aaa111",'
                '"next_agent":"review"}',
            ),
            (
                "review",
                'HERMES_RESULT={"status":"REVIEW_CLEAN","next_agent":"testing"}',
            ),
        )
        for agent, output in cases:
            with self.subTest(agent=agent):
                runner = FakeRunner([codex_stdout(f"{agent}-52", output)])
                with self.assertRaises(InvalidAgentResult):
                    self.invoke(agent, runner)

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

    def test_role_incompatible_status_fails_closed(self):
        runner = FakeRunner(
            [codex_stdout("T52", 'HERMES_RESULT={"status":"GREEN_COMPLETE"}')]
        )

        with self.assertRaises(InvalidAgentResult):
            self.invoke("testing", runner)


if __name__ == "__main__":
    unittest.main()
