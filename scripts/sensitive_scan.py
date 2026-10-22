#!/usr/bin/env python3
"""Scan the repository for personal data, work details and credentials that must never be published.

Rules (a line can be allowed with the marker  sensitive-scan: ignore):
  personal     e-mail addresses, mobile and landline numbers, ID-card numbers (Chinese 18-digit),
               card and account numbers, Google Drive file and folder IDs, home-directory paths with
               a real user name, Chinese personal fields ("姓名：", "账号：", "密码：" with a value)
  work         organisation names (有限公司, 集团, 研究院 ..., Co., Ltd, Inc., LLC, GmbH), internal
               domain suffixes (.corp .internal .intranet .lan .local), Windows drive and UNC paths,
               workstation mount paths (/media, /run/media, /mnt, gvfs, smb:// nfs:// cifs://) and
               system data directories (/opt /srv /data), private and public IPv4 addresses,
               confidentiality markers (机密, 内部资料, 不得外传 ..., CONFIDENTIAL, INTERNAL ONLY,
               DO NOT DISTRIBUTE, Proprietary)
  credentials  GitHub, Google, AWS, Slack, OpenAI and Anthropic tokens, private key blocks,
               passwords in URLs and password/secret/token assignments
  private      terms from a private list kept outside the repository (employer, system and people
               names that no rule can recognise): the environment variable SENSITIVE_TERMS (one term
               per line; CI sets it from the repository secret of the same name) and the file named
               by SENSITIVE_TERMS_FILE, default ~/.config/ask-local-files/sensitive_terms.txt.
               Matched without regard to case; the ignore marker does not apply to them.
  metadata     JPEG/PNG/WebP EXIF, XMP and text chunks, HEIC photos, Office document authors and
               companies, PDF authors
  identity     commit author and committer e-mails must be GitHub noreply or service addresses

Placeholders pass: example.com / noreply addresses, user names such as user, me, alice, masked
numbers (asterisks), the documentation IP ranges and values written as <...>, XXX or example.

Usage:
  python3 scripts/sensitive_scan.py                 every tracked file, its name and metadata
  python3 scripts/sensitive_scan.py --range A..B    lines added, commit messages, commit identities
                                                    and changed binary files between two commits
  python3 scripts/sensitive_scan.py --message FILE  a commit message (git commit-msg hook); # lines skipped
  python3 scripts/sensitive_scan.py --unpushed      every commit on a local branch not on a remote
                                                    (pre-push hooks)
  python3 scripts/sensitive_scan.py --history       every line ever added, every commit message,
                                                    identity and binary file in all of history
  python3 scripts/sensitive_scan.py --text FILE     any text, e.g. an issue or comment body

Findings print as file:line: rule: matched text. With --redact, and always under GitHub Actions
(whose logs are public for this repository), only file:line: rule is printed, never the text.
Exit code 1 when anything is found.
"""
import argparse
import ipaddress
import logging
import os
import re
import subprocess
import sys
import zipfile
from io import BytesIO

log = logging.getLogger("sensitive_scan")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELF = "scripts/sensitive_scan.py"
MARK = "sensitive-scan: ignore"
TERMS_FILE = os.path.join("~", ".config", "ask-local-files", "sensitive_terms.txt")
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".zip", ".pdf", ".ico",
            ".docx", ".xlsx", ".pptx"}
META_EXT = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".docx", ".xlsx", ".pptx", ".pdf"}
# Addresses of services, not people: GitHub's bots sign their commits with them
# ("Signed-off-by: dependabot[bot] <support@github.com>").
SERVICE_ADDRESSES = {"support@github.com", "noreply@github.com"}
# Commit identities that are allowed besides <id>+<login>@users.noreply.github.com
IDENTITY_ADDRESSES = {"noreply@github.com", "noreply@anthropic.com"}
OK_USERS = {"user", "me", "you", "alice", "bob", "runner", "name", "username", "<user>", "claude"}
# 192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24 are reserved for documentation (RFC 5737)
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
SAFE_IPS = {"0.0.0.0", "255.255.255.255"}
# Placeholder values: <name>, ${VAR}, XXX, ***, example, your-..., 某 (Chinese "some"), 示例 ("example")
PLACEHOLDER = re.compile(r"[<>{}$]|\bx{3,}\b|X{2,}|\*{2,}|example|\byour[-_ ]|placeholder|dummy|"
                         r"changeme|redacted|某|示例|样例|举例|例如|张三|李四", re.I)
