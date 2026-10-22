#!/usr/bin/env python3
"""Check that doc2kb_core.py is an unchanged copy of the doc2kb release named in DOC2KB_VERSION.

The converter is maintained in its own repository, walkwithedi/doc2kb; this one carries a copy.

  check_doc2kb.py DIR    DIR: a checkout of walkwithedi/doc2kb at tag v<DOC2KB_VERSION>
                         (CI checks it out); exit 1 unless DIR/VERSION matches DOC2KB_VERSION
                         and DIR/doc2kb_core.py matches the copy byte for byte

To update the copy: copy doc2kb_core.py from the new release, write its version into
DOC2KB_VERSION, run the tests, and note it in CHANGELOG.md.

stdout: one result line. stderr: log. Exit codes: 0 ok; 1 copy differs; 2 bad arguments or files.
"""
import argparse
import logging
import os
import sys

log = logging.getLogger("check_doc2kb")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path, mode="rb"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError as e:
        log.error("cannot read %s: %s", path, e)
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dir", help="doc2kb checkout at the release in DOC2KB_VERSION")
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "WARNING").upper(),
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = ap.parse_args()
    logging.basicConfig(level=a.log_level, format=LOG_FORMAT, stream=sys.stderr)
    pinned = read(os.path.join(ROOT, "DOC2KB_VERSION"), "r").strip()
    theirs = read(os.path.join(a.dir, "VERSION"), "r").strip()
    problems = []
    if theirs != pinned:
        problems.append(f"DOC2KB_VERSION is {pinned}, but {a.dir} holds doc2kb {theirs}")
    if read(os.path.join(ROOT, "doc2kb_core.py")) != read(os.path.join(a.dir, "doc2kb_core.py")):
        problems.append(f"doc2kb_core.py differs from doc2kb {theirs}")
    for p in problems:
        print(p)
    print("result: " + ("; ".join(problems) if problems else f"doc2kb_core.py is doc2kb {pinned}"))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
