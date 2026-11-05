"""Withhold credential-looking environment variables from child processes.

Threat model is indirect prompt injection: a malicious web page or file pulled
into context talks the model into running something that reads the ambient
environment (`env`, `set`, a `curl` that forwards it), and the provider
credential walks out with the tool result. The agent spawns children from four
places -- the bash tool, hook commands, plugin tools, and stdio MCP servers --
and every one of them used to inherit the full parent environment, so filtering
only the first would leave the shortest path open.

Names only are ever logged, never values.

The three `runtime.shell_env_*` settings reach all four spawn points through
the process-wide default policy below (see `set_default_policy`), because only
the bash tool has a RuntimeOptions in hand at the point it spawns.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("yucode.security.env")

# Variables whose *name* matches any of these globs are withheld.
# YUCODE_API_KEY (config/settings.py::_API_KEY_ENV_VARS) and BRAVE_API_KEY
# (tools/web.py) are this runtime's own secrets; both already match *KEY*, and
# YUCODE_API_KEY is spelled out so the link back is greppable.
SECRET_ENV_PATTERNS: tuple[str, ...] = (
    "*KEY*", "*SECRET*", "*TOKEN*", "*PASSWORD*", "*PASSWD*", "*CREDENTIAL*",
    "YUCODE_API_KEY",
    # Anchored on the underscore on purpose. A blanket "*PWD*" would also match
    # PWD and OLDPWD -- the current and previous working directory, not secrets,
    # and withholding them breaks anything that resolves a relative path.
    # Neither has an underscore, so "*_PWD" catches MYSQL_PWD (the mysql
    # client's password variable) and DB_PWD while leaving both alone.
    "*_PWD",
    # AUTHORIZATION, SSH_AUTH_SOCK, DOCKER_AUTH_CONFIG, NPM_CONFIG__AUTH: auth
    # material that carries no KEY/TOKEN/SECRET in its name. This also matches
    # the AUTHOR family, which is why GIT_AUTHOR_* is named exactly in
    # PRESERVED_ENV_PATTERNS below.
    #
    # SSH_AUTH_SOCK is the deliberate behaviour change here: it is only a
    # socket path, but handing it to a child hands over the ability to
    # authenticate as the user with every key the agent holds -- exactly the
    # capability an injected `git push` to an attacker remote would want. The
    # cost is that git-over-SSH stops working in agent-spawned children unless
    # the site puts it in runtime.shell_env_allowlist.
    "*AUTH*",
    # Connection strings embed the password inline (postgres://user:pw@host/db)
    # and nothing else in the name gives it away. Not "*URL*", which would take
    # out PIP_INDEX_URL and most of the proxy configuration.
    "*DATABASE_URL*",
)

# Kept even when a pattern above (or a user-added one) matches. Locked-down
# corporate boxes drive pip/git/curl through an enterprise proxy with a
# re-signing CA, and those settings arrive purely as environment variables --
# dropping them breaks tooling on site with nobody around to debug it.
# Deliberately *not* blanket "PIP_*"/"GIT_*": every PIP_/GIT_ variable that
# doesn't look like a credential is already passed through untouched (this is a
# denylist), so a blanket entry would only re-admit things like GIT_TOKEN --
# exactly what the filter exists to stop. Anything genuinely needed and
# genuinely secret-looking goes in runtime.shell_env_allowlist.
PRESERVED_ENV_PATTERNS: tuple[str, ...] = (
    # Proxy
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY",
    # TLS trust stores / client certs
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS", "AWS_CA_BUNDLE", "CONDA_SSL_VERIFY",
    "NPM_CONFIG_CAFILE", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
    "PIP_CERT", "PIP_CLIENT_CERT",
    # Commit identity, caught by "*AUTH*" via AUTHOR. Dropping these does not
    # fail loudly -- git just falls back to whatever the child's config says,
    # and the commits come out attributed to the wrong person. Exact names and
    # never a "*AUTHOR*" glob: a preserve entry beats *every* pattern, so the
    # glob would re-admit something like GIT_AUTHOR_API_KEY.
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
)


@dataclass(frozen=True)
class EnvScrubPolicy:
    """The three `runtime.shell_env_*` settings, detached from the config
    package so `security` keeps depending on nothing."""

    enabled: bool = True
    extra_patterns: tuple[str, ...] = ()
    allowlist: tuple[str, ...] = ()


def policy_from_runtime(runtime: Any) -> EnvScrubPolicy:
    """Build a policy from a `config.RuntimeOptions` (duck-typed to avoid the
    config -> security import cycle)."""
    return EnvScrubPolicy(
        enabled=bool(runtime.shell_env_filter_secrets),
        extra_patterns=tuple(runtime.shell_env_exclude_patterns),
        allowlist=tuple(runtime.shell_env_allowlist),
    )


_default_policy = EnvScrubPolicy()


def default_policy() -> EnvScrubPolicy:
    return _default_policy


def set_default_policy(policy: EnvScrubPolicy) -> EnvScrubPolicy:
    """Install the policy every spawn point uses unless told otherwise;
    returns the previous one so a caller (or a test) can put it back.

    Process-wide mutable state, chosen with eyes open. The alternative was
    threading RuntimeOptions through HookRunner, PluginManager/PluginRegistry/
    PluginTool and McpManager/StdioMcpClient -- five constructors, three of
    which exist only to reach a spawn a few frames later. What made the call is
    that the failure this fixes was silent: a user adds ARTIFACTORY_API_KEY to
    runtime.shell_env_allowlist, bash starts working, hooks and MCP servers
    keep failing, and nothing says why. A default that every path shares cannot
    drift apart again, whereas a fifth constructor added later can simply
    forget to pass it along. The cost is that this must be installed once
    during startup, and that tests have to restore it."""
    global _default_policy
    previous = _default_policy
    _default_policy = policy
    return previous


def env_name_matches(name: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive glob match against an environment variable *name*.

    Case-insensitive because Windows environment variable names are, and
    because a POSIX box may well spell it `api_key`.
    """
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in patterns)


def scrub_secrets(
    env: Mapping[str, str],
    *,
    enabled: bool | None = None,
    extra_patterns: Iterable[str] | None = None,
    allowlist: Iterable[str] | None = None,
    context: str = "child process",
) -> dict[str, str]:
    """Return a copy of *env* with credential-looking variables removed.

    `allowlist` (and the built-in preserved list) wins over any pattern, so a
    site that genuinely needs a secret-looking variable can put it back without
    disabling the filter wholesale.

    Each of the three knobs falls back to the process-wide default policy when
    left unset, so a caller with no RuntimeOptions in hand still gets the
    user's settings. An explicit argument wins, which is how the bash tool
    keeps passing its runtime straight through.
    """
    policy = _default_policy
    if enabled is None:
        enabled = policy.enabled
    if extra_patterns is None:
        extra_patterns = policy.extra_patterns
    if allowlist is None:
        allowlist = policy.allowlist
    if not enabled:
        return dict(env)
    patterns = (*SECRET_ENV_PATTERNS, *extra_patterns)
    preserved = (*PRESERVED_ENV_PATTERNS, *allowlist)
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for name, value in env.items():
        if env_name_matches(name, patterns) and not env_name_matches(name, preserved):
            dropped.append(name)
            continue
        kept[name] = value
    if dropped:
        # Names only, never values -- this line is what makes "why did pip stop
        # working on the locked-down box?" diagnosable in the field.
        _log.debug(
            "%s: withheld %d credential-looking env var(s): %s "
            "(add to runtime.shell_env_allowlist if one is needed)",
            context, len(dropped), ", ".join(sorted(dropped)),
        )
    return kept