# Untracked files above this size are reported, not read line by line
MAX_UNTRACKED_BYTES = 2 * 1024 * 1024
# ASCII-only classes in look-arounds: Unicode \w includes CJK, so a number or path right after a
# Chinese character would be missed
W = "A-Za-z0-9_"
PATH_START = rf"(?<![{W}./~:-])"


def placeholder(m):
    return bool(PLACEHOLDER.search(m.group(0)))


def ip_ok(m):
    try:
        addr = ipaddress.IPv4Address(m.group(0))
    except ValueError:
        return True  # a part above 255: a version number, not an address
    return m.group(0) in SAFE_IPS or addr.is_loopback or any(addr in n for n in DOC_NETS)


def home_ok(m):
    return m.group(1).lower() in OK_USERS


def email_ok(m):
    s = m.group(0)
    return bool(re.search(r"@(example\.(com|org|net)|users\.noreply\.github\.com|anthropic\.com)$", s, re.I)
                or s.lower() in SERVICE_ADDRESSES)


def drive_ok(m):
    s = m.group(0)
    return not (re.search(r"\d", s[1:]) and re.search(r"[A-Za-z]", s))


def org_ok(m):
    """A real name, not the suffix quoted or listed in a sentence about names ("后缀（有限公司、集团）")."""
    head = m.group(1)
    return placeholder(m) or not head or head[-1] in "（(、" or bool(re.search(r"(?:名称|后缀)[：:]?$", head))


def id_ok(m):
    """Chinese ID numbers carry a check digit (GB 11643); a long number that fails it is not one."""
    s = m.group(0)
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    return "10X98765432"[sum(int(c) * w for c, w in zip(s[:17], weights)) % 11] != s[17].upper()


def card_ok(m):
    """Card numbers pass the Luhn check; a number that fails it is not one."""
    digits = re.sub(r"\D", "", m.group(0))
    total = 0
    for i, c in enumerate(reversed(digits)):
        d = int(c) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 != 0


def domain_ok(m):
    return m.group(0).lower().startswith(("localhost.", "example.", "my-")) or placeholder(m)


# (label, pattern, allow): allow(match) returning True means the match is not a finding
RULES = [
    # personal
    ("e-mail", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), email_ok),
    ("phone", re.compile(r"(?<![\d.])(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"), None),
    ("phone", re.compile(r"(?<![\d.])\+?\d{1,3}[- ]\(?\d{2,4}\)?[- ]\d{3,4}[- ]\d{3,4}(?!\d)"), None),
    ("landline", re.compile(rf"(?<![{W}.-])0\d{{2,3}}-\d{{7,8}}(?![{W}-])"), None),
    ("ID number", re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"), id_ok),
    ("card number", re.compile(r"(?<![\d-])(?:\d{4}[ -]){3}\d{4,7}(?![\d-])|(?<!\d)\d{16,19}(?!\d)"), card_ok),
    ("Drive ID", re.compile(r"(?<![A-Za-z0-9_-])1[A-Za-z0-9_-]{32}(?![A-Za-z0-9_-])"), drive_ok),
    ("home path", re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\\\?Users\\\\?)([A-Za-z0-9._-]+)"), home_ok),
    ("personal field", re.compile(r"(?:姓名|联系人|负责人|经办人|审批人|申请人|收件人|账号|帐号|用户名|工号|员工号|"
                                  r"手机|电话|身份证号?|密码|口令)\s*[:：]\s*[^\s，。；,;」』\"'”）)、<>]{2,}"), placeholder),
    # work
    ("organisation", re.compile(r"([一-鿿A-Za-z0-9（）()]{0,20}?)(?:股份有限公司|有限责任公司|有限公司|集团|分公司|子公司|"
                                r"研究院|研究所|设计院|事业部)"), org_ok),
    ("organisation", re.compile(r"\b[A-Z][\w&-]*(?:\s+[A-Z][\w&-]*)*,?\s+(?:Co\.,?\s*Ltd|Ltd\.|Inc\.|LLC|GmbH|"
                                r"Pty\.? Ltd)(?![\w])"), placeholder),
    ("internal domain", re.compile(rf"(?<![{W}-])[{W}-]+(?:\.[{W}-]+)*\.(?:corp|internal|intranet|lan|local)(?![{W}(])"),
     domain_ok),
    ("IP address", re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])"), ip_ok),
    ("Windows path", re.compile(r"(?<![A-Za-z\\])[A-Za-z]:\\\\?(?![nrt\\\"'])[\w一-鿿]"), placeholder),
    ("UNC path", re.compile(r"(?<!\\)\\\\(?:\\\\)?[A-Za-z0-9][\w.-]+\\\\?(?:[\w$一-鿿]|(?=[\"'\s]|$))"), placeholder),
    ("mount path", re.compile(PATH_START + r"/(?:run/)?media/[\w.-]+/"), placeholder),
    ("mount path", re.compile(PATH_START + r"/mnt/(?!user-data\b|data\b)\w[\w.-]*"), placeholder),
    ("network share", re.compile(PATH_START + r"/run/user/\d+/gvfs/|(?<![\w])(?:smb|nfs|cifs|afp)://"), None),
    ("system directory", re.compile(PATH_START + r"/(?:opt|srv|data)/\w[\w.-]*"), placeholder),
    # Words a document carries when it must stay inside an organisation. Single English words only
    # as a marking (capitalised or upper case), so "confidential data" in prose passes.
    ("confidentiality marker", re.compile(r"机密|绝密|涉密|密级|内部资料|内部文件|内部使用|仅限内部|"
                                          r"内部公开|不得外传|严禁外传|请勿外传"), None),
    ("confidentiality marker", re.compile(r"(?i:\b(?:internal (?:use )?only|do not (?:distribute|forward|share)|"
                                          r"not for (?:distribution|external use))\b)|"
                                          r"\b(?:CONFIDENTIAL|Confidential|PROPRIETARY|Proprietary)\b"), None),
    # credentials
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"), None),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), None),
    ("Google OAuth secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}"), None),
    ("AWS key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), None),
    ("API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}"), None),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"), None),
    ("password in URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:([^\s:/@]+)@"), placeholder),
    ("secret value", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[-_]?key|access[-_]?token|auth[-_]?token)"
                                r"[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']"), placeholder),
]


