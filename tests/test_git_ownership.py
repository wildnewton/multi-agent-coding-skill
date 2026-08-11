import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from run_codex import InvalidAgentResult, invoke_agent


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
    def __init__(self, output):
        self.output = output
        self.calls = []

    def __call__(self, command, cwd, input_text):
        self.calls.append((command, Path(cwd), input_text))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=self.output,
            stderr="",
        )


class GitOwnershipContractTests(unittest.TestCase):
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

    def invoke(self, agent, output):
        runner = FakeRunner(codex_stdout(f"{agent}-thread", output))
        result = invoke_agent(
            agent=agent,
            workflow_id="issue-137",
            repo=self.repo,
            task="do the task",
            state_file=self.state_file,
            prompt_dir=self.prompts,
            runner=runner,
        )
        return result, runner

    def test_dirty_worktree_is_rejected_before_codex(self):
        (self.repo / "README.md").write_text("dirty\n", encoding="utf-8")
        runner = FakeRunner(
            codex_stdout(
                "testing-thread",
                'HERMES_RESULT={"status":"RED_COMPLETE",'
                '"test_command":"pytest tests/test_feature.py","summary":"RED"}',
            )
        )

        with self.assertRaisesRegex(RuntimeError, "clean worktree"):
            invoke_agent(
                agent="testing",
                workflow_id="issue-137",
                repo=self.repo,
                task="add RED coverage",
                state_file=self.state_file,
                prompt_dir=self.prompts,
                runner=runner,
            )

        self.assertEqual(runner.calls, [])

    def test_red_complete_requires_test_command(self):
        with self.assertRaises(InvalidAgentResult):
            self.invoke(
                "testing",
                'HERMES_RESULT={"status":"RED_COMPLETE","summary":"RED"}',
            )

    def test_testing_result_rejects_agent_owned_commit(self):
        with self.assertRaises(InvalidAgentResult):
            self.invoke(
                "testing",
                'HERMES_RESULT={"status":"RED_COMPLETE","commit":"abc123",'
                '"test_command":"pytest tests/test_feature.py","summary":"RED"}',
            )

    def test_review_handoff_does_not_require_agent_owned_commit(self):
        result, _ = self.invoke(
            "coordinator",
            'HERMES_RESULT={"status":"HANDOFF","next_agent":"review",'
            '"task":"Review verified GREEN","test_command":"pytest tests/test_feature.py",'
            '"full_test_command":"pytest"}',
        )

        self.assertEqual(result["status"], "HANDOFF")
        self.assertEqual(result["next_agent"], "review")
        self.assertNotIn("commit", result)

    def test_review_handoff_rejects_agent_owned_commit(self):
        with self.assertRaises(InvalidAgentResult):
            self.invoke(
                "coordinator",
                'HERMES_RESULT={"status":"HANDOFF","next_agent":"review",'
                '"task":"Review verified GREEN","commit":"abc123",'
                '"test_command":"pytest tests/test_feature.py",'
                '"full_test_command":"pytest"}',
            )

    def test_await_user_merge_requires_explicit_draft_false(self):
        invalid_results = (
            'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
            '"reviewed_head":"abc123"}',
            'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
            '"reviewed_head":"abc123","draft":true}',
        )
        for output in invalid_results:
            with self.subTest(output=output):
                with self.assertRaises(InvalidAgentResult):
                    self.invoke("coordinator", output)

    def test_await_user_merge_accepts_explicit_draft_false(self):
        result, _ = self.invoke(
            "coordinator",
            'HERMES_RESULT={"status":"AWAIT_USER_MERGE",'
            '"reviewed_head":"abc123","draft":false}',
        )

        self.assertEqual(result["status"], "AWAIT_USER_MERGE")
        self.assertIs(result["draft"], False)


if __name__ == "__main__":
    unittest.main()
