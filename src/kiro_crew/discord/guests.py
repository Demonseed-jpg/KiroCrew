"""The Discord guest tier: who counts as a guest, what a guest may run, and how a
listed bot is kept from looping the bot with itself.

``discord.allowed_user_ids`` names the operators. ``discord.guest_user_ids`` names
people and bots the channel answers WITHOUT trusting: every guest turn runs
tool-less on :data:`~kiro_crew.messaging.dispatch.TOOLLESS_TURN_AGENT` under a
session key no operator turn shares, and nothing a guest sends can change the
agent, the model, the session, the approval state or what is linked to it.

This module owns the three pure decisions the transport and the dispatcher share,
so the two copies of the roster cannot answer them differently:

* :func:`effective_guests` -- a user in both lists is an operator, so the guest set
  is always the list minus the operators;
* :func:`is_operator_only` -- the command classification, an ALLOW-list of what a
  guest may run, so a command added later is refused to guests until someone
  decides otherwise;
* :class:`BotTurnGuard` -- the chain cap and cooldown for turns a bot started.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

from kiro_crew.discord.transport_dispatch_origin import _CHAT_TYPE_THREAD
from kiro_crew.messaging.dispatch_tools import TOOLLESS_TURN_AGENT
from kiro_crew.messaging.link import CHAT_TYPE_DIRECT, build_dm_session_key, seed_generation

#: Scope-id prefix of a guest conversation's per-conversation state (generation
#: counter, awaiting-compact flag). A distinct prefix keeps a guest's counters apart
#: from the operator's for the same DM or thread.
GUEST_SCOPE_PREFIX = "guest:"

#: Scope segment appended to a thread id for the guests' session bucket in that
#: thread. Colon-free by construction, so it is one more scope segment in the
#: ``{channel}:{agent}:{chat_type}:{scope}`` key shape.
GUEST_THREAD_SEGMENT = "guest"

#: The commands a guest may run. Everything else, including a command added later,
#: is operator-only: ``!status`` describes the host process, ``!new`` /
#: ``!compact`` / ``!stop`` act on a session, ``!agent`` / ``!model`` change what
#: answers, ``!sessions`` / ``!link`` / ``!unlink`` reach other conversations and
#: the dashboard. ``!help`` discloses the guest card and nothing about the host.
GUEST_COMMANDS: frozenset[str] = frozenset({"help"})

GUEST_REFUSAL = (
    "🔒 That command is for the bot's operator. As a guest you can chat with me, "
    "but you can't change the agent, the model or the session."
)

GUEST_BUTTON_REFUSAL = "🔒 Only the bot's operator can use this button."

_GUEST_HELP = (
    "🦞 **Kiro Crew — Discord (guest)**\n"
    "\n"
    "Just send a message to chat. Replies stream in real-time.\n"
    "\n"
    "Guests talk to a restricted assistant: it can't run tools or touch files, and "
    "session, agent and model commands are reserved for the bot's operator."
)


def is_operator_only(command: str | None) -> bool:
    """Whether *command* is refused to a guest.

    ``None`` (not a command) is not restricted. An unknown name answers ``True``:
    a caller holding a name outside the table is confused about what it is
    running, and the safe reading of that is "restricted".
    """
    if command is None:
        return False
    return command not in GUEST_COMMANDS


def guest_help_text() -> str:
    """The ``!help`` card a guest sees: no operator command is listed."""
    return _GUEST_HELP


def guest_scope_id(user_id: str, thread_id: str = "") -> str:
    """The per-conversation state key of a guest's DM or thread."""
    scope = f"thread:{thread_id}" if thread_id else f"user:{user_id}"
    return f"{GUEST_SCOPE_PREFIX}{scope}"


def guest_session_key(user_id: str, thread_id: str = "", *, gen: int = 0) -> str:
    """The session a guest's turns run in, never one an operator's turns use.

    The bucket is built under the tool-less agent: ``get_or_create`` keeps an
    existing session's agent, so a key shared with an operator session would either
    hand the guest the operator's tools or refuse the turn. In a thread the guests
    share one bucket of their own (a ``guest`` segment on the thread id); in a DM it
    is the guest's alone, and ``dm_scope="unified"`` never applies, because it would
    pour the guest's messages into the operator's conversation.
    """
    return build_dm_session_key(
        "discord",
        TOOLLESS_TURN_AGENT,
        f"{thread_id}:{GUEST_THREAD_SEGMENT}" if thread_id else user_id,
        gen=gen,
        dm_scope="per-channel-peer",
        chat_type=(_CHAT_TYPE_THREAD if thread_id else CHAT_TYPE_DIRECT),
    )


