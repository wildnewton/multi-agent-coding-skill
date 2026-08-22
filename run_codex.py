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
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


AGENTS = {
    "testing": {"prompt": "testing.md", "persistent": True, "statuses": {"RED_COMPLETE", "TEST_FIX_COMPLETE", "BLOCKED"}},
    "coordinator": {"prompt": "coordinator.md", "persistent": True, "statuses": {"HANDOFF", "VERIFY_EXTERNAL", "COMPLETED", "AWAIT_USER_DECISION", "AWAIT_USER_MERGE", "BLOCKED"}},
    "task_review": {"prompt": "task_review.md", "persistent": False, "statuses": {"TASK_REVIEW_CLEAN", "CHANGES_REQUIRED", "BLOCKED"}},
    "review": {"prompt": "review.md", "persistent": False, "statuses": {"REVIEW_CLEAN", "CHANGES_REQUIRED", "BLOCKED"}},
}

RESULT_MARKER = "HERMES_RESULT="
DEFAULT_AGENT_TIMEOUT_SECONDS = 1800
DEFAULT_REPOSITORY_COMMAND_TIMEOUT_SECONDS = 60
MAX_EXTERNAL_EVIDENCE_CHARS = 12000
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
            "external_verification": None,
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
    state.setdefault("external_verification", None)
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
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=DEFAULT_REPOSITORY_COMMAND_TIMEOUT_SECONDS,
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


def _gh(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=_gh_env(),
        timeout=DEFAULT_REPOSITORY_COMMAND_TIMEOUT_SECONDS,
    )


def _current_pr_head(repo: Path) -> str:
    completed = _gh(repo, "pr", "view", "--json", "headRefOid", "--jq", ".headRefOid")
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
    completed = _gh(
        repo,
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
    completed = _gh(repo, "pr", "view", "--json", "isDraft", "--jq", ".isDraft")
    value = completed.stdout.strip().lower()
    if completed.returncode != 0 or value not in {"true", "false"}:
        detail = (completed.stderr or completed.stdout or "no PR draft state returned").strip()
        raise InvalidAgentResult(f"unable to verify actual GitHub PR draft state: {detail}")
    return value == "true"


def _current_pr_body_hash(repo: Path) -> str | None:
    if not _has_origin(repo):
        return None
    completed = _gh(repo, "pr", "view", "--json", "body", "--jq", ".body")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no PR body returned").strip()
        raise InvalidAgentResult(f"unable to read current PR description: {detail}")
    return hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest()


def _handoff_trace_payload(from_actor: str, payload: dict) -> dict:
    if from_actor != "executor" or payload.get("status") != "EXTERNAL_VERIFICATION_RESULT":
        return payload
    safe_fields = (
        "status",
        "request",
        "provenance",
        "head",
        "execution_status",
        "exit_status",
        "stdout_truncated",
        "stderr_truncated",
    )
    return {field: payload[field] for field in safe_fields if field in payload}


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
    trace_payload = _handoff_trace_payload(from_actor, payload)

    task_review_trace = "task_review" in {from_actor, to_actor}
    pr_number = None if task_review_trace else _current_pr_number(repo)
    issue_number = _issue_number_from_workflow(workflow_id)
    if task_review_trace and issue_number is None:
        raise InvalidAgentResult("Task Review trace requires an issue-<number> workflow")
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
            json.dumps(trace_payload, indent=2, ensure_ascii=False, sort_keys=True),
            "```",
        ]
    )
    body = "\n".join(lines)
    command = (
        ["gh", "pr", "comment", str(pr_number), "--body", body]
        if pr_number is not None
        else ["gh", "issue", "comment", str(issue_number), "--body", body]
    )
    completed = _gh(repo, *command[1:])
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
    completed = _gh(repo, *command[1:])
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unable to publish failure trace").strip()
        raise InvalidAgentResult(f"unable to publish specialist failure trace: {detail}")


def _worktree_status(repo: Path) -> list[str]:
    completed = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return [line for line in completed.stdout.splitlines() if line.strip()]


def _worktree_paths(repo: Path) -> set[str]:
    tracked = _git(repo, "diff", "--name-only", "-z", "--").stdout.split("\0")
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
    return {path for path in [*tracked, *untracked] if path}


def _worktree_content_snapshot(repo: Path) -> dict[str, tuple[int | None, str | None]]:
    snapshot = {}
    for path in sorted(_worktree_paths(repo)):
        file_path = repo / path
        try:
            mode = file_path.lstat().st_mode
        except FileNotFoundError:
            snapshot[path] = (None, None)
            continue
        if file_path.is_symlink():
            content = os.readlink(file_path).encode()
        elif file_path.is_file():
            content = file_path.read_bytes()
        else:
            content = b""
        snapshot[path] = (mode, hashlib.sha256(content).hexdigest())
    return snapshot


