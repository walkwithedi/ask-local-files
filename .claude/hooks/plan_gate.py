#!/usr/bin/env python3
"""Claude Code hook: no change to files or remote state before the session's design is approved.

Reads the hook JSON from stdin.
  PreToolUse   Edit, Write, MultiEdit, NotebookEdit, Bash and GitHub tools: until this session
               has an approval mark, every call that changes files or remote state is refused
               (exit code 2, reason on stderr). Read-only calls pass. Bash commands are judged
               word by word and anything unclear counts as a change.
  PostToolUse  ExitPlanMode (the plan was approved) or AskUserQuestion with an answer that starts
               with "批准方案" or "Approve plan": writes the approval mark for this session.
The mark is a file named after the session id under ~/.claude/plan-approved/, outside the
repository. Writes to the system temporary directory are not changes and always pass.
"""
import json
import os
import re
import shlex
import sys
import tempfile

APPROVAL_PREFIXES = ("批准方案", "Approve plan")
MARK_DIR = os.path.join(os.path.expanduser("~"), ".claude", "plan-approved")

FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
GITHUB_PREFIX = "mcp__github__"
GITHUB_READ_PREFIXES = ("get_", "list_", "search_")
GITHUB_READ_TOOLS = {"issue_read", "pull_request_read", "actions_get", "actions_list",
                     "run_secret_scanning"}

WRITE_COMMANDS = {"rm", "rmdir", "mv", "cp", "mkdir", "touch", "chmod", "chown", "chgrp", "ln",
                  "tee", "patch", "truncate", "dd", "install", "rsync", "unzip", "shred",
                  "mkfifo", "mktemp"}
WRAPPERS = {"sudo", "env", "nohup", "time", "command", "exec", "nice", "timeout", "xargs",
            "stdbuf", "builtin"}
WRAPPER_OPTIONS_WITH_VALUE = {"-n", "-u", "-I", "-P", "-d", "-L", "-s", "-E", "-a", "-k", "-C",
                              "-g", "-i", "-o", "-e"}
SHELLS = {"bash", "sh", "zsh", "dash", "eval"}
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
GIT_READ = {"status", "log", "diff", "show", "fetch", "ls-files", "ls-tree", "ls-remote",
            "rev-parse", "rev-list", "blame", "grep", "describe", "shortlog", "cat-file",
            "merge-base", "name-rev", "reflog", "for-each-ref", "show-ref", "check-ignore",
            "count-objects", "var", "help", "version", "whatchanged", "cherry", "range-diff"}
GH_READ_ACTIONS = {"view", "list", "ls", "status", "diff", "checks", "watch"}
GH_READ_TOP = {"status", "search", "help", "version", "--version", "--help"}
INSTALLERS = {"pip", "pip3", "npm", "yarn", "pnpm", "apt", "apt-get", "brew", "gem", "cargo",
              "go", "conda", "dnf", "yum"}
INSTALL_ACTIONS = {"install", "i", "ci", "add", "uninstall", "remove", "rm", "publish", "update",
                   "upgrade", "get"}
SAFE_TARGETS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")
OPERATORS = {";", "&&", "||", "|", "&", "(", ")", "|&", ";;"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}


def temp_roots():
    roots = {"/tmp", tempfile.gettempdir()}
    if os.environ.get("TMPDIR"):
        roots.add(os.environ["TMPDIR"])
    return [os.path.realpath(r) for r in roots]


def is_temp(path):
    if not path:
        return False
    path = os.path.normcase(os.path.realpath(os.path.expanduser(path)))
    for root in temp_roots():
        root = os.path.normcase(root)
        if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def mark_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return os.path.join(MARK_DIR, safe)


def approved(session_id):
    return os.path.exists(mark_path(session_id))


def tokens(command):
    """The command split into words and shell operators; newlines count as ';'.
    Raises ValueError when the quoting cannot be parsed."""
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def segments(words):
    """Split the word list at command separators; redirections stay inside their segment."""
    out, cur = [], []
    for w in words:
        if w in OPERATORS:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        out.append(cur)
    return out


def redirect_problem(seg):
    for i, w in enumerate(seg):
        if w in REDIRECTS or (w.endswith(">") and w.rstrip(">").isdigit() and w not in ("<",)):
            target = seg[i + 1] if i + 1 < len(seg) else ""
            if target.startswith("&") or target in SAFE_TARGETS or is_temp(target):
                continue
            if w.endswith(">") and target in (">", ""):
                continue
            return "output redirected to " + (target or "a file")
        if w == ">&":
            target = seg[i + 1] if i + 1 < len(seg) else ""
            if target.isdigit() or target == "-" or target in SAFE_TARGETS or is_temp(target):
                continue
            return "output redirected to " + target
    return None


