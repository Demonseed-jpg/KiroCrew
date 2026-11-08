"""The script hooks a channel or cron turn fires: the Hooks page's ``UserPromptSubmit``
and ``Stop``, and the agent spec's own hooks on a backend that never runs them.

A dashboard turn fires the hooks the user saved on the Hooks page through
``dashboard.chat_turn.turn_spec_hooks._fire``. A channel turn runs through
:class:`kiro_crew.messaging.driver.TurnDriver` instead, so it fires them here,
from the same global store, with the same result handling for these two events:
an exit-0 hook's stdout and an exit-2 hook's ``BLOCKED:`` line go into the
prompt's ``[Hook context]`` block, and any other result only logs.

An agent spec's own ``hooks`` run here only where Crew fires them in the harness's
place (KAS), read by the reader sub-agents and the task runner use
(:func:`kiro_crew.agent_sdk.spec_hooks.turn_spec_hooks`, attached with
:func:`attach_spec_hooks`): ``AgentSpawn`` on a new session, ``UserPromptSubmit``
and ``Stop`` beside the Hooks page's, and ``PreToolUse`` on each permission request
(:func:`spec_pre_tool_refusal`). A Stop hook's continuation request is not acted on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHookStore,
    get_global_hook_store,
    permission_pre_tool_block,
)

if TYPE_CHECKING:
    from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks

logger = logging.getLogger(__name__)

#: The provider type a claim returns.
_P = TypeVar("_P")


@dataclass
class ScriptHookTurn:
    """Who an inbound message's script hooks run for, and the prompt they match.

    *session_key* is the conversation's own key: per-session attribution and the
    governance scope of each run. *prompt* is the sender's message, not the
    context-built prompt, as on the dashboard.

    One per inbound message, shared by every attempt at it: ``UserPromptSubmit``
    runs once, and a replay after a failed compaction reuses its *prompt_context*.
    """

    session_key: str
    prompt: str
    prompt_context: list[str] | None = field(default=None, compare=False)
    # Bound by the caller to the Stop generation captured on session acquisition.
    # Checked after every pre-stream await, including the hook and monitor claim.
    cancelled: Callable[[], bool] | None = field(default=None, compare=False)
    # The Hooks page's hooks run: the operator's own conversation only.
    store_hooks: bool = field(default=True, compare=False)
    # The agent spec's own hooks for the session, set by attach_spec_hooks.
    spec: TurnSpecHooks | None = field(default=None, compare=False)
    # The provider those hooks belong to; its ACP session id is read at fire time.
    provider: Any = field(default=None, compare=False)
    # The agent the spec was read for, the payload's ``agent_role`` on PreToolUse.
    agent: str = field(default="", compare=False)
    # A new session: the spec's AgentSpawn hooks fire before its first prompt.
    fresh_session: bool = field(default=False, compare=False)

    @classmethod
    def stoppable(
        cls, sessions: Any, session_key: str, prompt: str, *, store_hooks: bool = True
    ) -> ScriptHookTurn:
        """A turn whose ``cancelled`` reads a Stop recorded on *sessions* from now on.

        Takes the session's Stop generation at once, so call it after the session
        is acquired and before anything awaits.
        """
        from kiro_crew.messaging.dispatch_recovery import session_stop_generation

        at_entry = session_stop_generation(sessions, session_key)
        return cls(
            session_key,
            prompt,
            cancelled=lambda: session_stop_generation(sessions, session_key) != at_entry,
            store_hooks=store_hooks,
        )


async def attach_spec_hooks(
    turn: ScriptHookTurn, provider: Any, agent: str, *, fresh_session: bool
) -> None:
    """Read the agent spec hooks *provider*'s session fires onto *turn*.

    *agent* is the one the session was opened with; a session a mode switch moved
    answers the agent it runs now (``running_agent``), as on the task runner. Call
    it once the session is acquired, on every attempt, so a replay on a fresh
    runtime fires for that runtime. No hooks on a backend whose harness runs them.
    """
    from kiro_crew.agent_sdk.spec_hooks import running_agent, turn_spec_hooks

    turn.agent = running_agent(provider, agent)
    turn.spec = await turn_spec_hooks(provider, turn.agent)
    turn.provider = provider
    turn.fresh_session = fresh_session


async def reproject_claim(
    sessions: Any,
    session_key: str,
    agent: str,
    claimed: tuple[_P, bool, bool],
    claim: Callable[[], Awaitable[tuple[_P, bool, bool]]],
) -> tuple[_P, bool, bool]:
    """*claimed*, or the session claimed again once its stale KAS batch is reset.

    A KAS session keeps the batch it registered, which withheld auto-approval only
    for what the PreToolUse hooks covered then, so a hook added since would never
    see a call the session auto-approves. Called right after the claim, with the
    lease held, as the dashboard, sub-agent and task-runner claims do
    (``spec_hooks.reproject_claimed_session``, which raises
    ``StaleProjectionError`` when two resets do not cure it).
    """
    from kiro_crew.agent_sdk.spec_hooks import reproject_claimed_session

    return await reproject_claimed_session(sessions, session_key, agent, claimed, claim)


async def observe_agent_switch(turn: ScriptHookTurn | None, event: Any) -> None:
    """After a mid-turn agent switch, gate what follows on the new agent's spec hooks.

    Any other *event* is ignored. An unnamed switch falls back to the agent the
    session recorded for it, as on the task runner.
    """
    if turn is None or turn.provider is None:
        return
    from kiro_crew.agent_sdk import CONTEXT_EVENT_AGENT_CHANGED
    from kiro_crew.agent_sdk.spec_hooks import refuse_stale_switch, session_agent, turn_spec_hooks

    if getattr(event, "kind", "") != CONTEXT_EVENT_AGENT_CHANGED:
        return
    agent = str(getattr(event, "text", "") or "")
    turn.spec = await turn_spec_hooks(turn.provider, agent)
    turn.agent = session_agent(turn.provider, agent)
    # A switch to an agent whose PreToolUse hooks the session's batch does not
    # withhold for ends the turn, as on the dashboard and the task runner.
    await refuse_stale_switch(turn.provider, agent)


async def spec_pre_tool_refusal(turn: ScriptHookTurn | None, event: Any) -> str | None:
    """Why the agent spec's PreToolUse hooks refuse *event*'s permission request, or ``None``.

    The sub-agent and task-runner floor (``tool_permission.SpecHooks``) with the
    spec's hooks alone: the Hooks page's PreToolUse hooks keep the role they have on
    these turns. A spec hook blocks on exit 2 and when a policy refused to run it,
    and one that crashed or timed out lets the call run, as on kiro-cli; a spec that
    could not be read blocks, as in a dashboard chat. ``None`` where the backend
    runs the spec's hooks itself.
    """
    spec = turn.spec if turn is not None else None
    if turn is None or spec is None or not spec.gated:
        return None
    if not spec.unreadable and not spec.hooks:
        return None
    # Deferred: tool_permission imports llm_helpers and through it the agent
    # driver, still initializing when kiro_crew.slack.handler loads this module.
    from kiro_crew import tool_permission

    floor = tool_permission.SpecHooks(
        spec,
        store=_spec_only_store,
        pre_tool=permission_pre_tool_block,
        agent_role=turn.agent or None,
        # The turn's own key scopes each run's governance, as on its prompt events.
        session_key=turn.session_key,
    )
    refusal = await floor.refuse(
        tool_permission.Ask(event, tool_permission.AcpWire(turn.provider), turn.session_key)
    )
    return None if refusal is None else refusal.reason


def spec_lost_notice(turn: ScriptHookTurn | None) -> str:
    """The dashboard's session-start notice for the spec keys this session lacks, or ``""``."""
    spec = turn.spec if turn is not None else None
    if turn is None or spec is None or not spec.lost:
        return ""
    from kiro_crew.agent_sdk.spec_hooks import spec_keys_notice

    return spec_keys_notice(turn.agent, list(spec.lost))


