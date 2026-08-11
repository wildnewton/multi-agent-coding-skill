import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from run_codex import InvalidAgentResult, invoke_agent


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
PROMPTS = ROOT / "prompts"

TESTING_RESULT = 'HERMES_RESULT={"status":"RED_COMPLETE","commit":"aaa111"}'
COORDINATOR_RESULT = (
    'HERMES_RESULT={"status":"HANDOFF","next_agent":"review",'
    '"task":"Review the verified GREEN implementation"}'
)
COORDINATOR_TESTING_RESULT = (
    'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing",'
    '"task":"Add RED coverage for AC3"}'
)
COORDINATOR_USER_RESULT = (
    'HERMES_RESULT={"status":"AWAIT_USER_MERGE","summary":"Ready to merge"}'
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


class RoutingTopologyContractTests(unittest.TestCase):
    def test_skill_defines_coordinator_as_only_semantic_router(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("Coordinator is the only semantic routing hub", text)
        self.assertIn("Testing always returns to Coordinator", text)
        self.assertIn("Review always returns to Coordinator", text)
        self.assertIn("Hermes does not choose the next specialist agent", text)

    def test_coordinator_prompt_owns_next_agent_decision(self):
        text = (PROMPTS / "coordinator.md").read_text(encoding="utf-8")
        self.assertIn("You are the only agent allowed to choose the next destination", text)
        self.assertIn('"next_agent":"testing"', text)
        self.assertIn('"next_agent":"review"', text)
        self.assertIn("AWAIT_USER_MERGE", text)

    def test_specialist_prompts_return_only_to_coordinator(self):
        for prompt_name in ("testing.md", "review.md"):
            text = (PROMPTS / prompt_name).read_text(encoding="utf-8")
            self.assertIn("Always return your result to Coordinator through Hermes", text)
            self.assertIn("Do not choose the next agent", text)


if __name__ == "__main__":
    unittest.main()
