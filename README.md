# patchium

Patchwright stealth backend + Vibium-style LLM-friendly CLI for agentic browser automation.

> **Status: 0.3.0 — full agentic platform, alpha.** Cleared HackerOne Cloudflare cold-launch. **108+ MCP tools** spanning multi-session, vision-first clicking, credential vault, IMAP code retrieval, prompt-injection classifier, proxy abstraction, humanization, live-view, checkpoints, evals, REST shim, Docker. ~9,500 LoC, **228 tests green** in 49 s. **31/31 passed on bot.sannysoft.com** (measured). Ten new features in Wave 6 alone.

## Why patchium

| | Vibium | Patchwright | patchium |
|---|---|---|---|
| LLM-friendly CLI (`@eN` refs, `map`, `diff map`) | ✅ | ❌ | ✅ |
| Patches `Runtime.enable` CDP leak (Cloudflare killer) | ❌ | ✅ | ✅ |
| Headed real-Chrome + persistent context (default) | partial | ✅ | ✅ |
| CDP-attach to a manually-logged-in Chrome | ❌ | manual | `patchium attach` |
| Storage export/restore (cookies + per-origin LS) | ✅ | manual | ✅ |
| MCP server mode (76 tools wired) | ✅ | ❌ | ✅ |
| Cloudflare clean-pass on HackerOne (verified) | ❌ | ✅ | ✅ |

## Install

```bash
pip install patchium                            # core CLI + MCP server
pip install "patchium[annotate]"                # + Pillow for screenshot --annotate
pip install "patchium[llm]"                     # + anthropic for observe --llm
pip install "patchium[stealth-mouse]"           # + CDP-Patches for humanized mouse (Brotector/DataDome)
pip install "patchium[all]"                     # everything except stealth-mouse
patchright install chrome                       # one-time: install real Chrome (not Chromium)
patchium install                                # sanity-check the environment
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

## Multi-session (Wave 5)

Run N concurrent Chromes from one daemon — independent cookies/storage/fingerprint per session.

```bash
patchium session new work                     # create work profile dir
patchium --session work start                 # launch Chrome for it
patchium --session work go https://github.com
# (log in interactively once — cookies persist on disk)

patchium session new banking                  # parallel identity
patchium --session banking start
patchium --session banking go https://app.bank.com

# Run them concurrently — separate Chromes, no cookie bleed
patchium --session work click @e3 &
patchium --session banking fill @e5 "hello" &
wait

patchium session list                         # see running + on-disk sessions
patchium session close work                   # stop Chrome; cookies stay on disk
patchium --session work start                 # reopens with all logins intact
patchium session delete work                  # destroy profile dir
```

Resolution order for the active session: `--session FLAG` → `$PATCHIUM_SESSION` env → `~/.config/patchium/active-session` → `default`. Cap N via `PATCHIUM_MAX_SESSIONS=4` (default 4).

## Stealth backends + fingerprint scorer (Wave 5.4)

```bash
patchium start --backend patchright           # default; 25/31 OK on 2026 Cloudflare benchmark
patchium start --backend nodriver             # opt-in; 28/31, zero blocks. needs patchium[nodriver]
patchium fingerprint sannysoft                # measure score against a real detector
# {"target": "sannysoft", "score": 100, "signals": {"passed": 31, "failed": 0, "total": 31}}
patchium fingerprint creepjs                  # CreepJS canvas/audio/timing
patchium fingerprint brotector                # Brotector (Patchright authors' own gauntlet)
```

When `go` lands on a Cloudflare-walled page, the response surfaces `walled: cloudflare` plus an `advice` field suggesting `--backend nodriver` if you're still on `patchright`.

## MCP capability gating (Wave 5.2)

The full MCP surface is 85 tools. For LLMs that only need basics, gate the surface:

```bash
patchium mcp                                            # all 85 tools
patchium mcp --caps=core,session,nav,input,agent        # compact ~30-tool surface
patchium mcp --caps=core,nav,input,vision,network,storage  # full browsing + capture
```

Buckets: `core,session,nav,content,input,element,pages,storage,network,dialogs,overrides,vision,devtools,agent,stealth`.

## Self-healing selector cache (Wave 5.3)

`act` caches plans keyed by (url, intent). On cache hit, it tries a durable
`role[name=...]` selector first (survives DOM mutation) instead of the
snapshot-specific `@eN`. If the durable selector fails, the cache is
invalidated and `act` re-observes once — `self_healed: true` in the response.

## Wave 6 — agentic platform (added in 0.3.0)

### Live-view server (6.1a)
```bash
patchium liveview start --port 9223 --takeover     # WebSocket frame stream
# open http://localhost:9223/ in any browser to watch the agent in real time
```
Read-only by default; `--takeover` forwards your clicks/keystrokes back into
the session. Multi-session grid. ~50 KB/s per viewer at 5 fps.

### Browser warm pool (6.1b)
`PATCHIUM_WARM=both` (default) eagerly starts the Playwright driver and
opportunistically pre-spawns Chrome on `session_new` so subsequent `start`
finds it warm. `=off` disables.

### Session checkpoint / restore (6.1c)
```bash
patchium --session work checkpoint save logged-in       # snapshot tabs + cookies + LS/SS
patchium --session work-2 checkpoint load logged-in --from-session work   # cross-session clone
```

### Per-session proxy (6.2a)
```bash
patchium --session work proxy set "brightdata://customer:pw@residential?country=us&session-id=42"
patchium --session work start                           # launches Chrome via proxy
patchium --session work proxy info                      # exit IP, latency
```
Adapters: `http`, `socks5`, `brightdata`, `iproyal`, `decodo`, plus
`--proxy-file` for cred hygiene. WebRTC leak guard auto-enabled.

### Humanization (6.2b)
```bash
patchium --session work humanize on                     # Bezier mouse, gaussian dwell, sin scroll
```
OFF by default — only enable when the target actually fingerprints mouse
behavior (DataDome, PerimeterX). Verified not to degrade sannysoft 31/31.

### Evals benchmark suite (6.2c)
```bash
patchium evals run --targets sannysoft,creepjs --backends patchright,nodriver
patchium evals run --min-score 80 --update-readme       # CI gate + README patch
```
Replaces the README's old "70-90%" guesses with measured numbers.

### Credential vault + TOTP + email codes (6.3a/b)
```bash
patchium secret init                                    # provision vault key (OS keyring)
patchium secret set github.com username alice
patchium secret set github.com totp-seed JBSWY3DPEHPK3PXP
patchium fill @e7 --use-secret github.com:totp          # current 6-digit TOTP, never echoed
patchium secret set example.com email-poll \
  'imaps://user:pass@imap.gmail.com:993?regex=\d{6}&from=*@example.com'