def _spec_only_store() -> Any:
    """A store with no saved hooks, so a fire runs the spec's hooks alone."""
    return ScriptHookStore(load=False)


async def _fire(turn: ScriptHookTurn, event: str, context: str, *, with_store: bool) -> list:
    """Fire *event*: the Hooks page's hooks when *with_store* and the turn runs them,
    and the spec's own. Empty when neither has any."""
    store = get_global_hook_store() if with_store and turn.store_hooks else None
    spec = turn.spec
    spec_hooks = spec.hooks if spec is not None else []
    if not spec_hooks:
        if store is None:
            return []
        return await store.fire(event, context, session_key=turn.session_key)
    # The ACP session id the spec hooks would see as KIRO_SESSION_ID under their
    # own harness, read at fire time: a reset gives the session a new one.
    sid = getattr(turn.provider, "session_id", "")
    return await (store or _spec_only_store()).fire(
        event,
        context,
        session_key=turn.session_key,
        extra_hooks=spec_hooks,
        extra_hooks_cwd=spec.cwd if spec is not None else None,
        acp_session_id=sid if isinstance(sid, str) and sid else None,
    )


async def fire_prompt_submit(turn: ScriptHookTurn) -> list[str]:
    """Fire ``UserPromptSubmit`` once per message; the texts its hook context carries.

    A new session fires the spec's ``AgentSpawn`` first, with the session key as
    its context, as the dashboard does; the Hooks page's AgentSpawn hooks do not
    run on these turns.
    """
    if turn.prompt_context is None:
        turn.prompt_context = await _prompt_submit(turn)
    return turn.prompt_context


