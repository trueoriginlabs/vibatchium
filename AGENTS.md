# AGENTS.md — vibatchium agent contract

If you're a coding agent (Codex, Cursor, Claude Code) and a user said "use vibatchium," read this. Saves ~15 min of environment-discovery friction.

## First-time setup (for users)

```bash
pipx install git+https://github.com/trueoriginlabs/vibatchium
patchright install chrome   # optional preflight — the first launch auto-installs Chrome if missing
vb setup            # wire vibatchium into Codex / Claude Code / Cursor (idempotent)
```

After `setup`, any agent session in any cwd sees vibatchium as a registered MCP server. Restart agent sessions to pick up the registration.

## TL;DR — the commands you actually need

```bash
# In this repo the binary is .venv/bin/vb. With pipx install it's on $PATH.
VB=/home/mono/projects/vibatchium/.venv/bin/vb    # or just `vibatchium`

$VB explore https://example.com                       # one-call: text-first, auto-closes (screenshot only as a fallback)
$VB research --target https://example.com \           # parallel fan-out
  --intent "..." --intent "..." --output-dir ./out
$VB verify_url --url https://maybe-dead.example       # ~50ms DNS pre-check
```

90% of agent use cases. Below is depth.

## DO NOT

- ❌ `pip install vibatchium` — Debian/Ubuntu blocks system pip (PEP 668). The `.venv` is set up; use the binary.
- ❌ `python -m vibatchium.cli` — `python` doesn't exist on Debian, only `python3`. Use the binary.
- ❌ `start && go && text` for a simple lookup. Use `explore` — one call, auto-headless, auto-closes.
- ❌ Headed Chrome for background work. As of 0.6.4 **everything is headless by default** — `explore`/`research`, the `x.*` plugin, the daemon's `start`, all programmatic callers. Only an interactive human terminal (`vb start` at a TTY) pops a visible window. If a window appears during agent work, someone passed `--headed` or set `VIBATCHIUM_DEFAULT_HEADED=1`. To force headless even at a TTY: `VIBATCHIUM_DEFAULT_HEADLESS=1`.
- ❌ Direct domain probes without `verify_url`. A bad URL guess burns 30s of nav timeout; `verify_url` is 50ms.

## Tool routing

| Task | Use |
|---|---|
| "Look at this URL" | `$VB explore <url>` |
| "Give me the page as clean Markdown" | `$VB extract` (boilerplate stripped, LLM-ready; `--max-chars` caps it) |
| "Research N independent angles in parallel" | `$VB research --target <url> --intent ... --intent ...` |
| "Does this domain exist?" | `$VB verify_url --url <url>` |
| "Hit a JSON/API endpoint behind my login" | `$VB fetch <url>` (reuses session cookies+proxy+UA; needs `[fetch]` extra, `fetch` cap) |
| Walled site (Cloudflare/Datadome 403) | `$VB explore` — patchright stealth clears most cold |
| Login-walled (X, LinkedIn) | Manual login + `$VB attach http://localhost:9222` |
| Google / news / Reddit threads | **WebSearch**, not vibatchium |
| Plain HTML, known URL, single fetch | **WebFetch**, not vibatchium |

## Multi-step interactive

When `explore`/`research` aren't enough:

```bash
$VB session new mywork
$VB --session mywork start              # headless by default for agent / non-TTY use
$VB --session mywork go https://example.com
$VB --session mywork text
$VB --session mywork click @e3
$VB --session mywork session_close
```

A single daemon process holds all sessions. Auto-spawns on first call.

### Selector forms for click / type / fill / hover

All target arguments accept any of these forms — pick the one that matches
what you know about the element:

| Form | Resolves to |
|---|---|
| `@e3` | last `map`'s ref (refresh map after navigation) |
| `"Sign Up"` (bare text with space) | `page.get_by_text("Sign Up")` — auto-fallback |
| `@text:Sign Up` | `page.get_by_text("Sign Up")` |
| `@label:Email` | `page.get_by_label("Email")` |
| `@role:button` | `page.get_by_role("button")` |
| `@role:button[name=Submit]` | `page.get_by_role("button", name="Submit")` |
| `@placeholder:Search...` | `page.get_by_placeholder("Search...")` |
| `@testid:submit-btn` | `page.get_by_test_id("submit-btn")` |
| `#new-account-email` / `.btn-primary` | raw CSS |
| `text=Foo` / `role=button[name=X]` | raw Playwright selector engine |

