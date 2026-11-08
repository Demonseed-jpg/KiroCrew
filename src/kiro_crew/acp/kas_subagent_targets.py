"""Register the agents a KAS session's sub-agent tool may name.

KAS's ``invoke_sub_agent`` (kiro-cli's ``use_subagent``) looks the name it is
given up in the session's agent registry: the ``customAgents`` batch plus the
on-disk profiles the engine keeps. The engine keeps no profile written for
kiro-cli (``allowedTools`` or ``toolsSettings`` and no ``permissions`` block), so
an agent the parent's ``toolsSettings.subagent.availableAgents`` names is found
only when the batch carries it. Each such agent is projected here through the
same translation as the session's own agent and appended to the batch, and the
session-start notice names every one that is not.

A target whose limits Kiro Crew enforces only at its own gate is not registered.
The gate applies the shell ``denyByDefault`` allowlist, the write
``fallbackAction: "deny"`` allowlist and the spec's own ``preToolUse`` hooks of
the agent the session runs, and a sub-agent's calls reach the gate under that
agent, so the target would run without its own limits.
"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any

from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.acp import kas_env_launch
from kiro_crew.acp.kas_permissions import (
    CREW_SHELL_ALLOWLIST_KEY,
    CREW_WRITE_ALLOWLIST_KEY,
    _agent_glob,
)
from kiro_crew.acp.kas_prompt import prompt_unread_notice
from kiro_crew.acp.kas_tool_limits import with_switched_off_tools
from kiro_crew.agent_files import is_mate_service_agent, kas_wire_agent_id
from kiro_crew.platform.tool_names import KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME

logger = logging.getLogger(__name__)

#: The KAS id of the sub-agent tool, kiro-cli's ``use_subagent``.
KAS_SUBAGENT_TOOL = KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME["use_subagent"][0]

#: What a KAS ``tools`` or ``excludedTools`` entry selects the sub-agent tool by:
#: its id and the two tags KAS 0.66.33 declares it under (``tags: [BUILTIN,
#: SUBAGENT_META]``). The engine's matcher takes an entry equal to one of these, or
#: a ``*`` or ``?`` pattern that matches one, so ``@builtin`` mounts it.
_SUBAGENT_TOOL_NAMES = (KAS_SUBAGENT_TOOL, "@builtin", "@subagent")

#: How many targets one session registers. Each is a whole projection at session
#: start (a spec read, the governance checks, its servers' launch files), so a
#: ``*`` pattern over a large agents folder would slow every session start. The
#: rest are counted in the notice.
MAX_SUBAGENT_TARGETS = 8

_NOT_FOUND = "no agent of that name was found"
_NO_MATCH = "it matches no agent"
_UNUSABLE = "its agent file could not be used"
_RESERVED = "the backend keeps that name for one of its own modes"
_SERVICE = "it is one of Kiro Crew's own agents"
_GATE_ONLY = (
    "Kiro Crew applies its shell or file-write limits and its preToolUse hooks "
    "only to the agent a chat runs"
)


def _selects_subagent_tool(entry: Any) -> bool:
    """Whether one KAS tool-list *entry* selects the sub-agent tool, as the engine reads it.

    An entry equal to a :data:`_SUBAGENT_TOOL_NAMES` member, or a pattern that
    matches one, where ``*`` is any run, ``?`` any one character and every other
    character itself.
    """
    if not isinstance(entry, str):
        return False
    # ``[`` opens a character set to fnmatch and is itself to the engine.
    pattern = entry.replace("[", "[[]")
    return any(fnmatch.fnmatchcase(name, pattern) for name in _SUBAGENT_TOOL_NAMES)


def mounts_subagent_tool(entry: Any) -> bool:
    """Whether the projected *entry* mounts KAS's sub-agent tool and keeps it.

    Read as the engine filters an agent's tools: ``"*"`` mounts every tool, a list
    mounts what one of its entries selects, and ``excludedTools`` takes away what
    one of its entries selects (:func:`_selects_subagent_tool`).
    """
    if not isinstance(entry, dict):
        return False
    excluded = entry.get("excludedTools")
    if isinstance(excluded, list) and any(_selects_subagent_tool(t) for t in excluded):
        return False
    tools = entry.get("tools")
    if tools == "*":
        return True
    return isinstance(tools, list) and any(_selects_subagent_tool(t) for t in tools)


def project_subagent_targets(
    parent: str,
    parent_spec: dict[str, Any],
    custom_agents: list[dict[str, Any]],
    work_dir: str | Path | None,
    *,
    stub_server_names: frozenset[str],
    session_key: str,
    withheld_out: kas_agents_mod.WithheldOut,
    engine_features: frozenset[str],
) -> tuple[list[dict[str, Any]], str]:
    """The entries to append to *parent*'s batch, and the notice for what is left out.

    Nothing when *parent*'s entry does not mount the sub-agent tool, or its spec
    declares no ``availableAgents`` (omitted, the engine's own roster answers, as
    before). Blocking file I/O: runs in the projection's worker thread. A target
    that fails to project costs that target and a notice line, never the session.
    """
    from kiro_crew.subagent import spawn_allowlist

    entry = next((a for a in custom_agents if a.get("id") == parent), None)
    if not mounts_subagent_tool(entry):
        return [], ""
    globs = spawn_allowlist(parent_spec)
    if not globs:
        # ``None`` is "allow all" and ``()`` is "spawn nothing": no list to register.
        return [], ""
    names, missed = _target_names(parent, globs, work_dir)
    overflow = max(0, len(names) - MAX_SUBAGENT_TARGETS)
    entries: list[dict[str, Any]] = []
    notices: list[str] = []
    for name in names[:MAX_SUBAGENT_TARGETS]:
        try:
            projected = _project_target(
                name,
                work_dir,
                stub_server_names=stub_server_names,
                session_key=session_key,
                withheld_out=withheld_out,
                engine_features=engine_features,
                notices=notices,
            )
        except Exception:  # noqa: BLE001 - a target never costs the session
            logger.warning("sub-agent %r could not be registered", name, exc_info=True)
            projected = _UNUSABLE
        if isinstance(projected, str):
            missed[name] = projected
        else:
            entries.append(projected)
    return entries, "\n\n".join(n for n in (*notices, _notice(parent, missed, overflow)) if n)


def _target_names(
    parent: str, globs: tuple[str, ...], work_dir: str | Path | None
) -> tuple[list[str], dict[str, str]]:
    """The agent ids *globs* name, in order, and each pattern that names none.

    A pattern KAS cannot read alike is skipped: the ``subagent`` rule
    (``kas_permissions._available_agents_rules``) already denies what it would
    name, and the translation notice names it. A ``*`` pattern is matched with
    kiro-cli's case-sensitive ``fnmatch`` against the agents the spawn gate offers.
    """
    names: list[str] = []
    missed: dict[str, str] = {}
    roster: list[str] | None = None
    # The parent's own wire id is the parent, never a second entry.
    own = {parent, kas_wire_agent_id(parent)}
    for raw in globs:
        glob = _agent_glob(raw.strip())
        if glob is None:
            continue
        if "*" not in glob:
            found = [glob]
        else:
            if roster is None:
                roster = _glob_roster(work_dir)
            found = [n for n in roster if fnmatch.fnmatchcase(n, glob)]
            if not found:
                missed[glob] = _NO_MATCH
        for name in found:
            if name not in own and name not in names:
                names.append(name)
    return names, missed


def _glob_roster(work_dir: str | Path | None) -> list[str]:
    """The agent names a ``*`` pattern is matched against, sorted.

    The spawn gate's set (``subagent._validate_agent``): the user-level agents and
    the checkout's, less Kiro Crew's generated specs and the agents reached by
    omitting a name. A crewmate's own folder is not listed, as no roster lists it.
    """
    from kiro_crew.agent_discovery import is_internal_agent_spec, list_agents
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.subagent import UNADVERTISED_AGENTS

    try:
        agents = list_agents(paths_mod.kiro_agents_dir(), project_dir=work_dir or None)
    except Exception:  # noqa: BLE001 - a pattern then matches nothing and is named
        logger.warning("agents folder unreadable for sub-agent patterns", exc_info=True)
        return []
    return sorted(
        {
            a.name
            for a in agents
            if a.name not in UNADVERTISED_AGENTS and not is_internal_agent_spec(a)
        }
    )


def _project_target(
    name: str,
    work_dir: str | Path | None,
    *,
    stub_server_names: frozenset[str],
    session_key: str,
    withheld_out: kas_agents_mod.WithheldOut,
    engine_features: frozenset[str],
    notices: list[str],
) -> dict[str, Any] | str:
    """*name*'s projected entry, or the reason it is not registered.

    The spec comes through the gates the session's own agent passes (fork
    governance, a derived spec's freshness, nearest-first resolution with a
    checkout's spec trusted for nothing), and the entry from the same projection.
    *withheld_out* and *notices* gain this target's lines only once it registers.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.acp.harness import kas as kas_harness
    from kiro_crew.acp.session_mcp import session_mcp_disabled_tools
    from kiro_crew.agent_sdk.spec_hooks import spec_script_hooks
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE

    own_notices: list[str] = []
    if kas_wire_agent_id(name) != name:
        # Registered under another id, which the parent's ``subagent`` rule denies.
        return _RESERVED
    if is_mate_service_agent(name):
        return _SERVICE
    agents_dir = paths_mod.kiro_agents_dir()
    try:
        agent_mod.require_fork_governance(name, work_dir)
        snapshot = agent_mod.require_fresh_derived_spec(name, work_dir)
        if snapshot is not None and snapshot.spec is not None:
            spec, prompt_dir = snapshot.spec, agents_dir
        else:
            spec, prompt_dir = kas_harness.resolve_projected_spec(
                agents_dir, name, work_dir, own_notices
            )
    except kas_agents_mod.KasAgentSpecAbsent:
        return _NOT_FOUND
    except (
        agent_mod.ForkGovernanceUnresolved,
        agent_mod.DerivedSpecStale,
        kas_agents_mod.KasAgentTranslationError,
    ) as exc:
        logger.warning("sub-agent %r is not registered for this session: %s", name, exc)
        return _UNUSABLE
    if any(
        h.enabled and h.event == HOOK_EVENT_PRE_TOOL_USE and h.command.strip()
        for h in spec_script_hooks(name, spec)
    ):
        return _GATE_ONLY
    scope_rules, skill_copies = kas_harness._skill_scope(name, spec, prompt_dir, work_dir)
    projected = spec
    if skill_copies:
        declared = spec.get("resources")
        kept = declared if isinstance(declared, list) else []
        projected = {**spec, "resources": [*kept, *skill_copies]}
    withheld: kas_agents_mod.WithheldOut = {}
    unread: list[Path] = []
    [entry] = kas_agents_mod.build_kas_custom_agents(
        prompt_dir,
        name,
        projected,
        stub_server_names=stub_server_names,
        session_key=session_key,
        skill_scope_rules=scope_rules,
        withheld_out=withheld,
        prompt_unread_out=unread,
        engine_features=engine_features,
    )
    if CREW_SHELL_ALLOWLIST_KEY in entry or CREW_WRITE_ALLOWLIST_KEY in entry:
        # The launch files this projection wrote belong to no registered entry.
        kas_env_launch.discard_batch([entry])
        return _GATE_ONLY
    withheld_out.update(withheld)
    notices.extend([*own_notices, prompt_unread_notice(name, unread)])
    [entry] = with_switched_off_tools([entry], session_mcp_disabled_tools(name, spec=spec))
    return entry


def _notice(parent: str, missed: dict[str, str], overflow: int) -> str:
    """The session-start line naming the targets the sub-agent tool cannot reach."""
    parts = [f"{name} ({reason})" for name, reason in missed.items()]
    if overflow:
        parts.append(
            f"{overflow} more agents (only the first {MAX_SUBAGENT_TARGETS} it names are tried)"
        )
    if not parts:
        return ""
    return (
        f"ℹ In this chat, agent {parent} cannot hand work to {'; '.join(parts)}. "
        "Choose that agent for a chat of its own to use it."
    )