async def _prompt_submit(turn: ScriptHookTurn) -> list[str]:
    events = [(HOOK_EVENT_AGENT_SPAWN, turn.session_key, False)] if turn.fresh_session else []
    events.append((HOOK_EVENT_USER_PROMPT_SUBMIT, turn.prompt, True))
    injected: list[str] = []
    for event, context, with_store in events:
        try:
            results = await _fire(turn, event, context, with_store=with_store)
        except Exception:
            logger.warning("%s hook fire error", event, exc_info=True)
            continue
        for r in results:
            if r.exit_code == 0 and r.stdout:
                injected.append(r.stdout)
            elif r.exit_code == 2 and r.refused:
                injected.append(f"BLOCKED:{r.hook_name}:{r.failure_detail}")
            elif r.exit_code == 2:
                injected.append(
                    f"BLOCKED:{r.hook_name}:{r.stderr[:200] if r.stderr else 'hook denied'}"
                )
            elif r.exit_code != 0 and r.stderr:
                logger.warning("Hook %s warning: %s", r.hook_name, r.stderr[-200:])
    return injected


async def with_hook_context(message: str, injected: list[str]) -> str:
    """*message* behind a ``[Hook context]`` block, its structural markers neutralized.

    Hook output is untrusted text placed in front of the trusted prompt, so a
    marker in it must not pass for one of the prompt's own boundaries.
    """
    if not injected:
        return message
    from kiro_crew.context import _neutralize_structural_markers  # circular at import

    block = "\n\n".join(injected)
    safe = await asyncio.to_thread(_neutralize_structural_markers, block)
    return f"[Hook context]\n{safe}\n[End hook context]\n\n{message}"


async def fire_stop_if_succeeded(turn: ScriptHookTurn, stop_reason: str, reply: str) -> None:
    """Fire ``Stop`` with *reply* when *stop_reason* classifies ``succeeded``.

    For a caller that streams without a ``TurnDriver`` and so judges the end of
    its own turn; the driver applies the same rule. ``stop_reason_landed`` is that
    one classifier read for a completion that was observed.
    """
    # circular import: turn_bracket -> agent_sdk -> acp.client, which is still
    # initializing when kiro_crew.slack.handler loads this module.
    from kiro_crew.messaging.turn_bracket import stop_reason_landed

    if stop_reason_landed(stop_reason):
        await fire_stop(turn, reply)


async def fire_stop(turn: ScriptHookTurn, reply: str) -> None:
    """Fire ``Stop`` with the turn's channel-safe reply as its context."""
    try:
        await _fire(turn, HOOK_EVENT_STOP, reply, with_store=True)
    except Exception:
        logger.warning("Stop hook fire error", exc_info=True)