**Pattern**: try visible text or label FIRST (`click "Sign Up"` or `type @label:Email "test@x.com"`). Only fall back to `html | grep` for CSS IDs when text/label/role don't disambiguate. The 7m51s Nemotron run on aave became a 30-second task with these selectors.

## Output

- `explore` → JSON to stdout `{url, title, text, screenshot_path?, screenshot_reason?, status, elapsed_ms, closed}`. **Text-first.** The MCP tool captures a screenshot *only* as a fallback when the page yields no usable text or is walled (`screenshot` = `auto`|`always`|`never`, `min_text_chars` tunes the auto threshold); when it does, the PNG comes back as a viewable image block, not base64. The CLI still screenshots by default, written to `~/.cache/vibatchium/explores/` (no base64 in stdout); `--auto-screenshot` makes the CLI text-first too, `-o <dir>` writes a chosen dir + markdown summary, `--inline-screenshot` returns base64 inline.
- `research` → per-thread markdown + landing screenshots + `index.md` in `--output-dir`.
- `screenshot` → PNG via `--path`. `text`/`html`/`content` → stdout.
- `extract` → `{markdown, chars, url?, title?, truncated?}`. Clean Markdown of the page (or a `target` subtree) with boilerplate stripped — the drop-in for "scrape this authenticated page to Markdown" that Crawl4AI/Firecrawl can't reach. Always text, never base64; `max_chars` (default 40000) caps it.
- `fetch` → `{status, ok, headers, body|body_b64, url, impersonate, cookie_sync, elapsed_ms}`. Authenticated HTTP fetch reusing the session's cookies+proxy+UA with a Chrome-matching JA3/HTTP2 fingerprint, **no renderer, no JS** — for JSON/XHR/static endpoints behind a login. It defeats the *static* TLS-fingerprint gate only: a DataDome/Kasada/Turnstile JS challenge will fail, so `go` instead. Cookies are one-way (browser→fetch); a `Set-Cookie` on the response is **not** persisted to the session. Needs `pip install vibatchium[fetch]`; gated behind the `fetch` cap (off in lean — grant `--caps fetch`).

## Debug

```bash
$VB logs --tail 50                    # session/error history
$VB logs --since 10m | grep walled    # Cloudflare/Datadome hits
$VB logs --since 10m --errors-only    # handler errors
$VB session prune --pattern <prefix>  # wipe stale sessions (by name)
$VB session prune --older-than 7d     # wipe sessions idle >7d (safer sweep)
$VB clean                             # dry-run: reclaimable disk report
$VB clean --apply                     # reclaim stale profiles/locks/caches/log
```

**Avoid profile-dir bloat.** Every distinct `--session <name>` leaves a
persistent profile dir under `~/.config/vibatchium/profiles/`. For throwaway
work, reuse a *bounded* pool of names (e.g. `work-0..3`) or pass
`$VB start --ephemeral`, which deletes the profile dir when the session closes
(never touches `default`; auto-disabled for goal-owned sessions). Run
`$VB clean` periodically to reclaim what accumulated.

## Reliability (0.7.0) — self-heal, leases, off-budget explore

**Self-healing renderer.** A Chrome `Page crashed` / `Target crashed` no longer
wedges a session until a manual restart. The daemon revives a fresh page (or
relaunches the dead context, reusing the same profile/proxy/geo and re-arming
any goal nav-allowlist) and retries the verb once. Read/navigation verbs retry
transparently; **mutating** verbs (`click`/`fill`/`type`/`press`/`upload`/`eval`,
all plugin verbs) recover the session but return `{ok:false, recovered:true}` so
a side-effect is never double-applied — re-issue the command. `vb status` and
`vb session list --json` carry a per-session `recovered` count. Disable with
`VIBATCHIUM_SELF_HEAL=0` (crash fails loudly instead).

**Session leases** coordinate concurrent clients sharing one session name. A
holder takes an advisory, TTL-bounded lease; non-holders get a clean `busy`
error instead of silently clobbering the page:

```bash
$VB session lease work --ttl 120 --owner my-scrape   # prints a token
$VB --lease-token <token> --session work go https://…
$VB session release work --token <token>             # or --force to break it
```