def load_terms():
    """Private terms from SENSITIVE_TERMS and the terms file; lines starting with # are comments."""
    texts = [os.environ.get("SENSITIVE_TERMS", "")]
    path = os.path.expanduser(os.environ.get("SENSITIVE_TERMS_FILE") or TERMS_FILE)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            texts.append(fh.read())
    terms = []
    for text in texts:
        for t in text.splitlines():
            t = t.strip()
            if t and not t.startswith("#") and t.casefold() not in [x.casefold() for x in terms]:
                terms.append(t)
    short = [t for t in terms if len(t) < 2]
    if short:
        log.warning("%d private term(s) shorter than two characters ignored", len(short))
    terms = [t for t in terms if len(t) >= 2]
    if not terms:
        log.info("no private terms (set SENSITIVE_TERMS or create %s)", TERMS_FILE)
    return [re.compile(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])" if t.isascii() else re.escape(t), re.I)
            for t in terms]


class Finding:
    def __init__(self, where, line, label, text, private=False):
        self.where, self.line, self.label, self.text, self.private = where, line, label, text, private

    def show(self, redact):
        loc = f"{self.where}:{self.line}" if self.line else self.where
        if redact or self.private and not self.text:
            return f"{loc}: {self.label}"
        return f"{loc}: {self.label}: {self.text}"


class Scanner:
    def __init__(self, terms=None):
        self.terms = load_terms() if terms is None else terms
        self.findings = []

    def add(self, *args, **kw):
        self.findings.append(Finding(*args, **kw))

    def line(self, where, i, line):
        """Check one line against the private terms, then (unless marked) against the rules."""
        for rx in self.terms:
            m = rx.search(line)
            if m:
                self.add(where, i, "private term", m.group(0), private=True)
        if MARK in line or re.sub(r"@[0-9a-f]{7}$", "", where) == SELF:
            return
        for label, rx, allow in RULES:
            for m in rx.finditer(line):
                if not (allow and allow(m)):
                    self.add(where, i, label, m.group(0))

    def lines(self, items):
        for where, i, line in items:
            self.line(where, i, line)

    def identity(self, sha, role, name, email):
        """A commit author or committer: a noreply address, and no private term in the name."""
        email = email.strip()
        low = email.lower()
        if not (low.endswith("@users.noreply.github.com") or low in IDENTITY_ADDRESSES):
            self.add(f"commit {sha[:7]}", 0, f"{role} e-mail is not a noreply address", email)
        for rx in self.terms:
            m = rx.search(name) or rx.search(email)
            if m:
                self.add(f"commit {sha[:7]}", 0, f"private term in {role}", m.group(0), private=True)

    def metadata(self, where, data):
        for label in file_metadata(where, data):
            self.add(where, 0, label, "")


def git(*args, binary=False):
    p = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=ROOT, capture_output=True, check=True)
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def tracked_files():
    """Tracked files and untracked ones that are not ignored: a hook runs before the git add
    in "git add -A && git commit", so a new file must be scanned before it is tracked."""
    out = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({f for f in out.split("\0") if f and os.path.isfile(os.path.join(ROOT, f))})