def strip_wrappers(seg):
    seg = list(seg)
    while seg:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
            seg.pop(0)
            continue
        name = os.path.basename(seg[0])
        if name not in WRAPPERS:
            break
        seg.pop(0)
        if name == "timeout" and seg and not seg[0].startswith("-"):
            seg.pop(0)
        while seg and (seg[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0])):
            opt = seg.pop(0)
            if opt in WRAPPER_OPTIONS_WITH_VALUE and seg:
                seg.pop(0)
            if name == "timeout" and seg and not seg[0].startswith("-"):
                seg.pop(0)
                break
    return seg


def git_problem(args):
    j = 0
    while j < len(args) and args[j].startswith("-"):
        j += 2 if args[j] in GIT_OPTIONS_WITH_VALUE else 1
    if j >= len(args):
        return None
    sub, rest = args[j], args[j + 1:]
    if sub in GIT_READ:
        return None
    if sub == "branch":
        if not rest or all(a.startswith("-") and a not in ("-d", "-D", "-m", "-M", "-c", "-C",
                                                             "--delete", "--move", "--copy",
                                                             "--set-upstream-to", "-u",
                                                             "--unset-upstream", "-f", "--force")
                           for a in rest):
            return None
    if sub == "remote" and (not rest or rest[0] in ("-v", "--verbose", "show", "get-url")):
        return None
    if sub == "config" and any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l")
                               for a in rest):
        return None
    if sub == "stash" and rest[:1] in (["list"], ["show"]):
        return None
    if sub == "tag" and (not rest or rest[0] in ("-l", "--list")):
        return None
    if sub == "worktree" and rest[:1] == ["list"]:
        return None
    return "git " + sub


def gh_problem(args):
    if not args or args[0] in GH_READ_TOP:
        return None
    if args[0] == "api":
        for k, a in enumerate(args):
            if a in ("-X", "--method") and k + 1 < len(args) and args[k + 1].upper() != "GET":
                return "gh api " + args[k + 1]
            if a.startswith("--method=") and a.split("=", 1)[1].upper() != "GET":
                return "gh api " + a
            if a in ("-f", "-F", "--field", "--raw-field", "--input") or \
                    a.startswith(("--field=", "--raw-field=", "--input=")):
                return "gh api with fields"
        return None
    if len(args) > 1 and args[1] in GH_READ_ACTIONS:
        return None
    return "gh " + " ".join(args[:2])


def curl_problem(args):
    for k, a in enumerate(args):
        if a in ("-X", "--request") and k + 1 < len(args) and args[k + 1].upper() not in ("GET", "HEAD"):
            return "curl " + args[k + 1]
        if re.match(r"^-X(?!GET|HEAD)[A-Z]+$", a):
            return "curl " + a
        if a in ("-d", "-F", "-T", "--form", "--upload-file", "--json") or a.startswith("--data"):
            return "curl sending data"
        if a in ("-o", "--output") and k + 1 < len(args) and not (
                args[k + 1] in ("-",) + SAFE_TARGETS or is_temp(args[k + 1])):
            return "curl writing " + args[k + 1]
        if a in ("-O", "--remote-name", "--remote-name-all"):
            return "curl writing a file"
    return None


