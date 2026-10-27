"""Security checks on proposed changes. Plain code: no model is involved.

They run on what is about to be written, before the user is asked to approve,
and report ``Finding`` objects:

  * high     the user must explicitly confirm before applying (the server
             enforces this): secrets, hidden characters, download-and-run
  * warning  worth a look: scripts, CI files, dangerous commands, big rewrites
  * info     noted for completeness

Only lines the change ADDS are scanned for content, so a secret that is already
in a file (or being removed) is not reported. Findings never contain a whole
secret, and ``redact_secrets`` masks secret-looking values in any text that is
shown or logged (the diff, tool previews), because the run's log is stored.
"""

import difflib
import json
import math
import os
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass

from app.backend.agents.changes import ChangeSet, FileChange

HIGH, WARNING, INFO = "high", "warning", "info"
_RANK = {HIGH: 0, WARNING: 1, INFO: 2}

MAX_FINDINGS = 40
MAX_SCAN_LINE_CHARS = 2000
EXCERPT_CHARS = 120

BIG_REWRITE_RATIO = 0.5       # of a file's lines touched
BIG_REWRITE_MIN_LINES = 20    # ignore tiny files
MOSTLY_REMOVED_RATIO = 0.5
MOSTLY_REMOVED_MIN_LINES = 20
BIG_CHANGE_LINES = 200        # added + removed across all files
LONG_NEW_FILE_LINES = 300
MANY_FILES = 4
LARGE_DIFF_FALLBACK_LINES = 40_000


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    path: str
    line: int | None
    message: str
    excerpt: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------- secrets

# (code, pattern, what it is, group holding the secret value or 0 for the match)
_SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----"), "a private key", 0),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "an AWS access key", 0),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"), "a GitHub token", 0),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "a Slack token", 0),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), "a Google API key", 0),
    ("stripe_key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}"), "a live Stripe key", 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "a JSON Web Token", 0),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@\"']+:([^\s@\"']{3,})@"), "a password inside a URL", 1),
]

_ASSIGNMENT = re.compile(
    r"\b(?P<name>[\w.\-]*(?:password|passwd|pwd|secret|api[_\-]?key|apikey|auth[_\-]?token|access[_\-]?token|"
    r"access[_\-]?key|private[_\-]?key|client[_\-]?secret|token)[\w.\-]*)"
    r"[\"']?\s*[:=]\s*"
    r"(?:(?P<q>[\"'])(?P<quoted>[^\"']{6,})(?P=q)|(?P<bare>[^\s\"'#,;)\]}]{8,}))",
    re.IGNORECASE,
)

_PLACEHOLDER_WORDS = {
    "changeme", "change-me", "password", "passwd", "secret", "example", "none", "null", "true", "false",
    "undefined", "redacted", "todo", "tbd", "empty", "default", "required", "string",
}
_PLACEHOLDER_PREFIXES = ("your", "my-", "my_", "example", "sample", "test", "dummy", "fake", "placeholder", "replace", "insert")
_PLACEHOLDER_MARKERS = ("${", "{{", "%(", "$(", "<", "environ", "getenv", "process.env", "config.", "secrets.", "vault", "input(")


def _entropy(text: str) -> float:
    if not text:
        return 0.0

    counts = {char: text.count(char) for char in set(text)}

    return -sum((n / len(text)) * math.log2(n / len(text)) for n in counts.values())


def _is_placeholder(value: str) -> bool:
    low = value.lower().strip()

    return (
        low in _PLACEHOLDER_WORDS
        or bool(re.fullmatch(r"[x*#.\-_ ]+", low))
        or low.startswith(_PLACEHOLDER_PREFIXES)
        or any(marker in low for marker in _PLACEHOLDER_MARKERS)
        or value.startswith("$")
    )


# The fixed start of well-known token types. Showing it is harmless and tells you what
# was found; for anything else (a password, an unknown token) nothing is shown at all.
_KNOWN_PREFIX = re.compile(r"^(?:AKIA|ASIA|gh[pousr]_|github_pat_|xox[abprs]-|AIza|[rs]k_live_|eyJ)")