The lease is advisory (it gates session verbs + the disruptive registry verbs —
stop/close/delete/proxy/geo — but NOT `session_close_all`/`shutdown`/`clean`).
The token is resolved client-side (`--lease-token` / `VIBATCHIUM_LEASE`) and
never read daemon-side. Over MCP it's threaded per-call as the `lease` arg.

**Off-budget `explore`.** `vb explore URL` *without* `--session` now runs on a
throwaway ephemeral session (`_ex-<pid>-<seq>`) counted against a **separate**
`VIBATCHIUM_MAX_EPHEMERAL` budget — so one-shot lookups never compete with your
pinned/production sessions for a `VIBATCHIUM_MAX_SESSIONS` slot, and never touch
`default`. On this no-`--session` lane `--keep-open` is **ignored** (response carries
`keep_open_ignored: true`): the minted `_ex-` name is unaddressable and the slot is
always reclaimed on return. To keep a page open for follow-up calls, pin an explicit
`--session` — `explore` *with* a `--session` is unchanged. Worst-case live
Chromes = `MAX_SESSIONS + MAX_EPHEMERAL` (+ any warms).

## Env overrides

```bash
VIBATCHIUM_DEFAULT_HEADLESS=1   # force headless even at an interactive TTY
VIBATCHIUM_DEFAULT_HEADED=1     # opt a whole daemon back into headed windows
VIBATCHIUM_MAX_SESSIONS=8       # raise 4-session persistent default for big fan-outs
VIBATCHIUM_MAX_EPHEMERAL=2      # off-budget one-shot lane cap (0 disables explore's lane)
VIBATCHIUM_SELF_HEAL=0          # disable Chrome crash auto-recovery (fail loudly)
VIBATCHIUM_LEASE=<token>        # client-side lease token presented on every call
VIBATCHIUM_LOG_VERBS=1          # per-verb DEBUG audit trail
VIBATCHIUM_DEFAULT_SAFETY=wrap  # auto-flag prompt-injection in scraped content
VIBATCHIUM_SKILLS=1             # surface per-host skill notes on go/explore (opt-in)
VIBATCHIUM_PLUGINS=0            # disable plugin discovery at daemon startup
VIBATCHIUM_AUTO_INSTALL=0       # disable one-time Chrome auto-install on first launch (offline/CI)
VIBATCHIUM_DAEMON_IDLE_TIMEOUT=0  # seconds; >0 self-shuts an idle (0-session) daemon; 0/unset = disabled (default)
VIBATCHIUM_LOG_FILE=<path>      # full daemon-log path (default: a persistent state dir, see below)
VIBATCHIUM_LOG_MAX_BYTES=10485760 # rotate the daemon log past this size (0 = never rotate)
VIBATCHIUM_LOG_BACKUPS=5        # how many rotated daemon-log backups to keep
```

