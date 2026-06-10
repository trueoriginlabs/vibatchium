"""Built-in daemon verbs for Goals (external-driver flow).

All goal verbs are session-independent at the dispatch layer (lock=``unlocked``):
they mutate the goals DB, not a browser session. A goal *targets* a session
(stored on the record), and a well-behaved driver drives that session with the
normal verbs between ``goal_next`` and ``goal_step``.

One ``GoalEngine`` per daemon, created lazily. On first creation it runs
``on_daemon_restart`` so any goal left ``running`` by a crashed prior daemon is
flipped to ``paused`` (durable crash-resume).

The engine routes all SQLite I/O through a thread executor (see
``goals/engine.py``), so these handlers stay non-blocking on the daemon loop.

Phase-2 hook: ``GoalEngine`` accepts ``checkpoint_cb``/``restore_cb`` to
snapshot+restore browser state at step boundaries — wired here against the
goal's session via ``checkpoint_save`` / ``checkpoint_load`` so pause/resume
round-trips browser state.
"""
from __future__ import annotations

import logging

from ..daemon.paths import get_active_session_name
from .engine import GoalEngine
from .store import GoalStore

log = logging.getLogger("vibatchium.goals")


def _make_checkpoint_cbs(daemon):
    """Build ``(checkpoint_cb, restore_cb)`` that snapshot/restore the goal's
    session via the daemon's own checkpoint verbs. The goal's session is pushed
    into ``current_session_ctx`` so the checkpoint handler targets it."""
    from ..daemon.registry import current_session_ctx

    async def checkpoint_cb(session: str, name: str) -> str | None:
        entry = daemon.registry.get(session)
        if entry is None or entry.session is None:
            return None  # no live session to snapshot
        tok = current_session_ctx.set(session)
        try:
            res = await daemon._handlers["checkpoint_save"](daemon, {"name": name})
        finally:
            current_session_ctx.reset(tok)
        return (res or {}).get("checkpoint") or (res or {}).get("name") or name

    async def restore_cb(session: str, checkpoint_id: str) -> None:
        entry = daemon.registry.get(session)
        if entry is None or entry.session is None:
            return
        tok = current_session_ctx.set(session)
        try:
            await daemon._handlers["checkpoint_load"](
                daemon, {"name": checkpoint_id})
        finally:
            current_session_ctx.reset(tok)

    return checkpoint_cb, restore_cb


def _make_caps_cb(daemon):
    """Pin/unpin a session's cap set on its ``SessionEntry.flags`` as a goal
    takes/releases ownership. The dispatcher consults ``flags['goal_caps']``."""
    def caps_cb(session: str, caps: str | None) -> None:
        entry = daemon.registry.get(session)
        if entry is None:
            return
        if caps:
            entry.flags["goal_caps"] = caps
        else:
            entry.flags.pop("goal_caps", None)
        # A goal-owned session must keep durable state: its checkpoints live in
        # the profile dir, and pause/resume re-binds the same profile. So clear
        # any `ephemeral` flag (e.g. from `start --ephemeral`) — otherwise the
        # profile + checkpoints would be deleted on the next `session close`,
        # and a later `goal resume` would silently bind a fresh empty profile.
        # caps_cb fires on every ownership change, so this is the right hook.
        if entry.ephemeral:
            entry.ephemeral = False
            log.info("session %s is now goal-owned — cleared its ephemeral flag "
                     "so the profile persists for checkpoint/resume", session)
    return caps_cb


def _make_domains_cb(daemon):
    """Pin/unpin a session's domain allowlist as a goal takes/releases ownership.

    Sets two things: ``SessionEntry.flags['goal_domains']`` (the raw CSV — read
    by the `go` handler for a clear, early "blocked by goal allowlist" error and
    by observability), and ``session.nav_allowlist`` (the parsed host set) which
    backs the lazily-installed navigation guard so off-allowlist navigation is
    blocked however it is triggered (clicks/redirects/JS), not just `go`."""
    async def domains_cb(session: str, allow: str | None) -> None:
        entry = daemon.registry.get(session)
        if entry is None:
            return
        sess = entry.session
        if allow:
            from .allowlist import parse_allowlist
            from ..daemon.browser import ensure_nav_guard
            entry.flags["goal_domains"] = allow
            sess.nav_allowlist = parse_allowlist(allow)
            try:
                await ensure_nav_guard(sess)
            except Exception:  # noqa: BLE001 — never let guard install fail goal start
                log.warning("nav-guard install failed for session %s",
                            session, exc_info=True)
        else:
            entry.flags.pop("goal_domains", None)
            sess.nav_allowlist = None
    return domains_cb