def _mask(secret: str) -> str:
    known = _KNOWN_PREFIX.match(secret)

    if known and len(secret) >= 20:
        return secret[: max(4, len(known.group(0)))] + "****"

    return "****"


def _redact(line: str, spans: list[tuple[int, int]]) -> str:
    """The line with each secret span masked, trimmed for display."""
    out = line

    for start, end in sorted(spans, reverse=True):
        out = out[:start] + _mask(out[start:end]) + out[end:]

    out = " ".join(out.split())

    return out if len(out) <= EXCERPT_CHARS else out[: EXCERPT_CHARS - 1] + "\u2026"


@dataclass(frozen=True)
class _Hit:
    """One secret-looking value found in a line."""

    code: str
    label: str                 # what it looks like ("an AWS access key")
    span: tuple[int, int]      # where the secret value is in the line
    severity: str
    name: str | None = None    # the variable name, for hard-coded assignments


def _secret_hits(scan: str) -> list[_Hit]:
    """Every secret-looking value in one line (already limited to a sane length)."""
    hits: list[_Hit] = []

    for code, pattern, label, group in _SECRET_PATTERNS:
        for match in pattern.finditer(scan):
            value = match.group(group)

            if code == "url_credentials" and _is_placeholder(value):
                continue

            hits.append(_Hit(code, label, (match.start(group), match.end(group)), HIGH))

    taken = [hit.span for hit in hits]

    for match in _ASSIGNMENT.finditer(scan):
        quoted = match.group("quoted")
        value = quoted if quoted is not None else match.group("bare")

        if _is_placeholder(value):
            continue

        # A bare value that is just a name (or a call/lookup) is code reading a value, not a
        # secret. Names have no digits: "abcDEF123456xyzQ" in a .env or YAML file is a value.
        if quoted is None and re.fullmatch(r"[A-Za-z_][A-Za-z_.]*(?:[(\[].*)?", value):
            continue

        group_name = "quoted" if quoted is not None else "bare"
        span = (match.start(group_name), match.end(group_name))

        if any(start <= span[0] < end for start, end in taken):
            continue    # already reported by a more specific pattern

        has_letter, has_digit = bool(re.search(r"[A-Za-z]", value)), bool(re.search(r"\d", value))
        strong = (len(value) >= 12 and _entropy(value) >= 3.0) or (len(value) >= 8 and has_letter and has_digit)

        hits.append(_Hit("hardcoded_secret", "", span, HIGH if strong else WARNING, match.group("name")))

    return hits


def _secret_findings(path: str, number: int, line: str) -> Iterator[Finding]:
    scan = line[:MAX_SCAN_LINE_CHARS]

    for hit in _secret_hits(scan):
        if hit.code == "hardcoded_secret":
            yield Finding(
                hit.severity, hit.code, path, number,
                f"A value is being assigned to '{hit.name}': is this a hard-coded secret?",
                _redact(scan, [hit.span]),
            )
        else:
            excerpt = _redact(scan, []) if hit.code == "private_key" else _redact(scan, [hit.span])

            yield Finding(hit.severity, hit.code, path, number, f"Looks like {hit.label}.", excerpt)


_BEGIN_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")
_END_KEY = re.compile(r"-----END (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")
_DIFF_HEADERS = ("---", "+++", "@@")


