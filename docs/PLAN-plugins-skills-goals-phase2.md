# Plan: Plugins / Skills / Goals — Phase 2 (harden, retrofit, integrate)

Follow-up to `PLAN-plugins-skills-goals.md`. That plan described the three
features; they are now implemented (44 new tests, ruff clean, full suite 455
passed — the only failures are pre-existing missing optional deps `nacl`/
`aiohttp`). This plan covers what it takes to make them **correct, discoverable,
proven on a real package, and tested through the actual daemon** — i.e. the gap
between "works in-process in unit tests" and "load-bearing."

Every work item below is written as an **outcome** ("Done when …") — a concrete,
verifiable acceptance criterion, not an activity. If the outcome can't be
demonstrated, the item isn't done.

---

## Where we are (ground truth, verified)

- **Plugins:** `add_verb` API + discovery (entry-point / local-dir / git) + per-verb
  lock-class routing in `dispatch`; `vb plugin list/show/reload/install/remove`;
  dotted-verb passthrough (`vb x.search`); MCP dynamic exposure + `plugins` bucket.
- **Skills:** host-keyed markdown store under `CONFIG_DIR/skills/<host>/*.md`;
  injection scan on read, secret scan on write/import; opt-in `go`/`explore`
  surfacing (`VIBATCHIUM_SKILLS=1`); `vb skill list/show/write/rm/import` + MCP.
- **Goals:** SQLite store, ULID ids, state machine, budget hard-stop, idempotent
  retries, crash-resume (running→paused on restart), observation injection-scan,
  stdout/webhook/mcp_push notifiers; full `vb goal …` surface + MCP.
- **REST:** `POST /v1/<verb>` auto-forwards, so new built-in verbs work
  unrestricted and under `--caps` (buckets added). *Gap:* capped REST blocks
  dotted plugin verbs (the `plugins` bucket is empty + no dotted bypass like MCP).
- **All current tests are in-process** (`Daemon().dispatch(...)`); the socket /
  real-daemon / MCP-process wiring is untested.

---

## Global Definition of Done (applies to every item)

1. `uvx ruff@0.15.14 check` clean on all touched files.
2. New behavior covered by a test that **fails before, passes after**.
3. Full suite shows **no new failures** vs. the 19 known `nacl`/`aiohttp` ones.
4. Tests run under an **isolated `HOME` + `XDG_RUNTIME_DIR`** so the user's
   `~/.config/vibatchium/active-session` is never touched (the suite rewrites it).
5. Leaked test daemons / Chromes cleaned up after runs (kill by PID, never by a
   `pkill -f` pattern that matches the running shell's own command line).
6. Nothing committed unless the user asks.

---

## Decisions to lock before coding (forks)

These shape the work; resolve them first (recommended pick in **bold**).

- **D1 — xscraper retrofit mechanism.** **B: keep RPC, register `lock="unlocked"`.**
  xscraper already routes *all* daemon access through `VibatchiumPage`
  (`_vibatchium_adapter.py`), which takes an injected call fn and wraps the sync
  `client.call` in `asyncio.to_thread`. So a plugin handler can run the existing
  scraper unchanged; its inner `to_thread(client.call,…)` opens a fresh socket to
  the same daemon and the loop (free, because the outer verb holds no lock) serves
  it. The only requirement is registering the verb `lock="unlocked"` so the outer
  handler doesn't hold the per-session lock its inner calls need. *No xscraper
  internals change.* Alternative **A′** (inject an in-process dispatcher to skip
  the self-socket) is a later perf optimization, not a correctness need. → This
  *corrects* the earlier "must refactor to drive `daemon.session` directly" note,
  which was wrong.
- **D2 — SQLite off the event loop.** **Wrap `GoalStore` access in a thread
  executor** (`asyncio.to_thread` / a dedicated single worker thread). The store
  is already thread-safe (`check_same_thread=False` + `RLock`); today that lock is
  unused because nothing runs off-thread. Pick one executor strategy and apply it
  consistently.
- **D3 — `mcp_push` notifier.** **Remove the in-memory buffer; make `mcp_push://`
  a no-op sink and rely on store-backed `goal_events` polling.** Events are already
  durably persisted; an MCP client polling `goal_events` is strictly better than a
  buffer that loses data on restart and currently has no drain verb. (Alternative:
  add a `goal_drain` verb — more surface for less durability.)
- **D4 — `plugin install` under PEP 668.** **Detect a pipx install and use
  `pipx inject vibatchium <pkg>`; otherwise `pip install`, and on the
  `externally-managed-environment` error, retry with `--break-system-packages`
  and print the exact command.** The user's environment is Debian/PEP-668; the
  current naive `pip install` will fail there.
- **D5 — per-goal caps enforcement.** **Enforce by tagging the owned session.**
  While a goal is `running` it owns its session; set `entry.flags["goal_caps"]`
  and have `dispatch` reject out-of-bucket verbs on that session. This *is*
  socket-boundary enforcement and is tractable — it corrects the earlier "much
  bigger project" framing. (Plugin in-process code remains unenforceable; that's
  the documented trust boundary and stays unchanged.)

