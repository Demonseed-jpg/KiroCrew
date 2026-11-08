"""A KAS agent's sub-agent tool reaches the agents its ``availableAgents`` names.

KAS resolves an ``invoke_sub_agent`` name only in the session's agent registry, and
its on-disk loader drops a spec written for kiro-cli, so Kiro Crew registers each
named agent in the session's ``customAgents`` batch, translated like the session's
own agent, and names every one it does not register in the session-start notice.
A remote workspace leaves out a target whose own rules it cannot bind, names it,
and still starts the session.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp.harness.kas import KasHarness

#: Shaped like a kiro-cli or AIM spec: ``allowedTools`` and ``toolsSettings``, no
#: ``permissions`` block, which KAS's own loader skips as a CLI-only profile.
_SLACK = {
    "name": "slack-agent",
    "description": "Reads and posts Slack messages",
    "prompt": "You are the Slack agent.",
    "tools": ["fs_read"],
    "allowedTools": ["fs_read"],
    "toolsSettings": {"fs_read": {"allowedPaths": ["~/slack"]}},
}


def _delegator(*available: str, tools: tuple[str, ...] = ("fs_read", "use_subagent")) -> dict:
    return {
        "name": "delegator",
        "prompt": "Delegate every Slack task to the slack-agent sub-agent.",
        "tools": list(tools),
        "allowedTools": ["fs_read"],
        "toolsSettings": {"subagent": {"availableAgents": list(available)}},
    }


@pytest.fixture
def agents_dir(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user


def _write(directory: Path, spec: dict) -> None:
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _extras():
    return await KasHarness().session_extras("delegator", work_dir=None)


def _ids(extras) -> list[str]:
    return [a["id"] for a in extras.custom_agents]


@pytest.mark.asyncio
async def test_a_delegating_agent_registers_the_agent_its_available_agents_names(agents_dir):
    _write(agents_dir, _delegator("slack-agent"))
    denied = {"fs_read": {"allowedPaths": ["~/slack"], "deniedPaths": ["~/slack/private"]}}
    _write(agents_dir, {**_SLACK, "toolsSettings": denied})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "slack-agent"]
    parent, target = extras.custom_agents
    assert "invoke_sub_agent" in parent["tools"]
    assert {
        "capability": "subagent",
        "match": ["*"],
        "exclude": ["slack-agent"],
        "effect": "deny",
    } in parent["permissions"]["rules"]
    # Through Crew's own translation: KAS tool ids, the description the engine's
    # tool lists, and its own toolsSettings as KAS permissions, never relayed raw.
    assert "read_file" in target["tools"]
    assert target["description"] == _SLACK["description"]
    assert target["prompt"] == _SLACK["prompt"]
    assert "allowedTools" not in target and "toolsSettings" not in target
    assert any(r.get("effect") == "deny" for r in target["permissions"]["rules"])
    assert "slack-agent" not in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_a_name_no_spec_declares_is_named_in_the_notice(agents_dir):
    _write(agents_dir, _delegator("ghost"))

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert "ghost (no agent of that name was found)" in extras.withheld_credentials_notice
    assert "agent delegator cannot hand work to" in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_a_pattern_registers_every_agent_it_matches(agents_dir):
    _write(agents_dir, _delegator("slack-*", "mail-*"))
    for name in ("slack-agent", "slack-bot", "other"):
        _write(agents_dir, {**_SLACK, "name": name})

    extras = await _extras()

    assert _ids(extras) == ["delegator", "slack-agent", "slack-bot"]
    assert "mail-* (it matches no agent)" in extras.withheld_credentials_notice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits",
    [
        pytest.param(
            {
                "tools": ["execute_bash"],
                "toolsSettings": {"shell": {"denyByDefault": True, "allowedCommands": ["ls"]}},
            },
            id="shell-deny-by-default",
        ),
        pytest.param(
            {
                "tools": ["fs_write"],
                "toolsSettings": {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["out"]}},
            },
            id="write-fallback-deny",
        ),
        pytest.param(
            {"hooks": {"preToolUse": [{"matcher": "*", "command": "exit 2"}]}},
            id="pre-tool-use-hook",
        ),
    ],
)
async def test_a_target_whose_limits_only_the_gate_applies_is_not_registered(agents_dir, limits):
    # Crew's gate applies these to the agent the session runs, and a sub-agent's
    # calls reach it under that agent, so registered they would not bind.
    _write(agents_dir, _delegator("guarded"))
    _write(agents_dir, {**_SLACK, "name": "guarded", **limits})

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert "guarded (Kiro Crew applies its shell" in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_an_agent_without_the_sub_agent_tool_registers_nothing(agents_dir):
    _write(agents_dir, _delegator("slack-agent", tools=("fs_read",)))
    _write(agents_dir, _SLACK)

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert "slack-agent" not in extras.withheld_credentials_notice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tools",
    [
        pytest.param(("fs_read", "use_subagent"), id="kiro-cli-name"),
        pytest.param(("*",), id="every-tool"),
        pytest.param(("@builtin",), id="builtin-tag"),
        pytest.param(("fs_read", "@subagent"), id="subagent-tag"),
        pytest.param(("fs_read", "@sub*"), id="pattern-over-a-tag"),
    ],
)
async def test_every_spelling_the_engine_mounts_the_tool_for_registers_the_target(
    agents_dir, tools
):
    # KAS declares invoke_sub_agent under the tags @builtin and @subagent, and its
    # tool filter mounts it for the id, either tag or a pattern over one.
    _write(agents_dir, _delegator("slack-agent", tools=tools))
    _write(agents_dir, _SLACK)

    extras = await _extras()

    assert _ids(extras) == ["delegator", "slack-agent"]


@pytest.mark.asyncio
async def test_an_exclusion_by_tag_takes_the_tool_and_its_targets_away(agents_dir):
    parent = {**_delegator("slack-agent", tools=("@builtin",)), "excludedTools": ["@subagent"]}
    _write(agents_dir, parent)
    _write(agents_dir, _SLACK)

    extras = await _extras()

    assert _ids(extras) == ["delegator"]


@pytest.mark.asyncio
async def test_a_name_the_engine_keeps_for_its_own_mode_is_named(agents_dir):
    # Sent under another wire id, which the parent's subagent rule denies.
    _write(agents_dir, _delegator("plan"))
    _write(agents_dir, {**_SLACK, "name": "plan"})

    extras = await _extras()

    assert _ids(extras) == ["delegator"]
    assert "plan (the backend keeps that name" in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_registration_stops_at_the_cap_and_counts_the_rest(agents_dir):
    from kiro_crew.acp.kas_subagent_targets import MAX_SUBAGENT_TARGETS

    _write(agents_dir, _delegator("helper-*"))
    names = [f"helper-{i:02d}" for i in range(MAX_SUBAGENT_TARGETS + 2)]
    for name in names:
        _write(agents_dir, {**_SLACK, "name": name})

    extras = await _extras()

    assert _ids(extras) == ["delegator", *names[:MAX_SUBAGENT_TARGETS]]
    assert "2 more agents" in extras.withheld_credentials_notice


_WORKSPACE = {
    "server": {"name": "workspace", "command": "bridge", "args": [], "env": []},
    "workdir": "/w",
}


def _restricted_target() -> dict:
    # A file-path limit KAS would judge workspace tools by as ``mcp``, never as
    # ``fs_read``, so a remote workspace cannot hold the agent to it.
    denied = {"fs_read": {"allowedPaths": ["~/slack"], "deniedPaths": ["~/slack/private"]}}
    return {**_SLACK, "toolsSettings": denied}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["session/new", "session/load"])
async def test_a_remote_workspace_starts_without_a_target_it_cannot_bind(
    agents_dir, monkeypatch, tmp_path, path
):
    """The session's own agent has no such rules, so the session starts on both
    paths and only the hand-off to the target is lost, named in the notice."""
    from acp_runtime_helpers import _make_runtime

    from kiro_crew.acp import runtime_session_load, session_handle_mcp
    from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_SESSION_LOAD, METHOD_SESSION_NEW

    _write(agents_dir, _delegator("slack-agent"))
    _write(agents_dir, _restricted_target())
    projected = await _extras()
    assert _ids(projected) == ["delegator", "slack-agent"]

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
    # No engine here reports an MCP server, so neither start-up wait has anything
    # to hold for.
    monkeypatch.setattr(session_handle_mcp, "_MCP_DRAIN_NO_REPORT_CEILING", 0.0)
    monkeypatch.setattr(runtime_session_load, "MOUNTED_MCP_WAIT_SECS", 0.0)
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._can_load_session = True
    rt._remote_workspace = dict(_WORKSPACE)
    sent: dict[str, dict] = {}

    async def _fake_send(method, params, timeout=None):
        sent.setdefault(method, params)
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-remote", "modes": {"currentModeId": "delegator"}}
        if method == METHOD_SESSION_LOAD:
            return {"modes": {"currentModeId": "delegator"}, "models": []}
        return {}

    # Without the projected servers: their readiness is not what this pins, and
    # no engine here reports them.
    batch = [{k: v for k, v in a.items() if k != "mcpServers"} for a in projected.custom_agents]
    extras = dataclasses.replace(
        projected, custom_agents=batch, managed_entries={}, app_launches={}
    )

    async def _fake_agents(agent, **_kw):
        return extras

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)

    if path == "session/new":
        handle = await rt.create_session(
            cwd=str(tmp_path), agent="delegator", mcp_servers=[], session_key="k"
        )
    else:
        handle = await rt.load_session(
            "", "sid-remote", cwd=str(tmp_path), agent="delegator", session_key="k"
        )

    assert [a["id"] for a in handle.kas_registered_agents] == ["delegator"]
    wire = sent[path]["_meta"]["kiro"]["customAgents"]
    assert [a["id"] for a in wire] == ["delegator"]
    notice = handle.kas_withheld_credentials_notice
    assert "agent delegator cannot hand work to slack-agent" in notice
    assert "remote workspace cannot enforce them" in notice


def test_a_remote_workspace_still_refuses_the_sessions_own_restricted_agent():
    from types import SimpleNamespace

    from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError

    denied = {"rules": [{"capability": "fs_read", "match": ["/x"], "effect": "deny"}]}
    agents = [
        {"id": "delegator", "tools": ["fs_read"], "permissions": denied},
        {"id": "slack-agent", "tools": ["fs_read"]},
    ]
    with pytest.raises(AcpRuntimeError, match="this agent's own rules"):
        AcpRuntime._remote_workspace_agents(
            SimpleNamespace(_remote_workspace=dict(_WORKSPACE)),
            agents,
            [{"name": "workspace"}],
            active_agent="delegator",
            notices_out=[],
        )
