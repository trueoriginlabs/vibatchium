# AGENTS.md — patchium agent contract

If you're a coding agent (Codex, Cursor, Claude Code) and a user said "use patchium," read this. Saves ~15 min of environment-discovery friction.

## First-time setup (for users)

```bash
pipx install git+https://github.com/monodev-eth/patchium
patchright install chrome
patchium setup            # wire patchium into Codex / Claude Code / Cursor (idempotent)
```

After `setup`, any agent session in any cwd sees patchium as a registered MCP server. Restart agent sessions to pick up the registration.

## TL;DR — the commands you actually need

```bash
# In this repo the binary is .venv/bin/patchium. With pipx install it's on $PATH.
PB=/home/mono/projects/patchium/.venv/bin/patchium    # or just `patchium`

$PB explore https://example.com                       # one-call: text + screenshot, auto-closes
$PB research --target https://example.com \           # parallel fan-out
  --intent "..." --intent "..." --output-dir ./out
$PB verify_url --url https://maybe-dead.example       # ~50ms DNS pre-check
```

90% of agent use cases. Below is depth.

## DO NOT

- ❌ `pip install patchium` — Debian/Ubuntu blocks system pip (PEP 668). The `.venv` is set up; use the binary.
- ❌ `python -m patchium.cli` — `python` doesn't exist on Debian, only `python3`. Use the binary.
- ❌ `start && go && text` for a simple lookup. Use `explore` — one call, auto-headless, auto-closes.
- ❌ Headed Chrome for background work. `explore`/`research` are headless; if calling `start` directly, pass `--headless` or set `PATCHIUM_DEFAULT_HEADLESS=1`.
- ❌ Direct domain probes without `verify_url`. A bad URL guess burns 30s of nav timeout; `verify_url` is 50ms.

## Tool routing

| Task | Use |
|---|---|
| "Look at this URL" | `$PB explore <url>` |
| "Research N independent angles in parallel" | `$PB research --target <url> --intent ... --intent ...` |
| "Does this domain exist?" | `$PB verify_url --url <url>` |
| Walled site (Cloudflare/Datadome 403) | `$PB explore` — patchright stealth clears most cold |
| Login-walled (X, LinkedIn) | Manual login + `$PB attach http://localhost:9222` |
| Google / news / Reddit threads | **WebSearch**, not patchium |
| Plain HTML, known URL, single fetch | **WebFetch**, not patchium |

## Multi-step interactive

When `explore`/`research` aren't enough:

```bash
$PB session new mywork
$PB --session mywork start --headless
$PB --session mywork go https://example.com
$PB --session mywork text
$PB --session mywork click @e3
$PB --session mywork session_close
```

A single daemon process holds all sessions. Auto-spawns on first call.

## Output

- `explore` → JSON to stdout `{url, title, text, screenshot_path, status, elapsed_ms, closed}`. Screenshot written to `~/.cache/patchium/explores/` by default (no base64 in stdout). `-o <dir>` writes to a chosen dir + markdown summary. `--inline-screenshot` returns base64 inline (the old default).
- `research` → per-thread markdown + landing screenshots + `index.md` in `--output-dir`.
- `screenshot` → PNG via `--path`. `text`/`html`/`content` → stdout.

## Debug

```bash
$PB logs --tail 50                    # session/error history
$PB logs --since 10m | grep walled    # Cloudflare/Datadome hits
$PB logs --since 10m --errors-only    # handler errors
$PB session prune --pattern <prefix>  # wipe stale sessions
```

## Env overrides

```bash
PATCHIUM_DEFAULT_HEADLESS=1   # headless `start` (no desktop clutter)
PATCHIUM_MAX_SESSIONS=8       # raise 4-session default for big fan-outs
PATCHIUM_LOG_VERBS=1          # per-verb DEBUG audit trail
PATCHIUM_DEFAULT_SAFETY=wrap  # auto-flag prompt-injection in scraped content
```

## Going deeper

- Full verb reference: [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) — 127 verbs across 30 categories
- Operator recipes + anti-patterns from real runs: [`docs/OPERATIONS.md`](docs/OPERATIONS.md)
- Stealth posture + defender clearance: [`docs/STEALTH.md`](docs/STEALTH.md)
