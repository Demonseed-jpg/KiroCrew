"""Enabling or installing an app brings its MCP servers to the chats already open.

``register_app`` writes the app's ``<app>:<server>`` entries into the agent config, which
a new chat mounts at its start. An open chat needs the step an MCP page write takes
(``dashboard.handlers.mcp.apply_mcp_change_to_open_chats``): where every live session
applies the change itself the re-injecting and relaunching ones are marked and nothing
is reset, and otherwise every session is reset. The enable and install routes take that
step only when the registration wrote an MCP server, and a failure in it never fails
the enable. Owning spec: app-kit-platform §7.4.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _make_app, _setup_env
from dashboard_owner_helpers import owner_claims

from kiro_crew.apps.bridges import RegistrationResult

_ROUTES = "kiro_crew.apps.routes"


class _Provider:
    """A live session's provider, declaring how it applies an MCP config change."""

    def __init__(self, *, reinjects: bool) -> None:
        self.mcp_config_hot_reload = False
        self.mcp_config_wire_reinject = reinjects
        self.mcp_config_relaunch_reload = False
        self.marks = 0

    def mark_mcp_config_changed(self) -> None:
        self.marks += 1


class _Sessions:
    """The session manager surface the live-apply gate reads."""

    def __init__(self, providers: list[_Provider]) -> None:
        self._providers = providers
        self.fenced = 0

    def active_providers(self) -> list[_Provider]:
        return list(self._providers)

    def warm_providers(self) -> list[_Provider]:
        return []

    def mark_mcp_config_changed(self) -> None:
        self.fenced += 1


def _request(sessions: _Sessions, *, body: Any = None) -> MagicMock:
    request = MagicMock()
    request.match_info = {"name": APP}
    state = MagicMock(owner_id="")
    state.sessions = sessions
    request.app = {"state": state}
    request.can_read_body = body is not None
    request.json = AsyncMock(return_value=body)
    return owner_claims(request)


def _registration(*servers: str) -> RegistrationResult:
    return RegistrationResult(mcp_servers=list(servers))


def _patch_all(stack: ExitStack, **targets: Any) -> None:
    """Patch each ``kiro_crew.apps.routes.<name>`` with its value; an async step gets an AsyncMock."""
    for name, value in targets.items():
        if isinstance(value, AsyncMock):
            stack.enter_context(patch(f"{_ROUTES}.{name}", value))
        else:
            stack.enter_context(patch(f"{_ROUTES}.{name}", return_value=value))


@pytest.fixture()
def reset_all() -> Any:
    with patch(
        "kiro_crew.dashboard.handlers.sessions._reset_all_sessions",
        new_callable=AsyncMock,
        return_value=1,
    ) as reset:
        yield reset


async def _enable(sessions: _Sessions, reg: RegistrationResult) -> dict[str, Any]:
    from kiro_crew.apps.routes import handle_enable_app

    with ExitStack() as stack:
        _patch_all(
            stack,
            get_app={"name": APP, "manifest": {}, "resources": "gateway", "enabled": False},
            app_enabled_state=False,
            enable_app=MagicMock(ok=True, to_dict=lambda: {"ok": True, "name": APP}),
            register_app=reg,
            start_app_backend=None,
            _backend_platform_refusal=AsyncMock(return_value=None),
            on_app_enable=AsyncMock(return_value=None),
            sel=MagicMock(),
        )
        response = await handle_enable_app(_request(sessions))
    assert response.status == 200
    return json.loads(response.body)


async def _install(sessions: _Sessions, reg: RegistrationResult, src: Path) -> dict[str, Any]:
    from kiro_crew.apps.routes import handle_install_app

    installed = MagicMock(ok=True, to_dict=lambda: {"ok": True, "name": APP})
    installed.name = APP
    with ExitStack() as stack:
        _patch_all(
            stack,
            _refuse_while_startup_hook_runs=AsyncMock(return_value=None),
            install_app=installed,
            _app_may_run_after_install=AsyncMock(return_value=True),
            register_app=reg,
            _start_backend_after_install=AsyncMock(return_value=None),
            sel=MagicMock(),
        )
        response = await handle_install_app(_request(sessions, body={"source": str(src)}))
    assert response.status == 201
    return json.loads(response.body)


def _app_source(tmp_path: Path) -> Path:
    src = tmp_path / "source" / APP
    src.mkdir(parents=True)
    (src / "app.json").write_text(json.dumps({"name": APP}), encoding="utf-8")
    return src


def _done_payload(text: str) -> dict[str, Any]:
    frames = [f for f in text.split("\n\n") if f.startswith("event: done")]
    assert frames, text
    data = [line[len("data: ") :] for line in frames[-1].split("\n") if line.startswith("data: ")]
    return json.loads("\n".join(data))