def redact_secrets(text: str, diff: bool = False) -> str:
    """``text`` with secret-looking values masked, for showing or logging.

    The same detection as the findings, plus the BODY of private keys (the
    lines between BEGIN and END). With ``diff=True`` each line's first
    character is a diff marker (+, - or space) and is left alone.
    """
    out: list[str] = []
    in_key = False

    for line in text.split("\n"):
        header = diff and line.startswith(_DIFF_HEADERS)
        marker, body = (line[:1], line[1:]) if diff and not header and line else ("", line)

        if header:
            out.append(line)
            continue

        if in_key:
            if _END_KEY.search(body):
                in_key = False
                out.append(line)
            else:
                out.append(marker + "(private key data hidden)")

            continue

        scan = body[:MAX_SCAN_LINE_CHARS]
        hits = _secret_hits(scan)
        masked = scan

        # The key header stays visible (so you can see what is hidden); its body is handled below.
        for hit in sorted((h for h in hits if h.code != "private_key"), key=lambda h: h.span, reverse=True):
            masked = masked[: hit.span[0]] + _mask(masked[hit.span[0] : hit.span[1]]) + masked[hit.span[1] :]

        out.append(marker + masked + body[MAX_SCAN_LINE_CHARS:])

        if _BEGIN_KEY.search(body) and not _END_KEY.search(body):
            in_key = True

    return "\n".join(out)


# ----------------------------------------------------------- suspicious content

_BIDI = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
# U+200C / U+200D (joiners) are NOT flagged: Devanagari and other scripts need them.
_INVISIBLE = set("\u200b\u2060\ufeff")

_LONG_BLOB = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")

_INJECTION = re.compile(
    r"ignore (?:all |any )?(?:the )?(?:previous|prior|above) instructions|disregard (?:all |the )?(?:previous|prior|above)|"
    r"you are now\b|new instructions:|reveal your (?:system )?(?:prompt|instructions)|system prompt",
    re.IGNORECASE,
)

# (code, severity, pattern, message)
_DANGEROUS = [
    ("remote_exec", HIGH, re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b", re.I),
     "Downloads something and runs it straight away."),
    ("remote_exec", HIGH, re.compile(r"\b(?:iex|invoke-expression)\b.*(?:downloadstring|invoke-webrequest|\biwr\b|https?:)", re.I),
     "Downloads something and runs it straight away."),
    ("encoded_command", HIGH, re.compile(r"powershell(?:\.exe)?\b[^\n]*\s-(?:enc|encodedcommand)\b", re.I),
     "Runs an encoded PowerShell command, which hides what it does."),
    ("dynamic_exec", WARNING, re.compile(r"\b(?:eval|exec)\s*\("), "Runs text as code (eval/exec)."),
    ("shell_exec", WARNING,
     re.compile(r"\bos\.system\s*\(|\bshell\s*=\s*True\b|\bchild_process\b|\bRuntime\.getRuntime\(\)\.exec|\bos\.popen\s*\("),
     "Runs shell commands."),
    ("destructive", WARNING,
     re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r|Remove-Item\b[^\n]*-Recurse|\bdel\s+/[sq]\b|\brmdir\s+/s\b|\bformat\s+[a-z]:|\bshutil\.rmtree\b", re.I),
     "Deletes files in bulk."),
    ("permissions", WARNING,
     re.compile(r"\bchmod\s+(?:-R\s+)?[0-7]*777\b|\bicacls\b[^\n]*\bEveryone\b|Set-ExecutionPolicy\s+(?:-\w+\s+)*(?:Bypass|Unrestricted)", re.I),
     "Loosens file or script permissions."),
    ("tls_off", WARNING,
     re.compile(r"\bverify\s*=\s*False\b|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['\"]?0|_create_unverified_context|--no-check-certificate|--insecure|\bcurl\b[^\n]*\s-k\b", re.I),
     "Turns off certificate checking."),
    ("sudo", WARNING, re.compile(r"\bsudo\b"), "Uses sudo (administrator rights)."),
    ("deserialize", WARNING, re.compile(r"\bpickle\.loads?\s*\(|\byaml\.load\s*\((?![^)]*Loader)"),
     "Loads data in a way that can run code."),
    ("network", INFO,
     re.compile(r"\brequests\.(?:get|post|put|delete)\s*\(|\burllib\.request\b|\bfetch\s*\(\s*['\"`]https?:|\bInvoke-(?:WebRequest|RestMethod)\b|"
                r"\bhttp\.client\b|\bsocket\.(?:create_connection|connect)\b|\bcurl\s+https?:|\bwget\s+https?:", re.I),
     "Makes a network request."),
]


