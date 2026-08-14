#!/usr/bin/env python3
"""Issue #9 smoke harness for proving Codex execution survives beyond 300 seconds.

Run this script through Hermes with:
    terminal(background=true, notify_on_complete=true)

The default sleep is intentionally longer than the historical 300-second
foreground timeout. Use --require-survive-seconds 0 for a fast local sanity run.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )


def write_fake_codex(path: Path, sleep_seconds: float) -> None:
    script = f"""#!/usr/bin/env python3
import json
import time

time.sleep({sleep_seconds!r})
events = [
    {{"type": "thread.started", "thread_id": "issue-9-smoke-thread"}},
    {{
        "type": "item.completed",
        "item": {{
            "type": "agent_message",
            "text": (
                'HERMES_RESULT={{"status":"RED_COMPLETE",'
                '"test_command":"python -m unittest",'
                '"summary":"issue #9 >300s smoke completed"}}'
            ),
        }},
    }},
]
for event in events:
    print(json.dumps(event), flush=True)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sleep-seconds", type=float, default=320)
    parser.add_argument("--require-survive-seconds", type=float, default=300)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)

    if args.sleep_seconds <= args.require_survive_seconds:
        parser.error("--sleep-seconds must exceed --require-survive-seconds")
    if args.timeout_seconds <= args.sleep_seconds:
        parser.error("--timeout-seconds must exceed --sleep-seconds")

    skill_root = Path(__file__).resolve().parents[1]
    wrapper = skill_root / "run_codex.py"
    prompt_dir = skill_root / "prompts"

    with tempfile.TemporaryDirectory(prefix="issue-9-smoke-") as temp:
        root = Path(temp)
        repo = root / "target-repo"
        repo.mkdir()
        run_git(repo, "init")
        run_git(repo, "config", "user.email", "issue-9-smoke@example.com")
        run_git(repo, "config", "user.name", "Issue 9 Smoke")
        (repo / "README.md").write_text("clean\n", encoding="utf-8")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-m", "initial")

        bin_dir = root / "bin"
        bin_dir.mkdir()
        write_fake_codex(bin_dir / "codex", args.sleep_seconds)

        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

        command = [
            sys.executable,
            str(wrapper),
            "--agent",
            "testing",
            "--workflow",
            "issue-9-smoke",
            "--repo",
            str(repo),
            "--task",
            "Prove the invocation survives beyond the historical 300-second boundary.",
            "--state-file",
            str(root / "state.json"),
            "--prompt-dir",
            str(prompt_dir),
            "--timeout-seconds",
            str(args.timeout_seconds),
        ]

        started_wall = timestamp()
        started = time.monotonic()
        print(
            json.dumps(
                {
                    "event": "started",
                    "timestamp": started_wall,
                    "command": command,
                    "sleep_seconds": args.sleep_seconds,
                    "require_survive_seconds": args.require_survive_seconds,
                    "timeout_seconds": args.timeout_seconds,
                }
            ),
            flush=True,
        )

        process = subprocess.Popen(
            command,
            cwd=skill_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        if args.require_survive_seconds:
            deadline = started + args.require_survive_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    print(
                        json.dumps(
                            {
                                "event": "failed_before_survival_boundary",
                                "timestamp": timestamp(),
                                "elapsed_seconds": round(time.monotonic() - started, 3),
                                "returncode": process.returncode,
                                "stdout": stdout,
                                "stderr": stderr,
                            }
                        ),
                        flush=True,
                    )
                    return 1
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

            if process.poll() is not None:
                stdout, stderr = process.communicate()
                print(
                    json.dumps(
                        {
                            "event": "failed_at_survival_boundary",
                            "timestamp": timestamp(),
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "returncode": process.returncode,
                            "stdout": stdout,
                            "stderr": stderr,
                        }
                    ),
                    flush=True,
                )
                return 1

            print(
                json.dumps(
                    {
                        "event": "alive_after_survival_boundary",
                        "timestamp": timestamp(),
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "pid": process.pid,
                    }
                ),
                flush=True,
            )

        stdout, stderr = process.communicate()
        elapsed = time.monotonic() - started
        evidence = {
            "event": "completed",
            "timestamp": timestamp(),
            "elapsed_seconds": round(elapsed, 3),
            "returncode": process.returncode,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "survived_required_boundary": elapsed > args.require_survive_seconds,
            "completed_before_agent_timeout": elapsed < args.timeout_seconds,
        }
        print(json.dumps(evidence), flush=True)

        if process.returncode != 0:
            return 1
        if elapsed <= args.require_survive_seconds:
            return 1
        if elapsed >= args.timeout_seconds:
            return 1
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return 1
        return 0 if result.get("status") == "RED_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
