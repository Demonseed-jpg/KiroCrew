"""An agent spec's own hooks on cron and channel turns, on a backend that never runs them.

kiro-cli runs a spec's ``hooks`` on every session. On KAS Crew fires them in its
place: a dashboard chat, a sub-agent and the task runner already did, and cron and
channel turns now do too, through :mod:`kiro_crew.messaging.script_hooks`:
``PreToolUse`` on each permission request (first, refusing only), ``AgentSpawn``
and ``UserPromptSubmit`` before the prompt, ``Stop`` once the turn lands. A spec
hook that crashed lets the call run, as on kiro-cli and in a dashboard chat. The
spec keys nothing carries to KAS reach these turns' notices too (P1-9).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import kiro_crew.hooks as hooks_mod
from kiro_crew import llm_helpers
from kiro_crew.acp.types import AcpEvent
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    ScriptHookResult,
)
from kiro_crew.messaging import APPROVAL_AUTO, TransportCapabilities, TurnDriver, script_hooks
from kiro_crew.messaging.renderer import SilentRenderer
from kiro_crew.messaging.script_hooks import ScriptHookTurn
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST


def _hook(event: str, command: str, matcher: str = "") -> ScriptHook:
    return ScriptHook(
        id=f"spec:a1:{event}:0",
        name=command,
        event=event,
        matcher=matcher,
        command=command,
        spec_origin="user",
    )


class _Provider:
    def __init__(self) -> None:
        self.approved: list = []
        self.rejected: list = []
        self.session_id = "acp-1"
        self.cwd = "/w"
        self.capabilities = capabilities_for(ACP_BACKEND_KAS)

    async def stream(self, message, **_kw):
        yield AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", title="Running: ls")
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, request_id, **_kw):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)


@pytest.fixture
def ran(monkeypatch) -> dict:
    """Every hook command that ran, and the exit code each one gives."""
    record: dict = {"commands": [], "exit": {}}

    async def fake_run(hook, context="", hook_event=None, cwd=None, **_kw):
        record["commands"].append((hook.event, hook.command, cwd))
        stdout = f"from {hook.command}" if hook.event != HOOK_EVENT_PRE_TOOL_USE else ""
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            exit_code=record["exit"].get(hook.command, 0),
            stdout=stdout,
        )

    # The store's methods run on the facade's globals (hook_runtime/__init__).
    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    # No Hooks-page store in this process: only the spec's own hooks may run.
    monkeypatch.setattr(script_hooks, "get_global_hook_store", lambda: None)
    return record


def _turn(provider, hooks, *, store_hooks=True) -> ScriptHookTurn:
    turn = ScriptHookTurn("slack:C1:1", "hello", store_hooks=store_hooks)
    turn.spec = spec_hooks.TurnSpecHooks(hooks, "/w", False, True)
    turn.provider = provider
    turn.agent = "a1"
    return turn


# ── P0-11: PreToolUse on a channel turn (TurnDriver) ─────────────────────────


@pytest.mark.parametrize(("exit_code", "blocked"), [(2, True), (0, False), (-1, False)])
def test_a_channel_turn_gates_its_permission_requests_on_the_specs_pre_tool_hooks(
    ran, exit_code, blocked
):
    ran["exit"]["guard.sh"] = exit_code
    provider = _Provider()
    turn = _turn(provider, [_hook(HOOK_EVENT_PRE_TOOL_USE, "guard.sh", "*")])
    driver = TurnDriver(
        provider,
        SilentRenderer(TransportCapabilities()),
        approval_mode=APPROVAL_AUTO,
        script_hooks=turn,
    )
    asyncio.run(driver.run("hello"))
    assert (HOOK_EVENT_PRE_TOOL_USE, "guard.sh", "/w") in ran["commands"]
    # Exit 2 blocks, as under kiro-cli; a hook that crashed lets the call run.
    assert (provider.rejected, provider.approved) == ((["rq1"], []) if blocked else ([], ["rq1"]))


def test_an_unreadable_spec_blocks_a_channel_turns_tools_as_in_a_dashboard_chat(ran):
    provider = _Provider()
    turn = _turn(provider, [])
    turn.spec = spec_hooks.TurnSpecHooks([], "/w", True, True)
    driver = TurnDriver(
        provider,
        SilentRenderer(TransportCapabilities()),
        approval_mode=APPROVAL_AUTO,
        script_hooks=turn,
    )
    asyncio.run(driver.run("hello"))
    assert provider.rejected == ["rq1"] and provider.approved == []


# ── P0-11: PreToolUse on a cron turn (stream_and_collect) ────────────────────


def test_a_cron_turn_gates_its_permission_requests_on_the_specs_pre_tool_hooks(ran):
    ran["exit"]["guard.sh"] = 2
    provider = _Provider()
    turn = _turn(provider, [_hook(HOOK_EVENT_PRE_TOOL_USE, "guard.sh", "*")], store_hooks=False)
    gate: list = []
    asyncio.run(
        llm_helpers.stream_and_collect(
            provider,
            "hello",
            approval_policy=llm_helpers.ToolApprovalPolicy.AUTO_APPROVE,
            on_tool_gate=lambda title, ok, security, why: gate.append((ok, security)),
            spec_hooks=turn,
        )
    )
    assert provider.rejected == ["rq1"] and provider.approved == []
    # A refusal that says nothing about the job: it does not count toward auto-pause.
    assert gate == [(False, False)]


# ── P0-11: AgentSpawn, UserPromptSubmit and Stop ─────────────────────────────


def test_the_specs_prompt_and_stop_hooks_fire_without_the_hooks_page(ran):
    provider = _Provider()
    turn = _turn(
        provider,
        [
            _hook(HOOK_EVENT_AGENT_SPAWN, "spawn.sh"),
            _hook(HOOK_EVENT_USER_PROMPT_SUBMIT, "submit.sh"),
            _hook(HOOK_EVENT_STOP, "stop.sh"),
        ],
        store_hooks=False,
    )
    turn.fresh_session = True
    injected = asyncio.run(script_hooks.fire_prompt_submit(turn))
    asyncio.run(script_hooks.fire_stop(turn, "done"))
    assert injected == ["from spawn.sh", "from submit.sh"]
    assert [c[:2] for c in ran["commands"]] == [
        (HOOK_EVENT_AGENT_SPAWN, "spawn.sh"),
        (HOOK_EVENT_USER_PROMPT_SUBMIT, "submit.sh"),
        (HOOK_EVENT_STOP, "stop.sh"),
    ]


def test_a_kas_session_attaches_its_agents_spec_hooks(monkeypatch):
    seen: list = []

    async def fake_turn_spec_hooks(provider, agent):
        seen.append(agent)
        return spec_hooks.TurnSpecHooks([], "/w", False, True, ("permissions",))

    monkeypatch.setattr(spec_hooks, "turn_spec_hooks", fake_turn_spec_hooks)
    provider = SimpleNamespace(kas_projected_agent="")
    turn = ScriptHookTurn("cron:j1", "go", store_hooks=False)
    asyncio.run(script_hooks.attach_spec_hooks(turn, provider, "a1", fresh_session=True))
    assert seen == ["a1"] and turn.provider is provider and turn.fresh_session
    # P1-9: the keys the session lacks read as the dashboard's notice.
    assert script_hooks.spec_lost_notice(turn) == spec_hooks.spec_keys_notice("a1", ["permissions"])


# ── P1-9: turn_spec_hooks keeps the keys nothing carries ─────────────────────


def test_turn_spec_hooks_returns_the_keys_nothing_carries(monkeypatch):
    seen: dict = {}

    def fake_fired(agent, project_dir=None, **kw):
        seen.update(kw)
        return [], ["toolsSettings"], 0

    monkeypatch.setattr(spec_hooks, "crew_fired_spec_hooks", fake_fired)
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd="/w")
    out = asyncio.run(spec_hooks.turn_spec_hooks(provider, "a1"))
    assert out.lost == ("toolsSettings",)
    # Read as the dashboard chat reads it: KAS translates toolsSettings.
    assert seen["translates_tools_settings"] is True


# ── P0-11: a stale KAS batch never hides a call from the spec's hooks ────────


def test_a_mid_turn_switch_to_a_stale_batch_ends_the_turn(monkeypatch):
    async def fake_turn_spec_hooks(provider, agent):
        return spec_hooks.TurnSpecHooks([], "/w", False, True)

    async def stale(provider, agent):
        return True

    monkeypatch.setattr(spec_hooks, "turn_spec_hooks", fake_turn_spec_hooks)
    monkeypatch.setattr(spec_hooks, "hook_projection_stale", stale)
    cancelled: list = []

    class _Cancelling(_Provider):
        async def cancel(self):
            cancelled.append(True)

    turn = _turn(_Cancelling(), [])
    switch = AcpEvent(kind="agent_switched", text="a2")
    with pytest.raises(spec_hooks.StaleProjectionError):
        asyncio.run(script_hooks.observe_agent_switch(turn, switch))
    assert cancelled == [True] and turn.agent == "a2"


def test_the_shared_channel_pipeline_reprojects_its_claim(monkeypatch):
    from test_messaging_dispatch import _CtxBuilder, _patch_pipeline, _Renderer, _Sessions
    from test_messaging_dispatch import _turn as _channel_turn

    from kiro_crew.messaging import dispatch as D

    seen: list = []

    async def fake_reproject(sessions, session_key, agent, claimed, claim):
        seen.append(session_key)
        return claimed

    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(D, "reproject_claim", fake_reproject)
    turn = _channel_turn(_Renderer())
    asyncio.run(D.drive_turn(turn, sessions=_Sessions(), ctx_builder=_CtxBuilder()))
    assert seen and set(seen) == {turn.session_key}


def test_a_pre_tool_hook_is_governed_under_the_turns_own_session(monkeypatch):
    """The governance scope a hook run is checked under is the firing session's key."""
    keys: list = []

    async def fake_run(hook, context="", hook_event=None, cwd=None, **_kw):
        keys.append((hook_event or {}).get("session_key"))
        return ScriptHookResult(hook_id=hook.id, hook_name=hook.name, event=hook.event)

    monkeypatch.setattr(hooks_mod, "run_script_hook", fake_run)
    provider = _Provider()
    turn = _turn(provider, [_hook(HOOK_EVENT_PRE_TOOL_USE, "guard.sh", "*")])
    event = AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", title="Running: ls")
    assert asyncio.run(script_hooks.spec_pre_tool_refusal(turn, event)) is None
    assert keys == ["slack:C1:1"]
