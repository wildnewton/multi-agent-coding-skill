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


TESTING_RESULT = (
    'HERMES_RESULT={"status":"RED_COMPLETE",'
    '"test_command":"python -m unittest tests.test_feature","summary":"RED"}'
)
COORDINATOR_RESULT = (
    'HERMES_RESULT={"status":"HANDOFF","next_agent":"review",'
    '"task":"Review the verified GREEN implementation",'
    '"reason":"GREEN implementation is ready for independent Review",'
    '"full_test_command":"python -m unittest discover -s tests"}'
)
COORDINATOR_TESTING_RESULT = (
    'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing",'
    '"task":"Add RED coverage for AC3","reason":"AC3 lacks RED coverage"}'
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

        self._git("init")
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "Test User")
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial")

    def _git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            capture_output=True,
        )

    def invoke(
        self,
        agent,
        runner,
        task="do the task",
        *,
        completed_agent=None,
    ):
        return invoke_agent(
            agent=agent,
            workflow_id="issue-51",
            repo=self.repo,
            task=task,
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=runner,
            completed_agent=completed_agent,
        )

    def read_state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_first_testing_invocation_starts_and_saves_session(self):
        runner = FakeRunner([codex_stdout("T52", TESTING_RESULT)])

        result = self.invoke("testing", runner)

        self.assertEqual(result["status"], "RED_COMPLETE")
        self.assertEqual(
            runner.calls[0]["command"], ["codex", "exec", "--json", "-"]
        )
        self.assertIn("ROLE:testing", runner.calls[0]["input_text"])
        state = self.read_state()
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

        state = self.read_state()
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
        self.assertEqual(self.read_state()["pending_agent"], "testing")

    def test_coordinator_can_handoff_to_review(self):
        runner = FakeRunner([codex_stdout("C52", COORDINATOR_RESULT)])

        result = self.invoke("coordinator", runner)

        self.assertEqual(result["status"], "HANDOFF")
        self.assertEqual(result["next_agent"], "review")
        state = self.read_state()
        self.assertEqual(state["pending_agent"], "review")
        self.assertIsNone(state["review_clean_head"])

    def test_coordinator_can_await_user_merge_after_verified_clean_review(self):
        first = FakeRunner([codex_stdout("C52", COORDINATOR_RESULT)])
        self.invoke("coordinator", first)
        review = FakeRunner([codex_stdout("R52", REVIEW_RESULT)])
        self.invoke("review", review)

        head = self._git("rev-parse", "HEAD").stdout.strip()
        merge_result = (
            'HERMES_RESULT={"status":"AWAIT_USER_MERGE","summary":"Ready to merge",'
            f'"reviewed_head":"{head}","draft":false}}'
        )
        coordinator = FakeRunner([codex_stdout("C52", merge_result)])

        result = self.invoke(
            "coordinator",
            coordinator,
            completed_agent="review",
        )

        self.assertEqual(result["status"], "AWAIT_USER_MERGE")
        self.assertEqual(result["reviewed_head"], head)
        self.assertIs(result["draft"], False)
        self.assertIsNone(self.read_state()["pending_agent"])

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
                    '"task":"ask a question","reason":"user input is required"}',
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
                    'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing",'
                    '"reason":"RED coverage is needed"}',
                )
            ]
        )

        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", runner)

    def test_coordinator_handoff_requires_reason(self):
        runner = FakeRunner(
            [
                codex_stdout(
                    "C52",
                    'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing",'
                    '"task":"Add RED coverage"}',
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
                    '"task":"Review this","reason":"GREEN is ready"}',
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
                    '"task":"Review this","reason":"GREEN is ready",'
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
                    '"task":"Review this","reason":"GREEN is ready",'
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
                    '"draft":false,"summary":"Ready"}',
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
                'HERMES_RESULT={"status":"RED_COMPLETE",'
                '"test_command":"pytest tests/test_feature.py",'
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
        state = self.read_state()
        self.assertNotIn("review", state["sessions"])

    def test_result_contract_is_parsed_from_codex_json_stream(self):
        runner = FakeRunner(
            [
                codex_stdout(
                    "T52",
                    'notes before\nHERMES_RESULT={"status":"RED_COMPLETE",'
                    '"test_command":"pytest tests/test_feature.py",'
                    '"summary":"3 tests"}\nnotes after',
                )
            ]
        )

        result = self.invoke("testing", runner)

        self.assertEqual(
            result,
            {
                "status": "RED_COMPLETE",
                "test_command": "pytest tests/test_feature.py",
                "summary": "3 tests",
            },
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

    def test_testing_timeout_keeps_pending_agent_for_coordinator_recovery(self):
        coordinator = FakeRunner([codex_stdout("C52", COORDINATOR_TESTING_RESULT)])
        self.invoke("coordinator", coordinator)

        def timeout_runner(command, cwd, input_text):
            raise subprocess.TimeoutExpired(command, 10)

        with self.assertRaises(CodexInvocationError):
            self.invoke("testing", timeout_runner)

        self.assertEqual(self.read_state()["pending_agent"], "testing")

        narrower = (
            'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing",'
            '"task":"Add RED only for the timeout recovery path",'
            '"reason":"Previous Testing task timed out and is being narrowed"}'
        )
        recovery = FakeRunner([codex_stdout("C52", narrower)])
        result = self.invoke("coordinator", recovery)

        self.assertEqual(result["status"], "HANDOFF")
        self.assertEqual(self.read_state()["pending_agent"], "testing")

    def test_recovery_coordinator_cannot_modify_worktree(self):
        coordinator = FakeRunner([codex_stdout("C52", COORDINATOR_TESTING_RESULT)])
        self.invoke("coordinator", coordinator)

        def editing_runner(command, cwd, input_text):
            (Path(cwd) / "tests_added_by_coordinator.py").write_text(
                "assert True\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=codex_stdout("C52", COORDINATOR_TESTING_RESULT),
                stderr="",
            )

        with self.assertRaises(AgentRepositoryMutationError):
            self.invoke("coordinator", editing_runner)

        self.assertEqual(self.read_state()["pending_agent"], "testing")

    def test_timeout_cannot_be_cleared_by_completion_handshake(self):
        coordinator = FakeRunner([codex_stdout("C52", COORDINATOR_TESTING_RESULT)])
        self.invoke("coordinator", coordinator)

        def timeout_runner(command, cwd, input_text):
            raise subprocess.TimeoutExpired(command, 10)

        with self.assertRaises(CodexInvocationError):
            self.invoke("testing", timeout_runner)

        decision = FakeRunner([codex_stdout("C52", COORDINATOR_DECISION_RESULT)])
        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", decision, completed_agent="testing")

        state = self.read_state()
        self.assertEqual(state["pending_agent"], "testing")
        self.assertIs(state["pending_agent_completed"], False)

    def test_completed_agent_handshake_must_match_pending_agent(self):
        coordinator = FakeRunner([codex_stdout("C52", COORDINATOR_TESTING_RESULT)])
        self.invoke("coordinator", coordinator)

        runner = FakeRunner([codex_stdout("C52", COORDINATOR_DECISION_RESULT)])
        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", runner, completed_agent="review")

        self.assertEqual(self.read_state()["pending_agent"], "testing")

    def test_completed_agent_handshake_clears_pending_before_normal_coordinator(self):
        coordinator = FakeRunner([codex_stdout("C52", COORDINATOR_TESTING_RESULT)])
        self.invoke("coordinator", coordinator)
        testing = FakeRunner([codex_stdout("T52", TESTING_RESULT)])
        self.invoke("testing", testing)

        decision = FakeRunner([codex_stdout("C52", COORDINATOR_DECISION_RESULT)])
        self.invoke("coordinator", decision, completed_agent="testing")

        self.assertIsNone(self.read_state()["pending_agent"])

    def test_unresolved_pending_agent_blocks_other_specialist(self):
        coordinator = FakeRunner([codex_stdout("C52", COORDINATOR_TESTING_RESULT)])
        self.invoke("coordinator", coordinator)

        review = FakeRunner([codex_stdout("R52", REVIEW_RESULT)])
        with self.assertRaises(InvalidAgentResult):
            self.invoke("review", review)

        self.assertEqual(self.read_state()["pending_agent"], "testing")

    def test_review_clean_records_actual_head_but_does_not_clear_pending(self):
        coordinator = FakeRunner([codex_stdout("C52", COORDINATOR_RESULT)])
        self.invoke("coordinator", coordinator)

        review = FakeRunner([codex_stdout("R52", REVIEW_RESULT)])
        self.invoke("review", review)

        state = self.read_state()
        head = self._git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(state["pending_agent"], "review")
        self.assertEqual(state["review_clean_head"], head)

    def test_new_review_handoff_invalidates_prior_clean_certification(self):
        head = self._git("rev-parse", "HEAD").stdout.strip()
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-51",
                    "sessions": {},
                    "pending_agent": None,
                    "review_clean_head": head,
                }
            ),
            encoding="utf-8",
        )
        runner = FakeRunner([codex_stdout("C52", COORDINATOR_RESULT)])

        self.invoke("coordinator", runner)

        state = self.read_state()
        self.assertEqual(state["pending_agent"], "review")
        self.assertIsNone(state["review_clean_head"])

    def test_pending_review_blocks_merge_readiness(self):
        coordinator = FakeRunner([codex_stdout("C52", COORDINATOR_RESULT)])
        self.invoke("coordinator", coordinator)
        head = self._git("rev-parse", "HEAD").stdout.strip()
        merge_result = (
            'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
            f'"reviewed_head":"{head}","draft":false}}'
        )
        recovery = FakeRunner([codex_stdout("C52", merge_result)])

        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", recovery)

    def test_stale_review_clean_head_blocks_merge_readiness(self):
        old_head = self._git("rev-parse", "HEAD").stdout.strip()
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-51",
                    "sessions": {},
                    "pending_agent": None,
                    "review_clean_head": old_head,
                }
            ),
            encoding="utf-8",
        )
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        self._git("add", "new.txt")
        self._git("commit", "-m", "new head")
        runner = FakeRunner(
            [
                codex_stdout(
                    "C52",
                    'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
                    f'"reviewed_head":"{old_head}","draft":false}}',
                )
            ]
        )

        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", runner)


if __name__ == "__main__":
    unittest.main()
