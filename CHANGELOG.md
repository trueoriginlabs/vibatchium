# Changelog

All notable changes to vibatchium are documented here. Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
minor bumps may include breaking changes; we'll always call them out here.

## [0.6.11] — 2026-06-10

### Added — timezone coherence (`vb geo`)
- **The host clock behind a foreign proxy IP was a louder bot tell than any UA
  leak.** A Chrome reporting `Australia/Sydney` (via
  `Intl.DateTimeFormat().resolvedOptions().timeZone`) while egressing through a
  US datacenter proxy is trivially flagged — and the whole point of a proxy is
  to move the IP, leaving the clock behind. New `vb geo set` persists a
  per-session **timezone** (mirrors `vb proxy set`: stored in the profile dir,
  applied on next `start`) so it coheres with the proxy's country:
  - `vb geo set --country us` — ISO-2 code → a representative IANA timezone
    (27 common proxy countries). `--timezone` overrides the lookup for precise
    control.
  - Applied via **protocol-level CDP Emulation** (`Emulation.setTimezoneOverride`)
    — which survives Patchright's `add_init_script` filter (it rides the CDP
    protocol, not an injected script) **and propagates to worker threads**.
    Verified with a real headless Chrome reporting two distinct zones
    (Europe/Berlin **and** Asia/Tokyo) — in the main thread *and* a Worker —
    with the actual wall-clock offset shifting, not just the label.
  - `vb geo info` shows the configured timezone **and** what the running browser
    actually reports — so you can prove the override took.
  - A geo-configured session always launches fresh (never claims a geo-less
    pre-warm), exactly like a proxied session. Setting a proxy **without** a geo
    override now logs a tz/IP-mismatch warning rather than leaking silently.
  - Distinct from the existing runtime `geolocation` (lat/lng) override; also
    exposed over MCP (`geo_set`/`geo_clear`/`geo_info`) alongside `proxy_*`.
  - **`navigator.language` is deliberately NOT overridden.** The only mechanism
    (Playwright's per-target `locale` / `Emulation.setLocaleOverride`) does not
    reach worker threads — it would leave a Worker reporting the host language
    while the main thread reports the override, a *hard* main-vs-worker mismatch
    (the exact class the 0.6.8 UA SharedWorker fix eliminated) and a stronger
    tell than the soft "language ≠ IP country" signal it would address. An
    English browser physically abroad is a common, unsuspicious profile; an
    impossible main≠worker language is not. (Empirically confirmed: timezone
    propagates to workers; locale does not.)

### Fixed — the audit's lower-severity tail (0.6.10 follow-through)
- **Three posture hardcodes now respect `VIBATCHIUM_DEFAULT_HEADED`.** The
  `go`-first auto-spawn, warm-recycle, and MCP `start` default all hardcoded
  `headless=True`, so opting the *whole daemon* headed (the documented promise)
  only half-worked. They now route through the canonical `resolve_headless()`.
  This also surfaced a latent bug: `VIBATCHIUM_MCP_HEADED_DEFAULT=1` was itself
  a **no-op** — it skipped forcing headless, but the daemon then defaulted to
  headless anyway, so it never actually produced a headed MCP session. It now
  forces headed as named (and defers to `VIBATCHIUM_DEFAULT_HEADED` otherwise).
- **Verb-log redaction keyed on fields that don't exist.** With `set_log_verbs`
  on, `route_add` redacted a phantom `json` field while leaving `headers` (which
  can carry `Authorization`) in the clear; `vision_type`'s typed `text` (a
  password vector, like `type`/`fill`) wasn't redacted at all. Both fixed.
  `secret_init`'s `{"key"}` entry redacted a nonexistent arg — the generated
  `key_b64` lives only in the *response* (gated behind `print_key`, never logged
  through the arg-only redactor), so the entry was removed rather than renamed
  to another field that would also never match (no dead config).

### Fixed — deeper adversarial audit (security + robustness)
- **WebRTC leak guard now applies on the `nodriver` backend too.** The
  patchright backend injects the WebRTC IP-handling flags whenever a proxy is
  set, but the `nodriver` launcher added `--proxy-server` *without* them — so a
  page could discover the real IP via STUN despite the proxy tunnel. The same
  guard flags are now added on the nodriver path.
