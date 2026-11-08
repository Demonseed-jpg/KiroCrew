"""The machine id a KAS ``initialize`` carries (``acp/kas_machine_id.py``).

KAS stamps every telemetry record of a process with
``clientCapabilities._meta.kiro.telemetry.machineId`` and records ``""`` when the
handshake carries none. These tests pin that a KAS spawn sends the
installation's own random id, the same on every spawn; that the id is not
computable from the beacon install id the heartbeat collector holds (the engine
sends it beside the person's Kiro sign-in, so a derivable id would join the
two); that each of Kiro Crew's telemetry opt-outs sends none and creates
nothing; that no other backend is sent one; and that the shared
``KAS_CLIENT_CAPABILITIES`` constant never carries it.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat

import pytest

from kiro_crew import beacon, platform_compat
from kiro_crew.acp import kas_secret_storage, runtime_spawn_launch
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO, KAS_CLIENT_CAPABILITIES
from kiro_crew.agent_sdk.tool_search import ToolSearchSettings
from kiro_crew.config.paths import config_dir

_INSTALL_ID = "0123456789abcdef0123456789abcdef"
_HEX64 = re.compile(r"[0-9a-f]{64}")
#: ``kas_machine_id.MACHINE_ID_FILE``, spelled out: the specs name the file.
_ID_FILE = "kas_machine_id"


def _public_hashes(install_id: str) -> set[str]:
    """Ids anyone holding *install_id* could compute from public source."""
    return {
        install_id,
        hashlib.sha256(install_id.encode()).hexdigest(),
        hashlib.sha256(("kirocrew-metrics-machine-id-v1:" + install_id).encode()).hexdigest(),
    }


def _install_policy(monkeypatch, doc) -> None:
    """Install *doc* as the boot-frozen governance ceiling (``None`` = ungoverned)."""
    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    class _Ctx:
        governance = parse_policy(doc) if doc is not None else None

    monkeypatch.setattr(pc, "current_context", lambda: _Ctx())


@pytest.fixture
def home(monkeypatch):
    """An ungoverned, opted-in data home whose spawns keep sign-ins in memory."""
    monkeypatch.delenv(beacon.DISABLE_ENV, raising=False)
    monkeypatch.setattr(kas_secret_storage, "persistence_enabled", lambda: False)
    _install_policy(monkeypatch, None)
    path = config_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_install_id(home, value: str = _INSTALL_ID) -> None:
    (home / beacon.INSTALL_ID_FILE).write_text(value, encoding="utf-8")


def _stored(home) -> str:
    return (home / _ID_FILE).read_text(encoding="utf-8").strip()


async def _handshake(backend: str = ACP_BACKEND_KAS, *, tool_search=None) -> dict:
    runtime = AcpRuntime(acp_backend=backend)
    runtime._tool_search = tool_search
    runtime._projected_spawn_spec = lambda: {"tools": ["tool_search"]}
    return await runtime_spawn_launch._handshake_client_capabilities(runtime)


def _telemetry(caps: dict):
    return caps.get("_meta", {}).get("kiro", {}).get("telemetry")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_search",
    [None, ToolSearchSettings(True, 5, 50_000)],
    ids=["no-tool-search", "tool-search"],
)
async def test_a_kas_handshake_carries_the_installations_machine_id(home, tool_search):
    _write_install_id(home)
    first = await _handshake(tool_search=tool_search)
    second = await _handshake(tool_search=tool_search)

    sent = _telemetry(first)["machineId"]
    assert _HEX64.fullmatch(sent)
    assert sent == _stored(home)
    assert _telemetry(second) == _telemetry(first)
    assert _INSTALL_ID not in json.dumps(first)
    assert sent not in _public_hashes(_INSTALL_ID)
    # The rest of the declaration is the harness's, and the constant stays clean.
    assert first["_meta"]["kiro"]["openExternalUrl"] is True
    assert "telemetry" not in KAS_CLIENT_CAPABILITIES["_meta"]["kiro"]


@pytest.mark.asyncio
async def test_the_machine_id_is_not_computable_from_the_install_id(home, tmp_path, monkeypatch):
    """Two installations sharing one install id get different machine ids, and
    replacing the install id leaves the machine id alone: it is no function of
    the install id, so the heartbeat collector cannot recompute it."""
    _write_install_id(home)
    first = _telemetry(await _handshake())["machineId"]
    _write_install_id(home, "fedcba9876543210fedcba9876543210")
    assert _telemetry(await _handshake())["machineId"] == first

    other = tmp_path / "second-installation"
    other.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(other))
    _write_install_id(config_dir())
    second = _telemetry(await _handshake())["machineId"]

    assert _HEX64.fullmatch(second)
    assert second != first
    assert {first, second}.isdisjoint(_public_hashes(_INSTALL_ID))


@pytest.mark.asyncio
async def test_a_home_with_no_id_gets_one_and_no_install_id(home):
    caps = await _handshake()

    stored = _stored(home)
    assert _HEX64.fullmatch(stored)
    assert _telemetry(caps) == {"machineId": stored}
    assert not (home / beacon.INSTALL_ID_FILE).exists()
    if platform_compat.IS_POSIX:
        mode = (home / _ID_FILE).stat().st_mode
        assert stat.S_IMODE(mode) & 0o077 == 0


@pytest.mark.asyncio
async def test_a_malformed_stored_id_is_replaced(home):
    (home / _ID_FILE).write_text("not-an-id", encoding="utf-8")
    caps = await _handshake()

    assert _HEX64.fullmatch(_stored(home))
    assert _telemetry(caps) == {"machineId": _stored(home)}


def _opt_out_env(monkeypatch, home) -> None:
    monkeypatch.setenv(beacon.DISABLE_ENV, "1")


def _opt_out_toggle(monkeypatch, home) -> None:
    (home / "config.json").write_text(json.dumps({"telemetry": {"beacon_enabled": False}}))


def _opt_out_policy(monkeypatch, home) -> None:
    _install_policy(
        monkeypatch,
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"telemetry": {"enabled": False}},
        },
    )


def _unreadable_config(monkeypatch, home) -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    def _boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))


def _degraded_base(monkeypatch, home) -> None:
    # The load succeeds with the field default (on) in place of whatever the
    # unparseable file says.
    (home / "config.json").write_text("{not json", encoding="utf-8")


def _degraded_overlay(monkeypatch, home) -> None:
    (home / "config.local.json").write_text("{not json", encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opt_out",
    [
        _opt_out_env,
        _opt_out_toggle,
        _opt_out_policy,
        _unreadable_config,
        _degraded_base,
        _degraded_overlay,
    ],
    ids=[
        "env",
        "beacon-toggle-off",
        "governance-pin",
        "config-unreadable",
        "config-json-degraded",
        "overlay-degraded",
    ],
)
async def test_an_opted_out_install_sends_no_machine_id_and_creates_none(
    home, monkeypatch, opt_out
):
    opt_out(monkeypatch, home)
    caps = await _handshake()

    assert _telemetry(caps) is None
    assert not (home / _ID_FILE).exists()
    assert not (home / beacon.INSTALL_ID_FILE).exists()


@pytest.mark.asyncio
async def test_no_other_backend_is_sent_a_machine_id(home):
    _write_install_id(home)
    caps = await _handshake(ACP_BACKEND_KIRO)
    assert _telemetry(caps) is None
    assert not (home / _ID_FILE).exists()
