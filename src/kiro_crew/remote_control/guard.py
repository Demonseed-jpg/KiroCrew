"""Keep the running relay in step with the Remote Control setting and the sign-in.

``tunnel.relay_enabled`` picks the relay client at boot, so the field is marked
restart-only. Turning Remote Control OFF must not wait for a restart, and neither
may a sign-in that stops being allowed. :class:`RemoteControlGuard` runs in the
gateway beside the tunnel manager and, every :data:`POLL_SECONDS`:

* stops the relay through :meth:`TunnelManager.stop` whenever it is running but
  the setting is off or the sign-in is refused (level-triggered, so a stop that
  failed is retried);
* starts it again through :meth:`TunnelManager.start` only on the tick the inputs
  CHANGE to "on and allowed" (edge-triggered, so a relay that cannot start is not
  retried every few seconds, and a stop the gateway made at shutdown is never
  undone).

Starting is only ever done through the tunnel manager the gateway built after
``tunnel/setup.py`` passed its token-auth and ``--no-tunnel`` gates, so the guard
cannot open a path those gates refused. It does nothing when the relay was not
selected at boot: there is no relay provider to start, and turning Remote Control
on then takes a restart.

The sign-in is re-read only when the vault's sign-in or sign-out marker moves
(:func:`~kiro_crew.remote_control.policy.sign_in_generation`); a token refresh
moves neither. Every file read runs off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Callable, Iterator, NamedTuple, Optional

from kiro_crew.remote_control.policy import (
    Decision,
    audit_refusal,
    current_decision_with_generation,
    sign_in_generation,
)

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0

#: The longest a one-click off waits for the relay's open HTTP requests to finish
#: before it stops the relay anyway. The off request itself may have come through the
#: relay, and its answer must reach the browser before the relay closes.
DRAIN_SECONDS = 5.0
_DRAIN_POLL_SECONDS = 0.05

#: How many times one tick redoes a sign-in read whose markers moved under it.
_SETTLE_ATTEMPTS = 2


class _Probe(NamedTuple):
    """A sign-in read made outside the lock, with the markers it was made under."""

    decision: Decision
    #: The markers the vault read reported with the verdict (the cache key).
    locked: tuple[str, ...]
    #: The markers read just before it, to tell whether a sign-in landed during it.
    before: tuple[str, ...]


#: ``ensure_available()`` answers this when no relay client is running.
_NOT_RUNNING = "disabled"
#: ...and this only while the client is connected to the relay.
_CONNECTED = "connected"


def _relay_http_idle(provider: Any) -> bool:
    """Whether no relayed HTTP request is still open on the relay client.

    Reads the relay client's connections through their ``http_streams_idle`` event,
    which a connection clears when a request arrives and sets once every request's
    answer has gone back to the relay. A provider without a running client is idle.
    """
    client = getattr(provider, "_client", None)
    for conn in list(getattr(client, "_live", None) or ()):
        idle = getattr(conn, "http_streams_idle", None)
        if idle is not None and not idle.is_set():
            return False
    return True


def _live_requested() -> bool:
    """The setting as the live config watcher last applied it (cheap, may lag a write)."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    snap = live.snapshot()
    cfg = snap if snap is not None else KiroCrewConfig.load()
    return cfg.tunnel.relay_enabled is True


def _disk_requested() -> bool:
    """The setting as stored on disk now (a full config load)."""
    from kiro_crew.remote_control.setting import requested

    return requested()


