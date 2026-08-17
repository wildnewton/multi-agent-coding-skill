import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_codex import InvalidAgentResult, invoke_agent


def codex_stdout(thread_id, final_message):
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final_message}},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeRunner:
    def __init__(self, output, mutation=None):
        self.output = output
        self.mutation = mutation
        self.calls = []

    def __call__(self, command, cwd, input_text):
        cwd = Path(cwd)
        self.calls.append((command, cwd, input_text))
        if self.mutation is not None:
            self.mutation(cwd)
        return subprocess.CompletedProcess(command, 0, stdout=self.output, stderr="")


class GitOwnershipContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "target-repo"
        self.repo.mkdir()
        self.remote = self.root / "origin.git"
        self.state_file = self.root / "workflow.json"
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
        self._git("branch", "-M", "feature")
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, text=True, capture_output=True)
        self._git("remote", "add", "origin", str(self.remote))
        self._git("push", "-u", "origin", "feature")
        self.write_state()

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, text=True, capture_output=True)

    def write_state(self, *, pending=None, review_certification=None):
        self.state_file.write_text(
            json.dumps(
                {
                    "workflow_id": "issue-137",
                    "sessions": {},
                    "pending": pending,
                    "review_certification": review_certification,
                    "task_review_clean_checkpoint": "fixture-approved",
                }
            ),
            encoding="utf-8",
        )

    def prime_pending(self, agent):
        self.write_state(
            pending={
                "from": "coordinator",
                "to": agent,
                "payload": {
                    "status": "HANDOFF",
                    "next_agent": agent,
                    "task": "do the task",
                    "reason": "needed",
                },
            }
        )

    def invoke_with_runner(self, agent, runner):
        current_head = self._git("rev-parse", "HEAD").stdout.strip()
        with (
            patch("run_codex._publish_handoff_trace"),
            patch("run_codex._publish_specialist_failure_trace"),
            patch("run_codex._current_pr_body_hash", return_value=None),
            patch("run_codex._current_pr_head", return_value=current_head),
            patch("run_codex._current_pr_is_draft", return_value=False),
        ):
            return invoke_agent(
                agent=agent,
                workflow_id="issue-137",
                repo=self.repo,
                task="do the task",
                state_file=self.state_file,
                prompt_dir=self.prompts,
                runner=runner,
            )

    def invoke(self, agent, output):
        runner = FakeRunner(codex_stdout(f"{agent}-thread", output))
        return self.invoke_with_runner(agent, runner), runner

    def test_dirty_worktree_is_rejected_before_codex(self):
        (self.repo / "README.md").write_text("dirty\n", encoding="utf-8")
        runner = FakeRunner(codex_stdout("testing-thread", 'HERMES_RESULT={"status":"RED_COMPLETE","test_command":"pytest"}'))
        with self.assertRaisesRegex(RuntimeError, "clean worktree"):
            self.invoke_with_runner("testing", runner)
        self.assertEqual(runner.calls, [])

    def test_agent_cannot_change_local_head(self):
        self.prime_pending("testing")
        def mutate(repo):
            subprocess.run(["git", "commit", "--allow-empty", "-m", "agent commit"], cwd=repo, check=True, text=True, capture_output=True)
        runner = FakeRunner(codex_stdout("testing-thread", 'HERMES_RESULT={"status":"RED_COMPLETE","test_command":"pytest"}'), mutation=mutate)
        with self.assertRaisesRegex(RuntimeError, "local git state"):
            self.invoke_with_runner("testing", runner)

    def test_agent_cannot_advance_remote_branch_without_local_head_change(self):
        original_head = self._git("rev-parse", "HEAD").stdout.strip()
        def mutate(repo):
            subprocess.run(["git", "commit", "--allow-empty", "-m", "remote-only agent commit"], cwd=repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "HEAD:feature"], cwd=repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "reset", "--hard", original_head], cwd=repo, check=True, text=True, capture_output=True)
        runner = FakeRunner(codex_stdout("coordinator-thread", 'HERMES_RESULT={"status":"HANDOFF","next_agent":"testing","task":"Add RED coverage","reason":"Coverage is missing"}'), mutation=mutate)
        with self.assertRaisesRegex(RuntimeError, "remote branch"):
            self.invoke_with_runner("coordinator", runner)

    def test_review_cannot_modify_worktree_files(self):
        self.prime_pending("review")
        def mutate(repo):
            (repo / "README.md").write_text("review changed this\n", encoding="utf-8")
        runner = FakeRunner(codex_stdout("review-thread", 'HERMES_RESULT={"status":"REVIEW_CLEAN"}'), mutation=mutate)
        with self.assertRaisesRegex(RuntimeError, "Review.*worktree"):
            self.invoke_with_runner("review", runner)

    def test_red_complete_requires_test_command(self):
        self.prime_pending("testing")
        with self.assertRaises(InvalidAgentResult):
            self.invoke("testing", 'HERMES_RESULT={"status":"RED_COMPLETE","summary":"RED"}')

    def test_testing_result_rejects_agent_owned_commit(self):
        self.prime_pending("testing")
        with self.assertRaises(InvalidAgentResult):
            self.invoke("testing", 'HERMES_RESULT={"status":"RED_COMPLETE","commit":"abc123","test_command":"pytest"}')

    def test_review_handoff_does_not_repeat_targeted_test_command(self):
        result, _ = self.invoke("coordinator", 'HERMES_RESULT={"status":"HANDOFF","next_agent":"review","task":"Review verified GREEN","reason":"GREEN is ready","full_test_command":"pytest"}')
        self.assertEqual(result["status"], "HANDOFF")
        self.assertNotIn("test_command", result)
        self.assertNotIn("commit", result)

    def test_review_handoff_rejects_agent_owned_commit(self):
        with self.assertRaises(InvalidAgentResult):
            self.invoke("coordinator", 'HERMES_RESULT={"status":"HANDOFF","next_agent":"review","task":"Review verified GREEN","reason":"GREEN is ready","commit":"abc123","full_test_command":"pytest"}')

    def test_await_user_merge_requires_explicit_draft_false(self):
        head = self._git("rev-parse", "HEAD").stdout.strip()
        self.write_state(review_certification={"head": head, "pr_body_hash": None})
        for output in (
            f'HERMES_RESULT={{"status":"AWAIT_USER_MERGE","reviewed_head":"{head}"}}',
            f'HERMES_RESULT={{"status":"AWAIT_USER_MERGE","reviewed_head":"{head}","draft":true}}',
        ):
            with self.subTest(output=output):
                with self.assertRaises(InvalidAgentResult):
                    self.invoke("coordinator", output)

    def test_await_user_merge_accepts_explicit_draft_false_after_clean_review(self):
        head = self._git("rev-parse", "HEAD").stdout.strip()
        self.write_state(review_certification={"head": head, "pr_body_hash": None})
        result, _ = self.invoke("coordinator", f'HERMES_RESULT={{"status":"AWAIT_USER_MERGE","reviewed_head":"{head}","draft":false}}')
        self.assertEqual(result["status"], "AWAIT_USER_MERGE")
        self.assertIs(result["draft"], False)


if __name__ == "__main__":
    unittest.main()
