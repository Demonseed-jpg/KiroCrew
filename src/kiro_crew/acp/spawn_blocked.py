"""The session-start refusal for a Windows host whose policy will not run the agent binary.

AppLocker, an EDR product or group policy can stop the agent executable from
starting. ``CreateProcess`` then fails with ``ERROR_ACCESS_DENIED`` (5) or
``ERROR_ACCESS_DISABLED_BY_POLICY`` (1260), and a respawn hits the same wall.
The spawn still makes main's one retry first, since a self-update window can
also answer access denied; a refusal that survives it is raised as
:class:`AcpSpawnBlocked`. Only an administrator can lift it, so the chat says
that once, naming the file, instead of a generic transport error.
"""

from __future__ import annotations

import os
import re

from kiro_crew.acp.session_handle_runtime_contract import AcpRuntimeError

#: ``winerror`` values a host policy answers a blocked spawn with.
_BLOCKED_WINERRORS = frozenset({5, 1260})

#: Fallback for a Windows ``OSError`` that carries no ``winerror``: the system's
#: own wording for the same two refusals. Never read when a code is present.
_RE_BLOCKED_TEXT = re.compile(r"access is denied|blocked by group policy", re.IGNORECASE)


def is_windows_spawn_blocked(exc: BaseException) -> bool:
    """True when *exc* is a Windows spawn refusal a host policy gave.

    The ``winerror`` code decides when it is present: 5 and 1260 match, any
    other code does not. Only a Windows ``OSError`` with no code falls back to
    the message text.
    """
    if not isinstance(exc, OSError):
        return False
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _BLOCKED_WINERRORS
    return os.name == "nt" and bool(_RE_BLOCKED_TEXT.search(str(exc)))


def spawn_blocked_message(binary: str) -> str:
    """The person-facing sentence for a blocked spawn of *binary*."""
    target = f"the agent program ({binary})" if binary else "the agent program"
    return (
        f"Windows would not start {target}. AppLocker, an EDR product or group "
        "policy on this computer likely blocked it. Ask your IT administrator to "
        "allow that file, then start a new chat."
    )


class AcpSpawnBlocked(AcpRuntimeError):
    """Windows refused to start the agent binary, so no process exists.

    Not ``transient``: a respawn meets the same policy. ``windows_spawn_blocked``
    is the structural tag the chat reads to word the card in the reader's
    language, and ``binary`` is the path it names.
    """

    transient = False
    windows_spawn_blocked = True

    def __init__(self, binary: str) -> None:
        super().__init__(spawn_blocked_message(binary))
        self.binary = binary