def command_problem(seg, depth=0):
    """Why this simple command changes something, or None when it only reads."""
    if depth > 4:
        return "nested command"
    problem = redirect_problem(seg)
    if problem:
        return problem
    seg = [w for w in seg if w != "<"]
    seg = strip_wrappers(seg)
    if not seg:
        return None
    name, args = os.path.basename(seg[0]), seg[1:]
    if name in WRITE_COMMANDS:
        targets = [a for a in args if not a.startswith("-")]
        if name not in ("tee", "chmod", "chown", "chgrp", "ln", "install", "rsync", "dd", "unzip",
                        "patch", "mktemp", "mkfifo") and targets and all(is_temp(t) for t in targets):
            return None
        if name == "tee" and all(is_temp(t) or t in SAFE_TARGETS for t in targets):
            return None
        if name == "mktemp" and not targets:
            return None
        return name
    if name == "sed" and any(a == "--in-place" or a.startswith("--in-place=") or
                             (re.match(r"^-[A-Za-z]+", a) and not a.startswith("--") and "i" in a.split(".")[0])
                             for a in args if a.startswith("-")):
        return "sed -i"
    if name == "perl" and any(re.match(r"^-[A-Za-z]*i", a) for a in args):
        return "perl -i"
    if name == "tar" and args and not re.match(r"^-?[A-Za-z]*t", args[0]) and "--list" not in args:
        return "tar"
    if name == "find":
        if "-delete" in args or "-fprint" in args or "-fprintf" in args or "-fls" in args:
            return "find writing or deleting"
        for k, a in enumerate(args):
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                inner = []
                for b in args[k + 1:]:
                    if b in (";", "+", "\\;"):
                        break
                    inner.append(b)
                problem = command_problem(inner, depth + 1)
                if problem:
                    return "find -exec " + problem
    if name == "git":
        return git_problem(args)
    if name == "gh":
        return gh_problem(args)
    if name == "curl":
        return curl_problem(args)
    if name == "wget":
        if any(a in ("-O-", "-qO-", "--spider") for a in args) or \
                any(a in ("-O", "--output-document") and k + 1 < len(args) and
                    (args[k + 1] == "-" or is_temp(args[k + 1])) for k, a in enumerate(args)):
            return None
        return "wget"
    if name in INSTALLERS and args and args[0] in INSTALL_ACTIONS:
        return name + " " + args[0]
    if name.startswith("python") and len(args) >= 3 and args[0] == "-m" and args[1] in ("pip", "pip3") \
            and args[2] in INSTALL_ACTIONS:
        return "pip " + args[2]
    if name in SHELLS:
        script = None
        if name == "eval":
            script = " ".join(args)
        elif "-c" in args and args.index("-c") + 1 < len(args):
            script = args[args.index("-c") + 1]
        elif args and not args[0].startswith("-"):
            return name + " running a script file"
        if script is not None:
            return bash_problem(script, depth + 1)
    return None


def bash_problem(command, depth=0):
    if "plan-approved" in command:
        return "touching the approval marks"
    try:
        words = tokens(command)
    except ValueError:
        return "command could not be parsed"
    for seg in segments(words):
        problem = command_problem(seg, depth)
        if problem:
            return problem
    return None


def github_problem(tool):
    name = tool[len(GITHUB_PREFIX):]
    if name.startswith(GITHUB_READ_PREFIXES) or name in GITHUB_READ_TOOLS:
        return None
    return tool


def file_problem(tool, tool_input):
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if is_temp(path):
        return None
    return "%s %s" % (tool, path)


def problem_for(tool, tool_input):
    if tool in FILE_TOOLS:
        return file_problem(tool, tool_input)
    if tool == "Bash":
        return bash_problem(tool_input.get("command") or "")
    if tool.startswith(GITHUB_PREFIX):
        return github_problem(tool)
    return None


def answers(data):
    """Answer strings of an AskUserQuestion call, from its input and its result."""
    found = []
    for source in (data.get("tool_input"), data.get("tool_response")):
        if isinstance(source, dict):
            values = source.get("answers")
            if isinstance(values, dict):
                found += [v for v in values.values() if isinstance(v, str)]
        elif isinstance(source, str):
            found += re.findall(r'"="([^"]*)"', source)
    resp = data.get("tool_response")
    if isinstance(resp, list):
        for item in resp:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                found += re.findall(r'"="([^"]*)"', item["text"])
    return found


def is_approval(data):
    tool = data.get("tool_name")
    if tool == "ExitPlanMode":
        return True
    if tool == "AskUserQuestion":
        return any(a.strip().startswith(APPROVAL_PREFIXES) for a in answers(data))
    return False


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        print("plan_gate: hook input is not JSON; refusing.", file=sys.stderr)
        return 2
    event = data.get("hook_event_name")
    session_id = data.get("session_id") or "unknown"
    if event == "PostToolUse":
        if is_approval(data):
            os.makedirs(MARK_DIR, exist_ok=True)
            with open(mark_path(session_id), "w", encoding="utf-8") as f:
                f.write(data.get("tool_name", "") + "\n")
        return 0
    if event != "PreToolUse" or approved(session_id):
        return 0
    problem = problem_for(data.get("tool_name") or "", data.get("tool_input") or {})
    if not problem:
        return 0
    print("plan_gate: blocked (%s). 方案获批前不得改动文件或执行有副作用的操作。"
          "先写出设计（目标、改动文件、方案与取舍、验证方式），"
          "用退出计划模式的审批，或用 AskUserQuestion 提问并以「批准方案」开头的选项让用户确认；"
          "获批后再执行。不得自行创建批准标记。 / No changes before the design is approved: "
          "present it through plan-mode approval or an AskUserQuestion option starting with "
          "\"Approve plan\", then retry. Never create the approval mark yourself." % problem,
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail closed
        print("plan_gate: internal error (%s); refusing." % exc, file=sys.stderr)
        sys.exit(2)
