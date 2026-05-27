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
- ❌ Headed Chrome for background work. `explore`/`research` are headless; `start` invoked from an agent (no TTY) is headless too as of Wave 7.7.11. If you ever see a window pop up, you're either running from a real terminal or someone passed `--headed` — pass `--headless` explicitly or set `VIBATCHIUM_DEFAULT_HEADLESS=1` to force it.
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
VIBATCHIUM_DEFAULT_HEADLESS=1   # headless `start` (no desktop clutter)
VIBATCHIUM_MAX_SESSIONS=8       # raise 4-session default for big fan-outs
VIBATCHIUM_LOG_VERBS=1          # per-verb DEBUG audit trail
VIBATCHIUM_DEFAULT_SAFETY=wrap  # auto-flag prompt-injection in scraped content
```

## Going deeper

- Full verb reference: [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) — 127 verbs across 30 categories
- Operator recipes + anti-patterns from real runs: [`docs/OPERATIONS.md`](docs/OPERATIONS.md)
- Stealth posture + defender clearance: [`docs/STEALTH.md`](docs/STEALTH.md)
