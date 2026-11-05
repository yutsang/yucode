"""Platform-correct invocation of a user/model-supplied shell command string.

On POSIX, ``subprocess`` with ``shell=True`` hands the string to ``/bin/sh``
verbatim — fine. On Windows, ``shell=True`` runs ``cmd.exe /C <command>``,
and cmd's quote-processing rule then MANGLES any command containing more
than two double-quote characters: it strips the first and last quote, so

    "C:\\Program Files\\python.exe" "C:\\path\\script.py"

becomes the unparseable ``C:\\Program Files\\python.exe" "C:\\path\\script.py``
and fails with empty stdout ("...is not recognized"). Any command quoting an
executable path AND an argument hits this — confirmed on a real Windows 11
box via diagnose_env.py (the same interpreter spawned fine via the list
form, proving it was cmd's parsing, not policy, that broke it).

The documented fix (``cmd /?``) is ``/S``: wrap the entire command in one
extra pair of quotes and pass ``/S /C``, which forces exactly one
deterministic rule — strip the first and last quote, treat everything
between them literally. We build that full line ourselves and run it with
``shell=False`` so neither subprocess nor cmd re-interprets the interior.
"""

from __future__ import annotations

import os


def shell_invocation(command: str) -> tuple[str, bool]:
    """Return ``(popen_args, use_shell)`` for running *command* via the
    platform shell: pass the results as the first argument and ``shell=``
    keyword of ``subprocess.run``/``subprocess.Popen``."""
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return f'"{comspec}" /S /C "{command}"', False
    return command, True