def _content_findings(path: str, number: int, line: str, first_line_of_file: bool) -> Iterator[Finding]:
    scan = line[:MAX_SCAN_LINE_CHARS]

    for char in scan:
        if char in _BIDI:
            yield Finding(
                HIGH, "hidden_characters", path, number,
                "Contains hidden direction-control characters, which can make code read differently than it runs.",
                _redact(scan.encode("unicode_escape").decode("ascii"), []),
            )
            break

    else:
        for index, char in enumerate(scan):
            if char in _INVISIBLE and not (char == "\ufeff" and first_line_of_file and index == 0):
                yield Finding(
                    WARNING, "invisible_characters", path, number,
                    "Contains an invisible character.",
                    _redact(scan.encode("unicode_escape").decode("ascii"), []),
                )
                break

    for code, severity, pattern, message in _DANGEROUS:
        if pattern.search(scan):
            yield Finding(severity, code, path, number, message, _redact(scan, []))

    if _LONG_BLOB.search(scan):
        yield Finding(WARNING, "encoded_blob", path, number, "Contains a very long encoded blob, which can hide what it is.", None)

    if _INJECTION.search(scan):
        yield Finding(WARNING, "prompt_injection", path, number, "The added text reads like instructions to an AI.", _redact(scan, []))


# ------------------------------------------------------------------ sensitive places

_SCRIPT_SUFFIXES = {
    ".bat", ".cmd", ".ps1", ".psm1", ".sh", ".bash", ".zsh", ".fish", ".vbs", ".wsf", ".scr", ".com", ".msi", ".reg",
}
_CI_DIRS = (".github/workflows", ".github/actions", ".circleci", ".gitlab", ".husky", ".githooks")
_CI_NAMES = {
    "jenkinsfile", ".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "appveyor.yml",
    "bitbucket-pipelines.yml", ".pre-commit-config.yaml",
}
_BUILD_NAMES = {
    "package.json", "setup.py", "setup.cfg", "pyproject.toml", "makefile", "dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "pom.xml", "build.gradle", "cargo.toml", "gemfile", "composer.json", "requirements.txt",
}
_SSH_NAMES = {"authorized_keys", "known_hosts", "sudoers", "ssh_config", "sshd_config"}


def _path_findings(change: FileChange) -> Iterator[Finding]:
    path = change.path
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    verb = "New" if change.original is None else "Changed"

    if suffix in _SCRIPT_SUFFIXES:
        yield Finding(WARNING, "script_file", path, None, f"{verb} script file: it can run commands when someone opens or runs it.")

    if name in _SSH_NAMES or "/.ssh/" in "/" + lowered:
        yield Finding(HIGH, "ssh_file", path, None, f"{verb} SSH or sudo configuration file, which controls who can log in or gain rights.")

    if name in _CI_NAMES or any(("/" + lowered).find("/" + directory + "/") != -1 for directory in _CI_DIRS):
        yield Finding(WARNING, "automation_file", path, None, f"{verb} automation file (CI or git hook): it runs automatically.")

    if name in _BUILD_NAMES or (name.startswith("requirements") and suffix == ".txt"):
        yield Finding(INFO, "build_file", path, None, f"{verb} build or dependency file.")

    if change.original is not None and os.name != "nt":
        try:
            if change.absolute.stat().st_mode & 0o111:
                yield Finding(INFO, "executable", path, None, "This file is marked executable.")
        except OSError:
            pass


# -------------------------------------------------------------------------- the diff