def file_lines(files):
    for f in files:
        yield f, 0, f  # the path itself: a file name can leak as much as its content
        if os.path.splitext(f)[1].lower() in SKIP_EXT:
            continue
        try:
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    yield f, i, line.rstrip("\n")
        except (UnicodeDecodeError, OSError):
            continue


def added_lines(diff, suffix=""):
    """(file, line number, text) of the added lines in a unified diff. Follows the line counts in
    each @@ header, so an added line starting with "++ " is never taken for a file header.
    A line "commit <sha>" outside a hunk (git log -p --format="commit %H") sets the suffix @sha."""
    path, lineno, old_left, new_left = None, 0, 0, 0
    for raw in diff.split("\n"):
        if old_left > 0 or new_left > 0:
            if raw.startswith("+"):
                if path:
                    yield path + suffix, lineno, raw[1:]
                lineno += 1
                new_left -= 1
            elif raw.startswith("-"):
                old_left -= 1
            elif raw.startswith(" "):
                lineno += 1
                old_left -= 1
                new_left -= 1
            continue  # "\ No newline at end of file" does not count
        if raw.startswith("\0commit "):
            suffix = "@" + raw[8:15]
        elif raw.startswith("+++ "):
            name = raw[4:].rstrip("\t")
            path = name[2:] if name.startswith("b/") else None
            if path:
                yield path + suffix, 0, path
        elif raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw)
            if m:
                old_left = int(m.group(1) or 1)
                lineno = int(m.group(2))
                new_left = int(m.group(3) or 1)


def message_lines(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if not line.startswith("#"):
                yield "commit message", i, line.rstrip("\n")


def text_lines(path, label):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            yield label, i, line.rstrip("\n")


def scan_commits(sc, revs):
    """Messages, identities, added lines and binary files of the commits that git log selects."""
    out = git("log", "--format=%x00%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B", *revs)
    parts = out.split("\0")[1:]
    for k in range(0, len(parts) - len(parts) % 6, 6):
        sha, an, ae, cn, ce, body = parts[k:k + 6]
        sc.identity(sha, "author", an, ae)
        sc.identity(sha, "committer", cn, ce)
        for i, line in enumerate(body.strip("\n").split("\n"), 1):
            sc.line(f"commit {sha[:7]} message", i, line)
    diff = git("log", "-p", "-U0", "--no-color", "--no-ext-diff", "--no-renames", "--format=%x00commit %H", *revs)
    sc.lines(added_lines(diff))
    for sha, path in changed_binaries(revs):
        sc.metadata(f"{path}@{sha[:7]}", git("cat-file", "blob", sha, binary=True))


def changed_binaries(revs):
    """(blob sha, path) of each distinct image, Office or PDF file added or changed by the commits."""
    out = git("log", "--format=", "--raw", "--no-renames", "--diff-filter=AM", "--no-abbrev", *revs)
    seen = set()
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) >= 4 and os.path.splitext(path)[1].lower() in META_EXT and fields[3] not in seen:
            seen.add(fields[3])
            yield fields[3], path


# ---- file metadata ---------------------------------------------------------------------------

def file_metadata(path, data):
    """Labels for metadata that can identify a person, a device or a place."""
    ext = os.path.splitext(path.split("@")[0])[1].lower()
    if ext in (".heic", ".heif"):
        return ["HEIC photo (carries camera and location data; export as PNG or JPEG without metadata)"]
    if data[:2] == b"\xff\xd8":
        return jpeg_metadata(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return png_metadata(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return webp_metadata(data)
    if ext in (".docx", ".xlsx", ".pptx"):
        return office_metadata(data)
    if data[:5] == b"%PDF-":
        return pdf_metadata(data)
    return []


def jpeg_metadata(data):
    found, i = [], 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):  # end of image, start of scan
            break
        size = int.from_bytes(data[i + 2:i + 4], "big")
        body = data[i + 4:i + 2 + size]
        if marker == 0xE1 and body.startswith(b"Exif\0"):
            found.append("EXIF metadata" + (" with GPS location" if b"\x88\x25" in body else ""))
        elif marker == 0xE1 and body.startswith(b"http://ns.adobe.com/xap/"):
            found.append("XMP metadata")
        elif marker == 0xFE:
            found.append("JPEG comment")
        i += 2 + size
    return found


def png_metadata(data):
    found, i = [], 8
    while i + 8 <= len(data):
        size = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + size]
        if kind == b"eXIf":
            found.append("EXIF metadata")
        elif kind in (b"tEXt", b"zTXt", b"iTXt"):
            key = body.split(b"\0", 1)[0].decode("latin-1")
            if key not in ("Software",):
                found.append(f"PNG text chunk '{key}'")
        elif kind == b"IEND":
            break
        i += 12 + size
    return found


