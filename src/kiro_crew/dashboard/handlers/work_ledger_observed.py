"""``POST /api/work-ledger/observed`` -- the ``accept_eval`` tool's pull-request verdicts.

The ``kirocrew-work`` MCP server runs ``accept_eval`` in its own process, so the
verdicts it computes (``gh pr checks`` / ``gh pr view``, never model text) reach the
crew log through this one route: the gateway appends one ``object/observed`` entry
per pull request, ``producer=accept_eval``, into the CALLER's own crew log unit. The
``objects`` slot fold then serves them to the Ultra Mode Board and goal-board as the
checker's own dated line, at no extra GitHub call. They never decide a PR's state:
that comes only from the warm welcome's own read (``warm_brief``).

MCP-only like every ``/api/work-ledger`` route (strict internal path, internal-secret
principal, the same session gate through :func:`work_ledger_caller._caller_key`), and
only for a caller that holds a work ledger. Every value is re-checked here: a repo of
the form ``owner/name``, a positive pull-request number, an acceptance kind of
``pr_checks`` or ``pr_merged`` and a verdict of ``pass``, ``fail`` or ``pending``.
Anything else in the batch is skipped, never stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

#: Pull requests one call may record; an evaluation batch holds a few dozen items.
MAX_OBSERVED = 32
OBSERVED_KINDS = ("pr_checks", "pr_merged")
OBSERVED_VERDICTS = ("pass", "fail", "pending")
_REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


def observations(raw: object) -> list[dict[str, Any]]:
    """The valid rows of *raw* (``{"observed": [...]}``), at most :data:`MAX_OBSERVED`."""
    rows = raw.get("observed") if isinstance(raw, dict) else None
    kept: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        repo, pr = row.get("repo"), row.get("pr")
        kind, verdict = row.get("kind"), row.get("verdict")
        if (
            isinstance(repo, str)
            and _REPO_RE.match(repo)
            and isinstance(pr, int)
            and not isinstance(pr, bool)
            and 0 < pr < 10**9
            and kind in OBSERVED_KINDS
            and verdict in OBSERVED_VERDICTS
        ):
            kept.append({"repo": repo, "pr": pr, "kind": kind, "verdict": verdict})
        if len(kept) >= MAX_OBSERVED:
            break
    return kept


def _record(unit: str, rows: list[dict[str, Any]], now: float) -> int:
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.entry_types import OBJECT_PRODUCER_ACCEPT_EVAL

    for row in rows:
        facts = {"accept": row["kind"], "verdict": row["verdict"]}
        crew_log_emit.on_object_observed(
            unit,
            producer=OBJECT_PRODUCER_ACCEPT_EVAL,
            kind="github_pull_request",
            target=f"https://github.com/{row['repo']}/pull/{row['pr']}",
            fingerprint=hashlib.sha256(repr(sorted(facts.items())).encode()).hexdigest()[:16],
            facts=facts,
            observed_at=now,
        )
    return len(rows)


async def api_work_observed(request: web.Request) -> web.Response:
    """Record the caller's ``accept_eval`` pull-request verdicts as observations."""
    from kiro_crew import work_ledger
    from kiro_crew.crew_log.resolve import unit_for_session_key
    from kiro_crew.dashboard.handlers.work_ledger_caller import _caller_key

    key, refusal = await _caller_key(request, "work_ledger_observed")
    if refusal is not None:
        return refusal
    assert key is not None
    if await asyncio.to_thread(work_ledger.read_conductor, key) is None:
        return web.json_response(
            {"error": "only a session that holds a work ledger records verdicts",
             "code": "no_ledger"},
            status=404,
        )  # fmt: skip
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON", "code": "bad_json"}, status=400)
    rows = observations(body)
    state = request.app["state"]
    try:
        unit = unit_for_session_key(state.sessions, request.headers.get("X-Session-Key", ""))
    except Exception:
        logger.debug("accept_eval verdicts: no crew log unit for the caller", exc_info=True)
        unit = ""
    if not rows or not unit:
        return web.json_response({"recorded": 0})
    recorded = await asyncio.to_thread(_record, unit, rows, time.time())
    return web.json_response({"recorded": recorded})
