# AGENTS.md — patchium agent contract

If you're a coding agent (Codex, Cursor, Claude Code, etc.) operating in this repo and a user has told you to "use patchium," read this file first. It saves ~15 min of environment-discovery friction.

## TL;DR — the only commands you actually need

```bash
# The CLI binary lives at this path — no install, no PATH magic, no python -m invocation.
PB=/home/mono/projects/patchium/.venv/bin/patchium

# One-call "look at this URL" (returns text + screenshot, auto-closes session)
$PB explore https://example.com

# Parallel fan-out across N independent questions
$PB research \
  --target https://example.com \
  --intent "what's the pricing model" \
  --intent "who are the customers" \
  --intent "what tech stack" \
  --output-dir ./research-out

# Quick DNS pre-check before committing to a 30s nav timeout
$PB verify_url --url https://maybe-this-domain.example
```

That's 90% of what you need. Everything below is depth.

## DO NOT

- ❌ `pip install patchium` — system Python on Debian/Ubuntu is PEP-668-managed and will refuse. The `.venv` is already set up; use the binary directly.
- ❌ `python -m patchium.cli` — Debian/Ubuntu only has `python3`, not `python`. Even with `python3` you'd need the right venv on PATH.
- ❌ `patchium start` followed by `go` followed by `text` for a simple lookup. Use `explore` instead — one call, auto-headless, auto-closes session.
- ❌ Headed Chrome for background work. The `explore` and `research` commands default to headless; if you call `start` directly, pass `--headless` or set `PATCHIUM_DEFAULT_HEADLESS=1`.
- ❌ Direct domain probes without `verify_url` first. A bad URL guess burns a 30s navigation timeout. `verify_url` checks DNS in ~50ms.

## Tool routing

| Task shape | Use |
|---|---|
| "Look at this URL and tell me what's there" | `$PB explore <url>` |
| "Research N independent angles in parallel" | `$PB research --target <url> --intent ... --intent ...` |
| "Check if this domain exists / is reachable" | `$PB verify_url --url <url>` |
| Walled site (Cloudflare/Datadome 403) | `explore` works — patchright stealth clears most cold |
| Login-walled site (X, LinkedIn, hardened auth) | Won't work via cold launch. Manual login + `$PB attach http://localhost:9222` |
| Google search / news / Reddit threads | **Use WebSearch instead.** Patchium is overkill. |
| Plain HTML, known URL, single fetch | **Use WebFetch instead.** Patchium is overkill. |

For deeper recipes (mixed-tool workflows, env vars, anti-patterns from real runs): see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Daemon lifecycle

A single daemon process holds all sessions. Auto-spawns on first call. `$PB explore` and `$PB research` handle session lifecycle internally — you don't manage sessions for them.

For multi-step interactive work that DOES need explicit session management:

```bash
$PB session new mywork
$PB --session mywork start --headless     # explicit because `start` defaults to headed
$PB --session mywork go https://example.com
$PB --session mywork text
$PB --session mywork click @e3
$PB --session mywork session_close       # underscored MCP form works too
```

## Output handling

- `explore` returns JSON to stdout with `{url, title, text, screenshot_b64, status, elapsed_ms, closed}`. Pass `-o <dir>` to save the screenshot to disk + write a markdown summary.
- `research` writes per-thread markdown + landing screenshots + `index.md` to `--output-dir`.
- `screenshot` writes a PNG to `--path`.
- `text`/`html`/`content` print extracted content to stdout.

## When things fail

```bash
# What sessions did the daemon see, and when?
$PB logs --tail 50

# Were any pages walled by Cloudflare/Datadome?
$PB logs --since 10m | grep walled

# Did handlers error?
$PB logs --since 10m --errors-only

# Wipe stale sessions left from prior agent runs
$PB session prune --pattern <prefix>
```

## Useful environment overrides

These compose with `$PB daemon start` or just `export` before any `$PB` call:

```bash
PATCHIUM_DEFAULT_HEADLESS=1   # make `start` default headless (no desktop clutter)
PATCHIUM_MAX_SESSIONS=8       # raise the 4-session default for big fan-outs
PATCHIUM_LOG_VERBS=1          # full per-verb audit trail (also set PATCHIUM_LOG_LEVEL=DEBUG)
PATCHIUM_DEFAULT_SAFETY=wrap  # auto-flag prompt-injection patterns in scraped content
```

## Full per-verb reference

114 daemon verbs total — see [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md). You probably don't need most of them. `explore`, `research`, `verify_url`, and the basic `go`/`text`/`screenshot` triplet cover the 95% use case.

## Asking for help

If you're stuck, the operator-facing playbook is [`docs/OPERATIONS.md`](docs/OPERATIONS.md). It has recipes, anti-patterns, and observability rituals derived from real dogfood runs. Stealth posture + trade-offs are in [`docs/STEALTH.md`](docs/STEALTH.md).