def _added_lines(change: FileChange) -> tuple[list[tuple[int, str]], int, int, int, int]:
    """(added lines with their new line numbers, added, removed, old total, new total)."""
    new = change.current.split("\n")

    if new and new[-1] == "":
        new.pop()

    if change.original is None:
        return list(enumerate(new, start=1)), len(new), 0, 0, len(new)

    old = change.original.split("\n")

    if old and old[-1] == "":
        old.pop()

    if len(old) + len(new) > LARGE_DIFF_FALLBACK_LINES:
        known = set(old)
        added = [(i, line) for i, line in enumerate(new, start=1) if line not in known]

        return added, len(added), max(0, len(old) - (len(new) - len(added))), len(old), len(new)

    added: list[tuple[int, str]] = []
    removed = 0

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            added.extend((j + 1, new[j]) for j in range(j1, j2))

        if tag in ("replace", "delete"):
            removed += i2 - i1

    return added, len(added), removed, len(old), len(new)


def _size_findings(change: FileChange, added: int, removed: int, old_total: int, new_total: int) -> Iterator[Finding]:
    path = change.path

    if change.original is not None and old_total >= BIG_REWRITE_MIN_LINES:
        touched = max(added, removed)
        ratio = touched / old_total

        if ratio > BIG_REWRITE_RATIO:
            yield Finding(
                WARNING, "big_rewrite", path, None,
                f"Rewrites about {int(ratio * 100)}% of the file, which is a lot for a small change.",
            )

        if old_total >= MOSTLY_REMOVED_MIN_LINES and new_total < old_total * MOSTLY_REMOVED_RATIO:
            yield Finding(
                WARNING, "mostly_removed", path, None,
                f"Removes more than half of the file ({old_total} lines down to {new_total}).",
            )

    if change.original is None and new_total > LONG_NEW_FILE_LINES:
        yield Finding(INFO, "long_new_file", path, None, f"Creates a long new file ({new_total} lines).")


def run_checks(changes: ChangeSet) -> list[Finding]:
    """Check everything staged in ``changes``. Returns findings, most serious first."""
    findings: list[Finding] = []
    total_touched = 0
    files = changes.files()

    for change in files:
        findings.extend(_path_findings(change))

        added_lines, added, removed, old_total, new_total = _added_lines(change)
        total_touched += added + removed

        findings.extend(_size_findings(change, added, removed, old_total, new_total))

        for number, line in added_lines:
            findings.extend(_secret_findings(change.path, number, line))
            findings.extend(_content_findings(change.path, number, line, first_line_of_file=(number == 1)))

    if total_touched > BIG_CHANGE_LINES:
        findings.append(
            Finding(WARNING, "big_change", "(all files)", None, f"Changes {total_touched} lines in total, which is a large change.")
        )

    if len(files) >= MANY_FILES:
        findings.append(Finding(INFO, "many_files", "(all files)", None, f"Touches {len(files)} files."))

    findings.sort(key=lambda f: (_RANK[f.severity], f.path, f.line or 0, f.code))

    if len(findings) > MAX_FINDINGS:
        extra = len(findings) - MAX_FINDINGS
        findings = findings[:MAX_FINDINGS] + [
            Finding(INFO, "more", "(more)", None, f"...and {extra} more finding{'' if extra == 1 else 's'} not shown.")
        ]

    return findings


# -------------------------------------------------------------------------- reporting

def counts(findings: list[Finding]) -> dict[str, int]:
    result = {HIGH: 0, WARNING: 0, INFO: 0}

    for finding in findings:
        if finding.code != "more":
            result[finding.severity] += 1

    return result


def has_high(findings: list[Finding]) -> bool:
    return any(f.severity == HIGH for f in findings)


def summarize(findings: list[Finding]) -> str:
    """One line, e.g. "1 high-risk finding, 2 warnings, 1 note"."""
    n = counts(findings)
    parts = []

    if n[HIGH]:
        parts.append(f"{n[HIGH]} high-risk finding{'' if n[HIGH] == 1 else 's'}")

    if n[WARNING]:
        parts.append(f"{n[WARNING]} warning{'' if n[WARNING] == 1 else 's'}")

    if n[INFO]:
        parts.append(f"{n[INFO]} note{'' if n[INFO] == 1 else 's'}")

    return ", ".join(parts) if parts else "nothing unusual found"


def to_json(findings: list[Finding]) -> str:
    return json.dumps({"findings": [f.to_dict() for f in findings]})
