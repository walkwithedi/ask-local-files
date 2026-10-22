#!/usr/bin/env python3
"""Run every local check a commit must pass, in the order CI runs them.

  1. unit tests            python -m unittest discover -s tests
  2. repository checks     scripts/check_repo.py
  3. personal data         scripts/sensitive_scan.py

Used by the git pre-commit hook (.githooks/pre-commit), the Claude Code hooks
(.claude/hooks/) and by hand before a pull request. Prints one line per check and the
output of any check that fails. Exit code 1 when any check fails.

Usage:
  python3 scripts/run_checks.py
  python3 scripts/run_checks.py --skip tests     leave out checks by name (tests, repo, scan)
"""
import argparse
import logging
import os
import subprocess
import sys
import time

log = logging.getLogger("run_checks")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKS = [
    ("tests", ["-m", "unittest", "discover", "-s", "tests"]),
    ("repo", ["scripts/check_repo.py"]),
    ("scan", ["scripts/sensitive_scan.py"]),
]


def run_checks(skip=()):
    """Run the checks; returns the names of the failed ones."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", LOG_LEVEL="WARNING")
    failed = []
    for name, args in CHECKS:
        if name in skip:
            print(f"skip  {name}")
            continue
        start = time.time()
        p = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        took = time.time() - start
        log.debug("%s exited %d in %.1fs", name, p.returncode, took)
        if p.returncode == 0:
            print(f"ok    {name} ({took:.1f}s)")
            continue
        failed.append(name)
        print(f"FAIL  {name}: python3 {' '.join(args)}")
        out = (p.stdout + p.stderr).strip().splitlines()
        for line in out[-40:]:
            print(f"      {line}")
    return failed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--skip", nargs="*", default=[], choices=[n for n, _ in CHECKS])
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "WARNING").upper(),
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = ap.parse_args()
    logging.basicConfig(level=a.log_level, format=LOG_FORMAT, stream=sys.stderr)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # a stream that cannot be reconfigured (replaced, or not a text stream) keeps its encoding
    failed = run_checks(set(a.skip))
    print("result: " + (f"failed: {', '.join(failed)}" if failed else "all checks passed"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
