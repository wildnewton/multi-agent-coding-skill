#!/usr/bin/env python3
"""Thin Codex CLI wrapper for the multi-agent-coding Hermes skill."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable


AGENTS = {
    "testing": {
        "prompt": "testing.md",
        "persistent": True,
        "statuses": {"RED_COMPLETE", "BLOCKED"},
    },
    "coordinator": {
        "prompt": "coordinator.md",
        "persistent": True,
        "statuses": {"HANDOFF", "AWAIT_USER_DECISION", "AWAIT_USER_MERGE", "BLOCKED"},
    },
    "review": {
        "prompt": "review.md",
        "persistent": False,
        "statuses": {"REVIEW_CLEAN", "CHANGES_REQUIRED", "BLOCKED"},
    },
}

RESULT_MARKER = "HERMES_RESULT="
DEFAULT_AGENT_TIMEOUT_SECONDS = 1800
GIT_OWNERSHIP_POLICY = """Repository state policy:
- Hermes owns all git and GitHub mutations.
- You may inspect repository state with read-only commands such as git status, git diff, git log, git show, git rev-parse, and read-only gh queries.
- Do not run git add, commit, push, restore, checkout, reset, rebase, merge, clean, or other commands that mutate git state.
- Do not mutate remote repository state through gh, gh api, or another API.
- Leave permitted file edits unstaged in the shared working tree for Hermes to validate and commit.
"""


class CodexInvocationError(RuntimeError):
    """Raised when Codex cannot be invoked successfully."""


class InvalidAgentResult(RuntimeError):
    """Raised when an agent does not return the required result contract."""


class DirtyWorktreeError(RuntimeError):
    """Raised when an agent invocation does not start from a clean worktree."""


class AgentRepositoryMutationError(RuntimeError):
    """Raised when a Codex agent mutates git or remote repository state."""


def _default_runner(command, cwd, input_text, *, timeout_seconds):
    return subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )


def _load_state(path: Path, workflow_id: str) -> dict:
    if not path.exists():
        return {
            "workflow_id": workflow_id,
            "sessions": {},
            "pending_agent": None,
            "pending_result_ready": False,
            "review_clean_head": None,
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("workflow_id") not in (None, workflow_id):
        raise ValueError(
            f"state file belongs to workflow {state.get('workflow_id')!r}, "
            f"not {workflow_id!r}"
        )
    state.setdefault("workflow_id", workflow_id)
    state.setdefault("sessions", {})
    state.setdefault("pending_agent", None)
    state.setdefault("pending_result_ready", False)
    state.setdefault("review_clean_head", None)
    return state


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _git(repo: Path, *args: str, allow_failure: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0 and not allow_failure:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise DirtyWorktreeError(
            f"unable to verify repository state with git {' '.join(args)}: {detail}"
        )
    return completed


def _worktree_status(repo: Path) -> list[str]:
    completed = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return [line for line in completed.stdout.splitlines() if line.strip()]


def _ensure_clean_worktree(repo: Path) -> None:
    if _worktree_status(repo):
        raise DirtyWorktreeError(
            "agent invocation requires a clean worktree; Hermes must commit or "
            "discard the previous agent's changes first"
        )


def _ensure_read_only_worktree(repo: Path, context: str) -> None:
    try:
        _ensure_clean_worktree(repo)
    except DirtyWorktreeError as exc:
        raise AgentRepositoryMutationError(
            f"{context} modified the worktree; this invocation must be read-only"
        ) from exc


def _remote_branch_head(repo: Path, branch: str) -> tuple[bool, str | None]:
    origin = _git(repo, "remote", "get-url", "origin", allow_failure=True)
    if origin.returncode != 0 or not origin.stdout.strip() or not branch:
        return False, None

    remote = _git(
        repo,
        "ls-remote",
        "--heads",
        "origin",
        f"refs/heads/{branch}",
    )
    line = remote.stdout.strip()
    return True, line.split()[0] if line else None


def _capture_repository_guard(repo: Path) -> dict:
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    branch = _git(repo, "branch", "--show-current").stdout.strip()
    staged = _git(repo, "diff", "--cached", "--name-only").stdout.strip()
    remote_checked, remote_head = _remote_branch_head(repo, branch)
    return {
        "head": head,
        "branch": branch,
        "staged": staged,
        "remote_checked": remote_checked,
        "remote_head": remote_head,
    }


def _verify_agent_did_not_mutate_repository(repo: Path, before: dict) -> None:
    after = _capture_repository_guard(repo)
    if (
        after["head"] != before["head"]
        or after["branch"] != before["branch"]
        or after["staged"]
    ):
        raise AgentRepositoryMutationError(
            "local git state changed during agent invocation; Hermes exclusively owns git mutations"
        )
    if before["remote_checked"] and after["remote_head"] != before["remote_head"]:
        raise AgentRepositoryMutationError(
            "remote branch changed during agent invocation; Hermes exclusively owns remote mutations"
        )


def _iter_strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _parse_output(stdout: str) -> tuple[str | None, dict]:
    thread_id = None
    candidate_strings = []

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            candidate_strings.append(line)
            continue

        if event.get("type") == "thread.started" and event.get("thread_id"):
            thread_id = event["thread_id"]
        candidate_strings.extend(_iter_strings(event))

    decoder = json.JSONDecoder()
    for text in reversed(candidate_strings):
        marker_at = text.rfind(RESULT_MARKER)
        if marker_at < 0:
            continue
        payload = text[marker_at + len(RESULT_MARKER) :].lstrip()
        try:
            result, _ = decoder.raw_decode(payload)
        except json.JSONDecodeError as exc:
            raise InvalidAgentResult("HERMES_RESULT is not valid JSON") from exc
        if not isinstance(result, dict) or not result.get("status"):
            raise InvalidAgentResult("HERMES_RESULT must be a JSON object with status")
        return thread_id, result

    raise InvalidAgentResult("Codex output did not contain HERMES_RESULT")


def _build_prompt(role_text: str, workflow_id: str, task: str, include_role: bool) -> str:
    parts = []
    if include_role:
        parts.append(role_text.strip())
    parts.extend(
        [
            GIT_OWNERSHIP_POLICY.strip(),
            f"Workflow: {workflow_id}",
            "Current task:",
            task.strip(),
            "",
            "Finish your final response with exactly one machine-readable line starting "
            "with HERMES_RESULT= followed by a JSON object containing at least a status field.",
        ]
    )
    return "\n\n".join(parts).strip() + "\n"


def _require_nonempty_text(result: dict, field: str, context: str) -> None:
    value = result.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidAgentResult(f"{context} must include non-empty {field}")


def _has_nonempty_text(result: dict, field: str) -> bool:
    value = result.get(field)
    return isinstance(value, str) and bool(value.strip())


def invoke_agent(
    *,
    agent: str,
    workflow_id: str,
    repo: str | Path,
    task: str,
    state_file: str | Path,
    prompt_dir: str | Path,
    runner: Callable | None = None,
    timeout_seconds: int = DEFAULT_AGENT_TIMEOUT_SECONDS,
    completed_agent: str | None = None,
) -> dict:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}; expected one of {', '.join(AGENTS)}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if completed_agent not in {None, "testing", "review"}:
        raise ValueError("completed_agent must be testing, review, or None")
    if completed_agent is not None and agent != "coordinator":
        raise InvalidAgentResult(
            "completed_agent may only be supplied when resuming Coordinator"
        )

    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise ValueError(f"repo is not a directory: {repo}")
    _ensure_clean_worktree(repo)
    repository_guard = _capture_repository_guard(repo)

    state_file = Path(state_file)
    prompt_dir = Path(prompt_dir)
    config = AGENTS[agent]
    role_path = prompt_dir / config["prompt"]
    role_text = role_path.read_text(encoding="utf-8")
    state = _load_state(state_file, workflow_id)

    if completed_agent is not None:
        if state.get("pending_agent") != completed_agent:
            raise InvalidAgentResult(
                f"cannot complete {completed_agent!r}; pending_agent is "
                f"{state.get('pending_agent')!r}"
            )
        if not state.get("pending_result_ready"):
            raise InvalidAgentResult(
                f"cannot complete {completed_agent!r}; the pending specialist "
                "has not produced a completed role-valid result"
            )
        state["pending_agent"] = None
        state["pending_result_ready"] = False
        _save_state(state_file, state)

    pending_agent = state.get("pending_agent")
    if (
        agent == "coordinator"
        and pending_agent is not None
        and completed_agent is None
        and state.get("pending_result_ready")
    ):
        state["pending_result_ready"] = False
        _save_state(state_file, state)

    if (
        agent in {"testing", "review"}
        and pending_agent is not None
        and pending_agent != agent
    ):
        raise InvalidAgentResult(
            f"cannot invoke {agent!r}; unresolved pending_agent is {pending_agent!r}"
        )

    recovery_coordinator = agent == "coordinator" and pending_agent is not None
    read_only_context = None
    if agent == "review":
        read_only_context = "Review"
    elif recovery_coordinator:
        read_only_context = "Coordinator recovery"

    session_id = state["sessions"].get(agent) if config["persistent"] else None
    if session_id:
        command = ["codex", "exec", "resume", session_id, "--json", "-"]
    else:
        command = ["codex", "exec", "--json", "-"]

    prompt = _build_prompt(
        role_text=role_text,
        workflow_id=workflow_id,
        task=task,
        include_role=session_id is None,
    )
    try:
        if runner is None:
            completed = _default_runner(
                command,
                repo,
                prompt,
                timeout_seconds=timeout_seconds,
            )
        else:
            completed = runner(command, repo, prompt)
    except subprocess.TimeoutExpired as exc:
        _verify_agent_did_not_mutate_repository(repo, repository_guard)
        if read_only_context is not None:
            _ensure_read_only_worktree(repo, read_only_context)
        raise CodexInvocationError(
            f"Codex timed out after {timeout_seconds} seconds"
        ) from exc

    _verify_agent_did_not_mutate_repository(repo, repository_guard)
    if read_only_context is not None:
        _ensure_read_only_worktree(repo, read_only_context)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise CodexInvocationError(
            f"Codex exited with status {completed.returncode}: {detail}"
        )

    thread_id, result = _parse_output(completed.stdout)
    status = result["status"]
    if status not in config["statuses"]:
        raise InvalidAgentResult(f"status {status!r} is invalid for agent {agent!r}")

    if "commit" in result:
        raise InvalidAgentResult(
            "agents must not include commit; Hermes owns git commit creation"
        )

    if agent in {"testing", "review"} and "next_agent" in result:
        raise InvalidAgentResult(f"agent {agent!r} is not allowed to choose next_agent")

    if agent == "testing" and status == "RED_COMPLETE":
        _require_nonempty_text(result, "test_command", "Testing RED_COMPLETE")

    if agent == "coordinator":
        if status == "HANDOFF":
            next_agent = result.get("next_agent")
            if next_agent not in {"testing", "review"}:
                raise InvalidAgentResult(
                    "Coordinator HANDOFF next_agent must be testing or review"
                )
            _require_nonempty_text(result, "task", "Coordinator HANDOFF")
            _require_nonempty_text(result, "reason", "Coordinator HANDOFF")
            if next_agent == "review":
                has_full_command = _has_nonempty_text(result, "full_test_command")
                has_unavailable_reason = _has_nonempty_text(
                    result, "full_test_unavailable_reason"
                )
                if has_full_command == has_unavailable_reason:
                    raise InvalidAgentResult(
                        "Coordinator review HANDOFF must include exactly one of "
                        "full_test_command or full_test_unavailable_reason"
                    )
            state["pending_agent"] = next_agent
            state["pending_result_ready"] = False
            if next_agent == "review":
                state["review_clean_head"] = None
        else:
            if "next_agent" in result:
                raise InvalidAgentResult(
                    f"Coordinator status {status!r} must not include next_agent"
                )
            if status == "AWAIT_USER_DECISION":
                _require_nonempty_text(
                    result, "question", "Coordinator AWAIT_USER_DECISION"
                )
            elif status == "AWAIT_USER_MERGE":
                _require_nonempty_text(
                    result, "reviewed_head", "Coordinator AWAIT_USER_MERGE"
                )
                if result.get("draft") is not False:
                    raise InvalidAgentResult(
                        "Coordinator AWAIT_USER_MERGE must include draft=false"
                    )
                if state.get("pending_agent") is not None:
                    raise InvalidAgentResult(
                        "Coordinator AWAIT_USER_MERGE requires no unresolved pending_agent"
                    )
                reviewed_head = result["reviewed_head"].strip()
                review_clean_head = state.get("review_clean_head")
                current_head = repository_guard["head"]
                if (
                    not review_clean_head
                    or reviewed_head != review_clean_head
                    or reviewed_head != current_head
                ):
                    raise InvalidAgentResult(
                        "Coordinator AWAIT_USER_MERGE requires reviewed_head to match "
                        "the current HEAD certified by REVIEW_CLEAN"
                    )

    if agent == "testing" and status == "RED_COMPLETE":
        state["pending_result_ready"] = True

    if agent == "review" and status in {"REVIEW_CLEAN", "CHANGES_REQUIRED"}:
        state["pending_result_ready"] = True
        if status == "REVIEW_CLEAN":
            state["review_clean_head"] = repository_guard["head"]

    if config["persistent"] and session_id is None:
        if not thread_id:
            raise CodexInvocationError("Codex did not emit thread.started for new session")
        state["sessions"][agent] = thread_id

    _save_state(state_file, state)
    return result


def _safe_workflow_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "workflow"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, choices=tuple(AGENTS))
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--state-file")
    parser.add_argument("--prompt-dir")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_AGENT_TIMEOUT_SECONDS,
        help=f"Codex subprocess timeout in seconds (default: {DEFAULT_AGENT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--completed-agent",
        choices=("testing", "review"),
        help=(
            "Hermes completion handshake: clear the matching pending specialist "
            "only after its existing mechanical acceptance has passed"
        ),
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent
    state_file = Path(args.state_file) if args.state_file else (
        root / "state" / f"{_safe_workflow_name(args.workflow)}.json"
    )
    prompt_dir = Path(args.prompt_dir) if args.prompt_dir else root / "prompts"

    try:
        result = invoke_agent(
            agent=args.agent,
            workflow_id=args.workflow,
            repo=args.repo,
            task=args.task,
            state_file=state_file,
            prompt_dir=prompt_dir,
            timeout_seconds=args.timeout_seconds,
            completed_agent=args.completed_agent,
        )
    except Exception as exc:
        error = {"status": "ERROR", "error": str(exc)}
        if isinstance(
            exc,
            (CodexInvocationError, InvalidAgentResult, AgentRepositoryMutationError),
        ):
            try:
                unverified_artifacts = _worktree_status(Path(args.repo).resolve())
            except Exception:
                unverified_artifacts = []
            if unverified_artifacts:
                error["unverified_artifacts"] = unverified_artifacts
        print(json.dumps(error), file=sys.stderr)
        return 2

    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
