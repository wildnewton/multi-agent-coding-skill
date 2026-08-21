import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import AgentRepositoryMutationError, InvalidAgentResult, invoke_agent


def codex_stdout(thread_id, result):
    message = "HERMES_RESULT=" + json.dumps(result)
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": thread_id},
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
        )
    ) + "\n"


class FakeRunner:
    def __init__(self, result, *, mutation=None, thread_id="T33"):
        self.result = result
        self.mutation = mutation
        self.thread_id = thread_id
        self.calls = []

    def __call__(self, command, cwd, input_text):
        cwd = Path(cwd)
        self.calls.append((command, cwd, input_text))
        if self.mutation is not None:
            self.mutation(cwd)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=codex_stdout(self.thread_id, self.result),
            stderr="",
        )


class TestFixCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("clean\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "fixture.txt").write_text("old\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md", "tests/fixture.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.repo, check=True, capture_output=True)

        self.prompts = root / "prompts"
        self.prompts.mkdir()
        for role in ("testing", "coordinator", "task_review", "review"):
            (self.prompts / f"{role}.md").write_text(f"ROLE:{role}\n", encoding="utf-8")
        self.state_file = root / "state.json"
        self.write_state()

    def write_state(self, pending=None):
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-33",
                    "sessions": {},
                    "pending": pending,
                    "task_review_clean_checkpoint": "clean",
                    "review_certification": None,
                }
            ),
            encoding="utf-8",
        )

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def invoke(self, agent, runner, task="ignored", timeout_seconds=10):
        with (
            patch("run_codex._publish_handoff_trace"),
            patch("run_codex._publish_specialist_failure_trace"),
        ):
            return invoke_agent(
                agent=agent,
                workflow_id="issue-33",
                repo=self.repo,
                task=task,
                state_file=self.state_file,
                prompt_dir=self.prompts,
                runner=runner,
                timeout_seconds=timeout_seconds,
            )

    def test_coordinator_accepts_explicit_test_fix_handoff(self):
        result = {
            "status": "HANDOFF",
            "next_agent": "testing",
            "testing_intent": "test_fix",
            "allowed_paths": ["tests/fixture.txt"],
            "task": "Correct confirmed stale fixture",
            "reason": "Fresh Review confirmed a test-only defect",
        }
        self.invoke("coordinator", FakeRunner(result, thread_id="C33"), task="canonical task")
        self.assertEqual(self.state()["pending"]["payload"], result)

    def test_coordinator_rejects_test_fix_without_allowed_paths(self):
        result = {
            "status": "HANDOFF",
            "next_agent": "testing",
            "testing_intent": "test_fix",
            "task": "Correct confirmed stale fixture",
            "reason": "Fresh Review confirmed a test-only defect",
        }
        with self.assertRaisesRegex(InvalidAgentResult, "non-empty allowed_paths"):
            self.invoke("coordinator", FakeRunner(result, thread_id="C33"), task="canonical task")
        self.assertIsNone(self.state()["pending"])

    def test_coordinator_rejects_non_relative_allowed_path(self):
        result = {
            "status": "HANDOFF",
            "next_agent": "testing",
            "testing_intent": "test_fix",
            "allowed_paths": ["../production.py"],
            "task": "Correct confirmed stale fixture",
            "reason": "Fresh Review confirmed a test-only defect",
        }
        with self.assertRaisesRegex(InvalidAgentResult, "repository-relative"):
            self.invoke("coordinator", FakeRunner(result, thread_id="C33"), task="canonical task")

    def test_valid_test_fix_completion_requires_passing_command_and_returns_to_coordinator(self):
        pending = {
            "from": "coordinator",
            "to": "testing",
            "payload": {
                "status": "HANDOFF",
                "next_agent": "testing",
                "testing_intent": "test_fix",
                "allowed_paths": ["tests/fixture.txt"],
                "task": "Correct confirmed stale fixture",
                "reason": "test-only defect",
            },
        }
        self.write_state(pending)
        result = {
            "status": "TEST_FIX_COMPLETE",
            "test_command": "true",
            "summary": "Updated the stale fixture",
        }
        runner = FakeRunner(
            result,
            mutation=lambda repo: (repo / "tests" / "fixture.txt").write_text("fixed\n", encoding="utf-8"),
        )
        self.invoke("testing", runner)
        self.assertEqual(
            self.state()["pending"],
            {"from": "testing", "to": "coordinator", "payload": result},
        )

    def test_failing_test_fix_command_preserves_pending(self):
        pending = {
            "from": "coordinator",
            "to": "testing",
            "payload": {
                "status": "HANDOFF",
                "next_agent": "testing",
                "testing_intent": "test_fix",
                "allowed_paths": ["tests/fixture.txt"],
                "task": "Correct confirmed stale fixture",
                "reason": "test-only defect",
            },
        }
        self.write_state(pending)
        runner = FakeRunner(
            {"status": "TEST_FIX_COMPLETE", "test_command": "false"},
            mutation=lambda repo: (repo / "tests" / "fixture.txt").write_text("fixed\n", encoding="utf-8"),
        )
        with self.assertRaisesRegex(InvalidAgentResult, "must pass"):
            self.invoke("testing", runner)
        self.assertEqual(self.state()["pending"], pending)

    def test_test_fix_rejects_change_outside_allowed_paths(self):
        pending = {
            "from": "coordinator",
            "to": "testing",
            "payload": {
                "status": "HANDOFF",
                "next_agent": "testing",
                "testing_intent": "test_fix",
                "allowed_paths": ["tests/fixture.txt"],
                "task": "Correct confirmed stale fixture",
                "reason": "test-only defect",
            },
        }
        self.write_state(pending)

        def mutate(repo):
            (repo / "tests" / "fixture.txt").write_text("fixed\n", encoding="utf-8")
            (repo / "production.py").write_text("bad\n", encoding="utf-8")

        runner = FakeRunner(
            {"status": "TEST_FIX_COMPLETE", "test_command": "true"},
            mutation=mutate,
        )
        with self.assertRaisesRegex(AgentRepositoryMutationError, "outside allowed_paths: production.py"):
            self.invoke("testing", runner)
        self.assertEqual(self.state()["pending"], pending)

    def test_test_fix_verification_command_cannot_mutate_worktree(self):
        pending = {
            "from": "coordinator",
            "to": "testing",
            "payload": {
                "status": "HANDOFF",
                "next_agent": "testing",
                "testing_intent": "test_fix",
                "allowed_paths": ["tests/fixture.txt"],
                "task": "Correct confirmed stale fixture",
                "reason": "test-only defect",
            },
        }
        self.write_state(pending)
        command = (
            "python -c \"from pathlib import Path; "
            "Path('generated.txt').write_text('x')\""
        )
        runner = FakeRunner(
            {"status": "TEST_FIX_COMPLETE", "test_command": command},
            mutation=lambda repo: (repo / "tests" / "fixture.txt").write_text("fixed\n", encoding="utf-8"),
        )
        with self.assertRaisesRegex(
            AgentRepositoryMutationError,
            "TEST_FIX_COMPLETE verification command modified the worktree",
        ):
            self.invoke("testing", runner)
        self.assertEqual(self.state()["pending"], pending)
        self.assertTrue((self.repo / "generated.txt").exists())

    def test_test_fix_result_is_rejected_from_ordinary_red_handoff(self):
        pending = {
            "from": "coordinator",
            "to": "testing",
            "payload": {
                "status": "HANDOFF",
                "next_agent": "testing",
                "task": "Add RED coverage",
                "reason": "Need RED",
            },
        }
        self.write_state(pending)
        runner = FakeRunner({"status": "TEST_FIX_COMPLETE", "test_command": "true"})
        with self.assertRaisesRegex(InvalidAgentResult, "requires a pending test_fix handoff"):
            self.invoke("testing", runner)
        self.assertEqual(self.state()["pending"], pending)

    def test_red_result_is_rejected_from_test_fix_handoff(self):
        pending = {
            "from": "coordinator",
            "to": "testing",
            "payload": {
                "status": "HANDOFF",
                "next_agent": "testing",
                "testing_intent": "test_fix",
                "allowed_paths": ["tests/fixture.txt"],
                "task": "Correct confirmed stale fixture",
                "reason": "test-only defect",
            },
        }
        self.write_state(pending)
        runner = FakeRunner({"status": "RED_COMPLETE", "test_command": "false"})
        with self.assertRaisesRegex(InvalidAgentResult, "must complete with TEST_FIX_COMPLETE"):
            self.invoke("testing", runner)
        self.assertEqual(self.state()["pending"], pending)


if __name__ == "__main__":
    unittest.main()
