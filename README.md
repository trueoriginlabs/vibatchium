# patchium

Patchwright stealth backend + Vibium-style LLM-friendly CLI for agentic browser automation.

> **Status: alpha, fully working.** Cleared HackerOne's Cloudflare wall on first cold-launch (no manual login, no attach). **65 MCP tools** registered. ~3,600 LoC of Python, 27 tests, all green.

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

## CLI surface

```
Lifecycle:    start  attach  stop  shutdown  status  install
Profiles:     profile list  profile new  profile use  profile delete
Navigation:   go  back  forward  reload  url  title
Content:      text  html  eval  attr  value  content
Elements:     map [--compact]  diff map  find  count  click  dblclick
              fill  type  hover  focus  press  keys  check  uncheck
              select  scroll  is  highlight
Frames:       frames  frame [--name|--url|--clear]
Pages:        pages  page new  page switch  page close
Mouse:        mouse click|move|down|up|dblclick|wheel  x y
Visual:       screenshot [--annotate]  viewport  pdf
Files:        upload  download arm|list|save
Dialogs:      dialog accept|dismiss  [--text]
Overrides:    geolocation  media
Network:      network start|stop|dump
Storage:      storage export  storage restore  cookies
Tracing:      record start  record stop  (Playwright Trace Viewer ZIP)
Waits:        wait selector  wait url  wait load  wait fn  sleep
Agents:       observe "<intent>"  act "<intent>"  [--llm]
Server:       mcp
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

End-to-end verified flows (all in the pytest suite):

1. **Lifecycle**: `start → status → stop → shutdown` + profile switch
2. **Navigation**: `go → back → forward` (about:blank-safe), `text`, `eval` (isolated context)
3. **Element model**: `map` (Playwright aria_snapshot AI mode), `map --compact` (browser-use one-liner format), `diff map`, `click @eN`, `fill`, `type`, `hover`, `press`, `keys`, `check`/`uncheck`, `select`, `scroll`, `is`, `highlight`
4. **Semantic find**: `find text|label|placeholder|role|testid|xpath|alt|title|css`
5. **Frames**: `frames` (live-only, dedupes stale), `frame --url=...` switch + locator-class verbs target the active frame
6. **Mouse**: `mouse click|move|down|up|dblclick|wheel` at pixel coordinates
7. **Visual**: `screenshot --annotate` overlays @eN bounding boxes via Pillow, `pdf` page export
8. **Files**: `upload` to `input[type=file]`, `download arm/list/save`
9. **Dialogs**: `dialog accept|dismiss --text=...` for alert/confirm/prompt
10. **Overrides**: `geolocation lat lng`, `media --color-scheme=dark`
11. **Network capture**: `network start/stop/dump` — ring buffer of request/response events
12. **Storage**: `storage export -o auth.json` (Playwright-compatible), `storage restore`
13. **Tracing**: `record start/stop -o trace.zip` (Playwright Trace Viewer compatible)
14. **Waits**: `wait selector|url|load|fn`, `sleep`
15. **Cloudflare-pass**: cold launch `go hackerone.com/anthropic` clears the wall
16. **Observe → act**: heuristic intent-matching with on-disk cache; LLM mode via `--llm` when `ANTHROPIC_API_KEY` is set
17. **Profiles**: `profile list|new|use|delete` for isolated browser identities (work vs recon vs personal)
18. **MCP**: `patchium mcp` exposes all 65 tools to Claude Code over stdio JSON-RPC, sharing the same daemon-managed browser

## License

Apache-2.0