> **The daemon log is persistent (0.9.2).** It lives at
> `$XDG_STATE_HOME/vibatchium/daemon.log` (default `~/.local/state/vibatchium/daemon.log`),
> not the volatile `$XDG_RUNTIME_DIR` — so tracebacks / self-heal / ghost-readback
> history survive a reboot or daemon bounce. A `RotatingFileHandler` keeps it
> bounded (`VIBATCHIUM_LOG_MAX_BYTES` × `VIBATCHIUM_LOG_BACKUPS`); the socket,
> pidfile, and singleton lock stay in the runtime dir by design. If the state
> dir can't be created (read-only HOME), the log falls back to the volatile
> runtime dir — the pre-0.9.2 behaviour — rather than crashing. Old logs in the
> runtime dir are abandoned, not migrated.
>
> **Per-daemon log files (0.9.3).** The state dir is shared by every daemon for a
> user, but the log *filename* now carries a suffix derived from the runtime dir,
> so two daemons never write — or rotate-clobber — the same file. The **primary**
> daemon (default `/run/user/<uid>`) keeps the bare `daemon.log`; an isolated
> daemon (a custom `XDG_RUNTIME_DIR`, e.g. project-scouter's `scouter-vb`) writes
> `daemon-<name>-<hash8>.log` automatically. Isolating a daemon by
> `XDG_RUNTIME_DIR` now isolates its log too — no need to also set
> `VIBATCHIUM_LOG_FILE` (though that still overrides the whole path if you want
> an explicit location). `vb` readers resolve the path dynamically, so a CLI run
> with the same `XDG_RUNTIME_DIR` as the daemon reads the right file.

> **One daemon per `XDG_RUNTIME_DIR`.** As of 0.9.1 a daemon holds an exclusive
> `flock` for life, so duplicate/non-isolated `vb` calls can't spawn a second
> daemon that orphans the first. `vb daemon list` shows the live socket-owner vs
> any orphans (read-only; "orphan?" is relative to the current `XDG_RUNTIME_DIR`).
> Enable `VIBATCHIUM_DAEMON_IDLE_TIMEOUT` on dogfood/isolated daemons so a stray
> one-shot daemon self-reaps; leave it off (default) for long-lived bot daemons.

**MCP tool surface (0.8.0).** `vb mcp` exposes the **lean** profile (~80 verbs — the 80%-case: browse, extract, interact, screenshot, tabs, multi-session, the agent loop incl. `explore`/`expect`) by default, not all ~150. Pass `vb mcp --caps=full` (or `all`) for everything, or a custom bucket CSV. The long tail (network, devtools incl. `console_*`, secrets, goals, storage, **and plugin `x.*` verbs**) is one re-registration away — note the lean default also hides dotted plugin verbs, so pass `--caps=full` or `--caps=lean,plugins` if an agent needs them over MCP.

## Plugins — extend the verb surface

Third-party packages and local dirs can register new dotted verbs (`x.search`,
`stripe.charges`, …) that dispatch exactly like built-ins (over the socket, REST,
and MCP). Dotted names can never shadow a built-in.

```bash
vb plugin list                  # installed plugins + their verbs
vb plugin show xscraper         # one plugin's metadata + verb specs
vb plugin install <pypi|git+url># pipx inject / pip (+PEP-668 fallback), then reload
vb plugin reload                # rescan after editing a local-dir plugin
vb x.search "$BTC" --count 20   # call a plugin verb (dotted passthrough)
```

Local-dir plugins live in `~/.config/vibatchium/plugins/<name>/__init__.py` with
a top-level `register(daemon)` that calls `daemon.add_verb(name="ns.verb", …)`.
**Trust boundary:** plugin code runs in-process as your user — `caps_required`
is descriptive only, never enforced against plugin code.

## Skills — per-host field notes the agent writes for itself

Skills are per-host Markdown notes under `~/.config/vibatchium/skills/<host>/`.
When you learn something non-obvious about driving a site (the real search box,
a rate-limit quirk, a login gotcha), **write it down** so the next run starts
ahead:

```bash
vb skill write github.com --title "scraping" --body "Use /api/v3 — faster than UI."
vb skill list                   # hosts with notes
vb skill show github.com scraping.md
vb skill import git+https://… # browser-use domain-skills format compatible
```

Surfacing is opt-in: with `VIBATCHIUM_SKILLS=1`, `go`/`explore` attach a `skills`
key listing matching notes for the host. Notes are **injection-scanned on read**
(high-risk content withheld) and **secret-scanned on write** (refused if they
look like they contain a token/key — use `--allow-secrets` only when you're sure
it's a false positive).

## Goals — durable, budget-capped, externally-driven tasks

A *goal* is a persisted task with a budget (steps / spend / wall-clock), an event
stream, crash-resume, and per-goal session ownership. The daemon is the budget
cop; **you are the driver** — there's no LLM inside the daemon. Drive the loop:

```bash
vb goal new "buy cheapest BTC" --session work --budget steps=40,spend_usd=2
# then loop:
vb goal next                    # pick a runnable goal, lock its session, get context
#   … drive the browser with normal verbs (click/fill/go/…) …
vb goal step <id> --observation '{"price": 64000}'   # record one step (charges budget)
vb goal ask <id> "which card?" # pause for a human answer (→ needs_input)
vb goal done <id> --outputs '{"ok": true}'           # finish
```

`goal next` returns `{goal, recent_events, caps, domain_allowlist}`; `goal step`
hard-stops at the budget (`failed:budget_exceeded`). A goal left `running` when
the daemon dies is flipped to `paused` on restart and `goal next` can pick it
back up. Events persist in SQLite — poll `vb goal events <id> --after-seq N` (the
`mcp_push://` notifier is a no-op; the store is the source of truth). Sub-goals:
`goal spawn --parent <id>`; `goal tree <id>`; artifacts: `goal artifacts <id>`.

## Going deeper

- Full verb reference: `vb --help` and `vb <command> --help` — every CLI / MCP / REST verb.
