"""Bash command safety checks and secret scanning.

Implements pattern-based detection of dangerous shell commands following
the guide's Section 5.1: dangerous git operations, destructive filesystem
ops, network exfiltration, hook/signature bypass, and shell injection.

Also provides secret-pattern scanning to redact accidental credential
exposure in tool outputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DANGEROUS_GIT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"git\s+push\s+.*--force", re.I), "git push --force can overwrite remote history"),
    (re.compile(r"git\s+push\s+-f\b", re.I), "git push -f can overwrite remote history"),
    (re.compile(r"git\s+reset\s+--hard", re.I), "git reset --hard discards uncommitted changes"),
    (re.compile(r"git\s+clean\s+-f", re.I), "git clean -f permanently removes untracked files"),
    (re.compile(r"git\s+checkout\s+--\s+\.", re.I), "git checkout -- . discards all local changes"),
    (re.compile(r"git\s+branch\s+-D\b", re.I), "git branch -D force-deletes a branch"),
    (re.compile(r"git\s+rebase\s+.*--force", re.I), "git rebase --force can rewrite history"),
]

_DANGEROUS_FS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/(?:\s|$)"), "rm -rf / would destroy the filesystem"),
    (re.compile(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/\*"), "rm -rf /* would destroy the filesystem"),
    (re.compile(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+~(?:\s|/)"), "rm -rf ~ would destroy the home directory"),
    (re.compile(r"chmod\s+-R\s+777\s+/"), "chmod -R 777 / makes everything world-writable"),
    (re.compile(r"mkfs\.", re.I), "mkfs commands format partitions"),
    (re.compile(r"dd\s+.*of=/dev/", re.I), "dd to a device can destroy data"),
    (re.compile(r":\(\)\{.*\};\s*:"), "fork bomb detected"),
]

_EXFILTRATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"curl\s+.*\|\s*(?:ba)?sh", re.I), "piping curl output to shell is dangerous"),
    (re.compile(r"wget\s+.*\|\s*(?:ba)?sh", re.I), "piping wget output to shell is dangerous"),
    (re.compile(r"curl\s+.*\|\s*python", re.I), "piping curl to python is dangerous"),
    (re.compile(r"wget\s+.*-O\s*-\s*\|\s*(?:ba)?sh", re.I), "piping wget to shell is dangerous"),
]

_BYPASS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"--no-verify\b"), "skipping git hooks with --no-verify"),
    (re.compile(r"--no-gpg-sign\b"), "skipping GPG signing"),
    (re.compile(r"--skip-hooks\b"), "skipping hooks"),
    (re.compile(r"GIT_AUTHOR_DATE|GIT_COMMITTER_DATE"), "forging git commit dates"),
]

_WARN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sudo\s"), "command uses sudo"),
    (re.compile(r"rm\s+-[a-zA-Z]*r", re.I), "recursive delete"),
    (re.compile(r">\s*/dev/(?:sd|nvme|vd)", re.I), "redirecting to block device"),
    (re.compile(r"pip\s+install\s+.*--break-system", re.I), "--break-system-packages may corrupt system Python"),
]


@dataclass(frozen=True)
class SafetyVerdict:
    """Result of a bash command safety check."""
    blocked: bool = False
    warning: bool = False
    reason: str = ""
    level: str = "safe"


def check_bash_safety(command: str) -> SafetyVerdict:
    """Inspect a shell command for dangerous patterns.

    Returns a SafetyVerdict indicating whether the command should be blocked,
    warned about, or allowed. Blocked commands should not be executed.
    """
    for pattern, reason in _DANGEROUS_FS_PATTERNS:
        if pattern.search(command):
            return SafetyVerdict(blocked=True, reason=reason, level="critical")

    for pattern, reason in _EXFILTRATION_PATTERNS:
        if pattern.search(command):
            return SafetyVerdict(blocked=True, reason=reason, level="critical")

    for pattern, reason in _DANGEROUS_GIT_PATTERNS:
        if pattern.search(command):
            return SafetyVerdict(warning=True, reason=reason, level="high")

    for pattern, reason in _BYPASS_PATTERNS:
        if pattern.search(command):
            return SafetyVerdict(warning=True, reason=reason, level="high")

    for pattern, reason in _WARN_PATTERNS:
        if pattern.search(command):
            return SafetyVerdict(warning=True, reason=reason, level="medium")

    return SafetyVerdict()


# ---------------------------------------------------------------------------
# Secret scanning / redaction
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "API key (sk-*)"),
    (re.compile(r"ghp_[a-zA-Z0-9]{36,}"), "GitHub personal access token"),
    (re.compile(r"gho_[a-zA-Z0-9]{36,}"), "GitHub OAuth token"),
    (re.compile(r"ghs_[a-zA-Z0-9]{36,}"), "GitHub server token"),
    (re.compile(r"github_pat_[a-zA-Z0-9_]{40,}"), "GitHub fine-grained PAT"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key ID"),
    (re.compile(r"glpat-[a-zA-Z0-9\-_]{20,}"), "GitLab personal access token"),
    (re.compile(r"xox[bpsa]-[a-zA-Z0-9\-]{10,}"), "Slack token"),
    (re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]{20,}"), "Bearer token"),
    (re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"), "Private key"),
    (re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}"), "JWT token"),
]

_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "email address"),
    (re.compile(r"\b\d{3}[-.]?\d{2}[-.]?\d{4}\b"), "possible SSN"),
    (re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "phone number"),
]


@dataclass(frozen=True)
class ScanResult:
    """Result of a secret/PII scan."""
    redacted_text: str
    redaction_count: int = 0
    matched_types: tuple[str, ...] = ()


def scan_and_redact_secrets(text: str) -> ScanResult:
    """Scan text for secret patterns and replace matches with [REDACTED]."""
    redacted = text
    count = 0
    matched: list[str] = []
    for pattern, label in _SECRET_PATTERNS:
        matches = pattern.findall(redacted)
        if matches:
            count += len(matches)
            matched.append(label)
            redacted = pattern.sub("[REDACTED]", redacted)
    return ScanResult(redacted_text=redacted, redaction_count=count, matched_types=tuple(matched))


def scan_pii(text: str) -> list[str]:
    """Return list of PII types found in text (does not redact)."""
    found: list[str] = []
    for pattern, label in _PII_PATTERNS:
        if pattern.search(text):
            found.append(label)
    return found