def _test_fix_allowed_paths(payload: dict) -> set[str]:
    values = payload.get("allowed_paths") if isinstance(payload, dict) else None
    if not isinstance(values, list) or not values:
        raise InvalidAgentResult(
            "Coordinator test-fix HANDOFF must include non-empty allowed_paths"
        )
    allowed = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise InvalidAgentResult(
                "Coordinator test-fix HANDOFF allowed_paths must contain non-empty strings"
            )
        raw = value.strip()
        path = PurePosixPath(raw)
        normalized = path.as_posix()
        if path.is_absolute() or ".." in path.parts or normalized in {"", "."} or normalized != raw:
            raise InvalidAgentResult(
                "Coordinator test-fix HANDOFF allowed_paths must be normalized repository-relative paths"
            )
        allowed.add(normalized)
    return allowed


def _verify_test_fix_paths(repo: Path, pending: dict) -> None:
    payload = pending.get("payload") if isinstance(pending, dict) else None
    allowed = _test_fix_allowed_paths(payload)
    outside = sorted(_worktree_paths(repo) - allowed)
    if outside:
        raise AgentRepositoryMutationError(
            "Testing TEST_FIX_COMPLETE changed paths outside allowed_paths: " + ", ".join(outside)
        )


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


def _build_prompt(
    role_text: str,
    workflow_id: str,
    task: str,
    include_role: bool,
    *,
    external_verification: dict | None = None,
) -> str:
    parts = []
    if include_role:
        parts.append(role_text.strip())
    parts.extend(
        [
            GIT_OWNERSHIP_POLICY.strip(),
            f"Workflow: {workflow_id}",
            "Current task:",
            task.strip(),
        ]
    )
    if external_verification is not None:
        parts.extend(
            [
                "Preserved required external-verification evidence (Executor-owned workflow context):",
                json.dumps(external_verification, indent=2, ensure_ascii=False, sort_keys=True),
            ]
        )
    parts.extend(
        [
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


def _external_verification_request(
    value: dict,
    context: str,
    *,
    require_expected_head: bool = False,
) -> dict:
    if not isinstance(value, dict):
        raise InvalidAgentResult(f"{context} external verification must be an object")
    request = {}
    for field in ("command", "boundary", "reason"):
        field_value = value.get(field)
        if not isinstance(field_value, str) or not field_value.strip():
            raise InvalidAgentResult(f"{context} must include non-empty {field}")
        request[field] = field_value.strip()
    if require_expected_head:
        expected_head = value.get("expected_head")
        if not isinstance(expected_head, str) or not expected_head.strip():
            raise InvalidAgentResult(f"{context} must include non-empty expected_head")
        request["expected_head"] = expected_head.strip()
    return request


def _bounded_evidence_text(value) -> tuple[str, bool]:
    if value is None:
        text = ""
    elif isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    if len(text) <= MAX_EXTERNAL_EVIDENCE_CHARS:
        return text, False
    half = MAX_EXTERNAL_EVIDENCE_CHARS // 2
    marker = "\n...[external verification output truncated]...\n"
    return text[:half] + marker + text[-half:], True


def _external_verification_head(evidence: dict) -> str | None:
    if not isinstance(evidence, dict):
        return None
    if evidence.get("provenance") == "executor":
        head = evidence.get("head")
        return head.strip() if isinstance(head, str) and head.strip() else None
    if evidence.get("provenance") == "externally_supplied":
        request = evidence.get("request")
        head = request.get("expected_head") if isinstance(request, dict) else None
        return head.strip() if isinstance(head, str) and head.strip() else None
    return None


def _ensure_external_verification_current(state: dict, current_head: str, context: str) -> None:
    evidence = state.get("external_verification")
    if evidence is None:
        return
    evidence_head = _external_verification_head(evidence)
    if not evidence_head or evidence_head != current_head:
        raise InvalidAgentResult(
            f"{context} requires required external verification evidence for the current HEAD"
        )


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


def _run_guarded_test_command(
    repo: Path,
    test_command: str,
    timeout_seconds: int,
    *,
    timeout_context: str,
    mutation_context: str,
    content_guard: bool = False,
) -> subprocess.CompletedProcess:
    before_guard = _capture_repository_guard(repo)
    before_status = _worktree_status(repo)
    before_content = _worktree_content_snapshot(repo) if content_guard else None
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
        if _worktree_status(repo) != before_status or (
            before_content is not None and _worktree_content_snapshot(repo) != before_content
        ):
            raise AgentRepositoryMutationError(
                f"{mutation_context} verification command modified the worktree"
            ) from exc
        raise InvalidAgentResult(
            f"{timeout_context} test_command timed out after {timeout_seconds} seconds"
        ) from exc

    _verify_agent_did_not_mutate_repository(repo, before_guard)
    if _worktree_status(repo) != before_status or (
        before_content is not None and _worktree_content_snapshot(repo) != before_content
    ):
        raise AgentRepositoryMutationError(
            f"{mutation_context} verification command modified the worktree"
        )
    return completed


def _verify_red_command(repo: Path, test_command: str, timeout_seconds: int) -> None:
    completed = _run_guarded_test_command(
        repo,
        test_command,
        timeout_seconds,
        timeout_context="Testing RED_COMPLETE",
        mutation_context="Testing RED",
    )
    if completed.returncode == 0:
        raise InvalidAgentResult(
            "Testing RED_COMPLETE test_command must still fail before GREEN"
        )


def _verify_test_fix_command(repo: Path, test_command: str, timeout_seconds: int) -> None:
    completed = _run_guarded_test_command(
        repo,
        test_command,
        timeout_seconds,
        timeout_context="Testing TEST_FIX_COMPLETE",
        mutation_context="Testing TEST_FIX_COMPLETE",
        content_guard=True,
    )
    if completed.returncode != 0:
        raise InvalidAgentResult(
            "Testing TEST_FIX_COMPLETE test_command must pass after the test-only correction"
        )


def _reverse_handoff(agent: str, result: dict) -> dict:
    return {"from": agent, "to": "coordinator", "payload": result}


def _release_consumed_handoff(state_file: Path, state: dict, consumed: bool) -> None:
    if consumed:
        state["pending"] = None
        _save_state(state_file, state)


def invoke_external_verification(
    *,
    workflow_id: str,
    repo: str | Path,
    state_file: str | Path,
    command_runner: Callable | None = None,
    unavailable_reason: str | None = None,
    timeout_seconds: int = DEFAULT_AGENT_TIMEOUT_SECONDS,
) -> dict:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise ValueError(f"repo is not a directory: {repo}")
    _ensure_clean_worktree(repo)
    repository_guard = _capture_repository_guard(repo)
    state_file = Path(state_file)
    state = _load_state(state_file, workflow_id)
    pending = state.get("pending")
    if not (
        isinstance(pending, dict)
        and pending.get("from") == "coordinator"
        and pending.get("to") == "executor"
        and isinstance(pending.get("payload"), dict)
        and pending["payload"].get("status") == "VERIFY_EXTERNAL"
    ):
        raise InvalidAgentResult(
            "external verification requires pending Coordinator -> Executor VERIFY_EXTERNAL ownership"
        )
    request = _external_verification_request(
        pending["payload"], "Coordinator VERIFY_EXTERNAL"
    )
    _verify_dispatch_bridge(repo, pending, repository_guard["head"])
    _publish_handoff_trace(
        repo,
        workflow_id,
        pending,
        head=repository_guard["head"],
    )

    if unavailable_reason is not None:
        if not isinstance(unavailable_reason, str) or not unavailable_reason.strip():
            raise InvalidAgentResult("external verification unavailable reason must be non-empty")
        result = {
            "status": "EXTERNAL_VERIFICATION_UNAVAILABLE",
            "request": request,
            "head": repository_guard["head"],
            "reason": unavailable_reason.strip(),
        }
        state["external_verification"] = None
        state["review_certification"] = None
        state["pending"] = {"from": "executor", "to": "coordinator", "payload": result}
        _save_state(state_file, state)
        return result

    before_status = _worktree_status(repo)
    execution_status = "completed"
    exit_status = None
    stdout = ""
    stderr = ""
    try:
        if command_runner is None:
            completed = subprocess.run(
                request["command"],
                cwd=repo,
                shell=True,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        else:
            completed = command_runner(request["command"], repo, timeout_seconds)
        exit_status = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        execution_status = "timeout"
        stdout = exc.output or ""
        stderr = exc.stderr or ""
    except OSError as exc:
        execution_status = "execution_error"
        stderr = str(exc)

    _verify_agent_did_not_mutate_repository(repo, repository_guard)
    if _worktree_status(repo) != before_status:
        raise AgentRepositoryMutationError(
            "external verification command modified the worktree"
        )

    bounded_stdout, stdout_truncated = _bounded_evidence_text(stdout)
    bounded_stderr, stderr_truncated = _bounded_evidence_text(stderr)
    evidence = {
        "status": "EXTERNAL_VERIFICATION_RESULT",
        "request": request,
        "provenance": "executor",
        "head": repository_guard["head"],
        "execution_status": execution_status,
        "exit_status": exit_status,
        "stdout": bounded_stdout,
        "stderr": bounded_stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
    state["external_verification"] = evidence
    state["review_certification"] = None
    state["pending"] = {"from": "executor", "to": "coordinator", "payload": evidence}
    _save_state(state_file, state)
    return evidence


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

    if (
        agent == "coordinator"
        and isinstance(pending, dict)
        and pending.get("from") == "coordinator"
        and pending.get("to") == "executor"
    ):
        raise InvalidAgentResult(
            "cannot invoke Coordinator while required external verification is pending on Executor"
        )

    recovery_coordinator = (
        agent == "coordinator"
        and isinstance(pending, dict)
        and pending.get("from") == "coordinator"
        and pending.get("to") in specialists
    )

    if agent == "coordinator" and isinstance(pending, dict) and pending.get("to") == "user":
        if not isinstance(task, str) or not task.strip():
            raise InvalidAgentResult("Coordinator resume from user requires a non-empty answer")
        question_payload = pending.get("payload")
        external_request = (
            question_payload.get("external_verification")
            if isinstance(question_payload, dict)
            else None
        )
        state["pending"] = {
            "from": "user",
            "to": "coordinator",
            "payload": {"question": question_payload, "answer": task},
        }
        if external_request is not None:
            request = _external_verification_request(
                external_request,
                "Coordinator AWAIT_USER_DECISION external_verification",
                require_expected_head=True,
            )
            bounded_answer, answer_truncated = _bounded_evidence_text(task)
            state["external_verification"] = {
                "status": "EXTERNAL_VERIFICATION_RESULT",
                "request": request,
                "provenance": "externally_supplied",
                "evidence": bounded_answer,
                "evidence_truncated": answer_truncated,
            }
            state["review_certification"] = None
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
            _ensure_external_verification_current(
                state, repository_guard["head"], "Review dispatch"
            )
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
        external_verification=(
            state.get("external_verification") if agent == "review" else None
        ),
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
        if agent == "testing" and status in {"RED_COMPLETE", "TEST_FIX_COMPLETE"}:
            _require_nonempty_text(result, "test_command", f"Testing {status}")
            payload = pending.get("payload") if isinstance(pending, dict) else None
            testing_intent = payload.get("testing_intent") if isinstance(payload, dict) else None
            if status == "TEST_FIX_COMPLETE":
                if testing_intent != "test_fix":
                    raise InvalidAgentResult(
                        "Testing TEST_FIX_COMPLETE requires a pending test_fix handoff"
                    )
                _test_fix_allowed_paths(payload)
            elif testing_intent == "test_fix":
                raise InvalidAgentResult(
                    "Testing test_fix handoff must complete with TEST_FIX_COMPLETE"
                )
        if agent == "task_review" and status in {"TASK_REVIEW_CLEAN", "CHANGES_REQUIRED"}:
            for field in TASK_REVIEW_FIELDS:
                _require_nonempty_text(result, field, f"Task Review {status}")
        if agent == "review" and status == "CHANGES_REQUIRED":
            findings = result.get("findings")
            if not isinstance(findings, list) or not findings:
                raise InvalidAgentResult("Review CHANGES_REQUIRED must include non-empty findings")
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
                state["external_verification"] = None
                _ensure_read_only_worktree(repo, "Coordinator task-review handoff")
            elif not state.get("task_review_clean_checkpoint"):
                raise InvalidAgentResult(
                    f"Coordinator cannot hand off to {next_agent!r} before TASK_REVIEW_CLEAN"
                )

            if next_agent == "testing":
                testing_intent = result.get("testing_intent")
                if testing_intent is None:
                    if "allowed_paths" in result:
                        raise InvalidAgentResult(
                            "Coordinator ordinary Testing HANDOFF must not include allowed_paths"
                        )
                elif testing_intent == "test_fix":
                    _test_fix_allowed_paths(result)
                else:
                    raise InvalidAgentResult(
                        "Coordinator Testing HANDOFF testing_intent must be 'test_fix' when provided"
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
            if status == "VERIFY_EXTERNAL":
                if not state.get("task_review_clean_checkpoint"):
                    raise InvalidAgentResult(
                        "Coordinator VERIFY_EXTERNAL requires TASK_REVIEW_CLEAN"
                    )
                _external_verification_request(result, "Coordinator VERIFY_EXTERNAL")
                state["external_verification"] = None
                state["review_certification"] = None
                state["pending"] = {
                    "from": "coordinator",
                    "to": "executor",
                    "payload": result,
                }
            elif status == "COMPLETED":
                _require_nonempty_text(result, "report", "Coordinator COMPLETED")
                if recovery_coordinator:
                    raise InvalidAgentResult(
                        "Coordinator COMPLETED requires no unresolved specialist handoff"
                    )
                if state.get("task_review_clean_checkpoint"):
                    raise InvalidAgentResult(
                        "Coordinator COMPLETED requires no current TASK_REVIEW_CLEAN checkpoint"
                    )
                if _worktree_status(repo):
                    raise InvalidAgentResult("Coordinator COMPLETED requires a clean worktree")
                if _current_pr_number(repo) is not None:
                    raise InvalidAgentResult(
                        "Coordinator COMPLETED requires no current implementation-stage PR"
                    )
                state["pending"] = None
            elif status == "AWAIT_USER_DECISION":
                _require_nonempty_text(result, "question", "Coordinator AWAIT_USER_DECISION")
                if recovery_coordinator:
                    raise InvalidAgentResult(
                        "Coordinator recovery cannot await user decision while a specialist handoff is unresolved"
                    )
                external_request = result.get("external_verification")
                if external_request is not None:
                    if not state.get("task_review_clean_checkpoint"):
                        raise InvalidAgentResult(
                            "Coordinator external verification request requires TASK_REVIEW_CLEAN"
                        )
                    request = _external_verification_request(
                        external_request,
                        "Coordinator AWAIT_USER_DECISION external_verification",
                        require_expected_head=True,
                    )
                    if request["expected_head"] != repository_guard["head"]:
                        raise InvalidAgentResult(
                            "Coordinator AWAIT_USER_DECISION external_verification expected_head must match current HEAD"
                        )
                    _ensure_clean_worktree(repo)
                    _verify_dispatch_bridge(
                        repo,
                        {"from": "coordinator", "to": "user", "payload": result},
                        repository_guard["head"],
                    )
                    state["external_verification"] = None
                    state["review_certification"] = None
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
                try:
                    _ensure_external_verification_current(
                        state, current_head, "Coordinator AWAIT_USER_MERGE"
                    )
                except InvalidAgentResult:
                    _release_consumed_handoff(state_file, state, consumed_result_handoff)
                    raise
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
        if status in {"RED_COMPLETE", "TEST_FIX_COMPLETE"}:
            try:
                if status == "RED_COMPLETE":
                    _verify_red_command(repo, result["test_command"], timeout_seconds)
                else:
                    _verify_test_fix_paths(repo, pending)
                    _verify_test_fix_command(repo, result["test_command"], timeout_seconds)
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
    parser.add_argument("--agent", choices=tuple(AGENTS))
    parser.add_argument("--run-external-verification", action="store_true")
    parser.add_argument("--external-verification-unavailable")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", default="")
    parser.add_argument("--state-file")
    parser.add_argument("--prompt-dir")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_AGENT_TIMEOUT_SECONDS,
        help=f"Codex subprocess timeout in seconds (default: {DEFAULT_AGENT_TIMEOUT_SECONDS})",
    )
    args = parser.parse_args(argv)
    action_count = sum(
        [
            args.agent is not None,
            bool(args.run_external_verification),
            args.external_verification_unavailable is not None,
        ]
    )
    if action_count != 1:
        parser.error(
            "specify exactly one of --agent, --run-external-verification, or --external-verification-unavailable"
        )
    if args.agent and not args.task.strip():
        parser.error("--task is required with --agent")
    if (
        args.external_verification_unavailable is not None
        and not args.external_verification_unavailable.strip()
    ):
        parser.error("--external-verification-unavailable requires a non-empty reason")

    root = Path(__file__).resolve().parent
    repo = Path(args.repo).resolve()
    state_file = Path(args.state_file) if args.state_file else (
        root / "state" / f"{_safe_workflow_name(args.workflow)}.json"
    )
    prompt_dir = Path(args.prompt_dir) if args.prompt_dir else root / "prompts"

    try:
        if args.run_external_verification or args.external_verification_unavailable is not None:
            result = invoke_external_verification(
                workflow_id=args.workflow,
                repo=repo,
                state_file=state_file,
                unavailable_reason=args.external_verification_unavailable,
                timeout_seconds=args.timeout_seconds,
            )
        else:
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
        if isinstance(
            exc,
            (
                CodexInvocationError,
                InvalidAgentResult,
                AgentRepositoryMutationError,
                subprocess.TimeoutExpired,
            ),
        ):
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
