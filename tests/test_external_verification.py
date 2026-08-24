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
        self.write_state(review_certification=self.full_review_certification())

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

    def full_review_certification(self):
        return {
            "head": self._git("rev-parse", "HEAD").stdout.strip(),
            "pr_body_hash": None,
        }

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

    def request_executor_verification(
        self,
        *,
        command="python -c 'raise SystemExit(3)'",
        boundary="real external service boundary",
        reason="required acceptance evidence",
    ):
        result = {
            "status": "VERIFY_EXTERNAL",
            "command": command,
            "boundary": boundary,
            "reason": reason,
        }
        runner = FakeAgentRunner(
            [codex_stdout("C25", "HERMES_RESULT=" + json.dumps(result))]
        )
        self.invoke_agent("coordinator", runner, task="implement issue 25")
        return result

    def report_executor_unavailable(self, reason="Hermes host lacks required browser/network access"):
        return run_codex.invoke_external_verification(
            workflow_id="issue-25",
            repo=self.repo,
            state_file=self.state_file,
            unavailable_reason=reason,
        )

    def test_external_verification_requires_full_review_certification(self):
        self.write_state(review_certification=None)
        result = {
            "status": "VERIFY_EXTERNAL",
            "command": "pytest -m live",
            "boundary": "real external boundary",
            "reason": "required acceptance evidence",
        }
        with self.assertRaisesRegex(
            run_codex.InvalidAgentResult, "clean Full Review certification"
        ):
            self.invoke_agent(
                "coordinator",
                FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(result))]),
            )

    def test_full_review_can_run_without_external_evidence(self):
        self.write_state(review_certification=None, external_verification=None)
        handoff = {
            "status": "HANDOFF",
            "next_agent": "review",
            "review_scope": "full",
            "task": "Review the full code/test diff.",
            "reason": "GREEN is ready before external verification.",
            "full_test_command": "python -m unittest",
        }
        self.invoke_agent(
            "coordinator",
            FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(handoff))]),
        )
        review = FakeAgentRunner(
            [codex_stdout("R25", 'HERMES_RESULT={"status":"REVIEW_CLEAN"}')]
        )
        self.invoke_agent("review", review)
        certification = self.state()["review_certification"]
        self.assertEqual(certification["head"], self.full_review_certification()["head"])
        self.assertNotIn("external_verification_digest", certification)
        self.assertNotIn("Preserved required external-verification evidence", review.calls[0][2])

    def test_full_review_ignores_stale_external_evidence(self):
        stale_evidence = {
            "status": "EXTERNAL_VERIFICATION_RESULT",
            "request": {"command": "live", "boundary": "service", "reason": "required"},
            "provenance": "executor",
            "head": "stale-head",
            "execution_status": "completed",
            "exit_status": 0,
            "stdout": "pass",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        self.write_state(review_certification=None, external_verification=stale_evidence)
        handoff = {
            "status": "HANDOFF",
            "next_agent": "review",
            "review_scope": "full",
            "task": "Review the new HEAD code/tests.",
            "reason": "HEAD changed; stale live evidence must not block code review.",
            "full_test_command": "python -m unittest",
        }
        self.invoke_agent(
            "coordinator",
            FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(handoff))]),
        )
        review = FakeAgentRunner(
            [codex_stdout("R25", 'HERMES_RESULT={"status":"REVIEW_CLEAN"}')]
        )
        self.invoke_agent("review", review)
        self.assertNotIn("stale-head", review.calls[0][2])
        self.assertIsNone(self.state()["external_verification"])

    def test_evidence_only_review_rejects_full_test_metadata(self):
        evidence = {
            "status": "EXTERNAL_VERIFICATION_RESULT",
            "request": {"command": "live", "boundary": "service", "reason": "required"},
            "provenance": "executor",
            "head": self.full_review_certification()["head"],
            "execution_status": "completed",
            "exit_status": 0,
            "stdout": "pass",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        certification = self.full_review_certification()
        certification["external_verification_digest"] = None
        self.write_state(
            review_certification=certification, external_verification=evidence
        )
        handoff = {
            "status": "HANDOFF",
            "next_agent": "review",
            "review_scope": "external_evidence",
            "task": "Certify evidence only.",
            "reason": "live run complete",
            "full_test_command": "python -m unittest",
        }
        with self.assertRaisesRegex(
            run_codex.InvalidAgentResult, "must not include full-test metadata"
        ):
            self.invoke_agent(
                "coordinator",
                FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(handoff))]),
            )

    def test_same_head_replacement_verification_does_not_require_full_rereview(self):
        self.request_executor_verification(command="printf first")
        run_codex.invoke_external_verification(
            workflow_id="issue-25",
            repo=self.repo,
            state_file=self.state_file,
            command_runner=lambda command, cwd, timeout_seconds: subprocess.CompletedProcess(
                command, 1, stdout="inconclusive", stderr=""
            ),
        )
        replacement = {
            "status": "VERIFY_EXTERNAL",
            "command": "printf second",
            "boundary": "real external service boundary",
            "reason": "replace inconclusive same-HEAD evidence",
        }
        self.invoke_agent(
            "coordinator",
            FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(replacement))]),
        )
        state = self.state()
        self.assertEqual(state["pending"]["to"], "executor")
        self.assertEqual(state["review_certification"]["head"], self.full_review_certification()["head"])
        self.assertIn("external_verification_digest", state["review_certification"])
        self.assertIsNone(state["review_certification"]["external_verification_digest"])

    def test_evidence_only_review_certifies_exact_evidence_for_merge(self):
        self.request_executor_verification(command="printf pass")
        evidence = run_codex.invoke_external_verification(
            workflow_id="issue-25",
            repo=self.repo,
            state_file=self.state_file,
            command_runner=lambda command, cwd, timeout_seconds: subprocess.CompletedProcess(
                command, 0, stdout="pass", stderr=""
            ),
        )
        handoff = {
            "status": "HANDOFF",
            "next_agent": "review",
            "review_scope": "external_evidence",
            "task": "Certify the external verification evidence only.",
            "reason": "live run complete",
        }
        self.invoke_agent(
            "coordinator",
            FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(handoff))]),
        )
        review = FakeAgentRunner(
            [codex_stdout("R25", 'HERMES_RESULT={"status":"REVIEW_CLEAN"}')]
        )
        self.invoke_agent("review", review)
        certification = self.state()["review_certification"]
        self.assertEqual(
            certification["external_verification_digest"],
            run_codex._external_verification_digest(evidence),
        )
        self.assertIn("Preserved required external-verification evidence", review.calls[0][2])

        merge = {
            "status": "AWAIT_USER_MERGE",
            "summary": "ready",
            "reviewed_head": certification["head"],
            "draft": False,
        }
        changed_evidence = dict(evidence)
        changed_evidence["stdout"] = "different evidence"
        state = self.state()
        state["external_verification"] = changed_evidence
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        with patch("run_codex._current_pr_body_hash", return_value=None):
            with self.assertRaisesRegex(
                run_codex.InvalidAgentResult, "Evidence-only Review certification"
            ):
                self.invoke_agent(
                    "coordinator",
                    FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(merge))]),
                )

        state = self.state()
        state["external_verification"] = evidence
        state["pending"] = {
            "from": "review",
            "to": "coordinator",
            "payload": {"status": "REVIEW_CLEAN"},
        }
        self.state_file.write_text(json.dumps(state), encoding="utf-8")
        with (
            patch("run_codex._current_pr_body_hash", return_value=None),
            patch("run_codex._current_pr_head", return_value=certification["head"]),
            patch("run_codex._current_pr_is_draft", return_value=False),
        ):
            self.invoke_agent(
                "coordinator",
                FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(merge))]),
            )
        self.assertEqual(self.state()["pending"]["to"], "user")

    def test_new_evidence_only_review_invalidates_prior_evidence_certification(self):
        self.request_executor_verification(command="printf pass")
        evidence = run_codex.invoke_external_verification(
            workflow_id="issue-25",
            repo=self.repo,
            state_file=self.state_file,
            command_runner=lambda command, cwd, timeout_seconds: subprocess.CompletedProcess(
                command, 0, stdout="pass", stderr=""
            ),
        )
        handoff = {
            "status": "HANDOFF",
            "next_agent": "review",
            "review_scope": "external_evidence",
            "task": "Certify the external verification evidence only.",
            "reason": "live run complete",
        }
        self.invoke_agent(
            "coordinator",
            FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(handoff))]),
        )
        self.invoke_agent(
            "review",
            FakeAgentRunner([codex_stdout("R25", 'HERMES_RESULT={"status":"REVIEW_CLEAN"}')]),
        )
        self.assertEqual(
            self.state()["review_certification"]["external_verification_digest"],
            run_codex._external_verification_digest(evidence),
        )

        self.invoke_agent(
            "coordinator",
            FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(handoff))]),
        )
        self.assertIsNone(
            self.state()["review_certification"]["external_verification_digest"]
        )
        changes_required = {
            "status": "CHANGES_REQUIRED",
            "findings": [
                {
                    "severity": "medium",
                    "type": "external/manual gate",
                    "summary": "Evidence is insufficient.",
                    "evidence": "The preserved output does not support the classification.",
                    "remediation_boundary": "Rerun or provide sufficient evidence.",
                }
            ],
        }
        self.invoke_agent(
            "review",
            FakeAgentRunner(
                [codex_stdout("R25", "HERMES_RESULT=" + json.dumps(changes_required))]
            ),
        )

        merge = {
            "status": "AWAIT_USER_MERGE",
            "summary": "ready",
            "reviewed_head": self.full_review_certification()["head"],
            "draft": False,
        }
        with (
            patch("run_codex._current_pr_body_hash", return_value=None),
            patch("run_codex._current_pr_head", return_value=merge["reviewed_head"]),
            patch("run_codex._current_pr_is_draft", return_value=False),
        ):
            with self.assertRaisesRegex(
                run_codex.InvalidAgentResult, "Evidence-only Review certification"
            ):
                self.invoke_agent(
                    "coordinator",
                    FakeAgentRunner(
                        [codex_stdout("C25", "HERMES_RESULT=" + json.dumps(merge))]
                    ),
                )

    def test_coordinator_can_assign_required_verification_to_executor(self):
        requested = self.request_executor_verification()
        state = self.state()
        self.assertEqual(
            state["pending"],
            {"from": "coordinator", "to": "executor", "payload": requested},
        )
        self.assertIsNone(state["external_verification"])
        certification = state["review_certification"]
        self.assertEqual(certification["head"], self.full_review_certification()["head"])
        self.assertIn("external_verification_digest", certification)
        self.assertIsNone(certification["external_verification_digest"])

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
        self.assertEqual(state["review_certification"]["head"], result["head"])
        self.assertIn("external_verification_digest", state["review_certification"])
        self.assertIsNone(state["review_certification"]["external_verification_digest"])
        self.assertEqual(
            state["pending"],
            {"from": "executor", "to": "coordinator", "payload": result},
        )

    def test_unavailable_gate_blocks_review_across_user_decision(self):
        self.request_executor_verification()
        unavailable = self.report_executor_unavailable()
        review_handoff = {
            "status": "HANDOFF",
            "next_agent": "review",
            "task": "Review current HEAD.",
            "reason": "GREEN is ready.",
            "full_test_command": "python -m unittest",
        }
        with self.assertRaisesRegex(
            run_codex.InvalidAgentResult, "unresolved required external verification"
        ):
            self.invoke_agent(
                "coordinator",
                FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(review_handoff))]),
            )
        self.assertEqual(self.state()["pending"]["payload"], unavailable)

        decision = {
            "status": "AWAIT_USER_DECISION",
            "question": "Which environment should Hermes use for this required verification?",
        }
        self.invoke_agent(
            "coordinator",
            FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(decision))]),
        )
        with self.assertRaisesRegex(
            run_codex.InvalidAgentResult, "unresolved required external verification"
        ):
            self.invoke_agent(
                "coordinator",
                FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(review_handoff))]),
                task="Use the staging VPN host.",
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

    def test_structured_user_fallback_requires_unavailable_gate(self):
        head = self._git("rev-parse", "HEAD").stdout.strip()
        decision = {
            "status": "AWAIT_USER_DECISION",
            "question": "Run live verification elsewhere.",
            "external_verification": {
                "command": "pytest -m live",
                "boundary": "real external boundary",
                "reason": "external execution is needed",
                "expected_head": head,
            },
        }
        with self.assertRaisesRegex(run_codex.InvalidAgentResult, "unresolved Hermes unavailability"):
            self.invoke_agent(
                "coordinator",
                FakeAgentRunner([codex_stdout("C25", "HERMES_RESULT=" + json.dumps(decision))]),
            )

    def test_structured_user_fallback_preserves_external_provenance(self):
        command = "pytest -q -m live tests/test_live_integration.py"
        boundary = "real Playwright-backed official scraper boundary"
        self.request_executor_verification(command=command, boundary=boundary)
        self.report_executor_unavailable("Hermes-side capability evidence confirms Chromium is unavailable")
        head = self._git("rev-parse", "HEAD").stdout.strip()
        decision = {
            "status": "AWAIT_USER_DECISION",
            "question": "Run the live command in a Chromium-capable environment and return HEAD plus output.",
            "external_verification": {
                "command": command,
                "boundary": boundary,
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
            "review_scope": "external_evidence",
            "task": "Certify the current external verification evidence only.",
            "reason": "External evidence has been classified.",
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
        with patch("run_codex._current_pr_body_hash", return_value=None):
            self.invoke_agent("review", review)
        review_prompt = review.calls[0][2]
        self.assertIn("externally_supplied", review_prompt)
        self.assertIn("00406A failed", review_prompt)

    def test_structured_user_fallback_requires_current_exact_head(self):
        requested = self.request_executor_verification(
            command="pytest -m live",
            boundary="real external boundary",
        )
        self.report_executor_unavailable()
        decision = {
            "status": "AWAIT_USER_DECISION",
            "question": "Run live verification elsewhere.",
            "external_verification": {
                "command": requested["command"],
                "boundary": requested["boundary"],
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
            "review_scope": "external_evidence",
            "task": "Certify current external evidence only.",
            "reason": "verification complete",
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
        with self.assertRaisesRegex(
            run_codex.InvalidAgentResult, "Full Review certification|external verification.*current HEAD"
        ):
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
            review_certification={
                "head": head,
                "pr_body_hash": None,
                "external_verification_digest": run_codex._external_verification_digest(stale_evidence),
            },
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