async def _get_engine(daemon) -> GoalEngine:
    eng = getattr(daemon, "_goal_engine", None)
    if eng is None:
        checkpoint_cb = restore_cb = None
        if "checkpoint_save" in daemon._handlers:
            checkpoint_cb, restore_cb = _make_checkpoint_cbs(daemon)
        eng = GoalEngine(GoalStore(), checkpoint_cb=checkpoint_cb,
                         restore_cb=restore_cb, caps_cb=_make_caps_cb(daemon),
                         domains_cb=_make_domains_cb(daemon))
        daemon._goal_engine = eng
        # Flip stale `running` goals from a crashed prior daemon to paused.
        try:
            await eng.on_daemon_restart()
        except Exception:  # noqa: BLE001
            log.debug("on_daemon_restart failed (ignored)", exc_info=True)
    return eng


_BUDGET_ALIASES = {
    "steps": "max_steps", "max_steps": "max_steps",
    "minutes": "max_wall_minutes", "wall_minutes": "max_wall_minutes",
    "max_wall_minutes": "max_wall_minutes",
    "spend_usd": "max_spend_usd", "spend": "max_spend_usd",
    "max_spend_usd": "max_spend_usd",
}


def _parse_budget(val) -> dict:
    """Normalize a budget into ``{max_steps, max_wall_minutes, max_spend_usd}``.
    Accepts a dict (canonical or shorthand keys) or a ``k=v,k=v`` string."""
    out: dict = {}
    if not val:
        return out
    if isinstance(val, str):
        items = (p.split("=", 1) for p in val.split(",") if "=" in p)
        val = {k.strip(): v.strip() for k, v in items}
    if not isinstance(val, dict):
        return out
    for k, v in val.items():
        canon = _BUDGET_ALIASES.get(k)
        if not canon:
            continue
        try:
            out[canon] = float(v) if canon == "max_spend_usd" else int(float(v))
        except (TypeError, ValueError):
            continue
    return out


