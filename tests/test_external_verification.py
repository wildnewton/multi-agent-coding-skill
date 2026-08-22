import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_codex


def codex_stdout(thread_id, final_message):
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final_message}},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeAgentRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, command, cwd, input_text):
        self.calls.append((command, Path(cwd), input_text))
        return subprocess.CompletedProcess(command, 0, stdout=self.outputs.pop(0), stderr="")


class ExternalVerificationTests(unittest.TestCase):
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
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            capture_output=True,
        )

    def write_state(self, *, pending=None, external_verification=None, review_certification=None):
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-25",
                    "sessions": {},
                    "pending": pending,
                    "task_review_clean_checkpoint": "approved",
                    "review_certification": review_certification,
                    "external_verification": external_verification,
                }
            ),
            encoding="utf-8",
        )

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def invoke_agent(self, agent, runner, task="external task"):
        return run_codex.invoke_agent(
            agent=agent,
            workflow_id="issue-25",
            repo=self.repo,
            task=task,
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=runner,
        )

    def request_executor_verification(self):
        result = {
            "status": "VERIFY_EXTERNAL",
            "command": "python -c 'raise SystemExit(3)'",
            "boundary": "real external service boundary",
            "reason": "required acceptance evidence",
        }
        runner = FakeAgentRunner(
            [codex_stdout("C25", "HERMES_RESULT=" + json.dumps(result))]
        )
        self.invoke_agent("coordinator", runner, task="implement issue 25")
        return result

    def test_coordinator_can_assign_required_verification_to_executor(self):
        requested = self.request_executor_verification()
        state = self.state()
        self.assertEqual(
            state["pending"],
            {"from": "coordinator", "to": "executor", "payload": requested},
        )
        self.assertIsNone(state["external_verification"])
        self.assertIsNone(state["review_certification"])

    def test_executor_nonzero_is_evidence_and_reverses_pending(self):
        self.request_executor_verification()

        def command_runner(command, cwd, timeout_seconds):
            return subprocess.CompletedProcess(command, 7, stdout="live stdout", stderr="live stderr")

        evidence = run_codex.invoke_external_verification(
            workflow_id="issue-25",
            repo=self.repo,
            state_file=self.state_file,
            command_runner=command_runner,
        )
        self.assertEqual(evidence["provenance"], "executor")
        self.assertEqual(evidence["execution_status"], "completed")
        self.assertEqual(evidence["exit_status"], 7)
        self.assertEqual(evidence["head"], self._git("rev-parse", "HEAD").stdout.strip())
        state = self.state()
        self.assertEqual(state["external_verification"], evidence)
        self.assertEqual(
            state["pending"],
            {"from": "executor", "to": "coordinator", "payload": evidence},
        )

    def test_executor_timeout_is_evidence_not_workflow_error(self):
        self.request_executor_verification()

        def timeout_runner(command, cwd, timeout_seconds):
            raise subprocess.TimeoutExpired(command, timeout_seconds, output="partial output", stderr="timeout stderr")

        evidence = run_codex.invoke_external_verification(
            workflow_id="issue-25",
            repo=self.repo,
            state_file=self.state_file,
            command_runner=timeout_runner,
        )
        self.assertEqual(evidence["execution_status"], "timeout")
        self.assertIsNone(evidence["exit_status"])
        self.assertIn("partial output", evidence["stdout"])
        self.assertEqual(self.state()["pending"]["from"], "executor")

    def test_executor_command_execution_error_is_evidence(self):
        self.request_executor_verification()

        def error_runner(command, cwd, timeout_seconds):
            raise OSError("browser executable unavailable")

        evidence = run_codex.invoke_external_verification(
            workflow_id="issue-25",
            repo=self.repo,
            state_file=self.state_file,
            command_runner=error_runner,
        )
        self.assertEqual(evidence["execution_status"], "execution_error")
        self.assertIsNone(evidence["exit_status"])
        self.assertIn("browser executable unavailable", evidence["stderr"])
        self.assertEqual(self.state()["pending"]["from"], "executor")

    def test_hermes_can_report_unavailable_without_running_command(self):
        requested = self.request_executor_verification()
        calls = []

        def command_runner(command, cwd, timeout_seconds):
            calls.append(command)
            raise AssertionError("verification command must not run")

        result = run_codex.invoke_external_verification(
            workflow_id="issue-25",
            repo=self.repo,
            state_file=self.state_file,
            command_runner=command_runner,
            unavailable_reason="Hermes host lacks required browser/network access",
        )
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "EXTERNAL_VERIFICATION_UNAVAILABLE")
        self.assertEqual(
            result["request"],
            {key: requested[key] for key in ("command", "boundary", "reason")},
        )
        self.assertEqual(result["head"], self._git("rev-parse", "HEAD").stdout.strip())
        state = self.state()
        self.assertIsNone(state["external_verification"])
        self.assertEqual(
            state["pending"],
            {"from": "executor", "to": "coordinator", "payload": result},
        )

    def test_executor_orchestration_failure_keeps_original_pending(self):
        requested = self.request_executor_verification()

        def mutating_runner(command, cwd, timeout_seconds):
            (Path(cwd) / "unexpected.txt").write_text("changed\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        with self.assertRaises(run_codex.AgentRepositoryMutationError):
            run_codex.invoke_external_verification(
                workflow_id="issue-25",
                repo=self.repo,
                state_file=self.state_file,
                command_runner=mutating_runner,
            )
        self.assertEqual(
            self.state()["pending"],
            {"from": "coordinator", "to": "executor", "payload": requested},
        )

    def test_executor_rejects_pr_head_behind_before_running_command(self):
        requested = self.request_executor_verification()
        calls = []

        def command_runner(command, cwd, timeout_seconds):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        with (
            patch("run_codex._has_origin", return_value=True),
            patch("run_codex._current_pr_number", return_value=37),
            patch("run_codex._current_pr_head", return_value="stale-pr-head"),
        ):
            with self.assertRaisesRegex(run_codex.InvalidAgentResult, "actual PR HEAD"):
                run_codex.invoke_external_verification(
                    workflow_id="issue-25",
                    repo=self.repo,
                    state_file=self.state_file,
                    command_runner=command_runner,
                )
        self.assertEqual(calls, [])
        self.assertEqual(
            self.state()["pending"],
            {"from": "coordinator", "to": "executor", "payload": requested},
        )

    def test_external_result_audit_trace_does_not_publish_raw_output(self):
        secret = "cookie=super-secret-value"
        evidence = {
            "status": "EXTERNAL_VERIFICATION_RESULT",
            "request": {
                "command": "pytest -m live",
                "boundary": "real external boundary",
                "reason": "required acceptance evidence",
            },
            "provenance": "executor",
            "head": "abc123",
            "execution_status": "completed",
            "exit_status": 1,
            "stdout": f"failure output {secret}",
            "stderr": "diagnostic stderr",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        handoff = {"from": "executor", "to": "coordinator", "payload": evidence}
        gh_result = subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")
        with (
            patch("run_codex._has_origin", return_value=True),
            patch("run_codex._current_pr_number", return_value=37),
            patch("run_codex._gh", return_value=gh_result) as gh,
        ):
            run_codex._publish_handoff_trace(
                self.repo,
                "issue-25",
                handoff,
                head="abc123",
            )
        body = gh.call_args.args[-1]
        self.assertNotIn(secret, body)
        self.assertNotIn("failure output", body)
        self.assertNotIn("diagnostic stderr", body)
        self.assertIn('"execution_status": "completed"', body)
        self.assertIn('"exit_status": 1', body)
        self.assertIn('"head": "abc123"', body)

    def test_coordinator_cannot_bypass_pending_executor_ownership(self):
        self.request_executor_verification()
        runner = FakeAgentRunner(
            [codex_stdout("C25", 'HERMES_RESULT={"status":"BLOCKED","summary":"skip"}')]
        )
        with self.assertRaisesRegex(run_codex.InvalidAgentResult, "pending.*Executor"):
            self.invoke_agent("coordinator", runner)
        self.assertEqual(runner.calls, [])

    def test_structured_user_fallback_preserves_external_provenance(self):
        head = self._git("rev-parse", "HEAD").stdout.strip()
        decision = {
            "status": "AWAIT_USER_DECISION",
            "question": "Run the live command in a Chromium-capable environment and return HEAD plus output.",
            "external_verification": {
                "command": "pytest -q -m live tests/test_live_integration.py",
                "boundary": "real Playwright-backed official scraper boundary",
                "reason": "Hermes-side capability evidence confirms Chromium is unavailable",
                "expected_head": head,
            },
        }
        self.invoke_agent(
            "coordinator",
            FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(decision))]),
        )
        answer = f"HEAD={head}\n2 passed, 1 failed\n00406A failed: HTTP 403"
        review_handoff = {
            "status": "HANDOFF",
            "next_agent": "review",
            "task": "Review current HEAD and validate the external evidence classification.",
            "reason": "External evidence has been classified.",
            "full_test_command": "python -m unittest",
        }
        resumed = FakeAgentRunner(
            [codex_stdout("C25", "HERMES_RESULT=" + json.dumps(review_handoff))]
        )
        self.invoke_agent("coordinator", resumed, task=answer)
        state = self.state()
        evidence = state["external_verification"]
        self.assertEqual(evidence["provenance"], "externally_supplied")
        self.assertEqual(evidence["request"]["expected_head"], head)
        self.assertIn("00406A failed", evidence["evidence"])
        self.assertIn("00406A failed", resumed.calls[0][2])
        self.assertEqual(state["pending"]["to"], "review")

        review = FakeAgentRunner(
            [codex_stdout("R25", 'HERMES_RESULT={"status":"REVIEW_CLEAN"}')]
        )
        with patch("run_codex._current_pr_body_hash", return_value="body"):
            self.invoke_agent("review", review)
        review_prompt = review.calls[0][2]
        self.assertIn("externally_supplied", review_prompt)
        self.assertIn("00406A failed", review_prompt)

    def test_structured_user_fallback_requires_current_exact_head(self):
        decision = {
            "status": "AWAIT_USER_DECISION",
            "question": "Run live verification elsewhere.",
            "external_verification": {
                "command": "pytest -m live",
                "boundary": "real external boundary",
                "reason": "local environment unavailable",
                "expected_head": "stale-head",
            },
        }
        with self.assertRaisesRegex(run_codex.InvalidAgentResult, "expected_head"):
            self.invoke_agent(
                "coordinator",
                FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(decision))]),
            )

    def test_head_change_makes_preserved_evidence_stale_for_review(self):
        self.request_executor_verification()

        def command_runner(command, cwd, timeout_seconds):
            return subprocess.CompletedProcess(command, 0, stdout="pass", stderr="")

        run_codex.invoke_external_verification(
            workflow_id="issue-25",
            repo=self.repo,
            state_file=self.state_file,
            command_runner=command_runner,
        )
        review_handoff = {
            "status": "HANDOFF",
            "next_agent": "review",
            "task": "Review current HEAD and evidence.",
            "reason": "verification complete",
            "full_test_command": "python -m unittest",
        }
        self.invoke_agent(
            "coordinator",
            FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(review_handoff))]),
        )

        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        self._git("add", "new.txt")
        self._git("commit", "-m", "move head")
        review = FakeAgentRunner(
            [codex_stdout("R25", 'HERMES_RESULT={"status":"REVIEW_CLEAN"}')]
        )
        with self.assertRaisesRegex(run_codex.InvalidAgentResult, "external verification.*current HEAD"):
            self.invoke_agent("review", review)
        self.assertEqual(review.calls, [])

    def test_stale_external_evidence_at_merge_releases_consumed_review_result(self):
        head = self._git("rev-parse", "HEAD").stdout.strip()
        stale_evidence = {
            "status": "EXTERNAL_VERIFICATION_RESULT",
            "request": {
                "command": "pytest -m live",
                "boundary": "real external boundary",
                "reason": "required acceptance evidence",
            },
            "provenance": "executor",
            "head": "stale-head",
            "execution_status": "completed",
            "exit_status": 0,
            "stdout": "pass",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        self.write_state(
            pending={
                "from": "review",
                "to": "coordinator",
                "payload": {"status": "REVIEW_CLEAN", "verdict": "APPROVE"},
            },
            external_verification=stale_evidence,
            review_certification={"head": head, "pr_body_hash": "body"},
        )
        merge_result = {
            "status": "AWAIT_USER_MERGE",
            "summary": "ready",
            "reviewed_head": head,
            "draft": False,
        }
        coordinator = FakeAgentRunner(
            [codex_stdout("C25", "HERMES_RESULT=" + json.dumps(merge_result))]
        )
        with self.assertRaisesRegex(run_codex.InvalidAgentResult, "external verification.*current HEAD"):
            self.invoke_agent("coordinator", coordinator)
        self.assertIsNone(self.state()["pending"])


if __name__ == "__main__":
    unittest.main()
