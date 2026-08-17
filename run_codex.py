#!/usr/bin/env python3
"""Thin Codex CLI wrapper for the multi-agent-coding Hermes skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable


AGENTS = {
    "testing": {"prompt": "testing.md", "persistent": True, "statuses": {"RED_COMPLETE", "BLOCKED"}},
    "coordinator": {"prompt": "coordinator.md", "persistent": True, "statuses": {"HANDOFF", "AWAIT_USER_DECISION", "AWAIT_USER_MERGE", "BLOCKED"}},
    "task_review": {"prompt": "task_review.md", "persistent": False, "statuses": {"TASK_REVIEW_CLEAN", "CHANGES_REQUIRED", "BLOCKED"}},
    "review": {"prompt": "review.md", "persistent": False, "statuses": {"REVIEW_CLEAN", "CHANGES_REQUIRED", "BLOCKED"}},
}

RESULT_MARKER = "HERMES_RESULT="
DEFAULT_AGENT_TIMEOUT_SECONDS = 1800
TASK_REVIEW_FIELDS = (
    "evidence_and_root_cause",
    "clearer_requirement",
    "acceptance_criteria",
    "simplest_approach",
)
GIT_OWNERSHIP_POLICY = """Repository state policy:
- The orchestration layer owns git and GitHub mutations.
- You may inspect repository state with read-only commands such as git status, git diff, git log, git show, git rev-parse, and read-only gh queries.
- Do not run git add, commit, push, restore, checkout, reset, rebase, merge, clean, or other commands that mutate git state.
- Do not mutate remote repository state through gh, gh api, or another API.
- Leave permitted file edits unstaged for the orchestration layer to validate and commit.
"""


class CodexInvocationError(RuntimeError):
    """Raised when Codex cannot be invoked successfully."""


class InvalidAgentResult(RuntimeError):
    """Raised when an agent does not return the required result contract."""


class DirtyWorktreeError(RuntimeError):
    """Raised when an agent invocation does not start from a clean worktree."""


class AgentRepositoryMutationError(RuntimeError):
    """Raised when a Codex agent mutates git or remote repository state."""


class MergePrHeadMismatch(InvalidAgentResult):
    """Raised when merge readiness is stale against the actual GitHub PR HEAD."""

    def __init__(self, reviewed_head: str, current_pr_head: str):
        super().__init__("actual GitHub PR HEAD does not match reviewed_head")
        self.reviewed_head = reviewed_head
        self.current_pr_head = current_pr_head


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
            "pending": None,
            "review_certification": None,
            "task_review_clean_checkpoint": None,
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("workflow_id") not in (None, workflow_id):
        raise ValueError(
            f"state file belongs to workflow {state.get('workflow_id')!r}, not {workflow_id!r}"
        )
    legacy_keys = {
        "pending_agent",
        "pending_result_ready",
        "pending_task_review_checkpoint",
        "review_clean_head",
    }
    if legacy_keys.intersection(state):
        raise ValueError("legacy workflow state is unsupported; start a new workflow")
    state.setdefault("workflow_id", workflow_id)
    state.setdefault("sessions", {})
    state.setdefault("pending", None)
    state.setdefault("review_certification", None)
    state.setdefault("task_review_clean_checkpoint", None)
    return state


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _task_review_checkpoint(task: str) -> str:
    return hashlib.sha256(task.strip().encode("utf-8")).hexdigest()


def _issue_number_from_workflow(workflow_id: str) -> int | None:
    match = re.fullmatch(r"issue-(\d+)", workflow_id)
    return int(match.group(1)) if match else None


def _git(repo: Path, *args: str, allow_failure: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    completed = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False, env=env
    )
    if completed.returncode != 0 and not allow_failure:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise DirtyWorktreeError(
            f"unable to verify repository state with git {' '.join(args)}: {detail}"
        )
    return completed


def _gh_env() -> dict:
    env = os.environ.copy()
    env.pop("GH_REPO", None)
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def _current_pr_head(repo: Path) -> str:
    completed = subprocess.run(
        ["gh", "pr", "view", "--json", "headRefOid", "--jq", ".headRefOid"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=_gh_env(),
    )
    head = completed.stdout.strip()
    if completed.returncode != 0 or not head:
        detail = (completed.stderr or completed.stdout or "no PR HEAD returned").strip()
        raise RuntimeError(f"unable to verify actual GitHub PR HEAD: {detail}")
    return head


def _has_origin(repo: Path) -> bool:
    origin = _git(repo, "remote", "get-url", "origin", allow_failure=True)
    if origin.returncode != 0:
        return False
    return "github.com" in origin.stdout.strip().lower()


def _current_pr_number(repo: Path) -> int | None:
    if not _has_origin(repo):
        return None
    branch = _git(repo, "branch", "--show-current").stdout.strip()
    if not branch:
        raise InvalidAgentResult("unable to determine current branch for PR lookup")
    completed = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number",
            "--limit",
            "1",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=_gh_env(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "PR lookup failed").strip()
        raise InvalidAgentResult(f"unable to determine current GitHub PR: {detail}")
    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InvalidAgentResult("current GitHub PR lookup returned invalid JSON") from exc
    if not isinstance(rows, list):
        raise InvalidAgentResult("current GitHub PR lookup returned invalid data")
    if not rows:
        return None
    first = rows[0]
    number = first.get("number") if isinstance(first, dict) else None
    if not isinstance(number, int):
        raise InvalidAgentResult("current GitHub PR lookup returned an invalid PR number")
    return number


def _current_pr_is_draft(repo: Path) -> bool:
    completed = subprocess.run(
        ["gh", "pr", "view", "--json", "isDraft", "--jq", ".isDraft"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=_gh_env(),
    )
    value = completed.stdout.strip().lower()
    if completed.returncode != 0 or value not in {"true", "false"}:
        detail = (completed.stderr or completed.stdout or "no PR draft state returned").strip()
        raise InvalidAgentResult(f"unable to verify actual GitHub PR draft state: {detail}")
    return value == "true"


def _current_pr_body_hash(repo: Path) -> str | None:
    if not _has_origin(repo):
        return None
    completed = subprocess.run(
        ["gh", "pr", "view", "--json", "body", "--jq", ".body"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=_gh_env(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no PR body returned").strip()
        raise InvalidAgentResult(f"unable to read current PR description: {detail}")
    return hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest()


def _publish_handoff_trace(
    repo: Path,
    workflow_id: str,
    handoff: dict,
    *,
    head: str,
    task_checkpoint: str | None = None,
) -> None:
    if not _has_origin(repo):
        return
    from_actor = handoff.get("from")
    to_actor = handoff.get("to")
    payload = handoff.get("payload")
    if not isinstance(payload, dict):
        raise InvalidAgentResult("handoff trace requires an object payload")

    task_review_trace = "task_review" in {from_actor, to_actor}
    pr_number = None if task_review_trace else _current_pr_number(repo)
    issue_number = _issue_number_from_workflow(workflow_id)
    if pr_number is None and issue_number is None:
        return

    lines = [
        "### Workflow handoff",
        "",
        f"From: `{from_actor}`",
        f"To: `{to_actor}`",
        f"HEAD: `{head}`",
    ]
    if task_checkpoint:
        lines.append(f"Task checkpoint: `{task_checkpoint}`")
    lines.extend(
        [
            "",
            "```json",
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
            "```",
        ]
    )
    body = "\n".join(lines)
    command = (
        ["gh", "pr", "comment", str(pr_number), "--body", body]
        if pr_number is not None
        else ["gh", "issue", "comment", str(issue_number), "--body", body]
    )
    completed = subprocess.run(
        command, cwd=repo, text=True, capture_output=True, check=False, env=_gh_env()
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unable to publish trace").strip()
        raise InvalidAgentResult(f"unable to publish workflow handoff trace: {detail}")


def _publish_specialist_failure_trace(
    repo: Path, workflow_id: str, pending: dict, *, head: str, reason: str
) -> None:
    if not _has_origin(repo):
        return
    task_review_trace = isinstance(pending, dict) and pending.get("to") == "task_review"
    pr_number = None if task_review_trace else _current_pr_number(repo)
    issue_number = _issue_number_from_workflow(workflow_id)
    if pr_number is None and issue_number is None:
        return
    payload = pending.get("payload") if isinstance(pending, dict) else None
    body = "\n".join(
        [
            "### Workflow specialist failure",
            "",
            f"Pending specialist: `{pending.get('to')}`",
            f"HEAD: `{head}`",
            f"Reason: {reason}",
            "",
            "Pending handoff remains unresolved.",
            "",
            "```json",
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
            "```",
        ]
    )
    command = (
        ["gh", "pr", "comment", str(pr_number), "--body", body]
        if pr_number is not None
        else ["gh", "issue", "comment", str(issue_number), "--body", body]
    )
    completed = subprocess.run(
        command, cwd=repo, text=True, capture_output=True, check=False, env=_gh_env()
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unable to publish failure trace").strip()
        raise InvalidAgentResult(f"unable to publish specialist failure trace: {detail}")


def _worktree_status(repo: Path) -> list[str]:
    completed = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return [line for line in completed.stdout.splitlines() if line.strip()]


def _ensure_clean_worktree(repo: Path) -> None:
    if _worktree_status(repo):
        raise DirtyWorktreeError(
            "agent invocation requires a clean worktree; the orchestration layer must commit or discard the previous agent's changes first"
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
    remote = _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
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
    if after["head"] != before["head"] or after["branch"] != before["branch"] or after["staged"]:
        raise AgentRepositoryMutationError(
            "local git state changed during agent invocation; agents may not mutate git state"
        )
    if before["remote_checked"] and after["remote_head"] != before["remote_head"]:
        raise AgentRepositoryMutationError(
            "remote branch changed during agent invocation; agents may not mutate remote state"
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
            "Finish your final response with exactly one machine-readable line starting with HERMES_RESULT= followed by a JSON object containing at least a status field.",
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


def _pending_payload_task(pending: dict) -> str:
    payload = pending.get("payload")
    task = payload.get("task") if isinstance(payload, dict) else None
    if not isinstance(task, str) or not task.strip():
        raise InvalidAgentResult("pending specialist handoff must include non-empty task")
    return task


def _verify_dispatch_bridge(repo: Path, pending: dict, local_head: str) -> None:
    if not _has_origin(repo) or "task_review" in {pending.get("from"), pending.get("to")}:
        return
    pr_number = _current_pr_number(repo)
    requires_pr = pending.get("from") in {"testing", "review"} or pending.get("to") == "review"
    if requires_pr and pr_number is None:
        raise InvalidAgentResult(
            "implementation-stage dispatch requires a Draft PR after the first real implementation commit"
        )
    if pr_number is not None and _current_pr_head(repo) != local_head:
        raise InvalidAgentResult(
            "implementation-stage dispatch requires actual PR HEAD to match local HEAD"
        )


def _verify_red_command(repo: Path, test_command: str, timeout_seconds: int) -> None:
    before_guard = _capture_repository_guard(repo)
    before_status = _worktree_status(repo)
    try:
        completed = subprocess.run(
            test_command,
            cwd=repo,
            shell=True,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        _verify_agent_did_not_mutate_repository(repo, before_guard)
        if _worktree_status(repo) != before_status:
            raise AgentRepositoryMutationError(
                "Testing RED verification command modified the worktree"
            ) from exc
        raise InvalidAgentResult(
            f"Testing RED_COMPLETE test_command timed out after {timeout_seconds} seconds"
        ) from exc

    _verify_agent_did_not_mutate_repository(repo, before_guard)
    if _worktree_status(repo) != before_status:
        raise AgentRepositoryMutationError(
            "Testing RED verification command modified the worktree"
        )
    if completed.returncode == 0:
        raise InvalidAgentResult(
            "Testing RED_COMPLETE test_command must still fail before GREEN"
        )


def _reverse_handoff(agent: str, result: dict) -> dict:
    return {"from": agent, "to": "coordinator", "payload": result}


def _release_consumed_handoff(state_file: Path, state: dict, consumed: bool) -> None:
    if consumed:
        state["pending"] = None
        _save_state(state_file, state)


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
) -> dict:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}; expected one of {', '.join(AGENTS)}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise ValueError(f"repo is not a directory: {repo}")
    _ensure_clean_worktree(repo)
    repository_guard = _capture_repository_guard(repo)

    state_file = Path(state_file)
    prompt_dir = Path(prompt_dir)
    config = AGENTS[agent]
    role_text = (prompt_dir / config["prompt"]).read_text(encoding="utf-8")
    state = _load_state(state_file, workflow_id)
    pending = state.get("pending")
    specialists = {"testing", "task_review", "review"}

    recovery_coordinator = (
        agent == "coordinator"
        and isinstance(pending, dict)
        and pending.get("from") == "coordinator"
        and pending.get("to") in specialists
    )

    if agent == "coordinator" and isinstance(pending, dict) and pending.get("to") == "user":
        if not isinstance(task, str) or not task.strip():
            raise InvalidAgentResult("Coordinator resume from user requires a non-empty answer")
        state["pending"] = {
            "from": "user",
            "to": "coordinator",
            "payload": {"question": pending.get("payload"), "answer": task},
        }
        _save_state(state_file, state)
        pending = state["pending"]

    effective_task = task
    consumed_result_handoff = False
    task_review_checkpoint = None
    review_pr_body_hash = None

    if agent in specialists:
        if not isinstance(pending, dict) or pending.get("to") != agent:
            target = pending.get("to") if isinstance(pending, dict) else None
            raise InvalidAgentResult(
                f"cannot invoke {agent!r}; pending handoff target is {target!r}"
            )
        if pending.get("from") != "coordinator":
            raise InvalidAgentResult(
                f"cannot invoke {agent!r}; pending handoff is not from coordinator"
            )
        task_from_handoff = _pending_payload_task(pending)
        effective_task = json.dumps(pending["payload"], ensure_ascii=False, sort_keys=True)
        if agent == "task_review":
            task_review_checkpoint = _task_review_checkpoint(task_from_handoff)
    elif agent == "coordinator" and isinstance(pending, dict) and pending.get("to") == "coordinator":
        payload = pending.get("payload")
        if not isinstance(payload, dict):
            raise InvalidAgentResult("Coordinator pending result payload must be an object")
        effective_task = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        consumed_result_handoff = True

    formal_pending_dispatch = (
        isinstance(pending, dict)
        and pending.get("to") == agent
        and pending.get("from") != "user"
    )
    if formal_pending_dispatch:
        _verify_dispatch_bridge(repo, pending, repository_guard["head"])
        if agent == "review":
            review_pr_body_hash = _current_pr_body_hash(repo)
        trace_checkpoint = None
        if pending.get("to") == "task_review":
            trace_checkpoint = task_review_checkpoint
        elif pending.get("from") == "task_review" and isinstance(pending.get("payload"), dict):
            trace_checkpoint = pending["payload"].get("task_review_checkpoint")
        _publish_handoff_trace(
            repo,
            workflow_id,
            pending,
            head=repository_guard["head"],
            task_checkpoint=trace_checkpoint,
        )

    pre_task_review_coordinator = agent == "coordinator" and not state.get("task_review_clean_checkpoint")
    read_only_context = None
    if agent == "task_review":
        read_only_context = "Task Review"
    elif agent == "review":
        read_only_context = "Review"
    elif recovery_coordinator:
        read_only_context = "Coordinator recovery"
    elif pre_task_review_coordinator:
        read_only_context = "Coordinator before clean Task Review"

    session_id = state["sessions"].get(agent) if config["persistent"] else None
    command = (
        ["codex", "exec", "resume", session_id, "--json", "-"]
        if session_id
        else ["codex", "exec", "--json", "-"]
    )
    prompt = _build_prompt(
        role_text=role_text,
        workflow_id=workflow_id,
        task=effective_task,
        include_role=session_id is None,
    )
    try:
        completed = (
            _default_runner(command, repo, prompt, timeout_seconds=timeout_seconds)
            if runner is None
            else runner(command, repo, prompt)
        )
    except subprocess.TimeoutExpired as exc:
        _verify_agent_did_not_mutate_repository(repo, repository_guard)
        if read_only_context is not None:
            _ensure_read_only_worktree(repo, read_only_context)
        if agent in specialists and isinstance(pending, dict):
            _publish_specialist_failure_trace(
                repo,
                workflow_id,
                pending,
                head=repository_guard["head"],
                reason=f"Codex timed out after {timeout_seconds} seconds",
            )
        raise CodexInvocationError(f"Codex timed out after {timeout_seconds} seconds") from exc

    _verify_agent_did_not_mutate_repository(repo, repository_guard)
    if read_only_context is not None:
        _ensure_read_only_worktree(repo, read_only_context)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if agent in specialists and isinstance(pending, dict):
            _publish_specialist_failure_trace(
                repo,
                workflow_id,
                pending,
                head=repository_guard["head"],
                reason=f"Codex exited with status {completed.returncode}: {detail}",
            )
        raise CodexInvocationError(f"Codex exited with status {completed.returncode}: {detail}")

    try:
        thread_id, result = _parse_output(completed.stdout)
        status = result["status"]
        if status not in config["statuses"]:
            raise InvalidAgentResult(f"status {status!r} is invalid for agent {agent!r}")
        if "commit" in result:
            raise InvalidAgentResult(
                "agents must not include commit; the orchestration layer owns git commit creation"
            )
        if agent in specialists and "next_agent" in result:
            raise InvalidAgentResult(f"agent {agent!r} is not allowed to choose next_agent")
        if agent == "testing" and status == "RED_COMPLETE":
            _require_nonempty_text(result, "test_command", "Testing RED_COMPLETE")
        if agent == "task_review" and status in {"TASK_REVIEW_CLEAN", "CHANGES_REQUIRED"}:
            for field in TASK_REVIEW_FIELDS:
                _require_nonempty_text(result, field, f"Task Review {status}")
    except InvalidAgentResult as exc:
        if agent in specialists and isinstance(pending, dict):
            _publish_specialist_failure_trace(
                repo,
                workflow_id,
                pending,
                head=repository_guard["head"],
                reason=f"Rejected specialist result: {exc}",
            )
        raise

    if agent == "coordinator":
        if status == "HANDOFF":
            next_agent = result.get("next_agent")
            if next_agent not in specialists:
                raise InvalidAgentResult(
                    "Coordinator HANDOFF next_agent must be testing, task_review, or review"
                )
            _require_nonempty_text(result, "task", "Coordinator HANDOFF")
            _require_nonempty_text(result, "reason", "Coordinator HANDOFF")

            if next_agent == "task_review":
                state["task_review_clean_checkpoint"] = None
                state["review_certification"] = None
                _ensure_read_only_worktree(repo, "Coordinator task-review handoff")
            elif not state.get("task_review_clean_checkpoint"):
                raise InvalidAgentResult(
                    f"Coordinator cannot hand off to {next_agent!r} before TASK_REVIEW_CLEAN"
                )

            if next_agent == "review":
                has_full_command = _has_nonempty_text(result, "full_test_command")
                has_unavailable_reason = _has_nonempty_text(result, "full_test_unavailable_reason")
                if has_full_command == has_unavailable_reason:
                    raise InvalidAgentResult(
                        "Coordinator review HANDOFF must include exactly one of full_test_command or full_test_unavailable_reason"
                    )
                state["review_certification"] = None

            state["pending"] = {"from": "coordinator", "to": next_agent, "payload": result}
        else:
            if "next_agent" in result:
                raise InvalidAgentResult(
                    f"Coordinator status {status!r} must not include next_agent"
                )
            if status == "AWAIT_USER_DECISION":
                _require_nonempty_text(result, "question", "Coordinator AWAIT_USER_DECISION")
                if recovery_coordinator:
                    raise InvalidAgentResult(
                        "Coordinator recovery cannot await user decision while a specialist handoff is unresolved"
                    )
                state["pending"] = {"from": "coordinator", "to": "user", "payload": result}
            elif status == "AWAIT_USER_MERGE":
                _require_nonempty_text(result, "reviewed_head", "Coordinator AWAIT_USER_MERGE")
                if not state.get("task_review_clean_checkpoint"):
                    _release_consumed_handoff(state_file, state, consumed_result_handoff)
                    raise InvalidAgentResult("Coordinator AWAIT_USER_MERGE requires TASK_REVIEW_CLEAN")
                if result.get("draft") is not False:
                    raise InvalidAgentResult("Coordinator AWAIT_USER_MERGE must include draft=false")
                if recovery_coordinator:
                    raise InvalidAgentResult(
                        "Coordinator AWAIT_USER_MERGE requires no unresolved specialist handoff"
                    )
                if _worktree_status(repo):
                    _release_consumed_handoff(state_file, state, consumed_result_handoff)
                    raise InvalidAgentResult(
                        "Coordinator AWAIT_USER_MERGE requires a clean worktree"
                    )
                certification = state.get("review_certification")
                reviewed_head = result["reviewed_head"].strip()
                current_head = repository_guard["head"]
                certified_head = certification.get("head") if isinstance(certification, dict) else None
                if not certified_head or reviewed_head != certified_head or reviewed_head != current_head:
                    _release_consumed_handoff(state_file, state, consumed_result_handoff)
                    raise InvalidAgentResult(
                        "Coordinator AWAIT_USER_MERGE requires reviewed_head to match the current HEAD certified by REVIEW_CLEAN"
                    )
                current_body_hash = _current_pr_body_hash(repo)
                certified_body_hash = certification.get("pr_body_hash")
                if current_body_hash != certified_body_hash:
                    _release_consumed_handoff(state_file, state, consumed_result_handoff)
                    raise InvalidAgentResult(
                        "Coordinator AWAIT_USER_MERGE requires the current PR description to match REVIEW_CLEAN certification"
                    )
                current_pr_head = _current_pr_head(repo)
                if current_pr_head != reviewed_head:
                    _release_consumed_handoff(state_file, state, consumed_result_handoff)
                    raise MergePrHeadMismatch(reviewed_head, current_pr_head)
                try:
                    current_pr_is_draft = _current_pr_is_draft(repo)
                except InvalidAgentResult:
                    _release_consumed_handoff(state_file, state, consumed_result_handoff)
                    raise
                if current_pr_is_draft:
                    _release_consumed_handoff(state_file, state, consumed_result_handoff)
                    raise InvalidAgentResult(
                        "Coordinator AWAIT_USER_MERGE requires the actual GitHub PR to be ready for review"
                    )
                state["pending"] = {"from": "coordinator", "to": "user", "payload": result}
                _publish_handoff_trace(
                    repo, workflow_id, state["pending"], head=repository_guard["head"]
                )
            elif status == "BLOCKED" and consumed_result_handoff:
                state["pending"] = None

    elif agent == "testing":
        if status == "RED_COMPLETE":
            try:
                _verify_red_command(repo, result["test_command"], timeout_seconds)
            except (InvalidAgentResult, AgentRepositoryMutationError) as exc:
                _publish_specialist_failure_trace(
                    repo,
                    workflow_id,
                    pending,
                    head=repository_guard["head"],
                    reason=f"Mechanical verification failed: {exc}",
                )
                raise
            state["pending"] = _reverse_handoff(agent, result)
        elif status == "BLOCKED":
            summary = result.get("summary")
            reason = f"BLOCKED: {summary.strip()}" if isinstance(summary, str) and summary.strip() else "BLOCKED"
            _publish_specialist_failure_trace(
                repo, workflow_id, pending, head=repository_guard["head"], reason=reason
            )
    elif agent == "task_review":
        if status in {"TASK_REVIEW_CLEAN", "CHANGES_REQUIRED"}:
            result["task_review_checkpoint"] = task_review_checkpoint
            state["pending"] = _reverse_handoff(agent, result)
            if status == "TASK_REVIEW_CLEAN":
                state["task_review_clean_checkpoint"] = task_review_checkpoint
        elif status == "BLOCKED":
            summary = result.get("summary")
            reason = f"BLOCKED: {summary.strip()}" if isinstance(summary, str) and summary.strip() else "BLOCKED"
            _publish_specialist_failure_trace(
                repo, workflow_id, pending, head=repository_guard["head"], reason=reason
            )
    elif agent == "review":
        if status in {"REVIEW_CLEAN", "CHANGES_REQUIRED"}:
            if status == "REVIEW_CLEAN":
                try:
                    current_pr_body_hash = _current_pr_body_hash(repo)
                    if current_pr_body_hash != review_pr_body_hash:
                        raise InvalidAgentResult(
                            "PR description changed during Review; fresh Review is required"
                        )
                except InvalidAgentResult as exc:
                    _publish_specialist_failure_trace(
                        repo,
                        workflow_id,
                        pending,
                        head=repository_guard["head"],
                        reason=f"Mechanical verification failed: {exc}",
                    )
                    raise
                state["review_certification"] = {
                    "head": repository_guard["head"],
                    "pr_body_hash": review_pr_body_hash,
                }
            state["pending"] = _reverse_handoff(agent, result)
        elif status == "BLOCKED":
            summary = result.get("summary")
            reason = f"BLOCKED: {summary.strip()}" if isinstance(summary, str) and summary.strip() else "BLOCKED"
            _publish_specialist_failure_trace(
                repo, workflow_id, pending, head=repository_guard["head"], reason=reason
            )

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
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent
    repo = Path(args.repo).resolve()
    state_file = Path(args.state_file) if args.state_file else (
        root / "state" / f"{_safe_workflow_name(args.workflow)}.json"
    )
    prompt_dir = Path(args.prompt_dir) if args.prompt_dir else root / "prompts"

    try:
        result = invoke_agent(
            agent=args.agent,
            workflow_id=args.workflow,
            repo=repo,
            task=args.task,
            state_file=state_file,
            prompt_dir=prompt_dir,
            timeout_seconds=args.timeout_seconds,
        )
    except MergePrHeadMismatch as exc:
        error = {
            "status": "ERROR",
            "error_code": "MERGE_PR_HEAD_MISMATCH",
            "error": str(exc),
            "reviewed_head": exc.reviewed_head,
            "current_pr_head": exc.current_pr_head,
        }
        print(json.dumps(error), file=sys.stderr)
        return 2
    except Exception as exc:
        error = {"status": "ERROR", "error": str(exc)}
        if isinstance(exc, (CodexInvocationError, InvalidAgentResult, AgentRepositoryMutationError)):
            try:
                unverified_artifacts = _worktree_status(repo)
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
