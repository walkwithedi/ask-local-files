#!/usr/bin/env python3
"""Claude Code PreToolUse hook: checks before Claude runs git commit or git push.

Reads the hook JSON from stdin. Any Bash command other than a git commit or push passes at once.
  git commit  runs scripts/run_checks.py (tests, repository checks, data scan) and
              scans the command itself (its -m message) and a message file given with -F
  git push    runs scripts/sensitive_scan.py --unpushed: messages, identities, added lines and
              binary files of every commit on a local branch that no remote has yet
A failing check stops the command (exit code 2) and the failures go to stderr, where Claude
reads them, with the instruction to report them to the user rather than work around them.
Commits and pushes by hand are covered by .githooks/.
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile

# Global options of git that take the next word as their value.
OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def git_subcommands(command):
    """The subcommands of every git invocation in the command: git, then any global options
    (-C dir, -c key=value, --flag), then the subcommand word.
    Word by word rather than a regular expression, so no input can make it slow."""
    words = (command or "").replace(";", " ; ").replace("&&", " && ").replace("||", " || ").split()
    found = set()
    for i, w in enumerate(words):
        if w != "git" and not w.endswith("/git"):
            continue
        j = i + 1
        while j < len(words) and words[j].startswith("-"):
            j += 2 if words[j] in OPTIONS_WITH_VALUE else 1
        if j < len(words):
            found.add(words[j])
    return found


REPORT = ("Stop and report the findings above to the user; do not edit them away, rewrite them or bypass the "
          "check on your own. A line the user confirms as harmless may carry the marker sensitive-scan: ignore, "
          "only with the user's consent; private-term findings cannot be allowed that way.\n")


def message_file(command):
    """The file given to git commit with -F / --file (also combined as -aF), or None."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for i, w in enumerate(words):
        if w in ("-F", "--file") and i + 1 < len(words):
            return words[i + 1]
        if w.startswith("--file="):
            return w[len("--file="):]
        if w.startswith("-") and not w.startswith("--") and "F" in w[1:]:
            k = w.index("F")
            if "m" not in w[1:k]:
                return w[k + 1:] or (words[i + 1] if i + 1 < len(words) else None)
    return None


def scan_command(root, command, cwd):
    """Scan the command text (a -m message) and a -F message file; returns the scanner output on a finding."""
    out = ""
    with tempfile.TemporaryDirectory() as d:
        texts = [("commit command", command)]
        path = message_file(command)
        if path and path != "-":
            try:
                with open(os.path.join(cwd, os.path.expanduser(path)), encoding="utf-8") as fh:
                    texts.append(("commit message file", fh.read()))
            except (OSError, UnicodeDecodeError):
                pass  # unreadable: git commit then fails on it too, and commit-msg scans what it reads
        for label, text in texts:
            f = os.path.join(d, "text.txt")
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(text)
            p = run(root, [os.path.join(root, "scripts", "sensitive_scan.py"), "--text", f, "--label", label])
            if p.returncode:
                out += p.stdout + p.stderr
    return out


def is_commit(command):
    return "commit" in git_subcommands(command)


def run(root, args):
    return subprocess.run([sys.executable, *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    command = (data.get("tool_input") or {}).get("command") or ""
    subs = git_subcommands(command)
    if not subs & {"commit", "push"}:
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if "commit" in subs:
        found = scan_command(root, command, data.get("cwd") or root)
        if found:
            sys.stderr.write("Commit blocked: the commit command or message holds possible personal data, work "
                             "details or credentials.\n" + found + REPORT)
            return 2
        p = run(root, [os.path.join(root, "scripts", "run_checks.py")])
        if p.returncode:
            scan_failed = "FAIL  scan" in p.stdout
            sys.stderr.write("Commit blocked: a check failed.\n" + p.stdout + p.stderr + (REPORT if scan_failed else
                             "Fix the failing check, then commit again.\n"))
            return 2
    if "push" in subs:
        p = run(root, [os.path.join(root, "scripts", "sensitive_scan.py"), "--unpushed"])
        if p.returncode:
            sys.stderr.write("Push blocked: the commits to push hold possible personal data, work details or "
                             "credentials.\n" + p.stdout + p.stderr + REPORT)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
