"""Steer decisions on the send path: the message-steer choice and the person's
steer into a Crew Mode worker's own composer.

``steer_is_auto`` / ``decided_message_handling`` read a send's ``steer`` flag and,
for a person's send into a running turn, ask the optional message-steer seam whether
to queue. The crew-steer helpers hold a person's steer record until the worker's
running turn reads it, move it onto the queue entry when the turn requeues it, and
drop it when the person takes it back, so a withdrawn steer is never on the ledger.
Split out of ``chat_api.chat_send`` to keep that file under the line cap; it runs on
the ``chat_handlers`` facade globals like every other ``chat_api`` owner, so
``chat_send.mark_crew_steer`` and ``chat_handlers.mark_crew_steer`` both still reach
these.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        STEER_AUTO,
        effective_session_key,
        logger,
    )


def steer_is_auto(value: object) -> bool:
    """Whether a send's ``steer`` flag asks Jev to choose the path.

    Only the exact string, case- and space-insensitively. A boolean ``True`` is a
    MANUAL steer and must never read as auto: that flag is what every existing
    client sends, and reading it as a request to decide would put an oracle on a
    path the sender already answered.
    """
    return isinstance(value, str) and value.strip().lower() == STEER_AUTO


async def decided_message_handling(slot: Any, message: str) -> tuple[bool, dict | None]:
    """Ask ``message.steer`` whether *message* queues instead of steering.

    Returns ``(queues, record)``: whether to take the QUEUE path, and the decision
    row to stamp on the persisted user row (``None`` when nothing was decided, or
    when the row itself was refused).

    A BOOLEAN rather than the point's choice string, so the caller holds no copy of
    that vocabulary and cannot drift from it: every refusal -- the seam off, the
    session unsampled, a timeout, an answer outside the two options, a failed
    import -- is ``False``, which is the steer path a manual Steer and the
    composer's default have always taken.

    Called for a send the dashboard's own human made while a turn is running, and
    nowhere else (the busy branch's ``_person_send`` conjunct): an app token, an
    integration or a cron has nobody watching the reply, and every such send
    already falls through to the fail-closed queue; an agent-origin send takes the
    manual steer path as a non-person steer. Consent and sampling are the
    seam's own gates, re-checked inside ``decide``.

    Never raises except cancellation. The seam may cost an observation and must
    never cost a send.
    """
    try:
        # Imported HERE, not at module scope: this module is on the gateway's boot
        # path and the decisions package is optional, off by default, and pulls the
        # config loader in behind it.
        from kiro_crew.decisions.points import message_steer

        session_key = effective_session_key(slot)
        decided = await message_steer.steer_or_queue(
            message,
            session_key=session_key,
            # The live list, sliced and read off the loop by the point; nothing is
            # written back.
            rows=getattr(slot, "messages", ()) or (),
        )
        if decided is None:
            return False, None
        # The row is written BEFORE the path is taken, and the receipt is stamped
        # only when it landed: the strip's thumbs POST this turn id, so a receipt
        # from a row `append` refused would invite a verdict about a decision the
        # log does not hold. Off the loop -- the append locks a file.
        record = await asyncio.to_thread(message_steer.record_outcome, session_key, decided)
        return decided.get("choice") == message_steer.CHOICE_QUEUE, record
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("message.steer: taking the shipped default", exc_info=True)
        return False, None


#: Request key the send path stamps when the person typed into a Crew Mode worker:
#: ``(root, worker, message)``, read by :func:`api_chat` once the send succeeded.
_CREW_PERSON_STEER = "kirocrew_crew_person_steer"

#: Queue-entry key naming ``(root, worker)`` on a person's queued send into a
#: Crew Mode worker; the drain records the steer when it delivers that entry.
CREW_STEER_QUEUE_KEY = "_crew_person_steer"


def mark_crew_steer(entry: dict[str, Any], root: str, worker: str) -> None:
    """Mark queue *entry* as the person's steer into *worker* of Crew Mode *root*.

    In memory only, like a held steer: an entry a restart restores comes off a
    writable file, so it is still delivered but never recorded as the person's.
    """
    entry[CREW_STEER_QUEUE_KEY] = (root, worker)


#: A person's steer written into a crew member's running turn and not yet read by
#: it: slot key -> steer text -> ``(root, worker)``. Recorded on the ledger when the
#: turn reads it (:func:`crew_steer_consumed`), moved onto the queue entry when the
#: turn's end requeues it (:func:`crew_steer_requeued`), and dropped when it is taken
#: back or discarded (:func:`crew_steer_discarded`), so a withdrawn steer is never
#: on the ledger. In memory only: a restart loses the record, never adds one.
_HELD_CREW_STEERS: dict[str, dict[str, tuple[str, str]]] = {}


def hold_crew_steer(slot_key: str, message: str, root: str, worker: str) -> bool:
    """Hold *message*'s record until the running turn reads it (see above).

    ``True`` only when this call made the hold. An earlier unread steer of the same
    text keeps its hold, and the caller must not drop it when its own attempt fails
    (the duplicate is refused, but the first one is still read and recorded).
    """
    held = _HELD_CREW_STEERS.setdefault(slot_key, {})
    if message in held:
        return False
    held[message] = (root, worker)
    return True


def _take_held(slot_key: str, message: str) -> tuple[str, str] | None:
    held = _HELD_CREW_STEERS.get(slot_key)
    if not held:
        return None
    marker = held.pop(message, None)
    if not held:
        _HELD_CREW_STEERS.pop(slot_key, None)
    return marker


def crew_steer_consumed(state: Any, slot: Any, message: str) -> None:
    """The running turn read *message*: record the held steer, off the turn's path."""
    marker = _take_held(getattr(slot, "key", ""), message)
    if marker is None:
        return
    try:
        from kiro_crew.dashboard.handlers import crew_workers

        task = asyncio.get_running_loop().create_task(
            crew_workers.person_steer(state, marker[0], marker[1], message)
        )
        tasks = getattr(state, "_background_tasks", None)
        if isinstance(tasks, set):
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except Exception:
        logger.warning("crew steer: a read steer was not recorded", exc_info=True)


def crew_steer_requeued(slot: Any, message: str, entry: dict[str, Any] | None) -> None:
    """*message* went back into the queue: the drain records it when it delivers it."""
    marker = _take_held(getattr(slot, "key", ""), message)
    if marker is not None and entry is not None:
        mark_crew_steer(entry, *marker)


def crew_steer_discarded(slot: Any, message: str) -> None:
    """*message* was taken back or thrown away unread: it is never recorded."""
    _take_held(getattr(slot, "key", ""), message)


def _person_crew_worker_root(request: web.Request, state: Any, slot: Any, app: str) -> str:
    """The Crew Mode root *slot* works for when the dashboard owner typed into it, else ``""``.

    Only the person's own send counts: no app token, the owner's dashboard request,
    and a slot the slot list stamps as some root's worker. ``""`` for every send
    while ``crew_mode.APPROVAL_ROUTING_LIVE`` is off (the Crew panel part turns it
    on), so a send typed into a crew member's own composer is an ordinary send until
    then. No I/O.
    """
    if app:
        return ""
    try:
        from kiro_crew import crew_mode

        if not crew_mode.APPROVAL_ROUTING_LIVE:
            return ""
        from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

        if not is_owner_dashboard_request(request):
            return ""
        from kiro_crew.dashboard.session_control_parts.crew_filing import _frame_parents

        # The lineage the slot frame reads: a session its conductor released is a
        # plain session again, and an adopted one belongs to its new conductor.
        crew_root, _is_root = crew_mode.live_stamps(
            state._slots, _frame_parents(state), settled=crew_mode.lineage_is_settled()
        ).get(slot.key, ("", False))
        return crew_root
    except Exception:
        logger.debug("crew steer: could not tell whether this send is to a worker", exc_info=True)
        return ""