async def _registry_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sessions: _Sessions,
    reg: RegistrationResult,
    *,
    stream: bool,
) -> dict[str, Any]:
    _setup_env(tmp_path, monkeypatch)
    app = _make_app(dashboard_user="owner")
    app["state"].sessions = sessions
    with ExitStack() as stack:
        _patch_all(
            stack,
            get_app=None,
            install_from_registry=AsyncMock(return_value={"ok": True, "name": APP}),
            _app_may_run_after_install=AsyncMock(return_value=True),
            register_app=reg,
            _start_backend_after_install=AsyncMock(return_value=None),
        )
        async with TestClient(TestServer(app)) as client:
            path = "/api/apps/registry/install-stream" if stream else "/api/apps/registry/install"
            resp = await client.post(path, json={"name": APP})
            if stream:
                assert resp.status == 200
                return _done_payload(await resp.text())
            assert resp.status == 201
            return await resp.json()


class TestEnableReachesOpenChats:
    @pytest.mark.asyncio
    async def test_a_session_that_applies_live_is_marked_and_not_reset(
        self, reset_all: AsyncMock
    ) -> None:
        provider = _Provider(reinjects=True)
        sessions = _Sessions([provider])

        body = await _enable(sessions, _registration(f"{APP}:srv"))

        assert provider.marks == 1
        assert sessions.fenced == 1
        reset_all.assert_not_awaited()
        assert body["sessions_reset"] == 0

    @pytest.mark.asyncio
    async def test_a_session_that_cannot_apply_live_is_reset(self, reset_all: AsyncMock) -> None:
        provider = _Provider(reinjects=False)

        body = await _enable(_Sessions([provider]), _registration(f"{APP}:srv"))

        reset_all.assert_awaited_once()
        assert provider.marks == 0
        assert body["sessions_reset"] == 1

    @pytest.mark.asyncio
    async def test_an_app_with_no_mcp_server_leaves_open_chats_alone(
        self, reset_all: AsyncMock
    ) -> None:
        provider = _Provider(reinjects=True)
        sessions = _Sessions([provider, _Provider(reinjects=False)])

        body = await _enable(sessions, _registration())

        assert provider.marks == 0
        assert sessions.fenced == 0
        reset_all.assert_not_awaited()
        assert "sessions_reset" not in body

    @pytest.mark.asyncio
    async def test_a_failed_reset_never_fails_the_enable(self, reset_all: AsyncMock) -> None:
        reset_all.side_effect = RuntimeError("drain failed")

        body = await _enable(_Sessions([_Provider(reinjects=False)]), _registration(f"{APP}:srv"))

        reset_all.assert_awaited_once()
        assert body["ok"] is True
        assert "sessions_reset" not in body


class TestInstallReachesOpenChats:
    @pytest.mark.asyncio
    async def test_a_session_that_applies_live_is_marked_and_not_reset(
        self, tmp_path: Path, reset_all: AsyncMock
    ) -> None:
        provider = _Provider(reinjects=True)

        body = await _install(
            _Sessions([provider]), _registration(f"{APP}:srv"), _app_source(tmp_path)
        )

        assert provider.marks == 1
        reset_all.assert_not_awaited()
        assert body["sessions_reset"] == 0

    @pytest.mark.asyncio
    async def test_a_session_that_cannot_apply_live_is_reset(
        self, tmp_path: Path, reset_all: AsyncMock
    ) -> None:
        body = await _install(
            _Sessions([_Provider(reinjects=False)]),
            _registration(f"{APP}:srv"),
            _app_source(tmp_path),
        )

        reset_all.assert_awaited_once()
        assert body["sessions_reset"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True], ids=["registry", "registry-stream"])
    async def test_a_registry_install_resets_a_session_that_cannot_apply_live(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reset_all: AsyncMock,
        stream: bool,
    ) -> None:
        body = await _registry_install(
            tmp_path,
            monkeypatch,
            _Sessions([_Provider(reinjects=False)]),
            _registration(f"{APP}:srv"),
            stream=stream,
        )

        reset_all.assert_awaited_once()
        assert body["sessions_reset"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True], ids=["registry", "registry-stream"])
    async def test_a_registry_install_marks_a_session_that_applies_live(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reset_all: AsyncMock,
        stream: bool,
    ) -> None:
        provider = _Provider(reinjects=True)

        body = await _registry_install(
            tmp_path,
            monkeypatch,
            _Sessions([provider]),
            _registration(f"{APP}:srv"),
            stream=stream,
        )

        assert provider.marks == 1
        reset_all.assert_not_awaited()
        assert body["sessions_reset"] == 0
