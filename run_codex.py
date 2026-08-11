#!/usr/bin/env python3
"""Thin Codex CLI wrapper for the multi-agent-coding Hermes skill."""

from __future__ import annotations

import argparse
import json
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


class CodexInvocationError(RuntimeError):
    """Raised when Codex cannot be invoked successfully."""


class InvalidAgentResult(RuntimeError):
    """Raised when an agent does not return the required result contract."""


def _default_runner(command, cwd, input_text):
    return subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def _load_state(path: Path, workflow_id: str) -> dict:
    if not path.exists():
        return {"workflow_id": workflow_id, "sessions": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("workflow_id") not in (None, workflow_id):
        raise ValueError(
            f"state file belongs to workflow {state.get('workflow_id')!r}, "
            f"not {workflow_id!r}"
        )
    state.setdefault("workflow_id", workflow_id)
    state.setdefault("sessions", {})
    return state


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    runner: Callable = _default_runner,
) -> dict:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}; expected one of {', '.join(AGENTS)}")

    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise ValueError(f"repo is not a directory: {repo}")

    state_file = Path(state_file)
    prompt_dir = Path(prompt_dir)
    config = AGENTS[agent]
    role_path = prompt_dir / config["prompt"]
    role_text = role_path.read_text(encoding="utf-8")
    state = _load_state(state_file, workflow_id)
    _save_state(state_file, state)

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
    completed = runner(command, repo, prompt)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise CodexInvocationError(
            f"Codex exited with status {completed.returncode}: {detail}"
        )

    thread_id, result = _parse_output(completed.stdout)
    status = result["status"]
    if status not in config["statuses"]:
        raise InvalidAgentResult(f"status {status!r} is invalid for agent {agent!r}")

    if agent in {"testing", "review"} and "next_agent" in result:
        raise InvalidAgentResult(f"agent {agent!r} is not allowed to choose next_agent")

    if agent == "coordinator":
        if status == "HANDOFF":
            next_agent = result.get("next_agent")
            if next_agent not in {"testing", "review"}:
                raise InvalidAgentResult(
                    "Coordinator HANDOFF next_agent must be testing or review"
                )
            _require_nonempty_text(result, "task", "Coordinator HANDOFF")
            if next_agent == "review":
                for field in ("commit", "test_command"):
                    _require_nonempty_text(result, field, "Coordinator review HANDOFF")
                has_full_command = _has_nonempty_text(result, "full_test_command")
                has_unavailable_reason = _has_nonempty_text(
                    result, "full_test_unavailable_reason"
                )
                if has_full_command == has_unavailable_reason:
                    raise InvalidAgentResult(
                        "Coordinator review HANDOFF must include exactly one of "
                        "full_test_command or full_test_unavailable_reason"
                    )
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
        )
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}), file=sys.stderr)
        return 2

    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
