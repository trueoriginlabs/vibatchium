# AGENTS.md — vibatchium agent contract

If you're a coding agent (Codex, Cursor, Claude Code) and a user said "use vibatchium," read this. Saves ~15 min of environment-discovery friction.

## First-time setup (for users)

```bash
pipx install git+https://github.com/trueoriginlabs/vibatchium
patchright install chrome
vb setup            # wire vibatchium into Codex / Claude Code / Cursor (idempotent)
```

After `setup`, any agent session in any cwd sees vibatchium as a registered MCP server. Restart agent sessions to pick up the registration.

## TL;DR — the commands you actually need

```bash
# In this repo the binary is .venv/bin/vb. With pipx install it's on $PATH.
VB=/home/mono/projects/vibatchium/.venv/bin/vb    # or just `vibatchium`

$VB explore https://example.com                       # one-call: text + screenshot, auto-closes
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
| "Research N independent angles in parallel" | `$VB research --target <url> --intent ... --intent ...` |
| "Does this domain exist?" | `$VB verify_url --url <url>` |
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

- `explore` → JSON to stdout `{url, title, text, screenshot_path, status, elapsed_ms, closed}`. Screenshot written to `~/.cache/vibatchium/explores/` by default (no base64 in stdout). `-o <dir>` writes to a chosen dir + markdown summary. `--inline-screenshot` returns base64 inline (the old default).
- `research` → per-thread markdown + landing screenshots + `index.md` in `--output-dir`.
- `screenshot` → PNG via `--path`. `text`/`html`/`content` → stdout.

## Debug

```bash
$VB logs --tail 50                    # session/error history
$VB logs --since 10m | grep walled    # Cloudflare/Datadome hits
$VB logs --since 10m --errors-only    # handler errors
$VB session prune --pattern <prefix>  # wipe stale sessions
```

## Env overrides

```bash
VIBATCHIUM_DEFAULT_HEADLESS=1   # force headless even at an interactive TTY
VIBATCHIUM_DEFAULT_HEADED=1     # opt a whole daemon back into headed windows
VIBATCHIUM_MAX_SESSIONS=8       # raise 4-session default for big fan-outs
VIBATCHIUM_LOG_VERBS=1          # per-verb DEBUG audit trail
VIBATCHIUM_DEFAULT_SAFETY=wrap  # auto-flag prompt-injection in scraped content
VIBATCHIUM_SKILLS=1             # surface per-host skill notes on go/explore (opt-in)
VIBATCHIUM_PLUGINS=0            # disable plugin discovery at daemon startup
```

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

- Full verb reference: [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) — 127 verbs across 30 categories
- Operator recipes + anti-patterns from real runs: [`docs/OPERATIONS.md`](docs/OPERATIONS.md)
- Stealth posture + defender clearance: [`docs/STEALTH.md`](docs/STEALTH.md)
