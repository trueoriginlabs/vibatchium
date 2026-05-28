# Changelog

All notable changes to vibatchium are documented here. Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0,
minor bumps may include breaking changes; we'll always call them out here.

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