---

## Phase 0 — Correctness fixes in shipped code  *(highest priority, ~1 day total)*

These are defects in code written this session. Cheap, and they make Goals safe
to actually run.

### 0.1 Webhook notifier must not block the event loop
- **Why:** `WebhookNotifier.notify` calls synchronous `urllib.request.urlopen(timeout=5)`
  inside `GoalEngine._emit`, which runs on the daemon's single async loop. A slow
  webhook stalls *all* sessions for up to 5s per event.
- **Files:** `vibatchium/goals/notifiers.py`, `vibatchium/goals/engine.py`.
- **Done when:** a test registers a goal with `webhook://<endpoint-that-sleeps-3s>`,
  performs a `goal_step`, and asserts the step returns in **< 300 ms** (proving the
  emit didn't block), and the webhook still receives the POST eventually. Webhook
  errors never propagate to the caller.

### 0.2 Resolve the `mcp_push` dead end
- **Why:** `McpPushNotifier` buffers events in memory with a `drain()` method, but
  no verb drains it — the events are unreachable.
- **Files:** `vibatchium/goals/notifiers.py` (+ docs), `tests/test_goals.py`.
- **Done when:** per D3, the buffer is removed and a test asserts that after a goal
  with `notifier="mcp_push://"` runs, **every** event is retrievable via
  `goal_events`; `grep` shows no orphaned buffer with no drain path.

### 0.3 Move SQLite off the event loop
- **Why:** every goal verb does synchronous `sqlite3` I/O on the async loop; under
  event volume this serializes browser work too.
- **Files:** `vibatchium/goals/store.py`, `vibatchium/goals/engine.py`.
- **Done when:** a test records `threading.get_ident()` inside a `GoalStore` query
  during a `goal_step` and asserts it **differs from the event-loop thread id**
  (proving store I/O runs in an executor), and all existing goal tests still pass.

### 0.4 `plugin install` survives PEP 668
- **Why:** `vb plugin install` runs `sys.executable -m pip install`, which Debian/
  Ubuntu (the user's environment) blocks with `externally-managed-environment`.
- **Files:** `vibatchium/cli.py` (`plugin install` / `plugin remove`).
- **Done when:** with `subprocess` mocked to emit the PEP-668 error, a test asserts
  `vb plugin install` either (a) routes through `pipx inject` when pipx-installed,
  or (b) retries with `--break-system-packages` and prints an actionable message
  naming the exact command. No silent failure.

---

## Phase 1 — Discoverability  *(~0.5 day, highest ROI for adoption)*

The features are invisible to a fresh agent. Skills' "agent writes its own notes"
loop and the Goals driver loop only work if the agent is *told* they exist.

### 1.1 AGENTS.md — the coding-agent contract
- **Files:** `AGENTS.md`.
- **Done when:** `AGENTS.md` documents: loading plugins + calling `vb x.search`;
  the Skills opt-in (`VIBATCHIUM_SKILLS=1`) and the convention that the agent
  should `skill write <host>` when it learns something non-obvious; the Goals
  external-driver loop (`goal next` → drive → `goal step` → `goal done`). `grep`
  finds `skill write`, `goal next`, `goal step`, `vb plugin`, `VIBATCHIUM_SKILLS`.

### 1.2 CAPABILITIES.md — per-verb reference
- **Files:** `docs/CAPABILITIES.md`.
- **Done when:** each of the ~22 new verbs (`plugin_*`, `skill_*`, `goal_*`,
  `list_verbs`) has an entry; `grep` confirms every verb name is present.

### 1.3 CHANGELOG entry
- **Files:** `CHANGELOG.md`.
- **Done when:** a dated entry describes Plugins, Skills, Goals with the new
  CLI/MCP surface and the opt-in/trust caveats.

---

## Phase 2 — xscraper plugin retrofit  *(proof of plugin value, ~0.5–1 day)*

Per D1. This is the first real plugin and the true test of the `add_verb` contract.

### 2.1 Ship xscraper as a plugin
- **Why:** validate the plugin model end-to-end on a real package; deliver
  `vb x.search`.
- **Files (in the xscraper repo):** new `src/xscraper/plugin.py`; `pyproject.toml`
  entry point `[project.entry-points."vibatchium.plugins"] xscraper = "xscraper.plugin:register"`.
- **Shape:** `register(daemon)` registers the full set of xscraper **read**
  methods as verbs — `x.search`, `x.home_timeline`, `x.user_tweets`, `x.about`,
  `x.discover_community_url` — with **`lock="unlocked"`**. Each handler reads the
  current session from `current_session_ctx`, constructs `XScraper(session_name=…)`,
  and `await`s the existing method. No scraper-internal changes (the adapter's
  `to_thread`-wrapped RPC + unlocked verb avoids the deadlock).
  > **`x.home_timeline` is load-bearing — do not omit it.** A downstream
  > consumer (the `twitter_persona` bot) reads the Following feed via
  > `home_timeline`; if the retrofit registers only search/user_tweets/about/
  > discover, the bot's timeline read has no plugin path. (Caught in cross-repo
  > review.) `cache_stats` / `was_rate_limited_recent` / `close` are
  > introspection/lifecycle, not scrape verbs — skip them as verbs.
- **Session sourcing (layering note).** The plugin verb's session comes from
  `current_session_ctx` — i.e. whatever the caller targeted via `--session` /
  `VIBATCHIUM_SESSION` / the active profile. This is a **parallel access path**,
  not a forced migration: an in-process consumer that imports `xscraper` directly
  (e.g. `twitter_persona`, which today selects the session via its own
  `TWITTER_PERSONA_SESSION`/`DEFAULT_SESSION` constants) keeps working unchanged.
  > Note the consumer's reader+writer **session-name convergence requirement does
  > not disappear if it migrates onto `vb x.*`** — it *relocates* to the
  > `--session` the bot passes (it must drive reads *and* posts on the same
  > session name). So that convergence work isn't wasted; only its mechanism
  > moves from in-process constants to the `--session` arg.
- **Concurrency caveat.** Because these verbs are `lock="unlocked"`, two
  concurrent `x.*` calls on the *same* session interleave their per-op RPCs (each
  RPC still grabs the per-session lock individually, but a multi-step scrape —
  e.g. `home_timeline` with its `setup_hook` — is not atomic across ops). Fine for
  a single-driver bot; if isolation is ever needed, the plugin should hold an
  internal per-session lock for the duration of a scrape.
- **Done when:** in a daemon env with xscraper installed, `vb plugin list` shows
  `xscraper` with its five verbs (incl. `x.home_timeline`), **and** a CI-able unit
  test injects a stub `CallFn` into the adapter, registers the plugin into an
  in-process `Daemon`, and asserts `dispatch("x.search", {query, count})` and
  `dispatch("x.home_timeline", {count})` return parsed tweets from fixture data.
  (A live `vb x.search "$BTC"` against a logged-in session is the manual
  acceptance check, documented but not in CI.)

### 2.2 (Optional) in-process adapter — drop the self-socket
- **Why:** path B does a Unix-socket round-trip to the same daemon per op; A′ skips it.
- **Done when:** an injected in-process `CallFn` (bridges to `daemon.dispatch` via
  `run_coroutine_threadsafe`) passes the same 2.1 test with no new socket
  connections opened (assert via a connection counter). Defer unless profiling
  shows the self-socket matters.

### 2.3 Fold xscraper CLI into verbs (nice-to-have)
- **Done when:** `vb x.login` / `vb x.check` exist as plugin verbs mirroring
  `xscraper login` / `xscraper check`.

---

## Phase 3 — Integration tests through the real daemon  *(~1 day)*

Current tests are all in-process. The wiring most likely to break in real use
(socket dispatch, daemon-process plugin discovery, MCP list/call, restart
durability) has zero coverage.

### 3.1 Plugin verb e2e over the socket
- **Done when:** a test sets `HOME`/`XDG_RUNTIME_DIR` to temp dirs, drops a
  local-dir plugin under `~/.config/vibatchium/plugins/`, `spawn_daemon()`s a real
  daemon subprocess, and asserts `client.call("<plugin>.verb")` returns the
  expected result; daemon is torn down by PID afterward.

### 3.2 Skill surfacing on a real navigation
- **Done when:** with `VIBATCHIUM_SKILLS=1` and a note written for host `127.0.0.1`,
  `client.call("go", {url: <local fixture>})` returns a `skills` key listing the
  note; with the env unset, no `skills` key appears.

### 3.3 MCP dynamic exposure
- **Done when:** with a real daemon + a loaded plugin, `mcp_server.list_tools()`
  includes the plugin verb and `mcp_server.call_tool("<plugin>.verb", …)` forwards
  and returns its result; under `--caps` without `plugins`, the verb is absent and
  `call_tool` refuses it.

### 3.4 Goals durability across a real restart
- **Done when:** a test creates a goal + `goal_next` (running) over the socket,
  `shutdown`s the daemon, respawns it, and asserts the goal is now `paused` and
  `goal_next` can pick it back up — proving crash-resume on the real process, not
  just the engine unit.

---

## Phase 4 — Goals Phase 2 (the deferred durability layer)  *(~1.5–2 wk)*

### 4.1 Wire checkpoint_cb / restore_cb
- **Why:** the engine has the hooks; the daemon doesn't pass them, so browser
  state isn't snapshotted/restored across pause/resume.
- **Files:** `vibatchium/goals/handlers.py` (build cbs that invoke
  `checkpoint_save`/`checkpoint_load` against the goal's session via the contextvar).
- **Done when:** a `goal_step` on a goal with a live session emits a
  `checkpoint_saved` event with a real checkpoint id and sets `checkpoint_id` on
  the record; after `goal_pause` + `goal_resume`, an integration test asserts the
  session's cookies/tabs match the pre-pause snapshot.

### 4.2 `goal tail` via SSE
- **Files:** `vibatchium/rest.py`.
- **Done when:** `GET /v1/goals/<id>/events?after=N` streams events as SSE; a test
  client connects and receives newly-appended events in order.

### 4.3 Sub-goals / tree / artifacts
- **Done when:** `goal_spawn --parent <id>` creates a child (`parent_id` set);
  `goal_tree <id>` returns the hierarchy; `goal_artifacts <id>` lists rows written
  via `add_artifact`. Each covered by a test.

---

## Phase 5 — Hardening & polish  *(prioritize per demand)*

### 5.1 Per-goal caps enforcement (per D5)
- **Done when:** while a goal created with `--caps=core,nav` is `running` and owns
  session S, `client.call("eval", …, session=S)` is **rejected** with a caps error;
  once the goal is `done`/`paused`, the same call succeeds. (In-process plugin code
  is explicitly out of scope — documented trust boundary.)

### 5.2 `host_key` registrable-domain option
- **Why:** today `m.youtube.com` / `music.youtube.com` / `youtube.com` are separate
  skill buckets (only `www.` is stripped).
- **Done when:** an opt-in (flag/env) collapses subdomains to the registrable
  domain (PSL or a bundled suffix list); default stays dumb (browser-use parity);
  test covers both modes.

### 5.3 `skill write --allow-secrets` override
- **Why:** the secret-scan refusal is a hard raise with no escape; a legit note
  mentioning a token-shaped selector is falsely blocked.
- **Done when:** `vb skill write … --allow-secrets` persists the note with a logged
  warning; without the flag the refusal stands; test covers both.

### 5.4 True idempotency result cache
- **Why:** a replayed `goal_step` returns `{idempotent, current_step}`, not the
  original step's observation/result.
- **Done when:** the original step result is stored keyed by `client_token` and a
  replay returns the identical recorded result; test asserts equality.

### 5.5 Plugin API stability (facade + version)
- **Why:** plugins receive the raw `Daemon`; `daemon.session` is an internal
  Patchright object with no stability guarantee.
- **Done when:** `add_verb` records an `api_version`; plugins receive a documented
  `PluginContext` facade (`ctx.session`, `ctx.call`, `ctx.emit`) instead of the raw
  daemon; the xscraper plugin uses the facade. (Larger — schedule once a second
  plugin exists to inform the surface.)

---

## Recommended sequencing

1. **D1–D5 decisions** (lock the forks) — ~0.
2. **Phase 0** (correctness bugs) — ~1 day. *Do first: it's broken code I shipped.*
3. **Phase 1** (AGENTS.md / CAPABILITIES.md) — ~0.5 day. *Highest adoption ROI.*
4. **Phase 2** (xscraper retrofit, path B) — ~0.5–1 day. *Proves the contract.*
5. **Phase 3** (integration tests) — ~1 day. *Covers the untested wiring.*
6. **Phase 4** (Goals checkpoint + SSE) — ~1.5–2 wk.
7. **Phase 5** (hardening) — as demand dictates; 5.1 (caps) and 5.4 (idempotency)
   are the most valuable.

**Critical path to "load-bearing":** Phase 0 → 1 → 2 → 3 (~3 days). After that the
three features are correct, discoverable, proven on a real package, and tested
through the real daemon. Phases 4–5 are depth.

---

## Outcome checklist (at-a-glance acceptance)

| # | Outcome (Done when) |
|---|---|
| 0.1 | `goal_step` returns < 300 ms with a 3 s-sleeping webhook; POST still delivered |
| 0.2 | every event of an `mcp_push` goal is retrievable via `goal_events`; no orphan buffer |
| 0.3 | store query runs on a non-loop thread id during `goal_step` |
| 0.4 | `plugin install` handles `externally-managed` (pipx inject / `--break-system-packages` + message) |
| 1.1 | `AGENTS.md` greps positive for `skill write`, `goal next/step`, `vb plugin`, `VIBATCHIUM_SKILLS` |
| 1.2 | every new verb has a `CAPABILITIES.md` entry |
| 2.1 | `vb plugin list` shows xscraper's 5 read verbs (incl. `x.home_timeline`); stub-injected `dispatch("x.search")` + `dispatch("x.home_timeline")` return parsed tweets |
| 3.1 | real daemon serves a local-dir plugin verb over the socket |
| 3.2 | `go` returns a `skills` key iff `VIBATCHIUM_SKILLS=1` |
| 3.3 | MCP `list_tools`/`call_tool` expose+forward plugin verbs; capped surface refuses them |
| 3.4 | goal survives a real daemon restart (running→paused→resumable) |
| 4.1 | pause/resume round-trips browser state via a real `checkpoint_id` |
| 4.2 | `GET /v1/goals/<id>/events` streams SSE in order |
| 5.1 | capped goal rejects out-of-bucket verbs on its owned session while running |
| 5.4 | replayed `goal_step` returns the identical recorded result |