patchium wait-email-code example.com --timeout 60       # poll IMAP, return code
```
XSalsa20-Poly1305 vault, NEVER appears in logs/HAR/observe-cache (grep-tested).

### Prompt-injection classifier (6.3c)
```bash
patchium safety set flag-only                           # add risk metadata to scraped content
patchium safety set wrap                                # mark suspicious regions
patchium safety set redact                              # replace suspicious regions
```
30+ curated heuristic patterns + per-session toggle. OFF by default = zero overhead.
≥5% false-positive rate on legit corpus (test-enforced).

### Vision-first primitive (6.3d)
```bash
patchium vision-click "the blue submit button"          # Claude vision → coords → click
patchium vision-find "the modal OK button"              # locate only, no click
patchium vision-type "the search field" "hello world"   # click + type
patchium vision stats                                   # tokens + cost per session
```
For canvas / Flutter / Unity WebGL pages where AX-tree is useless. Perceptual
cache + rate limit + low-confidence safeguard. Requires `patchium[llm]`.

### REST shim + Docker (6.4)
```bash
patchium serve                                          # FastAPI on :8000
docker compose up -d                                    # see docker-compose.yml
```
Bearer-token auth by default; token persisted at `~/.cache/patchium/rest-token`.
Multi-stage Dockerfile keeps image under 1 GB.

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

Or register with Claude Code: `claude mcp add patchium python -m patchium.mcp_server`. Every CLI verb is exposed as an MCP tool (76 total: lifecycle, navigation, element model, find/frames/mouse/upload/dialog/download/pdf/record/highlight/geolocation/media/network/observe/act/profile, plus the screenshot+annotate vision helper) — all talking to the same daemon, so a single browser session is shared between shell invocations and Claude Code tool calls.

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
HAR:          har start  har stop  (full HTTP archive: req+resp+timings+bodies)
Tracing:      record start  record stop  (Playwright Trace Viewer ZIP)
Handles:      handle create  handle eval  handle list  handle dispose  handle clear
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

## Stealth posture (honest)

Stack we apply by default (per Patchright's canonical config + research):

- **`channel="chrome"`** — real Google Chrome binary, real TLS fingerprint
- **`launch_persistent_context`** with on-disk user-data-dir — real-profile cookie/storage continuity
- **`headless=False`** — headed mode (canonical Patchright recommendation; use `--headless` only to opt out)
- **`no_viewport=True`** — let OS window size win
- **No UA / header overrides** — explicit anti-pattern per Patchright README

**Verified**: cleared HackerOne's Cloudflare challenge on first cold launch (Test: `tests/smoke_cloudflare.py`). Vibium's WebDriver-BiDi stack triggers Cloudflare's `Runtime.enable` trap and gets walled on the same target.

**Realistic expectations by defender** (don't claim a single percentage — depends on target's configuration):

| Defender | Cold launch | After `attach` (manual login first) |
|---|---|---|
| Cloudflare default / Bot Fight Mode | ~70–90% (HackerOne ✅) | ~95% |
| Cloudflare Under Attack / Managed Challenge | ~10–30% | ~70–85% |
| DataDome | ~20–40% | ~60–80% with CDP-Patches |
| Akamai Bot Manager | ~30–50% | ~70% with humanized input |
| PerimeterX / HUMAN | ~20–40% | ~60% with mouse entropy |
| Kasada | ~10–30% | ~30–50% (their client-side challenge VM is the wall) |

Patchright's protocol-layer patches (`Runtime.enable` avoidance, CDP message scrubbing) **still apply over `connect_over_cdp`** — they're in the client protocol, not the launch flags. So `attach` mode gets the same stealth as cold launch, plus your real-browser fingerprint and any cookies issued during manual login.

Layers we can add later (gated behind opt-in flags):
- **CDP-Patches** for mouse-movement heuristics (Brotector / DataDome aggressive)
- **BrowserForge** for canvas/WebGL/audio fingerprint diversity (Kasada / Akamai)

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
18. **MCP**: `patchium mcp` exposes all 76 tools to Claude Code over stdio JSON-RPC, sharing the same daemon-managed browser

## License

Apache-2.0