def register_goal_verbs(daemon) -> None:
    @daemon.handler("goal_new")
    async def _goal_new(d, args):
        desc = args.get("description")
        if not desc:
            raise ValueError("goal_new requires `description`")
        session = args.get("session") or get_active_session_name()
        eng = await _get_engine(d)
        return await eng.create(
            description=desc,
            session=session,
            budget=_parse_budget(args.get("budget")),
            inputs=args.get("inputs") or {},
            notifier=args.get("notifier"),
            driver=args.get("driver", "external"),
            parent_id=args.get("parent_id"),
            caps=args.get("caps"),
            domain_allowlist=args.get("allow_domains") or args.get("domain_allowlist"),
        )

    @daemon.handler("goal_list")
    async def _goal_list(d, args):
        eng = await _get_engine(d)
        return {"goals": await eng.list(args.get("status"))}

    @daemon.handler("goal_show")
    async def _goal_show(d, args):
        gid = args.get("goal_id") or args.get("id")
        eng = await _get_engine(d)
        goal = await eng.get(gid)
        if goal is None:
            raise ValueError(f"no goal {gid!r}")
        after = int(args.get("after_seq", 0))
        return {**goal,
                "events": await eng.events(gid, after),
                "artifacts": await eng.artifacts(gid)}

    @daemon.handler("goal_events")
    async def _goal_events(d, args):
        gid = args.get("goal_id") or args.get("id")
        eng = await _get_engine(d)
        return {"goal_id": gid,
                "events": await eng.events(gid, int(args.get("after_seq", 0)))}

    @daemon.handler("goal_next")
    async def _goal_next(d, args):
        ctx = await (await _get_engine(d)).next()
        return ctx if ctx is not None else {"goal": None,
                                            "message": "no runnable goal"}

    @daemon.handler("goal_step")
    async def _goal_step(d, args):
        gid = args.get("goal_id") or args.get("id")
        if not gid:
            raise ValueError("goal_step requires `goal_id`")
        eng = await _get_engine(d)
        return await eng.step(
            gid,
            action=args.get("action"),
            observation=args.get("observation"),
            model_call=args.get("model_call"),
            client_token=args.get("client_token"),
        )

    @daemon.handler("goal_ask")
    async def _goal_ask(d, args):
        gid = args.get("goal_id") or args.get("id")
        q = args.get("question")
        if not gid or not q:
            raise ValueError("goal_ask requires `goal_id` and `question`")
        return await (await _get_engine(d)).ask(gid, q)

    @daemon.handler("goal_answer")
    async def _goal_answer(d, args):
        gid = args.get("goal_id") or args.get("id")
        text = args.get("text")
        if not gid or text is None:
            raise ValueError("goal_answer requires `goal_id` and `text`")
        return await (await _get_engine(d)).answer(gid, text)

    @daemon.handler("goal_done")
    async def _goal_done(d, args):
        gid = args.get("goal_id") or args.get("id")
        if not gid:
            raise ValueError("goal_done requires `goal_id`")
        return await (await _get_engine(d)).done(gid, args.get("outputs"))

    @daemon.handler("goal_fail")
    async def _goal_fail(d, args):
        gid = args.get("goal_id") or args.get("id")
        if not gid:
            raise ValueError("goal_fail requires `goal_id`")
        return await (await _get_engine(d)).fail(gid, args.get("reason", "agent_failed"))

    @daemon.handler("goal_cancel")
    async def _goal_cancel(d, args):
        gid = args.get("goal_id") or args.get("id")
        if not gid:
            raise ValueError("goal_cancel requires `goal_id`")
        return await (await _get_engine(d)).cancel(gid)

    @daemon.handler("goal_pause")
    async def _goal_pause(d, args):
        gid = args.get("goal_id") or args.get("id")
        if not gid:
            raise ValueError("goal_pause requires `goal_id`")
        return await (await _get_engine(d)).pause(gid)

    @daemon.handler("goal_resume")
    async def _goal_resume(d, args):
        gid = args.get("goal_id") or args.get("id")
        if not gid:
            raise ValueError("goal_resume requires `goal_id`")
        return await (await _get_engine(d)).resume(gid)

    @daemon.handler("goal_spawn")
    async def _goal_spawn(d, args):
        parent = args.get("parent_id") or args.get("parent")
        desc = args.get("description")
        if not parent or not desc:
            raise ValueError("goal_spawn requires `parent_id` and `description`")
        eng = await _get_engine(d)
        return await eng.spawn(
            parent, description=desc, session=args.get("session"),
            budget=_parse_budget(args["budget"]) if args.get("budget") else None,
            inputs=args.get("inputs") or {}, notifier=args.get("notifier"),
            caps=args.get("caps"),
            domain_allowlist=args.get("allow_domains") or args.get("domain_allowlist"))

    @daemon.handler("goal_tree")
    async def _goal_tree(d, args):
        gid = args.get("goal_id") or args.get("id")
        if not gid:
            raise ValueError("goal_tree requires `goal_id`")
        return await (await _get_engine(d)).tree(gid)

    @daemon.handler("goal_artifacts")
    async def _goal_artifacts(d, args):
        gid = args.get("goal_id") or args.get("id")
        if not gid:
            raise ValueError("goal_artifacts requires `goal_id`")
        eng = await _get_engine(d)
        if args.get("name") and args.get("path"):
            return await eng.add_artifact(
                gid, args["name"], args["path"],
                args.get("mime", "application/octet-stream"),
                int(args.get("size", 0)))
        return {"goal_id": gid, "artifacts": await eng.artifacts(gid)}

    for v in ("goal_new", "goal_list", "goal_show", "goal_events", "goal_next",
              "goal_step", "goal_ask", "goal_answer", "goal_done", "goal_fail",
              "goal_cancel", "goal_pause", "goal_resume", "goal_spawn",
              "goal_tree", "goal_artifacts"):
        daemon._verb_lock_class[v] = "unlocked"
