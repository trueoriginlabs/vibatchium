# patchium

Patchwright stealth backend + Vibium-style LLM-friendly CLI for agentic browser automation.

> **Status: alpha, working end-to-end on phases 1–3.** Cleared HackerOne's Cloudflare wall on first cold-launch (no manual login, no attach). 36 MCP tools registered. ~750 LoC of Python.

## Why patchium

| | Vibium | Patchwright | patchium |
|---|---|---|---|
| LLM-friendly CLI (`@eN` refs, `map`, `diff map`) | ✅ | ❌ | ✅ |
| Patches `Runtime.enable` CDP leak (Cloudflare killer) | ❌ | ✅ | ✅ |
| Headed real-Chrome + persistent context (default) | partial | ✅ | ✅ |
| CDP-attach to a manually-logged-in Chrome | ❌ | manual | `patchium attach` |
| Storage export/restore (cookies + per-origin LS) | ✅ | manual | ✅ |
| MCP server mode (36 tools wired) | ✅ | ❌ | ✅ |
| Cloudflare clean-pass on HackerOne (verified) | ❌ | ✅ | ✅ |

## Install

```bash
git clone https://github.com/ClavIclar/patchium && cd patchium
python -m venv .venv && source .venv/bin/activate
pip install -e .
patchright install chrome             # one-time: install real Chrome (not Chromium)
```

Linux servers without a display: `Xvfb :99 -screen 0 1920x1080x24 &` then `export DISPLAY=:99` before launching. On a desktop with $DISPLAY already set, nothing extra needed.

## Quick start

```bash
patchium start                                # launches real Chrome (headed, persistent profile)
patchium go https://example.com
patchium map                                  # AX-tree snapshot with @eN refs
patchium click @e6                            # click the "Learn more" link
patchium url                                  # confirms navigation
patchium diff map                             # structural diff vs last snapshot
patchium screenshot -o page.png
patchium storage export -o auth.json          # cookies + LS/SS
patchium stop
```

## Attach mode — the practical Cloudflare workaround

For sites that wall even Patchwright's stealth (DataDome, Kasada, hardened auth flows), launch real Chrome yourself, log in by hand, and attach the daemon to that session — it inherits your real browser's fingerprint, including any cookies issued during the manual login.

```bash
# Step 1: launch real Chrome with remote debugging (NOT through patchium)
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/cdp-profile &

# Step 2: log into the walled site by hand in that window

# Step 3: attach patchium to the already-authenticated session
patchium attach http://localhost:9222
patchium go https://target.example.com        # now reads as your real browser
```

## MCP server mode

```bash
patchium mcp                                  # stdio JSON-RPC MCP server
```

Or register with Claude Code: `claude mcp add patchium python -m patchium.mcp_server`. Every CLI verb is exposed as an MCP tool (36 total: start/attach/stop/go/map/click/fill/type/hover/press/keys/eval/screenshot/text/html/storage/wait_url/wait_load/wait_fn/pages/page_new/page_switch/...) — all talking to the same daemon, so a single browser session is shared between shell invocations and Claude Code tool calls.

## CLI surface (Vibium-compatible where it makes sense)

```
Lifecycle:   start  attach  stop  shutdown  status
Navigation:  go  back  forward  reload  url  title
Content:     text  html  eval  attr  value
Elements:    map  diff map  click  dblclick  fill  type
             hover  focus  press  keys  check  uncheck  select  scroll  is
Visual:      screenshot  viewport
Storage:     storage export  storage restore  cookies
Waits:       wait selector  wait url  wait load  wait fn  sleep
Pages:       pages  page new  page switch  page close
Server:      mcp
```

`@eN` refs come from Playwright's `page.aria_snapshot(mode='ai')` and resolve via the `aria-ref=` selector engine. No DOM pollution, no script injection — the snapshot is the same one Playwright MCP uses internally, just rendered in Vibium's `@eN` notation.

## Architecture

```
┌──────────────┐    ┌──────────────────────────────┐    ┌─────────────────┐
│  CLI client  │ ──▶│ Daemon (Unix socket RPC)     │───▶│  Patchwright    │
│  shell user  │    │ ─────────────────────────── │    │  real Chrome    │
└──────────────┘    │  • async asyncio server     │    │  persistent ctx │
                    │  • holds page + AX snapshot │    └─────────────────┘
┌──────────────┐    │  • @eN ref resolver         │
│ MCP server   │ ──▶│  • storage / waits / pages  │
│ Claude Code  │    └──────────────────────────────┘
└──────────────┘
```

One long-lived browser, multiple thin clients (shell, MCP, future agents) talking to it over `$XDG_RUNTIME_DIR/patchium/daemon.sock` (or `~/.cache/patchium/daemon.sock`) via JSON-lines RPC. Page handle, element snapshots, and session storage live in the daemon. State survives across CLI invocations until `patchium shutdown`.

## Stealth posture

Stack we apply by default (per Patchright's canonical config + research):

- **`channel="chrome"`** — real Google Chrome binary, real TLS fingerprint
- **`launch_persistent_context`** with on-disk user-data-dir — real-profile cookie/storage continuity
- **`headless=False`** — headed mode (the canonical Patchright recommendation)
- **`no_viewport=True`** — let OS window size win
- **No UA / header overrides** — explicit anti-pattern per Patchright README

Verified: cleared HackerOne's Cloudflare challenge on first cold launch (Test: `tests/smoke_cloudflare.py`). Compare: Vibium's WebDriver-BiDi stack triggers Cloudflare's `Runtime.enable` trap and gets walled on the same target.

Layers we can add later (gated behind opt-in flags):
- **CDP-Patches** for mouse-movement heuristics (Brotector-class targets)
- **BrowserForge** for canvas/WebGL/audio fingerprint diversity

## What's working today

End-to-end verified flows:

1. `start → go example.com → map → click @e6 → diff map` — navigation + interactive verbs over @eN refs
2. `start → go hackerone.com/anthropic → screenshot` — Cloudflare cleared, full policy page rendered (135KB PNG)
3. `storage export -o auth.json` — produces a Playwright-compatible storage-state JSON
4. `wait load --state networkidle` / `wait fn 'document.title.length > 0'` / `sleep 500` — wait family
5. `patchium mcp` + MCP `tools/list` returns 36 tools

## License

Apache-2.0
