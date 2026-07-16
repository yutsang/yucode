"""Semantic bash-command analysis: intent classification and extra safety checks.

Complements ``safety.py`` (pattern-based blocks/warns) with checks that
claw-code's ``bash_validation.rs`` performs but yucode previously lacked:

  - ``classify_command`` — tag a command with a :class:`CommandIntent`
  - ``extract_first_command`` — strip ``KEY=val`` env prefixes and ``sudo`` flags
  - ``check_sed_in_place`` — warn on ``sed -i`` (silent in-place edit)
  - ``check_path_traversal`` — warn on ``../``, ``~/``, ``$HOME`` and system paths
"""

from __future__ import annotations

from enum import Enum

from .safety import SafetyVerdict


class CommandIntent(str, Enum):
    READ_ONLY = "read-only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    NETWORK = "network"
    PROCESS = "process"
    PACKAGE = "package"
    SYSTEM_ADMIN = "system-admin"
    UNKNOWN = "unknown"


_READ_ONLY_COMMANDS = frozenset([
    "ls", "cat", "head", "tail", "less", "more", "wc", "sort", "uniq",
    "grep", "egrep", "fgrep", "rg", "find", "which", "whereis", "whatis",
    "man", "info", "file", "stat", "du", "df", "free", "uptime", "uname",
    "hostname", "whoami", "id", "groups", "env", "printenv", "echo",
    "printf", "date", "cal", "bc", "expr", "test", "true", "false", "pwd",
    "tree", "diff", "cmp", "md5sum", "sha256sum", "sha1sum", "xxd", "od",
    "hexdump", "strings", "readlink", "realpath", "basename", "dirname",
    "seq", "yes", "tput", "column", "jq", "yq", "xargs", "tr", "cut",
    "paste", "awk", "sed",
])

_WRITE_COMMANDS = frozenset([
    "cp", "mv", "mkdir", "rmdir", "touch", "chmod", "chown", "chgrp",
    "ln", "install", "tee", "truncate", "mkfifo", "mknod",
])

_DESTRUCTIVE_COMMANDS = frozenset(["rm", "shred", "wipefs", "dd"])

_NETWORK_COMMANDS = frozenset([
    "curl", "wget", "ssh", "scp", "rsync", "ftp", "sftp", "nc", "ncat",
    "telnet", "ping", "traceroute", "dig", "nslookup", "host", "whois",
    "netstat", "ss", "nmap",
])

_PROCESS_COMMANDS = frozenset([
    "kill", "pkill", "killall", "ps", "top", "htop", "bg", "fg", "jobs",
    "nohup", "disown", "wait", "nice", "renice",
])

_PACKAGE_COMMANDS = frozenset([
    "apt", "apt-get", "yum", "dnf", "pacman", "brew", "pip", "pip3",
    "npm", "yarn", "pnpm", "bun", "cargo", "gem", "rustup", "snap",
    "flatpak",
])

_SYSTEM_ADMIN_COMMANDS = frozenset([
    "sudo", "su", "chroot", "mount", "umount", "fdisk", "parted",
    "systemctl", "service", "journalctl", "dmesg", "modprobe", "insmod",
    "rmmod", "iptables", "ufw", "firewall-cmd", "sysctl", "crontab", "at",
    "useradd", "userdel", "usermod", "groupadd", "groupdel", "passwd",
    "visudo", "reboot", "shutdown", "halt", "poweroff",
])

_GIT_READ_ONLY_SUBCOMMANDS = frozenset([
    "status", "log", "diff", "show", "branch", "tag", "remote", "fetch",
    "ls-files", "ls-tree", "cat-file", "rev-parse", "describe",
    "shortlog", "blame", "bisect", "reflog", "config",
])

_SYSTEM_PATHS = ("/etc/", "/usr/", "/var/", "/boot/", "/sys/", "/proc/",
                 "/dev/", "/sbin/", "/lib/", "/opt/")


def extract_first_command(command: str) -> str:
    """Return the first real command token, skipping ``KEY=val`` env prefixes.

    Examples:
        ``FOO=bar ls``           -> ``ls``
        ``A=1 B=2 rm -rf /tmp``  -> ``rm``
        ``sudo -n systemctl``    -> ``sudo`` (caller handles sudo unwrap)
    """
    tokens = command.strip().split()
    for tok in tokens:
        if "=" in tok:
            name, _, _ = tok.partition("=")
            if name and all(c.isalnum() or c == "_" for c in name):
                continue
        return tok
    return ""


def classify_command(command: str) -> CommandIntent:
    """Classify a shell command into a :class:`CommandIntent`."""
    first = extract_first_command(command)
    if not first:
        return CommandIntent.UNKNOWN
    if first == "sed" and " -i" in command:
        return CommandIntent.WRITE
    if first in _READ_ONLY_COMMANDS:
        return CommandIntent.READ_ONLY
    if first in _DESTRUCTIVE_COMMANDS:
        return CommandIntent.DESTRUCTIVE
    if first in _WRITE_COMMANDS:
        return CommandIntent.WRITE
    if first in _NETWORK_COMMANDS:
        return CommandIntent.NETWORK
    if first in _PROCESS_COMMANDS:
        return CommandIntent.PROCESS
    if first in _PACKAGE_COMMANDS:
        return CommandIntent.PACKAGE
    if first in _SYSTEM_ADMIN_COMMANDS:
        return CommandIntent.SYSTEM_ADMIN
    if first == "git":
        tokens = command.split()
        sub = next((t for t in tokens[1:] if not t.startswith("-")), None)
        if sub and sub in _GIT_READ_ONLY_SUBCOMMANDS:
            return CommandIntent.READ_ONLY
        return CommandIntent.WRITE
    return CommandIntent.UNKNOWN


def check_sed_in_place(command: str) -> SafetyVerdict | None:
    """Warn on ``sed -i`` — silently mutates files without review."""
    if extract_first_command(command) != "sed":
        return None
    if " -i" not in command and not command.strip().endswith("-i"):
        return None
    return SafetyVerdict(
        warning=True,
        reason="sed -i edits files in place without confirmation",
        level="medium",
    )


def check_path_traversal(command: str) -> SafetyVerdict | None:
    """Warn on directory traversal or system-path targeting.

    Only warns; does not block. The caller decides whether to escalate.
    """
    intent = classify_command(command)
    if intent in (CommandIntent.WRITE, CommandIntent.DESTRUCTIVE):
        for sys_path in _SYSTEM_PATHS:
            if sys_path in command:
                return SafetyVerdict(
                    warning=True,
                    reason=f"write/destructive command targets system path `{sys_path}`",
                    level="high",
                )
    if "../" in command:
        return SafetyVerdict(
            warning=True,
            reason="command contains `../` directory traversal",
            level="medium",
        )
    return None