class RemoteControlGuard:
    """Stops and restarts the relay so it runs only while on and allowed."""

    def __init__(
        self,
        manager: Any,
        provider: Any,
        *,
        requested: Optional[Callable[[], bool]] = None,
        stored: Optional[Callable[[], bool]] = None,
        decide: Optional[Callable[[], tuple[Decision, tuple[str, ...]]]] = None,
        generation: Optional[Callable[[], tuple[str, ...]]] = None,
        poll_seconds: float = POLL_SECONDS,
        idle: Optional[Callable[[], bool]] = None,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        self._manager = manager
        self._provider = provider
        self._requested = requested or _live_requested
        self._stored = stored or _disk_requested
        self._decide = decide or (lambda: current_decision_with_generation(team=_team(provider)))
        self._generation = generation or sign_in_generation
        self._poll = poll_seconds
        self._task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self._gen: Optional[tuple[str, ...]] = None
        self._decision: Optional[Decision] = None
        self._last_effective: Optional[bool] = None
        #: Set by :meth:`apply`: read the stored setting from disk, not the live
        #: snapshot, until the snapshot agrees with disk again.
        self._read_disk = False
        self._idle = idle or (lambda: _relay_http_idle(provider))
        self._drain = drain_seconds
        #: The pending :meth:`stop_after_drain` task. While it runs, the poll leaves
        #: the relay alone, so it cannot close the connection under an open answer.
        self._deferred: Optional[asyncio.Task[None]] = None
        self._stop_reservations = 0
        set_start_gate = getattr(provider, "set_start_gate", None)
        if callable(set_start_gate):
            set_start_gate(self._start_wanted)

    @property
    def decision(self) -> Optional[Decision]:
        return self._decision

    async def _probe(self) -> Optional[_Probe]:
        """Read the sign-in if the cached verdict is stale. Runs OUTSIDE the lock.

        The read may be ``kiro-cli whoami`` (up to 15 s), so holding the lock across
        it would make a one-click off wait for it. Returns ``None`` when the cache is
        fresh. Nothing is cached here: :meth:`_settle` commits the result under the
        lock, and only if the markers did not move while the read ran.
        """
        before = await asyncio.to_thread(self._generation)
        if self._decision is not None and before == self._gen:
            return None
        decision, locked = await asyncio.to_thread(self._decide)
        return _Probe(decision, locked, before)

    async def _settle(self, probe: Optional[_Probe]) -> Optional[Decision]:
        """Under the lock: the verdict to act on, or ``None`` to probe again.

        The cheap marker read decides whether *probe* still describes the sign-in.
        A verdict is cached under the markers the vault read reported WITH it, so a
        sign-in landing between the marker read and the vault read is seen on the
        next tick; a sign-in or sign-out landing while the probe ran discards it.
        """
        now = await asyncio.to_thread(self._generation)
        if probe is None:
            if self._decision is not None and now == self._gen:
                return self._decision
            return None
        if now != probe.before:
            return None
        self._decision, self._gen = probe.decision, probe.locked
        return probe.decision

    async def _peek_requested(self) -> bool:
        """The setting as :meth:`_read_requested` would read it, without side effects."""
        return await asyncio.to_thread(self._stored if self._read_disk else self._requested)

    async def _read_requested(self) -> bool:
        live_value = await asyncio.to_thread(self._requested)
        if not self._read_disk:
            return live_value
        stored = await asyncio.to_thread(self._stored)
        if stored == live_value:
            self._read_disk = False
        return stored

    async def running(self) -> bool:
        """Whether a relay client exists now, connected, starting or failed.

        The stop decision reads this, so a client that ended in error is still
        cleaned up. Whether other devices can reach this Crew is :meth:`connected`.
        """
        state = await self._provider.ensure_available(install=False)
        return state != _NOT_RUNNING

    async def connected(self) -> bool:
        """Whether the relay client is connected now, so other devices can reach Crew."""
        state = await self._provider.ensure_available(install=False)
        return state == _CONNECTED

    async def tick(self) -> None:
        """Read the setting and the sign-in once and bring the relay in line.

        The sign-in read (on the kiro-cli route a `kiro-cli whoami` of up to 15 s)
        runs before the lock is taken, so it never delays another tick, and in
        particular never delays the stop a one-click off asks for. Under the lock
        the setting is read again and the probe is used only if the markers held
        still; otherwise the probe is redone, at most :data:`_SETTLE_ATTEMPTS`
        times, after which this tick leaves the relay as it is for the next poll.
        """
        for _ in range(_SETTLE_ATTEMPTS):
            probe = await self._probe() if await self._peek_requested() else None
            async with self._lock:
                requested = await self._read_requested()
                # Off decides on its own, without a sign-in verdict.
                decision = await self._settle(probe) if requested else None
                if requested and decision is None:
                    continue
                start = await self._act(requested, decision)
            if start:
                await self._start_outside_lock()
            return
        logger.info("Remote Control guard: the sign-in kept changing; deciding on the next poll")

    async def _act(self, requested: bool, decision: Optional[Decision]) -> bool:
        """Bring the relay in line with *requested* and *decision*. Holds the lock.

        Returns whether the relay should be started; the caller starts it after
        releasing the lock (:meth:`_start_outside_lock`).
        """
        effective = requested and decision is not None and decision.ok
        changed = self._last_effective is not None and effective != self._last_effective
        first = self._last_effective is None
        self._last_effective = effective
        running = await self.running()
        if not effective and running and self.stop_pending:
            return False  # stop_after_drain stops it once the open answers are out
        if not effective and running:
            refused = decision if requested and decision is not None else None
            logger.info(
                "Remote Control stopping the relay (%s)",
                refused.reason if refused is not None else "turned off",
            )
            if refused is not None:
                await asyncio.to_thread(audit_refusal, "tunnel.stop_enforced", refused)
            await self._manager.stop()
            return False
        return effective and not running and changed and not first

    def _start_wanted(self) -> bool:
        """Whether a start the relay is running now is still wanted (the provider asks).

        False once an off is reserved, draining or applied, so the provider builds
        no client for a start that an off overtook during its sign-in read. Before
        the first tick has judged the inputs it leaves the start to the provider.
        """
        return self._last_effective is not False and not self.stop_pending

    async def _start_outside_lock(self) -> None:
        """Start the relay without the lock, then undo the start if off landed meanwhile.

        ``manager.start()`` re-reads the sign-in (the provider's own start-time
        refusal, a `kiro-cli whoami` of up to 15 s on the kiro-cli route), so under
        the lock it would hold a one-click off past :data:`DRAIN_SECONDS`. An off
        that arrives while it runs finds no relay to stop; this stops the one the
        start brings up, unless a pending drain is about to.
        """
        logger.info("Remote Control starting the relay")
        await self._manager.start()
        async with self._lock:
            if self.stop_pending or await self._read_requested():
                return
            if await self.running():
                logger.info("Remote Control stopping the relay (turned off while it started)")
                await self._manager.stop()

    async def apply(self) -> None:
        """Act on a write this process just made, without waiting for the poll.

        Reads the stored setting rather than taking the written value, so two
        writes racing each other end on whichever one landed last. Forces a fresh
        read of the sign-in too, so a sign-in finished a moment ago is not judged
        by the cached verdict.
        """
        self._read_disk = True
        self._gen = None
        await self.tick()

    @contextmanager
    def reserve_stop(self) -> Iterator[None]:
        """Keep polling from closing an off request while its write is in flight.

        Reserve before persistence can publish false. On success the caller starts
        the drain before releasing; on failure polling resumes. Count overlapping
        writes so one request cannot release another request's reservation.
        """
        self._stop_reservations += 1
        try:
            yield
        finally:
            self._stop_reservations -= 1

    @property
    def stop_pending(self) -> bool:
        """Whether an off write or its response drain is still pending."""
        return self._stop_reservations > 0 or (
            self._deferred is not None and not self._deferred.done()
        )

    def stop_after_drain(self) -> None:
        """Act on a turn-off this process just wrote, after its answer is sent.

        A one-click off can arrive through the relay itself, and stopping the relay
        closes the connection that carries the answer. So this returns at once and
        a background task waits until the relay has no open HTTP request (the off
        request's own answer included), for at most :data:`DRAIN_SECONDS`, then runs
        :meth:`apply`. A request that did not come through the relay leaves nothing
        open, so the stop follows straight away.
        """
        if self._deferred is not None and not self._deferred.done():
            return
        self._deferred = asyncio.get_running_loop().create_task(
            self._drain_then_apply(), name="remote-control-off"
        )

    async def _drain_then_apply(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self._drain, 0.0)
        # Yield once first, so the handler that scheduled this returns its answer
        # before the idle check can see the relay idle.
        await asyncio.sleep(0)
        while loop.time() < deadline:
            try:
                # On the loop, not a thread: it reads asyncio events, and is cheap.
                if self._idle():
                    break
            except Exception:  # noqa: BLE001 - an unreadable idle state ends the wait
                logger.debug("Remote Control drain check failed", exc_info=True)
                break
            await asyncio.sleep(_DRAIN_POLL_SECONDS)
        else:
            logger.info("Remote Control off: open requests outlived the drain; stopping anyway")
        # Cleared before apply(), so its tick is not skipped as "stop pending".
        self._deferred = None
        try:
            await self.apply()
        except Exception:  # noqa: BLE001 - the poll retries a stop that failed
            logger.warning("Remote Control off: stopping the relay failed", exc_info=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end the guard
                logger.warning("Remote Control guard tick failed", exc_info=True)
            await asyncio.sleep(self._poll)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._loop(), name="remote-control-guard"
            )

    async def stop(self) -> None:
        for task in (self._task, self._deferred):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._deferred = None


#: The gateway's guard, set when the relay was selected at boot. One per process.
_guard: Optional[RemoteControlGuard] = None


def install(manager: Any, provider: Any) -> RemoteControlGuard:
    """Create, start and publish the gateway's guard. Called once, on the loop."""
    global _guard
    guard = RemoteControlGuard(manager, provider)
    guard.start()
    _guard = guard
    return guard


def _team(provider: Any) -> Optional[bool]:
    """Whether the running relay leases as a team Crew, from its own fixed settings.

    A config edit while the relay runs does not change how it leases, so it must
    not change which rule judges it. ``None`` (read ``config.json``) only for a
    provider with no relay settings.
    """
    settings = getattr(provider, "settings", None)
    team = getattr(settings, "is_team_crew", None)
    return team if isinstance(team, bool) else None


def install_for_relay(manager: Any, provider: Any) -> Optional[RemoteControlGuard]:
    """:func:`install`, but only when *provider* is the relay client's provider.

    An edition's own tunnel provider is not Remote Control and is left alone.
    """
    from kiro_crew.relay_client.provider import RelayTunnelProvider

    if not isinstance(provider, RelayTunnelProvider):
        return None
    return install(manager, provider)


def current_guard() -> Optional[RemoteControlGuard]:
    return _guard


async def uninstall() -> None:
    """Stop the guard first at shutdown, so it cannot restart what shutdown stops."""
    global _guard
    guard, _guard = _guard, None
    if guard is not None:
        await guard.stop()
