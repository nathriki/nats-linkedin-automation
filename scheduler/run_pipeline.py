"""Runs the full batch pipeline once: scraper -> scorer -> drafter -> classifier -> poster.

Meant to be invoked periodically (e.g. Windows Task Scheduler, daily) -- this
is the scheduling mechanism the design doc's Next Steps left open. The
approval bot is deliberately NOT part of this: it's a persistent, always-on
service (see approval_bot/telegram_approval_bot.py's own docstring), not
something to run periodically -- it needs to be running continuously so a
human can act on pending_review items at any time.

Requires PYTHONUTF8=1 for the drafter/classifier stages (see the design
doc's Windows/UTF-8 constraint) -- set explicitly per subprocess below so
this works correctly regardless of whether the calling environment (e.g.
Task Scheduler) has it set. Also requires the NATS server to already be
running.

Each stage runs even if an earlier one failed -- they're independent
consumers of their own NATS subjects, so (for example) a scraper hiccup
shouldn't block the classifier from processing drafts already sitting in
the queue from a prior run.

Run: .venv/Scripts/python.exe scheduler/run_pipeline.py
"""
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")
STAGE_TIMEOUT_SECONDS = 300

STAGES = [
    ("scraper", PROJECT_ROOT / "scraper" / "news_scraper.py"),
    ("scorer", PROJECT_ROOT / "scorer" / "heuristic_scorer.py"),
    ("drafter", PROJECT_ROOT / "drafter" / "post_drafter.py"),
    ("classifier", PROJECT_ROOT / "classifier" / "risk_classifier.py"),
    ("poster", PROJECT_ROOT / "poster" / "linkedin_poster.py"),
]

LOG_DIR = PROJECT_ROOT / "scheduler" / "logs"


def run_stage(name: str, script_path: Path, log_lines: list[str]) -> bool:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    try:
        result = subprocess.run(
            [PYTHON, str(script_path)],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=STAGE_TIMEOUT_SECONDS,
        )
        ok = result.returncode == 0
        output = result.stdout + (result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        ok = False
        output = f"TIMED OUT after {STAGE_TIMEOUT_SECONDS}s: {exc}"

    header = f"=== {name} ({'OK' if ok else 'FAILED'}) ==="
    print(header)
    print(output)
    log_lines.append(header)
    log_lines.append(output)
    return ok


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"run-{timestamp}.log"

    log_lines = [f"Pipeline run started {datetime.now(timezone.utc).isoformat()}"]
    print(log_lines[0])

    failures = []
    for name, script_path in STAGES:
        ok = run_stage(name, script_path, log_lines)
        if not ok:
            failures.append(name)

    summary = f"Pipeline run finished. Failures: {failures or 'none'}"
    print(summary)
    log_lines.append(summary)

    log_path.write_text("\n".join(log_lines), encoding="utf-8")
    print(f"Log saved to {log_path}")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