def webp_metadata(data):
    found, i = [], 12
    while i + 8 <= len(data):
        kind = data[i:i + 4]
        size = int.from_bytes(data[i + 4:i + 8], "little")
        if kind == b"EXIF":
            found.append("EXIF metadata")
        elif kind == b"XMP ":
            found.append("XMP metadata")
        i += 8 + size + (size & 1)
    return found


def office_metadata(data):
    found = []
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            names = set(z.namelist())
            core = z.read("docProps/core.xml").decode("utf-8", "replace") if "docProps/core.xml" in names else ""
            app = z.read("docProps/app.xml").decode("utf-8", "replace") if "docProps/app.xml" in names else ""
    except (zipfile.BadZipFile, KeyError, OSError):
        return found
    for tag, label in (("dc:creator", "document author"), ("cp:lastModifiedBy", "document last editor")):
        if re.search(rf"<{tag}>[^<]*\S[^<]*</{tag}>", core):
            found.append(label)
    if re.search(r"<(Company|Manager)>[^<]*\S[^<]*</\1>", app):
        found.append("document company or manager")
    return found


def pdf_metadata(data):
    found = []
    if re.search(rb"/Author\s*(?:\((?!\s*\))|<(?!\s*>))", data):
        found.append("PDF author")
    if re.search(rb"<dc:creator>.*?<rdf:li[^>]*>\s*[^<\s]", data, re.S):
        found.append("PDF XMP creator")
    return found


def gh_escape(text, prop=False):
    """Escape a GitHub Actions workflow command: % and line breaks in data, also : and , in properties."""
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--range", help="commit range, e.g. origin/main..HEAD")
    g.add_argument("--message", metavar="FILE", help="a commit message file")
    g.add_argument("--unpushed", action="store_true", help="commits on local branches not on any remote")
    g.add_argument("--history", action="store_true", help="all of history, every branch and tag")
    g.add_argument("--text", metavar="FILE", help="any text file, e.g. an issue body")
    ap.add_argument("--label", default="text", help="name shown for --text findings")
    ap.add_argument("--redact", action="store_true", help="print where and which rule, never the text "
                    "(always on under GitHub Actions)")
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "WARNING").upper(),
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = ap.parse_args()
    logging.basicConfig(level=a.log_level, format=LOG_FORMAT, stream=sys.stderr)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # a stream that cannot be reconfigured keeps its encoding
    actions = bool(os.environ.get("GITHUB_ACTIONS"))
    redact = a.redact or actions
    sc = Scanner()
    if a.message:
        sc.lines(message_lines(a.message))
    elif a.text:
        sc.lines(text_lines(a.text, a.label))
    elif a.range:
        scan_commits(sc, [a.range.replace("...", "..")])
    elif a.unpushed:
        scan_commits(sc, ["--branches", "--not", "--remotes"])
    elif a.history:
        scan_commits(sc, ["--all"])
    else:
        files = tracked_files()
        untracked = set(git("ls-files", "-z", "--others", "--exclude-standard").split("\0"))
        for f in [f for f in files if f in untracked]:
            size = os.path.getsize(os.path.join(ROOT, f))
            if size > MAX_UNTRACKED_BYTES:
                # not read line by line, so a hook stays fast; still reported, to be checked or ignored
                sc.add(f, 0, f"large untracked file not scanned ({size // 1024} KB); check it or add it to "
                       ".gitignore", "")
                files.remove(f)
        sc.lines(file_lines(files))
        for f in files:
            if os.path.splitext(f)[1].lower() in META_EXT:
                with open(os.path.join(ROOT, f), "rb") as fh:
                    sc.metadata(f, fh.read())
    for f in sc.findings:
        print(f.show(redact))
        if actions and f.line and os.path.isfile(os.path.join(ROOT, f.where)):
            print(f"::error file={gh_escape(f.where, True)},line={f.line}::possible {gh_escape(f.label)}")
    if sc.findings and any(x.private for x in sc.findings):
        print("note: private-term findings come from your own list; edit the text, the ignore marker does not apply")
    print(f"result: {len(sc.findings)} possible findings" if sc.findings else "result: clean")
    sys.exit(1 if sc.findings else 0)


if __name__ == "__main__":
    main()
