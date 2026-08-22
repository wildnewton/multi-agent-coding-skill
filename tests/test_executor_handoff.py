import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import CodexInvocationError, InvalidAgentResult, MergePrHeadMismatch, invoke_agent


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

    def test_handoff_acceptance_does_not_publish_before_dispatch(self):
        with patch("run_codex._publish_handoff_trace") as publish:
            self.handoff_testing()
        publish.assert_not_called()

    def test_dispatch_trace_uses_post_bridge_head(self):
        self.handoff_testing()
        self._git("commit", "--allow-empty", "-m", "Hermes bridge commit")
        bridged_head = self._git("rev-parse", "HEAD").stdout.strip()
        testing = FakeRunner([codex_stdout("T21", TESTING_RESULT)])
        with (
            patch("run_codex._has_origin", return_value=True),
            patch("run_codex._current_pr_number", return_value=22),
            patch("run_codex._current_pr_head", return_value=bridged_head),
            patch("run_codex._publish_handoff_trace") as publish,
        ):
            self.invoke("testing", testing)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(publish.call_args.args[2]["from"], "coordinator")
        self.assertEqual(publish.call_args.args[2]["to"], "testing")
        self.assertEqual(publish.call_args.kwargs["head"], bridged_head)

    def test_dispatch_rejects_pr_head_behind_local_head(self):
        self.handoff_testing()
        original = self.state()["pending"]
        self._git("commit", "--allow-empty", "-m", "unpublished bridge commit")
        testing = FakeRunner([codex_stdout("T21", TESTING_RESULT)])
        with (
            patch("run_codex._has_origin", return_value=True),
            patch("run_codex._current_pr_number", return_value=22),
            patch("run_codex._current_pr_head", return_value="stale-pr-head"),
            patch("run_codex._publish_handoff_trace") as publish,
        ):
            with self.assertRaisesRegex(InvalidAgentResult, "actual PR HEAD to match local HEAD"):
                self.invoke("testing", testing)
        self.assertEqual(testing.calls, [])
        publish.assert_not_called()
        self.assertEqual(self.state()["pending"], original)

    def test_red_result_cannot_dispatch_coordinator_before_draft_pr_exists(self):
        self.handoff_testing()
        testing = FakeRunner([codex_stdout("T21", TESTING_RESULT)])
        self.invoke("testing", testing)
        accepted = self.state()["pending"]
        coordinator = FakeRunner(
            [codex_stdout("C21", 'HERMES_RESULT={"status":"BLOCKED","summary":"done"}')]
        )
        with (
            patch("run_codex._has_origin", return_value=True),
            patch("run_codex._current_pr_number", return_value=None),
            patch("run_codex._publish_handoff_trace") as publish,
        ):
            with self.assertRaisesRegex(InvalidAgentResult, "requires a Draft PR"):
                self.invoke("coordinator", coordinator)
        self.assertEqual(coordinator.calls, [])
        publish.assert_not_called()
        self.assertEqual(self.state()["pending"], accepted)

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
            {"from": "testing", "to": "coordinator", "payload": TESTING_RESULT_DICT},
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

    def test_task_review_uses_handoff_task_and_certifies_that_checkpoint(self):
        handoff = "HERMES_RESULT=" + json.dumps(
            {
                "status": "HANDOFF",
                "next_agent": "task_review",
                "task": "Review exact task contract",
                "reason": "Independent task review is required",
            }
        )
        coordinator = FakeRunner([codex_stdout("C21", handoff)])
        self.invoke("coordinator", coordinator, task="canonical issue")
        review_result = {
            "status": "TASK_REVIEW_CLEAN",
            "evidence_and_root_cause": "confirmed",
            "clearer_requirement": "clear",
            "acceptance_criteria": "testable",
            "simplest_approach": "minimal",
        }
        task_review = FakeRunner(
            [codex_stdout("TR21", "HERMES_RESULT=" + json.dumps(review_result))]
        )
        self.invoke("task_review", task_review, task="WRONG EXTERNAL TASK")
        prompt = task_review.calls[0][2]
        self.assertIn("Review exact task contract", prompt)
        self.assertIn("Independent task review is required", prompt)
        self.assertNotIn("WRONG EXTERNAL TASK", prompt)
        state = self.state()
        self.assertIsNotNone(state["task_review_clean_checkpoint"])
        self.assertEqual(state["pending"]["from"], "task_review")
        self.assertEqual(state["pending"]["to"], "coordinator")
        self.assertEqual(
            state["pending"]["payload"]["task_review_checkpoint"],
            state["task_review_clean_checkpoint"],
        )

    def test_user_answer_is_persisted_before_coordinator_runs_and_survives_retry(self):
        decision = (
            'HERMES_RESULT={"status":"AWAIT_USER_DECISION",'
            '"question":"Preserve compatibility?"}'
        )
        first = FakeRunner([codex_stdout("C21", decision)])
        self.invoke("coordinator", first, task="decide compatibility")
        self.assertEqual(self.state()["pending"]["to"], "user")

        def timeout_runner(command, cwd, input_text):
            raise subprocess.TimeoutExpired(command, 10)

        with self.assertRaises(CodexInvocationError):
            self.invoke(
                "coordinator",
                timeout_runner,
                task="Yes, preserve compatibility",
            )

        self.assertEqual(
            self.state()["pending"],
            {
                "from": "user",
                "to": "coordinator",
                "payload": {
                    "question": {
                        "status": "AWAIT_USER_DECISION",
                        "question": "Preserve compatibility?",
                    },
                    "answer": "Yes, preserve compatibility",
                },
            },
        )

        handoff = "HERMES_RESULT=" + json.dumps(
            {
                "status": "HANDOFF",
                "next_agent": "testing",
                "task": "Add RED for compatibility",
                "reason": "User chose compatibility",
            }
        )
        resumed = FakeRunner([codex_stdout("C21", handoff)])
        self.invoke("coordinator", resumed, task="WRONG RECONSTRUCTED ANSWER")
        prompt = resumed.calls[0][2]
        self.assertIn("Preserve compatibility?", prompt)
        self.assertIn("Yes, preserve compatibility", prompt)
        self.assertNotIn("WRONG RECONSTRUCTED ANSWER", prompt)
        self.assertEqual(self.state()["pending"]["to"], "testing")

    def test_await_user_merge_follow_up_resumes_coordinator(self):
        state = self.state()
        state["pending"] = {
            "from": "coordinator",
            "to": "user",
            "payload": {
                "status": "AWAIT_USER_MERGE",
                "summary": "PR is merge-ready",
                "reviewed_head": "reviewed-sha",
                "draft": False,
            },
        }
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

        handoff = "HERMES_RESULT=" + json.dumps(
            {
                "status": "HANDOFF",
                "next_agent": "testing",
                "task": "Add focused coverage for the newly reported failure",
                "reason": "The user requested more work on the same PR",
            }
        )
        coordinator = FakeRunner([codex_stdout("C21", handoff)])
        self.invoke(
            "coordinator",
            coordinator,
            task="Investigate the still-failing CTBC live smoke cases",
        )

        prompt = coordinator.calls[0][2]
        self.assertIn("AWAIT_USER_MERGE", prompt)
        self.assertIn("reviewed-sha", prompt)
        self.assertIn("Investigate the still-failing CTBC live smoke cases", prompt)
        self.assertEqual(self.state()["workflow_id"], "issue-21")
        self.assertEqual(self.state()["pending"]["to"], "testing")

    def test_review_certification_binds_pr_body_hash_and_stale_body_blocks_merge(self):
        head = self._git("rev-parse", "HEAD").stdout.strip()
        review_handoff = "HERMES_RESULT=" + json.dumps(
            {
                "status": "HANDOFF",
                "next_agent": "review",
                "task": "Review GREEN",
                "reason": "GREEN complete",
                "full_test_command": "python -m unittest",
            }
        )
        coordinator = FakeRunner([codex_stdout("C21", review_handoff)])
        self.invoke("coordinator", coordinator, task="route review")
        review = FakeRunner([codex_stdout("R21", 'HERMES_RESULT={"status":"REVIEW_CLEAN"}')])
        with patch("run_codex._current_pr_body_hash", return_value="body-v1"):
            self.invoke("review", review)
        self.assertEqual(
            self.state()["review_certification"],
            {"head": head, "pr_body_hash": "body-v1"},
        )

        merge_result = (
            'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
            f'"reviewed_head":"{head}","draft":false}}'
        )
        merge = FakeRunner([codex_stdout("C21", merge_result)])
        with patch("run_codex._current_pr_body_hash", return_value="body-v2"):
            with self.assertRaises(Exception):
                self.invoke("coordinator", merge)

    def test_pr_head_mismatch_does_not_publish_merge_readiness(self):
        head = self._git("rev-parse", "HEAD").stdout.strip()
        review_handoff = "HERMES_RESULT=" + json.dumps(
            {
                "status": "HANDOFF",
                "next_agent": "review",
                "task": "Review GREEN",
                "reason": "GREEN complete",
                "full_test_command": "python -m unittest",
            }
        )
        self.invoke(
            "coordinator",
            FakeRunner([codex_stdout("C21", review_handoff)]),
            task="route review",
        )
        with patch("run_codex._current_pr_body_hash", return_value="body-v1"):
            self.invoke(
                "review",
                FakeRunner([codex_stdout("R21", 'HERMES_RESULT={"status":"REVIEW_CLEAN"}')]),
            )

        merge_result = (
            'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
            f'"reviewed_head":"{head}","draft":false}}'
        )
        with (
            patch("run_codex._current_pr_body_hash", return_value="body-v1"),
            patch("run_codex._current_pr_head", return_value="moved-head"),
            patch("run_codex._publish_handoff_trace") as publish,
        ):
            with self.assertRaises(MergePrHeadMismatch):
                self.invoke(
                    "coordinator",
                    FakeRunner([codex_stdout("C21", merge_result)]),
                )

        self.assertIsNone(self.state()["pending"])
        self.assertEqual(publish.call_count, 1)
        reverse = publish.call_args.args[2]
        self.assertEqual((reverse["from"], reverse["to"]), ("review", "coordinator"))

    def test_task_review_blocked_trace_keeps_summary(self):
        handoff = "HERMES_RESULT=" + json.dumps(
            {
                "status": "HANDOFF",
                "next_agent": "task_review",
                "task": "Review task",
                "reason": "Need independent review",
            }
        )
        self.invoke(
            "coordinator",
            FakeRunner([codex_stdout("C21", handoff)]),
            task="canonical issue",
        )
        blocked = 'HERMES_RESULT={"status":"BLOCKED","summary":"Missing repository evidence"}'
        with patch("run_codex._publish_specialist_failure_trace") as publish:
            self.invoke("task_review", FakeRunner([codex_stdout("TR21", blocked)]))
        self.assertEqual(publish.call_args.kwargs["reason"], "BLOCKED: Missing repository evidence")

    def test_formal_agent_handoffs_publish_trace_when_receiver_is_dispatched(self):
        blocked = 'HERMES_RESULT={"status":"BLOCKED","summary":"stop after result"}'
        with patch("run_codex._publish_handoff_trace") as publish:
            self.handoff_testing()
            testing = FakeRunner([codex_stdout("T21", TESTING_RESULT)])
            self.invoke("testing", testing)
            coordinator = FakeRunner([codex_stdout("C21", blocked)])
            self.invoke("coordinator", coordinator)
        self.assertEqual(publish.call_count, 2)
        first = publish.call_args_list[0].args[2]
        second = publish.call_args_list[1].args[2]
        self.assertEqual((first["from"], first["to"]), ("coordinator", "testing"))
        self.assertEqual((second["from"], second["to"]), ("testing", "coordinator"))


if __name__ == "__main__":
    unittest.main()
