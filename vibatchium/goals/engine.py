"""The Goal engine — state machine, budget cop, session ownership, idempotency.

State machine::

    pending → running ⇄ paused
                  ↓        ↑
             needs_input ──┘
                  ↓
               done | failed | cancelled

- ``running`` holds exclusive ownership of its session (tracked in
  ``_session_owner``); ``next()`` won't hand a session to a second goal.
- ``paused`` releases ownership and snapshots a ``checkpoint_id``.
- ``needs_input`` is paused-with-a-question; resumes on ``answer``.
- On daemon restart, all ``running`` flip to ``paused`` (reason
  ``daemon_restart``) and ownership is dropped.

The daemon is the budget cop: every ``step`` charges steps / spend / wall and
hard-stops on exceed (``failed:budget_exceeded``). The LLM is NOT run here — an
external driver calls ``next``/``step`` in a loop.

**Threading.** Every public method is a coroutine and routes its SQLite access
through ``asyncio.to_thread`` so the daemon's single event loop never blocks on
disk I/O (the store is thread-safe — ``check_same_thread=False`` + an internal
``RLock``). In-memory state (``_session_owner``) is mutated only on the loop
thread, between awaits.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .. import safety as _safety
from . import notifiers
from .events import TERMINAL_STATES, make_event
from .store import GoalStore

log = logging.getLogger("vibatchium.goals")

# Rough per-Mtoken USD prices for spend estimation when a model_call carries
# token counts instead of an explicit cost_usd. Conservative ballpark; an
# explicit cost_usd in the model_call always wins.
_PRICE_TABLE = {
    "haiku":  (0.80, 4.0),    # (input $/Mtok, output $/Mtok)
    "sonnet": (3.0, 15.0),
    "opus":   (15.0, 75.0),
}


class GoalError(RuntimeError):
    pass


class GoalEngine:
    def __init__(self, store: GoalStore, *, checkpoint_cb=None, restore_cb=None,
                 caps_cb=None):
        """``checkpoint_cb(session, name) -> checkpoint_id`` (async) is called
        at each step boundary to snapshot the session; ``restore_cb(session,
        checkpoint_id)`` (async) re-applies it when a paused goal resumes.
        ``caps_cb(session, caps_csv_or_None)`` (sync) pins/unpins the session's
        cap set as the goal takes/releases ownership (per-goal caps enforcement).
        All optional — omitted in tests with no live session."""
        self.store = store
        self._checkpoint_cb = checkpoint_cb
        self._restore_cb = restore_cb
        self._caps_cb = caps_cb
        self._session_owner: dict[str, str] = {}   # session → goal_id
        self._notifiers: dict[str, notifiers.Notifier] = {}

    def _apply_caps(self, session: str, caps: str | None) -> None:
        if self._caps_cb is None:
            return
        try:
            self._caps_cb(session, caps)
        except Exception:  # noqa: BLE001
            log.debug("caps_cb raised (ignored)", exc_info=True)

    # ─── store access (off the event loop) ───────────────────────────────────

    async def _db(self, method: str, *args, **kwargs):
        """Run one ``GoalStore`` call in a worker thread so SQLite I/O never
        blocks the daemon's event loop."""
        return await asyncio.to_thread(getattr(self.store, method), *args, **kwargs)

    # ─── notifiers ─────────────────────────────────────────────────────────

    def _notifier_for(self, goal: dict) -> notifiers.Notifier:
        gid = goal["id"]
        n = self._notifiers.get(gid)
        if n is None:
            n = notifiers.build(goal.get("notifier"))
            self._notifiers[gid] = n
        return n

    async def _emit(self, goal: dict, kind: str, payload: dict | None = None) -> dict:
        ev = make_event(kind, payload)
        # Persist durably FIRST (off-loop) — the event survives even if the
        # side-channel notifier drops it.
        seq = await self._db("append_event", goal["id"], kind, ev["payload"],
                             ts=ev["ts"])
        ev["seq"] = seq
        try:
            # Notifiers must be non-blocking (webhook POSTs run on their own
            # thread); we never await them.
            self._notifier_for(goal).notify(goal["id"], ev)
        except Exception:  # noqa: BLE001
            log.debug("notifier raised (ignored)", exc_info=True)
        return ev

    # ─── lifecycle ─────────────────────────────────────────────────────────

    async def create(self, *, description: str, session: str, budget: dict,
                     inputs: dict | None = None, notifier: str | None = None,
                     driver: str = "external", parent_id: str | None = None,
                     caps: str | None = None,
                     domain_allowlist: str | None = None) -> dict:
        return await self._db(
            "create_goal", description=description, session=session,
            budget=budget, inputs=inputs, notifier=notifier, driver=driver,
            parent_id=parent_id, caps=caps, domain_allowlist=domain_allowlist)

    async def get(self, gid: str) -> dict | None:
        return await self._db("get_goal", gid)

    async def list(self, status: str | None = None) -> list[dict]:
        return await self._db("list_goals", status)

    async def events(self, gid: str, after_seq: int = 0) -> list[dict]:
        return await self._db("list_events", gid, after_seq)

    async def tree(self, gid: str) -> dict:
        """Return ``{goal, children: [tree…]}`` rooted at ``gid``."""
        goal = await self._must_get(gid)
        kids = await self._db("list_children", gid)
        return {"goal": goal,
                "children": [await self.tree(k["id"]) for k in kids]}

    async def artifacts(self, gid: str) -> list[dict]:
        return await self._db("list_artifacts", gid)

    # ─── pick + start a runnable goal ────────────────────────────────────────

    async def next(self) -> dict | None:
        """Pick the oldest runnable goal whose session is free, mark it running,
        and return its driver context. None if nothing is runnable."""
        for status in ("paused", "pending"):
            for goal in await self._db("list_goals", status):
                sess = goal["session"]
                owner = self._session_owner.get(sess)
                if owner and owner != goal["id"]:
                    continue  # session busy with another running goal
                return await self._start(goal)
        return None

    async def _start(self, goal: dict) -> dict:
        gid = goal["id"]
        sess = goal["session"]
        consumed = dict(goal["consumed"])
        first_run = "started_at" not in consumed
        if first_run:
            consumed["started_at"] = time.time()
        # Claim ownership synchronously (before any await) so a concurrent
        # next() can't hand the same session to a second goal.
        self._session_owner[sess] = gid
        self._apply_caps(sess, goal.get("caps"))
        await self._db("update_goal", gid, status="running", consumed=consumed)
        goal = await self._db("get_goal", gid)
        # Resume: re-apply checkpoint into the (fresh) session bind.
        if goal["checkpoint_id"] and self._restore_cb is not None:
            try:
                await self._restore_cb(sess, goal["checkpoint_id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("checkpoint restore failed for goal %s: %s", gid, exc)
        await self._emit(goal, "session_attached",
                         {"session": sess, "resumed": not first_run,
                          "checkpoint_id": goal["checkpoint_id"]})
        return {
            "goal": goal,
            "recent_events": await self._db(
                "list_events", gid, max(0, goal["current_step"] - 5)),
            "caps": goal["caps"],
            "domain_allowlist": goal["domain_allowlist"],
        }

    # ─── step ────────────────────────────────────────────────────────────────

    async def step(self, gid: str, *, action: dict | None = None,
                   observation: dict | None = None,
                   model_call: dict | None = None,
                   client_token: str | None = None) -> dict:
        goal = await self._must_get(gid)
        if goal["status"] != "running":
            raise GoalError(
                f"goal {gid} is {goal['status']!r}, not running — "
                f"call goal_next first")

        # Idempotent retry: a step replayed with a seen client_token returns the
        # original recorded result (5.4) — never re-charges budget or re-acts.
        token_idx = dict(goal["client_token_idx"])
        if client_token and client_token in token_idx:
            recorded = token_idx[client_token]
            if isinstance(recorded, dict):
                return {**recorded, "idempotent": True}
            # Legacy rows stored just the step number.
            return {"idempotent": True, "goal_id": gid,
                    "current_step": recorded, "status": goal["status"]}

        await self._emit(goal, "step_start", {"action": action or {}})

        if observation is not None:
            scan = self._scan_observation(observation)
            await self._emit(goal, "observation",
                            {"observation": observation,
                             "safety_flagged": scan["risk"] != "none",
                             "safety": scan})

        consumed = dict(goal["consumed"])
        consumed["steps"] = consumed.get("steps", 0) + 1
        if model_call:
            cost = self._model_call_cost(model_call)
            consumed["spend_usd"] = round(consumed.get("spend_usd", 0.0) + cost, 6)
            await self._emit(goal, "model_call", {**model_call, "cost_usd": cost})
        started = consumed.get("started_at", goal["created_at"])
        consumed["wall_seconds"] = round(time.time() - started, 3)

        new_step = goal["current_step"] + 1

        await self._emit(goal, "budget_consumed", dict(consumed))

        # Budget check (hard stop).
        exceeded = self._budget_exceeded(goal["budget"], consumed)
        if exceeded:
            result = {"goal_id": gid, "status": "failed",
                      "budget_exceeded": exceeded, "consumed": consumed}
            if client_token:
                token_idx[client_token] = result
            await self._db("update_goal", gid, status="failed",
                           consumed=consumed, current_step=new_step,
                           client_token_idx=token_idx)
            goal = await self._db("get_goal", gid)
            self._release(goal)
            await self._emit(goal, "failed", {"reason": "budget_exceeded",
                                              "limit": exceeded})
            return result

        # Checkpoint at the step boundary (best-effort). The cb returns a real
        # id only when there's a live session to snapshot; on None we keep the
        # prior checkpoint_id and emit no (noisy, null-id) checkpoint_saved.
        checkpoint_id = goal["checkpoint_id"]
        if self._checkpoint_cb is not None:
            try:
                new_cp = await self._checkpoint_cb(goal["session"], f"goal-{gid}")
                if new_cp:
                    checkpoint_id = new_cp
                    await self._emit(goal, "checkpoint_saved",
                                    {"checkpoint_id": checkpoint_id})
            except Exception as exc:  # noqa: BLE001
                log.debug("checkpoint at step failed for goal %s: %s", gid, exc)

        result = {"goal_id": gid, "status": "running", "current_step": new_step,
                  "consumed": consumed}
        if client_token:
            token_idx[client_token] = result
        await self._db("update_goal", gid, consumed=consumed,
                       current_step=new_step, client_token_idx=token_idx,
                       checkpoint_id=checkpoint_id)
        await self._emit(await self._db("get_goal", gid), "step_done",
                        {"step": new_step})
        return result

    # ─── ask / answer ────────────────────────────────────────────────────────

    async def ask(self, gid: str, question: str) -> dict:
        goal = await self._must_get(gid)
        if goal["status"] not in ("running", "paused"):
            raise GoalError(f"cannot ask on a {goal['status']!r} goal")
        await self._db("update_goal", gid, status="needs_input",
                       pending_question=question)
        goal = await self._db("get_goal", gid)
        self._release(goal)
        await self._emit(goal, "question", {"question": question})
        return {"goal_id": gid, "status": "needs_input", "question": question}

    async def answer(self, gid: str, text: str) -> dict:
        goal = await self._must_get(gid)
        if goal["status"] != "needs_input":
            raise GoalError(f"goal {gid} is not awaiting input "
                            f"(status={goal['status']!r})")
        await self._db("update_goal", gid, status="paused", pending_question=None)
        goal = await self._db("get_goal", gid)
        await self._emit(goal, "user_input", {"text": text,
                                              "question": goal["pending_question"]})
        return {"goal_id": gid, "status": "paused"}

    # ─── terminal + control transitions ──────────────────────────────────────

    async def done(self, gid: str, outputs: dict | None = None) -> dict:
        await self._must_get(gid)
        await self._db("update_goal", gid, status="done", outputs=outputs or {})
        goal = await self._db("get_goal", gid)
        self._release(goal)
        await self._emit(goal, "done", {"outputs": outputs or {}})
        return {"goal_id": gid, "status": "done"}

    async def fail(self, gid: str, reason: str = "agent_failed") -> dict:
        await self._must_get(gid)
        await self._db("update_goal", gid, status="failed")
        goal = await self._db("get_goal", gid)
        self._release(goal)
        await self._emit(goal, "failed", {"reason": reason})
        return {"goal_id": gid, "status": "failed"}

    async def cancel(self, gid: str) -> dict:
        goal = await self._must_get(gid)
        if goal["status"] in TERMINAL_STATES:
            return {"goal_id": gid, "status": goal["status"]}
        await self._db("update_goal", gid, status="cancelled")
        goal = await self._db("get_goal", gid)
        self._release(goal)
        await self._emit(goal, "cancelled", {})
        return {"goal_id": gid, "status": "cancelled"}

    async def pause(self, gid: str) -> dict:
        goal = await self._must_get(gid)
        if goal["status"] != "running":
            raise GoalError(f"can only pause a running goal (is {goal['status']!r})")
        await self._db("update_goal", gid, status="paused")
        goal = await self._db("get_goal", gid)
        self._release(goal)
        await self._emit(goal, "session_released", {"reason": "paused"})
        return {"goal_id": gid, "status": "paused"}

    async def resume(self, gid: str) -> dict:
        """Un-pause a paused goal and immediately start it (targeted ``next``).
        Raises if its session is owned by another running goal."""
        goal = await self._must_get(gid)
        if goal["status"] not in ("paused", "needs_input"):
            raise GoalError(f"can only resume a paused goal (is {goal['status']!r})")
        owner = self._session_owner.get(goal["session"])
        if owner and owner != gid:
            raise GoalError(
                f"session {goal['session']!r} is busy with goal {owner}")
        if goal["status"] == "needs_input":
            await self._db("update_goal", gid, status="paused",
                           pending_question=None)
            goal = await self._db("get_goal", gid)
        return await self._start(goal)

    # ─── sub-goals ─────────────────────────────────────────────────────────

    async def spawn(self, parent_id: str, *, description: str,
                    session: str | None = None, budget: dict | None = None,
                    inputs: dict | None = None, notifier: str | None = None,
                    caps: str | None = None,
                    domain_allowlist: str | None = None) -> dict:
        """Create a child goal under ``parent_id``. Inherits the parent's
        session / budget / caps unless overridden."""
        parent = await self._must_get(parent_id)
        child = await self.create(
            description=description,
            session=session or parent["session"],
            budget=budget if budget is not None else dict(parent["budget"]),
            inputs=inputs or {}, notifier=notifier or parent["notifier"],
            parent_id=parent_id,
            caps=caps if caps is not None else parent["caps"],
            domain_allowlist=(domain_allowlist if domain_allowlist is not None
                              else parent["domain_allowlist"]))
        await self._emit(parent, "plan_update",
                        {"spawned_child": child["id"],
                         "description": description})
        return child

    async def add_artifact(self, gid: str, name: str, path: str,
                           mime: str = "application/octet-stream",
                           size: int = 0) -> dict:
        goal = await self._must_get(gid)
        await self._db("add_artifact", gid, name, path, mime, size)
        await self._emit(goal, "artifact",
                        {"name": name, "path": path, "mime": mime, "size": size})
        return {"goal_id": gid, "name": name, "path": path}

    async def on_daemon_restart(self) -> int:
        """Flip every running goal to paused (reason daemon_restart) and drop
        session ownership. Called from daemon startup. Returns count flipped."""
        n = 0
        for goal in await self._db("list_goals", "running"):
            await self._db("update_goal", goal["id"], status="paused")
            g = await self._db("get_goal", goal["id"])
            await self._emit(g, "session_released", {"reason": "daemon_restart"})
            n += 1
        self._session_owner.clear()
        return n

    # ─── helpers ─────────────────────────────────────────────────────────────

    async def _must_get(self, gid: str) -> dict:
        goal = await self._db("get_goal", gid)
        if goal is None:
            raise GoalError(f"no goal {gid!r}")
        return goal

    def _release(self, goal: dict) -> None:
        sess = goal["session"]
        if self._session_owner.get(sess) == goal["id"]:
            del self._session_owner[sess]
            self._apply_caps(sess, None)   # un-pin the session's cap set

    @staticmethod
    def _budget_exceeded(budget: dict, consumed: dict):
        """Return a dict describing the first exceeded limit, or None."""
        if not budget:
            return None
        ms = budget.get("max_steps")
        if ms is not None and consumed.get("steps", 0) > ms:
            return {"max_steps": ms, "steps": consumed["steps"]}
        msp = budget.get("max_spend_usd")
        if msp is not None and consumed.get("spend_usd", 0.0) > msp:
            return {"max_spend_usd": msp, "spend_usd": consumed["spend_usd"]}
        mw = budget.get("max_wall_minutes")
        if mw is not None and consumed.get("wall_seconds", 0.0) > mw * 60:
            return {"max_wall_minutes": mw,
                    "wall_seconds": consumed["wall_seconds"]}
        return None

    @staticmethod
    def _model_call_cost(model_call: dict) -> float:
        if "cost_usd" in model_call:
            try:
                return float(model_call["cost_usd"])
            except (TypeError, ValueError):
                return 0.0
        model = str(model_call.get("model", "")).lower()
        rate = next((v for k, v in _PRICE_TABLE.items() if k in model), None)
        if rate is None:
            return 0.0
        in_tok = float(model_call.get("input_tokens", 0) or 0)
        out_tok = float(model_call.get("output_tokens", 0) or 0)
        return round((in_tok * rate[0] + out_tok * rate[1]) / 1_000_000, 6)

    @staticmethod
    def _scan_observation(observation: dict) -> dict:
        """Run the injection classifier over string values in an observation."""
        worst = "none"
        signals: list[str] = []
        order = {"none": 0, "low": 1, "high": 2}
        for v in (observation or {}).values():
            if isinstance(v, str) and v:
                r = _safety.classify(v)
                if order[r["risk"]] > order[worst]:
                    worst = r["risk"]
                for s in r["signals"]:
                    if s not in signals:
                        signals.append(s)
        return {"risk": worst, "signals": signals}
