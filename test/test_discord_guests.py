"""The Discord guest tier: ``discord.guest_user_ids``.

A guest (a person or a listed bot) is answered on a tool-less turn, in a session no
operator turn shares, and cannot change the agent, the model, the session or an
approval. A listed bot is answered only when it addresses this bot, and only within
the loop guard's budget. A user in both lists is an operator.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeClient, _dispatcher, _inbound

from kiro_crew.agent_sdk.backends_routing import ACP_BACKEND_KIRO
from kiro_crew.discord.client import DiscordClient, DiscordInbound, DiscordInteraction
from kiro_crew.discord.client_gateway import _mentions_user, _replies_to_user
from kiro_crew.discord.commands import COMMAND_SPEC, build_help_text
from kiro_crew.discord.guests import (
    GUEST_BUTTON_REFUSAL,
    GUEST_REFUSAL,
    BotTurnGuard,
    effective_guests,
    guest_help_text,
    guest_session_key,
    is_operator_only,
)
from kiro_crew.discord.renderer import DiscordApprovalDecider, session_provenance_tag
from kiro_crew.discord.transport import DiscordInboundMessage, DiscordTransport
from kiro_crew.messaging.dispatch import (
    TOOLLESS_TURN_AGENT,
    TOOLLESS_TURN_REFUSAL_NOTE,
    Asker,
    Audience,
    ChannelTurns,
    TurnOutcome,
    Verdict,
)
from kiro_crew.messaging.transport import InboundMessage

OPERATOR = "op1"
GUEST = "g1"
BOT = "bot1"


# ── the pure policy ──────────────────────────────────────────────────────────────


class TestPolicy:
    def test_every_published_command_but_help_is_refused_to_a_guest(self) -> None:
        names = {name for name, _desc in COMMAND_SPEC}
        assert {name for name in names if not is_operator_only(name)} == {"help"}

    def test_an_unknown_command_is_refused_to_a_guest(self) -> None:
        assert is_operator_only("a-command-added-later") is True
        assert is_operator_only(None) is False

    def test_a_user_in_both_lists_is_an_operator(self) -> None:
        assert effective_guests({"a", "b"}, {"b"}) == frozenset({"a"})

    def test_the_guest_card_lists_no_operator_command(self) -> None:
        card = guest_help_text()
        for name, _desc in COMMAND_SPEC:
            assert f"!{name}" not in card
        assert "!agent" in build_help_text()

    def test_a_guest_key_names_the_toolless_agent_and_never_an_operator_bucket(self) -> None:
        dm = guest_session_key("g1")
        thread = guest_session_key("g1", "t1")
        assert dm == f"discord:{TOOLLESS_TURN_AGENT}:direct:g1"
        assert thread == f"discord:{TOOLLESS_TURN_AGENT}:group:t1:guest"
        assert guest_session_key("g1", "t1", gen=2).endswith(":gen2")


class TestBotTurnGuard:
    def _guard(self, **kw: Any) -> tuple[BotTurnGuard, list[float]]:
        now = [1000.0]
        guard = BotTurnGuard(clock=lambda: now[0], **kw)
        return guard, now

    def test_the_first_turn_is_admitted_and_a_quick_second_is_not(self) -> None:
        guard, now = self._guard(cooldown=10.0, chain_cap=5, reset_after=600.0)
        assert guard.admit("c") is True
        now[0] += 9.9
        assert guard.admit("c") is False
        now[0] += 0.2
        assert guard.admit("c") is True

    def test_a_refused_attempt_does_not_extend_the_cooldown(self) -> None:
        guard, now = self._guard(cooldown=10.0, chain_cap=5, reset_after=600.0)
        assert guard.admit("c")
        for step in (4.0, 4.0):
            now[0] += step
            assert guard.admit("c") is False
        # 10.5s after the one admitted turn: had the refusals at 4s and 8s restarted
        # the cooldown, this would still be inside it.
        now[0] += 2.5
        assert guard.admit("c") is True

    def test_the_chain_cap_holds_until_a_person_speaks(self) -> None:
        guard, now = self._guard(cooldown=1.0, chain_cap=3, reset_after=600.0)
        for _ in range(3):
            assert guard.admit("c") is True
            now[0] += 2.0
        assert guard.admit("c") is False
        guard.note_person("c")
        assert guard.admit("c") is True

    def test_a_quiet_conversation_forgets_its_chain(self) -> None:
        guard, now = self._guard(cooldown=1.0, chain_cap=1, reset_after=600.0)
        assert guard.admit("c") is True
        now[0] += 2.0
        assert guard.admit("c") is False
        now[0] += 600.0
        assert guard.admit("c") is True

    def test_conversations_are_counted_apart(self) -> None:
        guard, _now = self._guard(cooldown=10.0, chain_cap=1, reset_after=600.0)
        assert guard.admit("a") is True
        assert guard.admit("b") is True
        assert guard.admit("a") is False

    def test_the_tracked_set_is_bounded(self) -> None:
        guard, _now = self._guard()
        for i in range(2000):
            guard.admit(f"c{i}")
        assert len(guard._chains) <= 512


# ── the gateway: bots and the mention/reply facts ────────────────────────────────


class TestGatewayEvents:
    def _client(self, guests: frozenset[str] = frozenset()) -> tuple[DiscordClient, list[Any]]:
        got: list[DiscordInbound] = []

        async def _on_message(inbound: DiscordInbound) -> None:
            got.append(inbound)

        client = DiscordClient(token="t", on_message=_on_message)
        client.bot_user_id = "me"
        client.guest_bot_ids = guests
        return client, got

    @staticmethod
    def _event(author: str, *, bot: bool, **extra: Any) -> dict[str, Any]:
        return {
            "id": "m1",
            "channel_id": "c1",
            "guild_id": "g",
            "content": "hello",
            "author": {"id": author, "username": "x", "bot": bot},
            **extra,
        }

    @pytest.mark.asyncio
    async def test_an_unlisted_bot_and_our_own_messages_are_dropped(self) -> None:
        client, got = self._client(frozenset({BOT}))
        client._on_dispatch("MESSAGE_CREATE", self._event("other-bot", bot=True))
        client._on_dispatch("MESSAGE_CREATE", self._event("me", bot=True))
        await asyncio.sleep(0)
        assert got == []

    @pytest.mark.asyncio
    async def test_our_own_message_is_dropped_even_when_listed(self) -> None:
        client, got = self._client(frozenset({"me"}))
        client._on_dispatch("MESSAGE_CREATE", self._event("me", bot=True))
        await asyncio.sleep(0)
        assert got == []

    @pytest.mark.asyncio
    async def test_a_listed_bot_is_delivered_with_what_it_addressed(self) -> None:
        client, got = self._client(frozenset({BOT}))
        client._on_dispatch(
            "MESSAGE_CREATE",
            self._event(BOT, bot=True, mentions=[{"id": "me"}], referenced_message=None),
        )
        client._on_dispatch(
            "MESSAGE_CREATE",
            self._event(BOT, bot=True, referenced_message={"author": {"id": "me"}}),
        )
        client._on_dispatch("MESSAGE_CREATE", self._event(BOT, bot=True))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert [(m.mentions_bot, m.replies_to_bot) for m in got] == [
            (True, False),
            (False, True),
            (False, False),
        ]

    def test_mention_and_reply_readers_ignore_malformed_payloads(self) -> None:
        assert _mentions_user(None, "me") is False
        assert _mentions_user(["me"], "me") is False
        assert _mentions_user([{"id": "me"}], "") is False
        assert _replies_to_user({"author": "me"}, "me") is False
        assert _replies_to_user(None, "me") is False


# ── the transport ────────────────────────────────────────────────────────────────


class TestTransport:
    def _transport(
        self,
        *,
        guests: list[str] | None = None,
        allowed: list[str] | None = None,
        threads: list[str] | None = None,
        channels: list[str] | None = None,
        guard: BotTurnGuard | None = None,
    ) -> tuple[DiscordTransport, list[InboundMessage], FakeClient]:
        dispatched: list[InboundMessage] = []

        async def _dispatch(m: InboundMessage) -> None:
            dispatched.append(m)

        client = FakeClient()
        client.thread_channels.update(threads or [])
        transport = DiscordTransport(
            client,  # type: ignore[arg-type]
            allowed_user_ids=allowed if allowed is not None else [OPERATOR],
            guest_user_ids=guests or [],
            allowed_thread_ids=threads or [],
            allowed_channel_ids=channels or [],
            dispatch=_dispatch,
            bot_turn_guard=guard,
        )
        return transport, dispatched, client

    @pytest.mark.asyncio
    async def test_a_guest_dm_is_dispatched(self) -> None:
        t, dispatched, _ = self._transport(guests=[GUEST])
        await t.receive(DiscordInbound(channel_id="dm", user_id=GUEST, text="hi", message_id="m"))
        assert [m.user_id for m in dispatched] == [GUEST]

    @pytest.mark.asyncio
    async def test_a_stranger_is_still_denied(self) -> None:
        t, dispatched, _ = self._transport(guests=[GUEST])
        await t.receive(DiscordInbound(channel_id="dm", user_id="x", text="hi"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_an_empty_guest_list_changes_nothing(self) -> None:
        t, dispatched, _ = self._transport()
        await t.receive(DiscordInbound(channel_id="dm", user_id=GUEST, text="hi"))
        await t.receive(DiscordInbound(channel_id="dm", user_id=OPERATOR, text="hi"))
        assert [m.user_id for m in dispatched] == [OPERATOR]

    @pytest.mark.asyncio
    async def test_a_guest_in_an_approved_thread_is_dispatched(self) -> None:
        t, dispatched, _ = self._transport(guests=[GUEST], threads=["t1"])
        await t.receive(
            DiscordInbound(channel_id="t1", user_id=GUEST, text="hi", guild_id="g", message_id="m")
        )
        assert [(m.user_id, m.thread_id) for m in dispatched] == [(GUEST, "t1")]

    @pytest.mark.asyncio
    async def test_a_guest_in_an_unapproved_thread_is_not_dispatched(self) -> None:
        t, dispatched, _ = self._transport(guests=[GUEST], threads=["t1"])
        await t.receive(DiscordInbound(channel_id="t9", user_id=GUEST, text="hi", guild_id="g"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_a_guest_in_an_approved_channel_gets_a_thread(self) -> None:
        t, dispatched, client = self._transport(guests=[GUEST], channels=["c1"])
        await t.receive(
            DiscordInbound(channel_id="c1", user_id=GUEST, text="hi", guild_id="g", message_id="m1")
        )
        assert client.created_threads == [("c1", "m1", "hi")]
        assert [(m.user_id, m.thread_id) for m in dispatched] == [(GUEST, "thread-m1")]

    @pytest.mark.asyncio
    async def test_an_operator_listed_as_guest_too_is_not_a_guest(self) -> None:
        t, _dispatched, client = self._transport(guests=[OPERATOR])
        assert t.is_guest(OPERATOR) is False
        assert client.guest_bot_ids == frozenset()

    @pytest.mark.asyncio
    async def test_guests_are_never_a_proactive_send_target(self) -> None:
        t, _d, _c = self._transport(guests=[GUEST])
        assert [x.target_id for x in t.configured_targets()] == [f"user:{OPERATOR}"]
        assert await t.resolve_configured_target(f"user:{GUEST}") is None
        assert t.may_send_to("dm", principal=GUEST) is False

    @pytest.mark.asyncio
    async def test_the_reply_to_a_guest_dm_is_a_permitted_destination(self) -> None:
        t, _d, client = self._transport(guests=[GUEST])
        client.cached_dm_recipient = lambda channel_id: GUEST  # type: ignore[attr-defined]
        assert t._still_may_send_to("dm").permitted is True

    @pytest.mark.asyncio
    async def test_a_removed_guest_is_no_longer_a_destination(self) -> None:
        t, _d, client = self._transport(guests=[GUEST])
        client.cached_dm_recipient = lambda channel_id: GUEST  # type: ignore[attr-defined]
        t.reconfigure(SimpleNamespace(guest_user_ids=[]))
        assert t._still_may_send_to("dm").permitted is False

    @pytest.mark.asyncio
    async def test_a_reload_updates_the_guests_and_the_clients_bot_list(self) -> None:
        t, dispatched, client = self._transport(guests=[GUEST])
        assert client.guest_bot_ids == frozenset({GUEST})
        t.reconfigure(
            SimpleNamespace(
                allowed_user_ids=[OPERATOR], guest_user_ids=["g2", OPERATOR], allowed_thread_ids=[]
            )
        )
        assert client.guest_bot_ids == frozenset({"g2"})
        await t.receive(DiscordInbound(channel_id="dm", user_id=GUEST, text="hi"))
        await t.receive(DiscordInbound(channel_id="dm", user_id="g2", text="hi"))
        assert [m.user_id for m in dispatched] == ["g2"]

    @pytest.mark.asyncio
    async def test_a_reload_without_the_field_or_with_a_bad_one_keeps_the_guests(self) -> None:
        t, _d, _c = self._transport(guests=[GUEST])
        t.reconfigure(SimpleNamespace(allowed_user_ids=[OPERATOR]))
        t.reconfigure(SimpleNamespace(allowed_user_ids=[OPERATOR], guest_user_ids="oops"))
        assert t.is_guest(GUEST)

    @pytest.mark.asyncio
    async def test_promoting_a_guest_to_operator_ends_the_guest_tier(self) -> None:
        t, _d, client = self._transport(guests=[GUEST])
        t.reconfigure(SimpleNamespace(allowed_user_ids=[OPERATOR, GUEST], guest_user_ids=[GUEST]))
        assert t.is_guest(GUEST) is False
        assert client.guest_bot_ids == frozenset()


class TestBotGuests:
    def _bot(self, text: str = "hi", **kw: Any) -> DiscordInbound:
        fields: dict[str, Any] = {
            "channel_id": "t1",
            "user_id": BOT,
            "text": text,
            "guild_id": "g",
            "message_id": "m",
            "is_bot": True,
        }
        fields.update(kw)
        return DiscordInbound(**fields)

    def _transport(self, now: list[float] | None = None, **kw: Any) -> Any:
        clock = now if now is not None else [100.0]
        guard = BotTurnGuard(cooldown=10.0, chain_cap=2, reset_after=600.0, clock=lambda: clock[0])
        return TestTransport()._transport(guests=[BOT], threads=["t1"], guard=guard, **kw), clock

    @pytest.mark.asyncio
    async def test_a_bot_that_does_not_address_us_is_ignored(self) -> None:
        (t, dispatched, _), _clock = self._transport()
        await t.receive(self._bot())
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_a_listed_bot_that_mentions_us_is_answered(self) -> None:
        (t, dispatched, _), _clock = self._transport()
        await t.receive(self._bot(mentions_bot=True))
        assert [m.user_id for m in dispatched] == [BOT]

    @pytest.mark.asyncio
    async def test_a_listed_bot_that_replies_to_us_is_answered(self) -> None:
        (t, dispatched, _), _clock = self._transport()
        await t.receive(self._bot(replies_to_bot=True))
        assert [m.user_id for m in dispatched] == [BOT]

    @pytest.mark.asyncio
    async def test_an_unlisted_bot_is_not_answered_even_when_it_mentions_us(self) -> None:
        (t, dispatched, _), _clock = self._transport()
        await t.receive(self._bot(user_id="stranger-bot", mentions_bot=True))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_a_bot_that_is_also_an_operator_is_not_answered(self) -> None:
        (t, dispatched, _), _clock = self._transport(allowed=[BOT])
        await t.receive(self._bot(mentions_bot=True))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_a_bot_cannot_start_a_turn_in_a_dm(self) -> None:
        (t, dispatched, _), _clock = self._transport()
        await t.receive(self._bot(guild_id="", mentions_bot=True))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_the_loop_guard_stops_a_bot_ping_pong_and_a_person_resets_it(self) -> None:
        (t, dispatched, _), clock = self._transport()
        for _ in range(4):
            await t.receive(self._bot(mentions_bot=True))
            clock[0] += 11.0
        assert len(dispatched) == 2, "the chain cap is 2 in this fixture"
        # A person speaking in that thread starts the budget over.
        await t.receive(DiscordInbound(channel_id="t1", user_id=OPERATOR, text="ok", guild_id="g"))
        await t.receive(self._bot(mentions_bot=True))
        assert [m.user_id for m in dispatched] == [BOT, BOT, OPERATOR, BOT]

    @pytest.mark.asyncio
    async def test_the_cooldown_refuses_a_burst(self) -> None:
        (t, dispatched, _), clock = self._transport()
        await t.receive(self._bot(mentions_bot=True))
        clock[0] += 1.0
        await t.receive(self._bot(mentions_bot=True))
        assert len(dispatched) == 1


# ── the dispatcher ───────────────────────────────────────────────────────────────


class _Answers:
    """Records what the shared pipeline was asked, in place of running a turn."""

    def __init__(self) -> None:
        self.calls: list[tuple[Asker, str, Any]] = []

    async def answer(
        self, _turns: ChannelTurns, asker: Asker, text: str, _renderer: Any, **kw: Any
    ) -> TurnOutcome:
        self.calls.append((asker, text, kw.get("decider")))
        return TurnOutcome(Verdict.ANSWERED, reply_text="ok", landed=True)


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> _Answers:
    rec = _Answers()
    monkeypatch.setattr(ChannelTurns, "answer", lambda self, *a, **kw: rec.answer(self, *a, **kw))
    return rec


def _both(**kw: Any) -> Any:
    return _dispatcher({OPERATOR}, guests={GUEST}, **kw)


def _guest_says(text: str, *, thread: str | None = None, chan: str = "dm") -> InboundMessage:
    return _inbound(text, user_id=GUEST, conversation_id=thread or chan, thread_id=thread)


class TestGuestTurns:
    @pytest.mark.asyncio
    async def test_a_guest_dm_runs_a_toolless_turn_in_its_own_session(
        self, answers: _Answers
    ) -> None:
        d, _cli, _sess = _both()
        await d.handle_message(_guest_says("hello"))
        ((asker, text, decider),) = answers.calls
        assert text == "hello"
        assert asker.audience is Audience.GUEST
        assert asker.session_key == f"discord:{TOOLLESS_TURN_AGENT}:direct:{GUEST}"
        assert asker.session_key != d._session_key(OPERATOR)
        assert asker.origin is None and decider is None
        assert asker.can_prompt is False

    @pytest.mark.asyncio
    async def test_a_guest_in_an_approved_thread_has_a_separate_session_key(
        self, answers: _Answers
    ) -> None:
        d, _cli, _sess = _dispatcher({OPERATOR}, allowed_threads={"t1"}, guests={GUEST})
        await d.handle_message(_guest_says("hello", thread="t1"))
        await d.handle_message(
            _inbound("hello", user_id=OPERATOR, conversation_id="t1", thread_id="t1")
        )
        (guest_asker, _t, _d1), (op_asker, _t2, op_decider) = answers.calls
        assert guest_asker.audience is Audience.GUEST and op_asker.audience is Audience.OWNER
        assert guest_asker.session_key == f"discord:{TOOLLESS_TURN_AGENT}:group:t1:guest"
        assert op_asker.session_key != guest_asker.session_key
        assert TOOLLESS_TURN_AGENT not in op_asker.session_key
        assert op_asker.origin is not None
        assert op_decider is not None, "the operator's turn keeps its approval decider"

    @pytest.mark.asyncio
    async def test_unified_dm_scope_never_pours_a_guest_into_the_operators_session(
        self, answers: _Answers
    ) -> None:
        d, _cli, _sess = _both(dm_scope="unified")
        await d.handle_message(_guest_says("hello"))
        await d.handle_message(_inbound("hello", user_id=OPERATOR, conversation_id="opdm"))
        (guest_asker, *_), (op_asker, *_) = answers.calls
        assert op_asker.session_key.startswith("unified:")
        assert guest_asker.session_key == f"discord:{TOOLLESS_TURN_AGENT}:direct:{GUEST}"

    @pytest.mark.asyncio
    async def test_a_user_in_both_lists_runs_as_the_operator(self, answers: _Answers) -> None:
        d, _cli, _sess = _dispatcher({OPERATOR}, guests={OPERATOR})
        await d.handle_message(_inbound("hello", user_id=OPERATOR, conversation_id="opdm"))
        ((asker, *_),) = answers.calls
        assert asker.audience is Audience.OWNER
        assert TOOLLESS_TURN_AGENT not in asker.session_key

    @pytest.mark.asyncio
    async def test_the_operators_turn_is_unchanged_by_an_empty_guest_list(
        self, answers: _Answers
    ) -> None:
        d, _cli, _sess = _dispatcher({OPERATOR})
        await d.handle_message(_inbound("hello", user_id=OPERATOR, conversation_id="opdm"))
        ((asker, *_),) = answers.calls
        assert asker.audience is Audience.OWNER
        assert asker.session_key == d._session_key(OPERATOR)
        assert asker.origin is not None

    @pytest.mark.asyncio
    async def test_a_guest_never_routes_into_a_resumed_dashboard_session(
        self, answers: _Answers
    ) -> None:
        d, _cli, _sess = _both()
        routed: list[str] = []

        async def _route(channel_id: str) -> Any:
            routed.append(channel_id)
            return SimpleNamespace(refusal=None, resumed_key="dashboard:operators")

        d._session_resume.route = _route  # type: ignore[method-assign]
        await d.handle_message(_guest_says("hello"))
        ((asker, *_),) = answers.calls
        assert routed == []
        assert asker.resumed is False and asker.session_key.startswith("discord:")

    @pytest.mark.asyncio
    async def test_guests_do_not_make_the_sessions_owner_ambiguous(self) -> None:
        d, _cli, _sess = _both()
        assert d._session_resume.owner_id == OPERATOR

    @pytest.mark.asyncio
    async def test_a_guest_scope_has_its_own_generation_and_agent(self) -> None:
        d, _cli, _sess = _both()
        assert d._resolve_agent("guest:user:g1") == TOOLLESS_TURN_AGENT
        before = d._session_key(GUEST)
        d._conv.bump_gen("guest:user:g1")
        assert d._session_key(GUEST) == before + ":gen1"
        assert d._session_key(OPERATOR) == _both()[0]._session_key(OPERATOR)

    @pytest.mark.asyncio
    async def test_a_guest_gets_no_context_nudge_naming_operator_commands(self) -> None:
        d, cli, sess = _both()
        sess.check_context_usage = lambda key, provider: 99.0  # type: ignore[method-assign]
        await d._maybe_notice("dm", "guest:user:g1", d._session_key(GUEST), object())
        assert cli.sent == []

    @pytest.mark.asyncio
    async def test_a_reload_moves_users_between_the_tiers(self, answers: _Answers) -> None:
        d, _cli, _sess = _both()
        d.reconfigure(
            SimpleNamespace(
                allowed_user_ids=[OPERATOR, GUEST], guest_user_ids=[GUEST], allowed_thread_ids=[]
            )
        )
        await d.handle_message(_guest_says("hello"))
        ((asker, *_),) = answers.calls
        assert asker.audience is Audience.OWNER
        d.reconfigure(
            SimpleNamespace(allowed_user_ids=[OPERATOR], guest_user_ids=[], allowed_thread_ids=[])
        )
        assert d._guest_listed == set()


_REFUSED_TEXT = [
    "!agent default",
    "!agent",
    "!model",
    "!models",
    "!new",
    "!start",
    "!compact",
    "!sessions",
    "!session foo",
    "!link",
    "!unlink",
    "!stop",
    "!cancel",
    "!status",
    "/agent default",
    "/model",
    "/new",
    "/sessions",
    "/status",
    "/stop",
]


class TestGuestCommands:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", _REFUSED_TEXT)
    async def test_a_guest_cannot_run_an_operator_command(
        self, text: str, answers: _Answers
    ) -> None:
        d, cli, sess = _both()
        key_before = d._session_key(GUEST)
        op_key_before = d._session_key(OPERATOR)
        await d.handle_message(_guest_says(text))
        assert [t for t, _c in cli.sent] == [GUEST_REFUSAL]
        assert answers.calls == [], "the command text must not reach the model either"
        assert d._session_key(GUEST) == key_before and d._session_key(OPERATOR) == op_key_before
        assert d._agent_picks.resolve("user:op1", "x") == "x"
        assert d._model_pref == {}
        assert sess.acquired == [] and sess.destroyed == []

    @pytest.mark.asyncio
    async def test_a_guest_help_card_hides_the_operator_commands(self, answers: _Answers) -> None:
        d, cli, _sess = _both()
        await d.handle_message(_guest_says("!help"))
        assert [t for t, _c in cli.sent] == [guest_help_text()]
        assert answers.calls == []

    @pytest.mark.asyncio
    async def test_a_mistyped_command_does_not_leak_the_operator_card(
        self, answers: _Answers
    ) -> None:
        d, cli, _sess = _both()
        await d.handle_message(_guest_says("!sesions"))
        text, _c = cli.sent[-1]
        assert "isn't a command" in text and "!agent" not in text
        assert answers.calls == []

    @pytest.mark.asyncio
    async def test_an_operator_command_still_works_for_the_operator(self) -> None:
        d, cli, _sess = _both()
        await d.handle_message(_inbound("!help", user_id=OPERATOR, conversation_id="opdm"))
        assert "!agent" in cli.sent[-1][0]

    @pytest.mark.asyncio
    async def test_a_command_is_not_read_in_a_queued_replay_either(self, answers: _Answers) -> None:
        d, cli, _sess = _both()
        await d.handle_message(_guest_says("!agent default"), interpret_commands=False)
        ((asker, text, _d),) = answers.calls
        assert text == "!agent default" and asker.audience is Audience.GUEST
        assert cli.sent == []


def _itx(user: str, *, custom_id: str = "", command: str = "", chan: str = "dm", **kw: Any) -> Any:
    return DiscordInteraction(
        interaction_id="i1",
        interaction_token="tok",
        channel_id=chan,
        user_id=user,
        message_id="m1",
        custom_id=custom_id,
        kind=2 if command else 3,
        command_name=command,
        **kw,
    )


class TestGuestInteractions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["agent", "model", "new", "sessions", "link", "status"])
    async def test_a_guest_slash_command_is_refused_ephemerally(
        self, command: str, answers: _Answers
    ) -> None:
        d, cli, _sess = _both()
        await d.on_interaction(_itx(GUEST, command=command))
        assert cli.responses == [("i1", GUEST_REFUSAL, True)]
        assert answers.calls == []

    @pytest.mark.asyncio
    async def test_a_guest_slash_help_is_the_guest_card(self) -> None:
        d, cli, _sess = _both()
        await d.on_interaction(_itx(GUEST, command="help"))
        assert cli.responses == [("i1", guest_help_text(), True)]

    @pytest.mark.asyncio
    async def test_a_guest_cannot_approve_an_operators_tool(self) -> None:
        d, cli, _sess = _both()
        key = DiscordApprovalDecider.key(d._session_key(OPERATOR), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(_itx(GUEST, custom_id=f"a:r1:{nonce}:1"))
            await d.on_interaction(_itx(GUEST, custom_id=f"a:r1:{nonce}:0"))
            assert not fut.done(), "neither Approve nor Deny from a guest resolves anything"
            assert cli.responses == [("i1", GUEST_BUTTON_REFUSAL, True)] * 2
            assert cli.edits == []
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_a_guest_cannot_approve_in_a_thread_the_operators_turn_runs_in(self) -> None:
        d, cli, _sess = _dispatcher({OPERATOR}, allowed_threads={"t1"}, guests={GUEST})
        cli.thread_channels.add("t1")
        key = DiscordApprovalDecider.key(d._session_key(OPERATOR, "t1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(
                _itx(GUEST, custom_id=f"a:r1:{nonce}:1", chan="t1", guild_id="g")
            )
            assert not fut.done()
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("custom_id", ["s:abc:0", "m:0", "x:what"])
    async def test_a_guest_cannot_press_a_picker_or_anything_unknown(self, custom_id: str) -> None:
        d, cli, _sess = _both()
        await d.on_interaction(_itx(GUEST, custom_id=custom_id))
        assert cli.responses == [("i1", GUEST_BUTTON_REFUSAL, True)]
        assert cli.acked == []

    @pytest.mark.asyncio
    async def test_a_guest_may_press_a_choice_under_its_own_reply(self, answers: _Answers) -> None:
        d, cli, _sess = _both()
        tag = session_provenance_tag(d._session_key(GUEST))
        await d.on_interaction(_itx(GUEST, custom_id=f"opt:0:{tag}", label="Yes"))
        ((asker, text, _d),) = answers.calls
        assert text == "Yes" and asker.audience is Audience.GUEST

    @pytest.mark.asyncio
    async def test_a_guest_cannot_inject_a_choice_into_the_operators_session(
        self, answers: _Answers
    ) -> None:
        d, cli, _sess = _both()
        tag = session_provenance_tag(d._session_key(OPERATOR))
        await d.on_interaction(_itx(GUEST, custom_id=f"opt:0:{tag}", label="rm -rf"))
        assert answers.calls == []
        assert any("NOT applied" in text for text, _c in cli.sent)

    @pytest.mark.asyncio
    async def test_an_unknown_user_is_still_ignored(self) -> None:
        d, cli, _sess = _both()
        await d.on_interaction(_itx("stranger", command="help"))
        assert cli.responses == [] and cli.acked == []


class TestWiring:
    def test_the_config_field_round_trips_and_defaults_empty(self) -> None:
        from kiro_crew.config.section_builders import _build_discord_config
        from kiro_crew.config.sections_channels import DiscordConfig

        assert DiscordConfig().guest_user_ids == []
        built = _build_discord_config({"guest_user_ids": [123456789012345678, " 42 ", "", None]})
        assert built.guest_user_ids == ["123456789012345678", "42"]

    @pytest.mark.asyncio
    async def test_a_guest_turn_is_refused_on_a_backend_that_cannot_run_it(self) -> None:
        from kiro_crew.messaging.dispatch import (
            toolless_turns_supported,
            warn_if_toolless_turns_unservable,
        )

        assert toolless_turns_supported(ACP_BACKEND_KIRO) is True
        assert (
            warn_if_toolless_turns_unservable(
                "discord",
                admits_non_operators=True,
                backend="no-such-backend",
                admission="guest_user_ids=1",
            )
            is True
        )
        assert (
            warn_if_toolless_turns_unservable(
                "discord",
                admits_non_operators=False,
                backend="no-such-backend",
                admission="guest_user_ids=0",
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_inbound_message_carries_no_guest_flag_the_roster_decides(self) -> None:
        # The tier is read from the dispatcher's own roster, never from a field on the
        # message, so a synthetic replay cannot claim to be an operator.
        assert not hasattr(DiscordInboundMessage, "guest")


class TestRealPipeline:
    """The same turn through the real shared pipeline, with the Discord fakes."""

    @pytest.fixture(autouse=True)
    def _spec_reads_toolless(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        import kiro_crew.messaging.dispatch as pipeline

        monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws"))
        monkeypatch.setattr(pipeline, "_toolless_spec_before_spawn", lambda _key: True)

        async def _confirmed(*_a: Any, **_kw: Any) -> None:
            return None

        monkeypatch.setattr(pipeline, "_refuse_unless_toolless", _confirmed)

    @pytest.mark.asyncio
    async def test_a_guest_dm_acquires_the_toolless_agent(self) -> None:
        d, _cli, sess = _both()
        await d.handle_message(_guest_says("hello"))
        assert sess.last_agent == TOOLLESS_TURN_AGENT
        assert sess.last_extra.get("crew_agent") == ""
        assert sess.last_extra.get("cwd"), "a tool-less turn runs in its own directory"

    @pytest.mark.asyncio
    async def test_the_operator_keeps_the_configured_agent(self) -> None:
        d, _cli, sess = _both(default_agent="agentA")
        await d.handle_message(_inbound("hello", user_id=OPERATOR, conversation_id="opdm"))
        assert sess.last_agent == "agentA"

    @pytest.mark.asyncio
    async def test_a_guest_turn_is_refused_when_the_backend_cannot_serve_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.messaging.dispatch as pipeline
        from kiro_crew.messaging.dispatch_tools import ToollessTurnUnavailable

        async def _refuse(*_a: Any, **_kw: Any) -> None:
            raise ToollessTurnUnavailable("backend cannot mount the guest spec")

        monkeypatch.setattr(pipeline, "_refuse_unless_toolless", _refuse)
        d, cli, sess = _both()
        await d.handle_message(_guest_says("hello"))
        assert any(TOOLLESS_TURN_REFUSAL_NOTE in text for text, _c in cli.sent)
        assert sess.failures == [], "a refusal is not a session failure"
