import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.calls.append({"command": command, "cwd": Path(cwd), "input_text": input_text})
        return subprocess.CompletedProcess(command, 0, stdout=self.outputs.pop(0), stderr="")


TESTING_RESULT = 'HERMES_RESULT={"status":"RED_COMPLETE","test_command":"false","summary":"RED"}'
REVIEW_RESULT = 'HERMES_RESULT={"status":"REVIEW_CLEAN"}'
TESTING_HANDOFF = 'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing","task":"Add RED","reason":"Need RED"}'
REVIEW_HANDOFF = 'HERMES_RESULT={"status":"HANDOFF","next_agent":"review","task":"Review GREEN","reason":"GREEN ready","full_test_command":"python -m unittest"}'


class InvokeAgentTests(unittest.TestCase):
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
        self.write_state(clean="fixture-approved")

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, text=True, capture_output=True)

    def write_state(self, *, clean="fixture-approved", pending=None, sessions=None, review_certification=None):
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-51",
                    "sessions": sessions or {},
                    "pending": pending,
                    "task_review_clean_checkpoint": clean,
                    "review_certification": review_certification,
                }
            ),
            encoding="utf-8",
        )

    def prime_pending(self, agent, *, task="specialist task"):
        self.write_state(
            pending={
                "from": "coordinator",
                "to": agent,
                "payload": {"status": "HANDOFF", "next_agent": agent, "task": task, "reason": "needed"},
            }
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

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_testing_session_starts_then_resumes(self):
        self.prime_pending("testing")
        first = FakeRunner([codex_stdout("T52", TESTING_RESULT)])
        self.invoke("testing", first)
        self.assertEqual(first.calls[0]["command"], ["codex", "exec", "--json", "-"])
        self.assertEqual(self.state()["sessions"]["testing"], "T52")

        self.prime_pending("testing", task="next RED")
        state = self.state(); state["sessions"]["testing"] = "T52"
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        second = FakeRunner([codex_stdout("T52", TESTING_RESULT)])
        self.invoke("testing", second)
        self.assertEqual(second.calls[0]["command"], ["codex", "exec", "resume", "T52", "--json", "-"])

    def test_coordinator_session_is_separate_from_testing(self):
        self.prime_pending("testing")
        testing = FakeRunner([codex_stdout("T52", TESTING_RESULT)])
        self.invoke("testing", testing)
        coordinator = FakeRunner([codex_stdout("C52", TESTING_HANDOFF)])
        self.invoke("coordinator", coordinator)
        state = self.state()
        self.assertEqual(state["sessions"]["testing"], "T52")
        self.assertEqual(state["sessions"]["coordinator"], "C52")

    def test_review_always_starts_fresh(self):
        for expected in ("R1", "R2"):
            self.prime_pending("review")
            runner = FakeRunner([codex_stdout(expected, REVIEW_RESULT)])
            with patch("run_codex._current_pr_body_hash", return_value="body"):
                self.invoke("review", runner)
            self.assertEqual(runner.calls[0]["command"], ["codex", "exec", "--json", "-"])
        self.assertNotIn("review", self.state()["sessions"])

    def test_coordinator_handoff_requires_valid_target_task_and_reason(self):
        invalid = (
            'HERMES_RESULT={"status":"HANDOFF","next_agent":"user","task":"x","reason":"x"}',
            'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing","reason":"x"}',
            'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing","task":"x"}',
        )
        for output in invalid:
            with self.subTest(output=output):
                runner = FakeRunner([codex_stdout("C", output)])
                with self.assertRaises(InvalidAgentResult):
                    self.invoke("coordinator", runner)

    def test_review_handoff_requires_exactly_one_green_evidence_field(self):
        for output in (
            'HERMES_RESULT={"status":"HANDOFF","next_agent":"review","task":"x","reason":"x"}',
            'HERMES_RESULT={"status":"HANDOFF","next_agent":"review","task":"x","reason":"x","full_test_command":"pytest","full_test_unavailable_reason":"none"}',
        ):
            with self.subTest(output=output):
                with self.assertRaises(InvalidAgentResult):
                    self.invoke("coordinator", FakeRunner([codex_stdout("C", output)]))

    def test_await_user_decision_requires_question(self):
        runner = FakeRunner([codex_stdout("C", 'HERMES_RESULT={"status":"AWAIT_USER_DECISION"}')])
        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", runner)

    def test_specialists_cannot_choose_next_agent(self):
        self.prime_pending("testing")
        output = 'HERMES_RESULT={"status":"RED_COMPLETE","test_command":"pytest","next_agent":"review"}'
        with self.assertRaises(InvalidAgentResult):
            self.invoke("testing", FakeRunner([codex_stdout("T", output)]))

    def test_result_contract_parsing_and_role_status_fail_closed(self):
        self.prime_pending("testing")
        with self.assertRaises(InvalidAgentResult):
            self.invoke("testing", FakeRunner([codex_stdout("T", "RED is done")]))
        self.prime_pending("testing")
        with self.assertRaises(InvalidAgentResult):
            self.invoke("testing", FakeRunner([codex_stdout("T", 'HERMES_RESULT={"status":"GREEN_COMPLETE"}')]))

    def test_review_changes_required_requires_findings(self):
        self.prime_pending("review")
        before = self.state()["pending"]
        output = 'HERMES_RESULT={"status":"CHANGES_REQUIRED"}'
        with self.assertRaisesRegex(InvalidAgentResult, "must include non-empty findings"):
            self.invoke("review", FakeRunner([codex_stdout("R", output)]))
        self.assertEqual(self.state()["pending"], before)

    def test_specialist_requires_matching_pending_target(self):
        runner = FakeRunner([codex_stdout("R", REVIEW_RESULT)])
        with self.assertRaises(InvalidAgentResult):
            self.invoke("review", runner)
        self.assertEqual(runner.calls, [])

    def test_testing_timeout_keeps_pending_for_read_only_recovery(self):
        coordinator = FakeRunner([codex_stdout("C", TESTING_HANDOFF)])
        self.invoke("coordinator", coordinator)
        before = self.state()["pending"]

        def timeout_runner(command, cwd, input_text):
            raise subprocess.TimeoutExpired(command, 10)

        with self.assertRaises(CodexInvocationError):
            self.invoke("testing", timeout_runner)
        self.assertEqual(self.state()["pending"], before)

        def editing_recovery(command, cwd, input_text):
            (Path(cwd) / "bad.py").write_text("x\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=codex_stdout("C", TESTING_HANDOFF), stderr="")

        with self.assertRaises(AgentRepositoryMutationError):
            self.invoke("coordinator", editing_recovery, task="timeout evidence")
        self.assertEqual(self.state()["pending"], before)

    def test_unresolved_pending_blocks_other_specialist(self):
        self.prime_pending("testing")
        review = FakeRunner([codex_stdout("R", REVIEW_RESULT)])
        with self.assertRaises(InvalidAgentResult):
            self.invoke("review", review)

    def test_new_review_handoff_invalidates_prior_review_certification(self):
        self.write_state(review_certification={"head": "old", "pr_body_hash": "old"})
        self.invoke("coordinator", FakeRunner([codex_stdout("C", REVIEW_HANDOFF)]))
        self.assertIsNone(self.state()["review_certification"])

    def test_pending_specialist_blocks_merge_readiness(self):
        self.prime_pending("review")
        head = self._git("rev-parse", "HEAD").stdout.strip()
        result = f'HERMES_RESULT={{"status":"AWAIT_USER_MERGE","reviewed_head":"{head}","draft":false}}'
        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", FakeRunner([codex_stdout("C", result)]))

    def test_stale_review_head_blocks_merge_readiness(self):
        old = self._git("rev-parse", "HEAD").stdout.strip()
        self.write_state(review_certification={"head": old, "pr_body_hash": None})
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        self._git("add", "new.txt"); self._git("commit", "-m", "new")
        result = f'HERMES_RESULT={{"status":"AWAIT_USER_MERGE","reviewed_head":"{old}","draft":false}}'
        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", FakeRunner([codex_stdout("C", result)]))


if __name__ == "__main__":
    unittest.main()