- **REST shim could 500 on verbs whose tool name ≠ daemon command.** `/v1/tools`
  advertises MCP tool names (e.g. `is_state`), but `invoke()` passed the name
  straight to the daemon, which registers the command as `is` — so
  `POST /v1/is_state` failed. REST now translates name → daemon command (and
  applies the tool's arg-mapper), mirroring the MCP call path.
- **URL-bearing verb args are credential-masked in debug logs.** With
  `set_log_verbs` on, `go`/`verify_url`/`wait_url` could log a URL embedding
  `user:pass@host`. Their userinfo is now masked (`***@host`) — keeping the host
  visible for debugging while honoring "credentials never appear in logs."
  `_redact_for_log` also now always returns a copy (never the caller's dict).
- **`geo_info` takes the per-session lock before touching the live page.** It's
  a registry verb (holds only the registry lock), but it probes the running
  browser — so it now acquires `entry.lock` to avoid racing a concurrent
  session-scoped verb on the same page.
- **`delete_profile_dir` no longer races an in-flight pre-warm.** It scheduled
  the pre-warm cancel as fire-and-forget, then immediately `rmtree`'d — which
  could delete the profile out from under a launching Chrome. It now awaits the
  cancel first (made async; callers updated).
- **`close_all` cancels each pre-warm once.** A completed pre-warm lives in both
  `_warm_sessions` and `_warm_tasks`; the drain loop concatenated the lists and
  cancelled it twice. Now de-duplicated via a set union.

### Changed
- Dead code removed: an orphaned async `_run_one_cell` + its unreachable
  `if False` branch in `evals.py` (the sync path is the real one), and an unused
  `pg` closure param in `browser.py`. Stale docs corrected: the MCP "CLI is
  headed-default" comment (it's headless-default by design) and the `evals`
  default-matrix docstring (all three targets × patchright × humanize-off).

### Note
- Closes the audit tail tracked in 0.6.10. The same review confirmed `humanize`
  (which superseded the removed `--stealth-mouse`) is genuinely wired — Bezier
  paths (≥30 points), gaussian dwell, per-keystroke typing across all three
  click/type call sites — with unit + integration tests; re-verified live.

## [0.6.10] — 2026-06-10

### Fixed — three controls that silently did nothing
- **Goal `domain_allowlist` is now enforced.** `--allow-domains` (CLI) /
  `allow_domains` (MCP) was parsed, stored, surfaced, and inherited by subgoals
  — but never actually applied, so an autonomous goal could navigate anywhere.
  It is now enforced at the **navigation layer**: while a goal owns a session, a
  context-level guard aborts top-level navigations to any host that isn't an
  allowed host (or a subdomain of one), so the boundary holds however navigation
  is triggered — the `go` verb, a link click, an HTTP redirect, or JS
  `location=` — and it covers new tabs. The guard installs only while a goal
  pins an allowlist (no HTTP-cache cost on normal sessions). Matching is
  scheme/port-agnostic, subdomain-aware, refuses host-less URLs
  (`about:`/`data:`), and rejects suffix-confusion (`example.com.evil.com`).
- **Hidden-DOM injection scanner no longer desyncs on void elements.**
  `extract_hidden_text` pushed a visible/hidden stack frame for every start tag,
  but HTML void elements (`<img>`, `<br>`, `<input>`, …) never fire an end tag —
  so the stack drifted on nearly every real page, both missing genuine injection
  hidden in `display:none`/`aria-hidden` (false negative) and flagging
  plainly-visible text as hidden (false positive). Fixed: skip the push for void
  elements + handle explicit self-closing tags.
- **Proxy parse errors no longer leak credentials.** A malformed proxy URL with
  inline `user:pass@host` embedded the raw URL in the `ProxyParseError` message →
  the RPC error response and the daemon log. Credentials are now masked
  (`***@host`) at every raise site, restoring the "credentials never appear in
  logs" guarantee.

### Removed — `--stealth-mouse` (it never did anything)
- The `--stealth-mouse` flag (plus the `stealth_mouse` MCP arg and the `stealth/`
  package) built a CDP-Patches `AsyncInput` layer, reported `stealth_mouse: true`
  / "installed", then **never wired it into any mouse or keyboard operation** — a
  silent no-op since it shipped. `humanize on` already provides humanized input
  (Bezier mouse, gaussian typing) through the verified, hit-tested verb path and
  works headless, so it supersedes it. Removed rather than left advertising a
  capability it didn't deliver — use `humanize on`.

### Changed
- README refreshed: current PyPI version (the stale "0.1.0 is stale, install from
  git" note is gone) and an accurate test count.

### Note
- All four were surfaced by an adversarial audit prompted by the 0.6.9 warm-claim
  bug — every one the same class (a control that reports success while doing
  nothing). Lower-severity audit findings (remaining posture hardcodes, dropped
  log-redaction fields, doc drift) are tracked for 0.6.11.

## [0.6.9] — 2026-06-10

### Fixed — `--headed` silently served a headless pre-warm
- **A `--headed` start could be handed an already-headless pre-warmed Chrome.**
  The daemon opportunistically pre-warms Chrome headless (the non-TTY default),
  and `registry.create()`'s warm-claim guard matched on backend + profile +
  proxy but **omitted headless** — the one config its own comment promised to
  check ("the requested config matches (backend, headless, no proxy)"). Result:
  `--headed` "succeeded" (exit 0, files written) while opening zero windows,
  because it reused the headless warm. Latent until 0.6.4 made the default
  pre-warm headless.
- **Fix:** record the launch posture on the session (`BrowserSession.headless`,
  set in `launch_session` and the nodriver path) and add `warm.headless ==
  headless` to the claim guard. A posture-mismatched warm is now rejected
  (closed, and a fresh Chrome launched with the requested posture); a matching
  warm is still reused, so the pre-warm optimization is intact. A regression
  test asserts a `--headed` request never claims a headless pre-warm — and is
  verified to fail without the guard.

## [0.6.8] — 2026-06-10

### Fixed (stealth) — headless no longer announces `HeadlessChrome`
- **The headless default leaked the automation token on the wire.** Since 0.6.4
  made headless the default for every programmatic path (MCP, `go`-first
  auto-spawn, non-TTY CLI), new-headless Chrome (which Patchright launches via
  bare `--headless`) stamps `HeadlessChrome/<v>` into the User-Agent **string** —
  on every JS context (main page **and** SharedWorkers) and in the `User-Agent`
  request header, sent before any JS runs, so the cheapest edge/WAF rule
  (`UA contains "Headless"`) caught every agent session. The JS-runtime gate
  (sannysoft 31/31) never tested the UA, so it shipped silently. *(Measured: this
  is UA-string-only — the Sec-CH-UA client hints don't leak in new-headless;
  `userAgentData.brands` already reports `Google Chrome`.)*
- **Fix: a browser-wide `--user-agent` flag** set to the de-Headless'd string
  (`browser.coherent_headless_ua` probes the real Chrome UA once per daemon
  lifetime — no hardcoded version — and strips only the `Headless` marker;
  OS/platform/version preserved verbatim, not OS spoofing). A flag, not a
  Playwright `user_agent` context option, on purpose: the context option rides
  per-context `setUserAgentOverride`, which can't reach a SharedWorker — it would
  leave the worker UA saying `HeadlessChrome` while the main thread says `Chrome`,
  a mismatch that's a *stronger* tell than the original. The flag sets the
  browser's actual UA, covering every context. Headed is untouched (already
  reports `Chrome`). Propagated to the nodriver backend too (`uc.start`
  `browser_args`), which bypasses the patchright launch path.

### Added — headless posture gate
- **Two stealth-gate assertions** (`tests/test_wave7_stealth_gate.py`) pin no
  `Headless` token in `navigator.userAgent` on **both the main thread and a
  SharedWorker** — the SharedWorker assertion is the regression guard against
  reverting to the per-context mechanism. (The earlier `userAgentData.brands`
  assertion was dropped as vacuous — new-headless brands are already clean, so
  it could never fail.)

### Changed
- **Walled-page `advice` now leads with posture escalation.** On a detected
  wall, the hint suggests restarting **headed** first (stealthier, keeps cookies
  via the persistent profile) before the heavier nodriver backend swap.
- Corrected stale "headed is the default / headless not recommended for stealth"
  wording in the `start` MCP tool description and `registry.create` docstring
  (both predated 0.6.4).

### Known residuals (headless, unchanged)
- The UA fix closes the UA-string tell only. Headless still renders WebGL via
  Mesa **SwiftShader** and reports an **800×600** screen at **dpr 1** with a
  **0px scrollbar** (the last is Patchright's own `--hide-scrollbars`) —
  distinguishable from a real desktop by a CreepJS-class fingerprinter.
  `--window-size` does **not** cure the screen tell (moves only the viewport →
  incoherent window-larger-than-screen); only headed (or headed-under-Xvfb)
  clears these. Going headed on a wall (see advice above) is the escape hatch.

## [0.6.7] — 2026-06-07

### Added — on-system discoverability (agents reach for vb unprompted)
- **`vb setup` now installs an auto-discoverable Claude Code skill** at
  `~/.claude/skills/vibatchium/SKILL.md`, not just an MCP registration + a docs
  paragraph. Its `description` is the trigger the host matches to *auto-invoke*
  vb for the right tasks (walled sites, SPAs, login, multi-step, parallel)
  without the user naming it. Codex continues to use its `~/.codex/AGENTS.md`
  block. Cursor registers the MCP server only — Cursor has no user-scope
  auto-applied rule mechanism (global rules are plain-text in Settings; `.mdc`
  rules are project-scoped), so add an `.mdc` to a project's `.cursor/rules/`
  for per-project auto-invoke. The new `skill=` column in `vb setup` output
  reports each.

### Changed — curated default MCP surface (less tool overload)
- **`vb setup` registers the MCP server with a lean default cap set**
  (`core,nav,content,input,element,agent,vision,session,pages`) instead of the
  full ~145-verb surface — enough to browse, extract, interact, screenshot,
  switch tabs (OAuth/popup login), and run parallel sessions, without burying
  the ~10 verbs an agent actually reaches for. The long tail (network, devtools,
  secrets, goals, storage, dialogs…) stays one re-registration away via
  `--caps=all`. Existing registrations are untouched (setup short-circuits on
  already-registered).

## [0.6.6] — 2026-06-06

### Added — human-like input wired into the semantic verbs
- **`humanize on` now affects `click` and `type`** (previously only the
  low-level coordinate `mouse` verb). For `click @eN`: a Bezier mouse approach
  to a jittered interior point + pre-click hover + mouse-down dwell — but the
  actual click is still Playwright's verified, hit-tested `locator.click()`, so
  it can never land on the wrong element (humanization only adds motion +
  timing). For `type`: gaussian per-keystroke cadence, with the **total time
  bounded** so a long field can't exceed the RPC timeout. Explicit `--delay`
  bypasses the humanized typing path. Default OFF; opt in per session.
- Bulk `fill` is intentionally **not** humanized (it sets the value instantly,
  no keystroke events), so text entered via the high-level `observe`/`act` flow
  isn't humanized — use `type` for humanized keystrokes.

## [0.6.5] — 2026-06-05

### Added — profile-dir bloat prevention
- **`vb start --ephemeral`** — the session's profile dir is deleted when the
  session closes, so one-shot work leaves no cookies/login state on disk.
  Prevents the bloat that accrues when callers mint a fresh `--session` name
  per run. Never deletes `default`; refuses to delete any dir outside
  `~/.config/vibatchium/profiles/` (so an absolute `--profile` can't be
  rmtree'd); auto-disabled once a session becomes goal-owned (its checkpoints
  must persist). Also exposed on the MCP `start` tool.
- **`vb session prune --older-than <dur>`** — prune only profiles idle at least
  the given duration (`7d`, `12h`, `30m`, `2w`, or bare seconds), so a sweep
  reclaims stale per-run profiles without touching anything used recently.
  `session_list` rows gain a `last_active` field (newest mtime of the profile
  dir + immediate children) to drive it.
- **`vb clean`** — one-shot housekeeping. Dry-run by default (prints a
  reclaimable-space report); `--apply` to delete. Prunes stale profile dirs (by
  idle age, default 14d), removes leftover Chrome `SingletonLock`/`Socket`/
  `Cookie` files from stopped profiles (the "profile already in use" failures),
  clears regenerable caches (vision/observe caches, `screenshots/`, `explores/`),
  and truncates the daemon log to its last 256 KB. Never touches `default`, the
  active/running/warming sessions, `--keep` names, or the vision-spend ledger.
  Reachable under the `session` capability bucket.

## [0.6.4] — 2026-05-29

### Changed (behavior) — headless by default
- **The daemon now defaults to headless.** A background daemon owns no display,
  so popping visible Chrome windows for programmatic callers was the wrong
  default. Previously only the `vb start` *CLI* inferred headless from a missing
  TTY; callers that bypass the CLI — the `x.*` plugin, the xscraper reader,
  `research` fan-out, any direct `start` — fell through to a **headed** daemon
  default and opened windows on the operator's desktop.
- Now: **headless everywhere except an interactive human terminal.** `vb start`
  at a TTY still opens a visible window (visual debugging); everything else is
  headless. Precedence: explicit `--headless`/`--headed` → `VIBATCHIUM_DEFAULT_HEADLESS`
  → `VIBATCHIUM_DEFAULT_HEADED` → TTY inference → headless.
- New `VIBATCHIUM_DEFAULT_HEADED=1` to opt a whole daemon back into headed.
- Login/challenge flows that genuinely need a human (e.g. `xscraper login`) still
  request headed explicitly, so "headed only when essential" holds.

## [0.6.3] — 2026-05-28

### Added
- **Daemon version + staleness warning.** `ping`/`status` now report the daemon's
  version; `vb status` surfaces `daemon_version` vs `client_version` and warns
  (`⚠ daemon is running X but the CLI is Y — run vb update`) on a mismatch — so
  the "forgot to restart the daemon after upgrading" footgun is now visible.
- **`vb goal events --follow` (`-f`)** — live-tail a goal's event stream in the
  terminal, stopping on a terminal event (done/failed/cancelled) or Ctrl-C.

### Changed
- **`vb goal new` accepts a positional description** (`vb goal new "do X"`),
  matching `skill write` / `go` / `plugin show`; `-d/--description` still works.
- `checkpoint_saved` is no longer emitted with a null id when a goal has no live
  session to snapshot (removes a noisy event; an existing checkpoint id is kept).

## [0.6.2] — 2026-05-28

### Added
- **`vb update`** — one-command self-upgrade + daemon restart. Detects a pipx
  install (`pipx upgrade` / `pipx install --force`) else `pip install -U` with a
  PEP-668 `--break-system-packages` fallback, then stops the running daemon so
  the next command loads the new version (the long-running daemon serves old code
  until it's bounced). `--version` pins a release; `--no-restart` skips the bounce.
- README **Updating** section documenting `vb update` and the manual
  upgrade + `vb shutdown` flow.

## [0.6.1] — 2026-05-28

### Fixed
- **Added the missing `vb goal events <id>` CLI subcommand.** The `goal_events`
  daemon verb already existed (and was exposed over MCP, REST, and the SSE tail),
  and `goal show` embedded the stream, but there was no CLI wrapper — so the
  `vb goal events … --after-seq N` invocation documented in `AGENTS.md` errored.
  The CLI now has parity with the daemon verb. (Caught by the 0.6.0 live smoke test.)

## [0.6.0] — 2026-05-28

### Added — Plugins, Skills, Goals

- **Plugins** — extend the daemon's verb surface from third-party packages or
  local dirs. A plugin's `register(daemon)` calls `daemon.add_verb(name="ns.verb",
  handler, lock=…)`; dotted names dispatch identically over CLI, MCP, and REST
  and can never shadow a built-in. Discovery via pip entry points
  (`[project.entry-points."vibatchium.plugins"]`), local dirs
  (`~/.config/vibatchium/plugins/<name>/__init__.py`), and `git+` installs.
  New verbs: `plugin_list`, `plugin_show`, `plugin_reload`, `list_verbs`; CLI:
  `vb plugin list/show/install/remove/reload` and dotted passthrough
  (`vb x.search …`). Broken plugins are isolated (logged, never fatal). Disable
  discovery with `VIBATCHIUM_PLUGINS=0`.
  **Trust caveat:** plugin code runs in-process as your user, so
  `caps_required`/`secrets_required` on a `VerbSpec` are descriptive only — the
  daemon cannot enforce them against plugin code.
  `vb plugin install` is PEP-668 aware: it uses `pipx inject` under a pipx
  install, else `pip install` with a `--break-system-packages` retry (and prints
  the exact command) on `externally-managed-environment`.

- **Skills** — per-host Markdown field-notes under
  `~/.config/vibatchium/skills/<host>/` (browser-use `domain-skills` layout
  compatible). New verbs: `skill_list`, `skill_show`, `skill_write`, `skill_rm`,
  `skill_import` (CLI + MCP). Surfacing on `go`/`explore` is **opt-in** via
  `VIBATCHIUM_SKILLS=1` — when set, the navigation response carries a `skills`
  key with matching notes. Notes are **injection-scanned on read** (high-risk
  content withheld but still flagged) and **secret-scanned on write/import**
  (refused unless `skill write --allow-secrets`).

- **Goals** — durable, budget-capped, externally-driven tasks backed by SQLite
  (ULID ids, append-only event stream, crash-resume: `running`→`paused` on
  daemon restart). The daemon is the budget cop (steps / spend / wall-clock,
  hard-stop on exceed); the LLM is **not** run in the daemon — an external driver
  loops `goal next` → drive the browser → `goal step`. New verbs: `goal_new`,
  `goal_list`, `goal_show`, `goal_events`, `goal_next`, `goal_step`, `goal_ask`,
  `goal_answer`, `goal_done`, `goal_fail`, `goal_cancel`, `goal_pause`,
  `goal_resume`, `goal_spawn`, `goal_tree`, `goal_artifacts` (CLI + MCP).
  Notifiers: `stdout://`, `webhook://<full-url>` (non-blocking — POSTs run off the
  event loop), `mcp_push://` (no-op sink; read events back via `goal_events`).
  Pause/resume round-trips browser state via `checkpoint_save`/`checkpoint_load`.

### Changed

- Goal engine now routes all SQLite I/O through a thread executor so the daemon's
  single event loop never blocks on disk; webhook notifier POSTs run on their own
  thread (a slow endpoint can no longer stall every session).
- `goal step` idempotency now returns the **identical recorded result** for a
  replayed `client_token`, not just the step number.

## [0.5.1] — 2026-05-28

### Fixed (BLOCKERs surfaced post-rename audit)
- **`vb vision-find` crashed on every invocation** — click decorator declared
  `--min-confidence` but the function signature didn't accept it
  (`cli.py:1503`).
- **`vb secret init` silently destroyed existing vaults** — running it against
  an already-initialized `secrets.enc` would write a fresh keyring entry,
  rendering all prior entries permanently undecryptable. Now requires
  `--force` and raises `VaultAlreadyInitialized` otherwise
  (`secrets.py:119`).
- **`vb secret init` raw `ModuleNotFoundError: No module named 'nacl'`** —
  wrapped with install hint (`pip install vibatchium[secrets]`).
- **`vb serve` printed "REST listening" before crashing** on missing fastapi
  import — import check now runs first, no misleading banner (`rest.py:328`).
- **REST API OpenAPI version was hardcoded `"0.3.0"`** — now sources from
  `__version__` (`rest.py:114`).
- **xscraper cross-project import broken**: the in-tree rename of
  `patchium/` → `vibatchium/` left `~/projects/xscraper`'s editable
  install pointing at a non-existent package. xscraper's `pyproject.toml`,
  imports, and adapter file renamed to depend on `vibatchium`. All 48
  xscraper tests pass.

### Added
- **6 missing MCP tools registered**: `dblclick`, `focus`, `select`,
  `page_close`, `wait_selector`, `wait_ref` (`mcp_server.py`). The handlers
  always existed; only the MCP advertisement was missing. Tool count goes
  118 → 124.
- **`isError=True` on MCP error returns** — `vb mcp` errors are now
  spec-compliant; clients can distinguish failures from successful text
  returns without string-sniffing (`mcp_server.py:805`).
- **`vb session prune --yes`** — confirmation prompt required for destructive
  prune (parity with `session delete` and `profile delete`).
- **`vb record stop --output` required** — previously defaulted to
  `./trace.zip` and silently polluted CWD (`cli.py:1255`).
- **`vb status` stable shape post-shutdown** — same keys whether daemon is
  running or not. Scripts that key off `status["running"]` no longer break
  on shutdown (`cli.py:565`).
- **`vb mcp --caps=<bogus>`** now reports a clean `click.BadParameter`
  instead of a bare Python traceback (`cli.py:2047`).
- **Defensive token extraction in `vision.py`** — Anthropic SDK response
  shape drift now logs a warning instead of silently returning 0 (which
  would corrupt spend tracking).
- **`stealth-mouse` PID-extraction fix** — passes Chrome PID from Patchright
  internals instead of a `BrowserContext` (CDP-Patches 1.1 has a broken
  `isinstance` dispatch that can't accept the context). Tested-by-design
  on X11 + xdotool/wmctrl; cannot be smoke-verified on Wayland.

### Changed
- **23+ shipped error messages updated** `vibatchium <verb>` →  `vb <verb>`.
  Every error that hinted at "run `vibatchium foo`" was telling users to
  type a command that doesn't exist (the binary is `vb`).
- **Stealth-mouse docs** rewritten to reflect that `[stealth-mouse]` is no
  longer a pip extra; users install CDP-Patches via `pip install
  git+https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches.git@main`.
- **Dependency upper bounds** added to all extras and core deps
  (`patchright<2.0`, `click<9.0`, `mcp<2.0`, `anthropic<1.0`, `aiohttp<4.0`,
  `fastapi<2.0`, etc.). Future major upgrades won't silently break the
  install.

### Removed
- Dead code `{"sleep", "ping"} & {…}` union in `mcp_server.py:766` — the
  intersection was always empty (neither verb has an MCP tool entry).

### Operations notes for users upgrading from 0.5.0
- If you ran `vb` against a state directory containing live profiles, those
  remain in `~/.config/vibatchium/` and `~/.cache/vibatchium/` — no further
  migration needed.
- If you had a `secrets.enc` from 0.5.0 with no recoverable keyring entry,
  the new `vb secret init` will refuse to clobber it. Archive it (or pass
  `--force`) before re-initializing.

## [0.5.0] — 2026-05-27

### Breaking
- **Package rename**: `patchium` → `vibatchium`. Binary `patchium` → `vb`.
  No backwards-compat alias. Nothing was ever published as `patchium` on
  PyPI so external users are unaffected; local installs must
  `pip install vibatchium`.
- **State directories moved**: `~/.config/patchium/` → `~/.config/vibatchium/`,
  `~/.cache/patchium/` → `~/.cache/vibatchium/`. Manual migration required
  for existing profiles and the secrets vault.
- **Env var prefix renamed**: `PATCHIUM_*` → `VIBATCHIUM_*` across the
  runtime and tests (e.g. `PATCHIUM_DEFAULT_HEADLESS` →
  `VIBATCHIUM_DEFAULT_HEADLESS`).
- **MCP tool prefix renamed**: `mcp__patchium__*` → `mcp__vibatchium__*` —
  existing agent skills/configs referencing the old prefix must be updated.
- **`[stealth-mouse]` pip extra removed** — CDP-Patches is git-only and PyPI
  forbids `git+https://` deps in published metadata. Users who want
  stealth-mouse install separately:
  `pip install git+https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches.git@main`.

### Added
- **Wave 7.7.11**: tri-state `--headless/--headed` CLI flag with TTY-aware
  default. Agent/pipe contexts (no TTY) default to headless; interactive
  terminals default to headed. `VIBATCHIUM_DEFAULT_HEADLESS=1` overrides
  everywhere; explicit `--headless` / `--headed` always wins.
- **Wave 7.7.12**: bounded SPA-hydration wait after `goto` (5s, body.innerText
  > 100 chars). Fixes empty `text`/`screenshot` on Immunefi, HackerOne,
  bughunters.google.com. Opt out per-call with `wait_for_render=false`.
- **GitHub Actions Trusted Publishing** to PyPI via OIDC — no long-lived
  tokens. Tag pushes (`v*`) trigger build + publish + GitHub release.

### Fixed
- `network_start` accepts `url_filter` + `capture_response_headers`
  (pre-rename).
