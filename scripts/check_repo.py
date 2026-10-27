#!/usr/bin/env python3
"""Repository checks that tests do not cover.

  1. every Python file compiles
  2. the tool's own scripts import only the standard library (and each other)
  3. VERSION holds a version, and CHANGELOG.md has a section for it
  4. DOC2KB_VERSION holds a version (scripts/check_doc2kb.py compares the copy with that release)
  5. the page loads nothing from the network: no http(s) URL in a src or href attribute

stdout: one line per problem and a result line. Exit code 1 when any check fails.
Usage: python3 scripts/check_repo.py [--log-level LEVEL]
"""
import argparse
import ast
import logging
import os
import re
import sys

log = logging.getLogger("check_repo")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_SCRIPTS = ["ask_local_files.py", "ask_memory.py", "doc2kb_core.py"]
LOCAL_MODULES = {"ask_memory", "doc2kb_core"}
PAGE = "ask_local_files.html"


def python_files():
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(d, f)
    for d in (".claude/hooks",):
        for f in sorted(os.listdir(os.path.join(ROOT, d))):
            if f.endswith(".py"):
                yield os.path.join(ROOT, d, f)


def imports(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module.split(".")[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "WARNING").upper(),
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = ap.parse_args()
    logging.basicConfig(level=a.log_level, format=LOG_FORMAT, stream=sys.stderr)
    problems = []
    for path in python_files():
        try:
            compile(open(path, encoding="utf-8").read(), path, "exec")
        except SyntaxError as e:
            problems.append(f"{os.path.relpath(path, ROOT)}: does not compile: {e}")
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:
        log.warning("Python %s has no sys.stdlib_module_names; import check skipped", sys.version.split()[0])
    else:
        for name in TOOL_SCRIPTS:
            for mod in imports(os.path.join(ROOT, name)):
                if mod not in stdlib and mod not in LOCAL_MODULES:
                    problems.append(f"{name}: imports {mod}, which is not in the standard library")
    version = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        problems.append(f"VERSION: {version!r} is not X.Y.Z")
    elif not re.search(r"^## %s$" % re.escape(version), open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read(), re.M):
        problems.append(f"CHANGELOG.md: no '## {version}' section")
    pinned = open(os.path.join(ROOT, "DOC2KB_VERSION"), encoding="utf-8").read().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", pinned):
        problems.append(f"DOC2KB_VERSION: {pinned!r} is not X.Y.Z")
    page = open(os.path.join(ROOT, PAGE), encoding="utf-8").read()
    for m in re.finditer(r"\b(?:src|href)\s*=\s*[\"']?\s*(?:https?:)?//", page, re.I):
        line = page.count("\n", 0, m.start()) + 1
        problems.append(f"{PAGE}:{line}: loads something from the network")
    for line in problems:
        print(line)
    print("result: " + (f"{len(problems)} problems" if problems else "ok"))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
