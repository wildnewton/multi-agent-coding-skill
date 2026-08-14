#!/usr/bin/env python3
"""Issue #9 real-execution smoke harness for the historical 300s boundary."""

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


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    )


def write_fake_codex(path: Path, sleep_seconds: float) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import time

time.sleep({sleep_seconds!r})
print(json.dumps({{"type":"thread.started","thread_id":"issue-9-smoke"}}))
print(json.dumps({{
    "type":"item.completed",
    "item":{{
        "type":"agent_message",
        "text":'HERMES_RESULT={{"status":"RED_COMPLETE","test_command":"python -m unittest"}}'
    }}
}}))
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sleep-seconds", type=float, default=320)
    parser.add_argument("--require-survive-seconds", type=float, default=300)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    if not args.require_survive_seconds < args.sleep_seconds < args.timeout_seconds:
        parser.error(
            "require-survive-seconds < sleep-seconds < timeout-seconds is required"
        )

    skill_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="issue-9-smoke-") as temp:
        root = Path(temp)
        repo = root / "repo"
        repo.mkdir()
        git(repo, "init")
        git(repo, "config", "user.email", "smoke@example.com")
        git(repo, "config", "user.name", "Issue 9 Smoke")
        (repo / "README.md").write_text("clean\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "initial")

        bin_dir = root / "bin"
        bin_dir.mkdir()
        write_fake_codex(bin_dir / "codex", args.sleep_seconds)
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")

        command = [
            sys.executable,
            str(skill_root / "run_codex.py"),
            "--agent", "testing",
            "--workflow", "issue-9-smoke",
            "--repo", str(repo),
            "--task", "Prove this invocation survives the historical 300s boundary.",
            "--state-file", str(root / "state.json"),
            "--prompt-dir", str(skill_root / "prompts"),
            "--timeout-seconds", str(args.timeout_seconds),
        ]

        started = time.monotonic()
        print(
            json.dumps(
                {
                    "event": "started",
                    "timestamp": now(),
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
            try:
                process.wait(timeout=args.require_survive_seconds)
            except subprocess.TimeoutExpired:
                print(
                    json.dumps(
                        {
                            "event": "alive_after_survival_boundary",
                            "timestamp": now(),
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "pid": process.pid,
                        }
                    ),
                    flush=True,
                )
            else:
                stdout, stderr = process.communicate()
                print(
                    json.dumps(
                        {
                            "event": "failed_before_survival_boundary",
                            "timestamp": now(),
                            "returncode": process.returncode,
                            "stdout": stdout.strip(),
                            "stderr": stderr.strip(),
                        }
                    ),
                    flush=True,
                )
                return 1

        try:
            stdout, stderr = process.communicate(timeout=args.timeout_seconds + 30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            print(json.dumps({"event": "harness_timeout", "timestamp": now()}))
            return 1

        elapsed = time.monotonic() - started
        evidence = {
            "event": "completed",
            "timestamp": now(),
            "elapsed_seconds": round(elapsed, 3),
            "returncode": process.returncode,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "survived_required_boundary": elapsed > args.require_survive_seconds,
            "completed_before_agent_timeout": elapsed < args.timeout_seconds,
        }
        print(json.dumps(evidence), flush=True)

        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return 1
        return 0 if (
            process.returncode == 0
            and evidence["survived_required_boundary"]
            and evidence["completed_before_agent_timeout"]
            and result.get("status") == "RED_COMPLETE"
        ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
