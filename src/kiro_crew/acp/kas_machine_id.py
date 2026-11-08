"""The machine id Kiro Crew gives the KAS engine in ``initialize``.

KAS reads ``clientCapabilities._meta.kiro.telemetry`` once per process, takes it
only when the object carries a ``machineId`` key, and stamps that value on every
telemetry record the process writes. Told none, it records ``machineId: ""``, so
every Kiro Crew session on every machine looks alike to the service, and a burst
of parallel sessions from one machine cannot be told apart from many people's
traffic.

The id is 64 random hex characters generated for this purpose alone and kept
owner-only as :data:`MACHINE_ID_FILE` in the data home. It is derived from
nothing: not the beacon install id, the hostname, the user name or the hardware.
Deliberately NOT a hash of the install id: the engine sends this id beside the
person's Kiro sign-in, and the heartbeat collector holds the install id from
every heartbeat, so any id computable from it (a public prefix hides nothing)
would join an account's telemetry to the anonymous heartbeat row. Kiro Crew
sends it to nothing but the KAS engines it starts. Like the beacon's files it
stays on the host: export and snapshots copy fixed file lists that do not name
it, so a restored copy on a second machine generates its own.

It is sent only while Kiro Crew's own telemetry opt-outs allow it. Any one of
``KIROCREW_TELEMETRY_DISABLED``, a governance policy pinning
``capabilities.telemetry`` off, or ``telemetry.beacon_enabled`` set false
(``kirocrew telemetry disable``) sends none, and so does a config read that
failed or came back degraded, because the field default would then stand in for
a stored ``false``. The engine reads the id once, so an opt-out reaches the KAS
processes started after it. The beacon's other suppressions (CI, a non-default
data home, the first-run disclosure) are not applied: they keep the daily count
of installations honest, and this id counts nothing. It rides traffic the person
started, on an engine they signed in to.

When the opt-outs allow it and the installation has no id yet, one is created.
A data home that cannot hold one sends none.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import os
import re
import secrets
import tempfile
from typing import Any

from kiro_crew import beacon, platform_compat

logger = logging.getLogger(__name__)

__all__ = ["MACHINE_ID_FILE", "machine_id", "stored_machine_id", "with_machine_id"]

#: The data-home file holding the id; read and written only by this module.
MACHINE_ID_FILE = "kas_machine_id"
_ID_RE = re.compile(r"[0-9a-f]{64}")


def stored_machine_id(*, create: bool = True) -> str:
    """This installation's KAS machine id, generated on first use, or ``""``.

    The same atomic create as ``install_receipt.receipt_secret``: an owner-only
    temp file linked into place, so two spawns racing the first id converge on
    one. A malformed file is replaced. With ``create=False`` the file is only
    read. Every failure returns ``""``.
    """
    try:
        path = beacon.config_dir() / MACHINE_ID_FILE
        if path.exists():
            existing = beacon._read_state(path)
            if _ID_RE.fullmatch(existing):
                return existing
            path.unlink(missing_ok=True)
        if not create:
            return ""
        fresh = secrets.token_hex(32)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent))
        try:
            os.write(tmp_fd, fresh.encode("ascii"))
            os.close(tmp_fd)
            tmp_fd = -1
            with contextlib.suppress(OSError):
                platform_compat.restrict_to_owner(tmp_path)
            os.link(tmp_path, str(path))
            return fresh
        except FileExistsError:
            # Lost the race: adopt the winner's id.
            existing = beacon._read_state(path)
            return existing if _ID_RE.fullmatch(existing) else ""
        finally:
            if tmp_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(tmp_fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
    except (OSError, RuntimeError, KeyError) as exc:
        logger.debug("KAS machine id unavailable (%s)", type(exc).__name__)
        return ""


def _heartbeat_toggle_on() -> bool:
    """``telemetry.beacon_enabled`` as stored, read fail-closed.

    On by default, so a load that raised or came back degraded counts as off:
    either hands back the field default in place of a ``false`` the owner may
    have written.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

        cfg = KiroCrewConfig.load()
    except Exception as exc:  # noqa: BLE001
        logger.debug("KAS machine id withheld: config unreadable (%s)", type(exc).__name__)
        return False
    degraded = cfg.degraded_sections
    if (
        cfg._base_unreadable
        or cfg._overlay_unreadable
        or "telemetry" in degraded
        or DEGRADED_WHOLE_CONFIG in degraded
    ):
        logger.debug("KAS machine id withheld: the config read was degraded")
        return False
    return cfg.telemetry.beacon_enabled is True


def machine_id() -> str:
    """This installation's KAS machine id, or ``""`` when none may be sent.

    Blocking (a config load, the governance probe and a small file read); call
    it off the event loop. Never raises: a missing machine id must not fail a
    spawn. None of the opt-outs reads or creates the id.
    """
    if beacon.is_env_opted_out():
        return ""
    # The read-only probe: a refusal only withholds an optional field, and the
    # audited one would write a SEL row for every KAS process started.
    if beacon.is_governance_pinned_off():
        return ""
    if not _heartbeat_toggle_on():
        return ""
    return stored_machine_id(create=True)


def with_machine_id(capabilities: dict[str, Any], machine_id: str) -> dict[str, Any]:
    """*capabilities* with ``_meta.kiro.telemetry.machineId`` set to *machine_id*.

    A copy, never the argument: the harness hands out a module constant, and
    mutating it would put one process's id on every later spawn. An empty
    *machine_id* returns *capabilities* itself, declaring nothing, because KAS
    takes a ``telemetry`` object only when it names a ``machineId``.
    """
    if not machine_id:
        return capabilities
    caps = copy.deepcopy(capabilities)
    caps.setdefault("_meta", {}).setdefault("kiro", {})["telemetry"] = {"machineId": machine_id}
    return caps