def guest_seed_generation(sessions: object, scope_id: str) -> int:
    """The highest generation already persisted for a guest scope's bucket."""
    inner = scope_id.removeprefix(GUEST_SCOPE_PREFIX)
    if inner.startswith("thread:"):
        bucket = guest_session_key("", inner.removeprefix("thread:"))
        return int(sessions.max_generation(bucket))  # type: ignore[attr-defined]
    return seed_generation(
        sessions,
        channel="discord",
        agent=TOOLLESS_TURN_AGENT,
        user_id=inner.removeprefix("user:"),
        dm_scope="per-channel-peer",
    )


def is_guest(dispatcher: Any, user_id: str) -> bool:
    """Whether *user_id* is a guest of *dispatcher*: listed, and not also an operator.

    A function over the dispatcher's two rosters rather than a method, so the
    dispatcher class surface stays what its composition test pins.
    """
    return (
        bool(user_id) and user_id in dispatcher._guest_listed and user_id not in dispatcher._allowed
    )


def is_admitted(dispatcher: Any, user_id: str) -> bool:
    """Whether *user_id* is an operator or a guest of *dispatcher*."""
    return bool(user_id) and (user_id in dispatcher._allowed or is_guest(dispatcher, user_id))


def effective_guests(guests: Iterable[str], operators: Iterable[str]) -> frozenset[str]:
    """*guests* minus *operators*: a user in both lists is an operator."""
    return frozenset(str(g) for g in guests) - frozenset(str(o) for o in operators)


#: Minimum spacing between two bot-started turns in one conversation.
BOT_TURN_COOLDOWN_SECS = 15.0
#: Bot-started turns allowed in one conversation before a person speaks there.
BOT_TURN_CHAIN_CAP = 5
#: A conversation quiet for this long forgets its chain, so a cap that was hit does
#: not mute a bot forever when no person happens to write.
BOT_TURN_CHAIN_RESET_SECS = 900.0
#: Conversations tracked at once; the least recently used is forgotten first.
_MAX_TRACKED_CONVERSATIONS = 512


class BotTurnGuard:
    """Chain cap and cooldown for turns a listed bot starts.

    A bot guest is only answered when it addresses this bot, which stops a bot
    that merely talks in a channel. Two bots that reply to each other would still
    ping-pong indefinitely, and every reply here costs a model turn, so each
    conversation also gets:

    * a cooldown: a bot-started turn closer than ``cooldown`` to the previous one
      is refused;
    * a chain cap: at most ``chain_cap`` bot-started turns in a row. A person
      speaking in the conversation (:meth:`note_person`) resets the chain, and so
      does ``reset_after`` seconds of quiet.

    A refused attempt consumes nothing, so a bot that keeps trying cannot extend
    its own cooldown. The clock is injected for tests.
    """

    def __init__(
        self,
        *,
        cooldown: float = BOT_TURN_COOLDOWN_SECS,
        chain_cap: int = BOT_TURN_CHAIN_CAP,
        reset_after: float = BOT_TURN_CHAIN_RESET_SECS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown = cooldown
        self._chain_cap = chain_cap
        self._reset_after = reset_after
        self._clock = clock
        # conversation id -> (bot-started turns in the chain, time of the last one)
        self._chains: OrderedDict[str, tuple[int, float]] = OrderedDict()

    def note_person(self, conversation_id: str) -> None:
        """A person spoke in *conversation_id*: the bot chain starts over."""
        self._chains.pop(conversation_id, None)

    def admit(self, conversation_id: str) -> bool:
        """Whether a bot-started turn in *conversation_id* may run now; records it."""
        now = self._clock()
        count, last = self._chains.get(conversation_id, (0, 0.0))
        if count and now - last >= self._reset_after:
            count = 0
        if count and now - last < self._cooldown:
            return False
        if count >= self._chain_cap:
            return False
        self._chains[conversation_id] = (count + 1, now)
        self._chains.move_to_end(conversation_id)
        while len(self._chains) > _MAX_TRACKED_CONVERSATIONS:
            self._chains.popitem(last=False)
        return True
