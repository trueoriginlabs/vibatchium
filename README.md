# patchium

> **Agent-piloted browser automation that actually clears Cloudflare.**
> Patched Playwright stealth backend + LLM-friendly CLI + multi-session daemon + credential vault + vision clicking + prompt-injection safety, all behind one daemon, all scriptable from a shell, MCP, or REST.

**Status: 0.3.0 — full agentic platform, alpha.**
- **249 tests green** in ~54s
- **31/31** on bot.sannysoft.com (measured), HackerOne Cloudflare cleared cold
- **112 MCP tools** across **18 capability buckets**
- **123 daemon verbs** total
- ~11k LoC, Apache-2.0 (core); GPL/AGPL-only via opt-in extras

## Table of contents
- [Why patchium](#why-patchium) — comparison to Vibium / Patchwright / Browser-Use
- [Install](#install)
- [Quick start](#quick-start) — five lines to clear Cloudflare
- [Multi-session](#multi-session) — N persistent Chromes, one daemon
- [Authenticated flows](#authenticated-flows) — vault + TOTP + IMAP email codes
- [Vision-first clicking](#vision-first-clicking) — for canvas / Unity / Flutter
- [Stealth backends + fingerprint scorer](#stealth-backends--fingerprint-scorer)
- [Per-session proxy](#per-session-proxy)
- [Humanization](#humanization)
- [Live view + REST stream](#live-view--rest-stream)
- [Session checkpoints](#session-checkpoints)
- [Network observability](#network-observability) — HAR, capture, route mocking
- [Prompt-injection safety](#prompt-injection-safety)
- [MCP capability gating](#mcp-capability-gating)
- [Self-healing selectors](#self-healing-selectors)
- [Evals harness](#evals-harness)
- [Attach mode](#attach-mode--the-practical-cloudflare-workaround)
- [REST shim + Docker](#rest-shim--docker)
- [Architecture](#architecture)
- [Stealth posture (honest)](#stealth-posture-honest)
- [**Agent capability reference**](#agent-capability-reference) — every verb, grouped
- [License](#license)

## Why patchium

| | Vibium | Patchwright | Browser-Use | patchium |
|---|---|---|---|---|
| LLM-friendly CLI (`@eN` refs, `map`, `diff map`) | ✅ | ❌ | ❌ | ✅ |
| Patches `Runtime.enable` CDP leak (Cloudflare killer) | ❌ | ✅ | ❌ | ✅ |
| Headed real-Chrome + persistent context (default) | partial | ✅ | partial | ✅ |
| CDP-attach to a manually-logged-in Chrome | ❌ | manual | ❌ | `patchium attach` |
| **Multiple parallel browsers from one daemon** | ❌ | manual | ❌ | ✅ (`--session`) |
| Per-session persistent cookies / login state | ✅ | manual | manual | ✅ (built-in) |
| Cloudflare clean-pass on HackerOne (verified) | ❌ | ✅ | ❌ | ✅ |
| MCP server with capability buckets | partial | ❌ | ❌ | 112 tools / 18 buckets |
| **Encrypted credential vault** (passwords + TOTP) | ❌ | ❌ | ❌ | ✅ (XSalsa20-Poly1305) |
| **IMAP email-code polling** (2FA flows) | ❌ | ❌ | ❌ | ✅ |
| **Per-session proxy** + WebRTC leak guard | ❌ | manual | ❌ | ✅ |
| **Humanized mouse / dwell / scroll** | ❌ | manual | ❌ | ✅ (toggle) |
| **Vision-first clicking** for canvas pages | ❌ | ❌ | ✅ | ✅ (with budget cap) |
| **Prompt-injection classifier** on scraped content | ❌ | ❌ | ❌ | ✅ (0% FP / 204-sample) |
| **Live-view server** with takeover | ❌ | ❌ | partial | ✅ (WebSocket) |
| **Session checkpoints** + cross-session clone | ❌ | manual | ❌ | ✅ |
| **REST shim** + Docker image | ❌ | ❌ | manual | ✅ (bearer-token auth) |
| **Evals harness** with auto-README patch | ❌ | ❌ | ❌ | ✅ |

## Install

```bash
pip install patchium                            # core CLI + MCP server + daemon
pip install "patchium[annotate]"                # + Pillow for screenshot --annotate
pip install "patchium[llm]"                     # + anthropic for observe --llm + vision-click
pip install "patchium[stealth-mouse]"           # + CDP-Patches humanized mouse (GPL, opt-in)
pip install "patchium[nodriver]"                # + nodriver backend (AGPL, opt-in)
pip install "patchium[liveview]"                # + aiohttp for the live-view server
pip install "patchium[secrets]"                 # + pynacl + keyring for the credential vault
pip install "patchium[rest]"                    # + fastapi + uvicorn for the REST shim
pip install "patchium[all]"                     # everything except GPL/AGPL extras
patchright install chrome                       # one-time: install real Chrome (not Chromium)
patchium install                                # sanity-check the environment
```

Linux servers without a display: `Xvfb :99 -screen 0 1920x1080x24 &` then `export DISPLAY=:99`. On a desktop with `$DISPLAY` already set, nothing extra needed.

## Quick start

```bash
patchium start                                  # real Chrome, headed, persistent profile
patchium go https://example.com
patchium map                                    # AX-tree snapshot with @eN refs
patchium click @e6                              # click the "Learn more" link
patchium url                                    # confirm navigation
patchium diff map                               # structural diff vs last snapshot
patchium screenshot -o page.png
patchium storage export -o auth.json            # cookies + per-origin LS/SS
patchium stop
```

## Multi-session

Run N concurrent Chromes from one daemon — independent cookies, storage, fingerprint, and proxy per session.

```bash
patchium session new work                       # create work profile dir
patchium --session work start                   # launch Chrome for it
patchium --session work go https://github.com
# log in interactively once — cookies persist on disk

patchium session new banking                    # parallel identity
patchium --session banking start
patchium --session banking go https://app.bank.com

# Run them concurrently — separate Chromes, no cookie bleed
patchium --session work click @e3 &
patchium --session banking fill @e5 "hello" &
wait

patchium session list                           # see running + on-disk sessions
patchium session switch banking                 # change the default for unflagged calls
patchium session close work                     # stop Chrome; cookies stay on disk
patchium --session work start                   # reopens with all logins intact
patchium session delete work                    # destroy profile dir
```

Active-session resolution: `--session FLAG` → `$PATCHIUM_SESSION` env → `~/.config/patchium/active-session` → `default`. Concurrency cap via `PATCHIUM_MAX_SESSIONS=4` (default 4).

**Warm pool** (on by default, disable with `PATCHIUM_WARM=off`): eagerly starts the Playwright driver and opportunistically pre-spawns Chrome on `session_new` so subsequent `start` finds it warm.

## Authenticated flows

Patchium handles end-to-end login flows — vault for passwords, TOTP for time-based codes, IMAP polling for email codes — without secrets ever appearing in logs, HAR captures, or AI-visible response fields.

```bash
patchium secret init                            # provision vault key (OS keyring)
patchium secret set github.com username alice
patchium secret set github.com password 'hunter2'
patchium secret set github.com totp-seed JBSWY3DPEHPK3PXP

patchium --session work go https://github.com/login
patchium --session work fill @e2 --use-secret github.com:username
patchium --session work fill @e4 --use-secret github.com:password
patchium --session work click @e6
patchium --session work fill @e9 --use-secret github.com:totp  # current 6-digit code
```

Email-code flow (for sites that send a one-time code instead of TOTP):

```bash
patchium secret set example.com email-poll \
  'imaps://user:pass@imap.gmail.com:993?regex=\d{6}&from=*@example.com'
patchium wait-email-code example.com --timeout 60 --mark-read
# returns the matched code; the message is flagged read so it won't be re-matched
```

- **Vault**: XSalsa20-Poly1305 AEAD via PyNaCl. Key sourced from OS keyring (Keychain, Secret Service, Windows Credential Locker) or `PATCHIUM_SECRETS_KEY` env (base64 32 bytes). Vault file mode 0600. **Grep-tested** in CI to never leak into logs, HAR, or the observe cache.
- **TOTP**: RFC 6238 HMAC-SHA1, pure stdlib, no third-party TOTP libs.
- **IMAP**: RFC 3501 over `imaplib.IMAP4_SSL`. Supports `from=`, `subject=`, `regex=`, `max-age-s=`, `mark-read=`. Polls every 2s by default.

## Vision-first clicking

For canvas / Flutter / Unity WebGL pages where the accessibility tree is empty or useless, patchium ships a vision-first locator powered by Claude Haiku.

```bash
patchium vision-click "the blue Sign Up button"        # locate → click
patchium vision-find "the modal OK button"             # locate only, no click (returns x,y)
patchium vision-type "the search field" "hello world"  # locate → click → type
patchium vision stats                                  # tokens + cost per session
patchium vision budget --daily 1.00 --lifetime 50.00   # hard caps; refuses calls when exceeded
patchium vision clear-cache                            # nuke the perceptual hash cache
```

- **Perceptual hash cache** — identical screenshots never re-bill the model.
- **Sliding-window rate limit** — protects against runaway loops.
- **Persistent spend log** — daily + lifetime running total written to disk.
- **Hard budget gate** — refuses the API call before it leaves the machine when caps are exceeded.

Requires `patchium[llm]` and `ANTHROPIC_API_KEY`.

## Stealth backends + fingerprint scorer

```bash
patchium start --backend patchright             # default: 31/31 sannysoft
patchium start --backend nodriver               # opt-in: zero-block alternative, requires patchium[nodriver]
patchium fingerprint sannysoft                  # measure against a real detector
# {"target": "sannysoft", "score": 100, "signals": {"passed": 31, "failed": 0, "total": 31}}
patchium fingerprint creepjs                    # CreepJS canvas/audio/timing
patchium fingerprint brotector                  # Patchright authors' own gauntlet
```

When `go` lands on a walled page, the response surfaces `walled: cloudflare|datadome|akamai|...` plus an `advice` field. Walled-page detector recognises 14+ challenge titles across Cloudflare, Datadome, PerimeterX, Akamai, hCaptcha, Sucuri, and Imperva.

Fingerprint customization per session:

```bash
patchium --session work fingerprint --ua "..." --timezone "America/New_York" \
  --locale "en-US" --hardware-concurrency 8 --device-memory 16
```

## Per-session proxy

```bash
patchium --session work proxy set "brightdata://customer:pw@residential?country=us&session-id=42"
patchium --session work start                   # launches Chrome via proxy
patchium --session work proxy info              # exit IP + latency
patchium --session work proxy clear             # back to direct
```

Adapters: `http`, `https`, `socks4`, `socks5`, `brightdata`, `iproyal`, `decodo`, plus `--proxy-file <path>` for credential hygiene (file must be 0600). WebRTC leak guard flags auto-enabled when a proxy is set.

## Humanization

OFF by default (zero overhead). Only enable when the target actually fingerprints input behavior (DataDome, PerimeterX, HUMAN).

```bash
patchium --session work humanize on             # Bezier mouse, gaussian dwell, sin scroll
patchium --session work humanize status
patchium --session work humanize off
```

Verified not to degrade the sannysoft 31/31 score.

## Live view + REST stream

Two ways to watch / take over a session in real time:

**Dedicated live-view server** (best for ops dashboards):

```bash
patchium liveview start --port 9223             # WebSocket JPEG stream + viewer HTML
patchium liveview start --port 9223 --takeover  # forward clicks + keys from viewer back into session
patchium liveview url                           # print the viewer URL
patchium liveview stop
# open http://localhost:9223/ in any browser
```

**REST WebSocket passthrough** (best when you already have the REST shim running):

```bash
patchium serve                                  # FastAPI on :8000
# from a browser, after grabbing the bearer token:
# ws://localhost:8000/v1/stream/work?token=<TOKEN>&fps=10&takeover=1
```

Read-only by default; `--takeover` accepts `{type:click,x,y}`, `{type:type,text}`, `{type:key,code}`, `{type:scroll,dx,dy}` events. ~50 KB/s per viewer at 5 fps. Live-view binds 127.0.0.1 only by default; `--insecure-public` required to bind 0.0.0.0.

## Session checkpoints

Snapshot a session's tabs + cookies + per-origin storage and restore it later, optionally into a **different** session for cloning logged-in state.

```bash
patchium --session work checkpoint save logged-in
patchium --session work checkpoint list
patchium --session work checkpoint load logged-in       # restore into same session
patchium --session work-2 checkpoint load logged-in --from-session work   # cross-session clone
patchium --session work checkpoint delete logged-in
```

## Network observability

Three layers of network insight, all independent and composable.

**HAR archive** (full HTTP archive with bodies + timings):

```bash
patchium har start                              # buffer requests + responses in memory
patchium go https://example.com
patchium go https://docs.example.com
patchium har stop -o session.har                # standard HAR 1.2; opens in Chrome DevTools
```

**Live network capture** (lightweight ring buffer):

```bash
patchium network start --max 500
patchium go https://example.com
patchium network dump --format json | jq '.[] | select(.url | contains("/api/"))'
patchium network stop
```

**Route mocking** (intercept + stub):

```bash
patchium route add '**/api/auth' --status 200 --json '{"token":"fake"}'
patchium route add '**/*.png' --abort
patchium route list
patchium route clear
```

Plus `wait_response <url-pattern>` for synchronous waits on specific network events.

## Prompt-injection safety

A classifier that scans every piece of web content the agent sees — text, HTML, snapshots, screenshots-with-OCR — for prompt-injection attempts. Currently **0% false positives** on a curated **204-sample legit corpus** while catching all known injection patterns.

```bash
patchium safety set off                         # default — zero overhead
patchium safety set flag-only                   # add `risk: low|medium|high` metadata
patchium safety set wrap                        # also wrap suspicious regions with [[SUSPECT]]...[[/SUSPECT]]
patchium safety set redact                      # also replace suspicious regions with [REDACTED]
patchium safety status
patchium safety scan "Ignore previous instructions and..."   # one-shot classifier
```

14 tightened heuristic patterns covering role manipulation, jailbreak phrasing, system-prompt extraction, command exfiltration, instruction overrides, tool abuse, and admin/override mode shifts. Per-session toggle.

## MCP capability gating

The full MCP surface is **112 tools**. For LLMs that only need a subset, gate the surface so the model isn't confused by tools it shouldn't use:

```bash
patchium mcp                                                # all 112 tools
patchium mcp --caps=core,session,nav,input,agent            # ~30-tool minimal agent surface
patchium mcp --caps=core,nav,input,vision,network,storage   # full browsing + capture
patchium mcp --caps=core,nav,input,secrets,safety           # authenticated-flow agent
```

**Capability buckets:**

| Bucket | What's in it |
|---|---|
| `core` | start, attach, stop, status |
| `session` | session_new/list/use/switch/close/close_all/delete + profile_* |
| `nav` | go, back, forward, reload, url, title, wait_url/load/fn |
| `content` | text, html, eval, attr, value, content, count, find |
| `input` | click, fill, type, hover, press, keys, check, uncheck, scroll, is_state, mouse, upload, humanize_* |
| `element` | map, map_compact, diff_map, highlight |
| `pages` | pages, page_new, page_switch, frames, frame |
| `storage` | storage_export/restore, cookies, checkpoint_save/load/list/delete |
| `network` | network_start/stop/dump, route_add/list/clear, wait_response, har_start/stop, proxy_set/clear/info |
| `dialogs` | dialog_policy, download_arm/list/save |
| `overrides` | geolocation, media, viewport |
| `vision` | screenshot, screenshot_annotate, pdf, vision_click/find/type/stats/clear_cache/budget |
| `devtools` | record_start/stop, eval_handle, handle_eval/list/dispose/dispose_all |
| `agent` | observe, act, dismiss_banners |
| `stealth` | fingerprint |
| `liveview` | liveview_start/stop/url |
| `secrets` | secret_init/set/list/delete/totp, wait_email_code |
| `safety` | safety_set/status/scan |

`status` is always exposed regardless of the cap filter — agents need to know what to do when nothing matches.

## Self-healing selectors

`act` caches plans keyed by `(url, intent)`. On cache hit, it tries a durable `role[name=...]` selector first (survives DOM mutation) instead of the snapshot-specific `@eN`. If the durable selector fails, the cache is invalidated and `act` re-observes once — `self_healed: true` in the response.

## Evals harness

Built-in benchmark runner against bot-detection sites, with optional auto-patching of the README between marker comments.

```bash
patchium evals run --targets sannysoft,creepjs --backends patchright,nodriver
patchium evals run --min-score 80 --update-readme       # CI gate + README patch
patchium evals run --format json -o results.json        # for downstream dashboards
```

Replaces the bad old "70-90%" guesses with measured numbers across (backend × target × humanize) tuples.

## Attach mode — the practical Cloudflare workaround

For sites that wall even Patchwright's stealth (DataDome, Kasada, hardened auth flows), launch real Chrome yourself, log in by hand, and attach the daemon to that session — it inherits your real browser's fingerprint, including any cookies issued during manual login.

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/cdp-profile &
# log into the walled site by hand in that window
patchium attach http://localhost:9222
patchium go https://target.example.com          # now reads as your real browser
```

Patchright's protocol-layer patches (`Runtime.enable` avoidance, CDP message scrubbing) still apply over `connect_over_cdp` — they're in the client protocol, not the launch flags. So `attach` mode gets the same stealth as cold launch, **plus** your real-browser fingerprint and any cookies issued during manual login.

## REST shim + Docker

```bash
patchium serve                                  # FastAPI on 127.0.0.1:8000
patchium serve --host 0.0.0.0 --port 8000       # bind public
patchium serve --insecure-no-auth               # disable bearer-token auth (explicit opt-in)
```

Bearer token is generated on first launch and persisted at `~/.cache/patchium/rest-token` (mode 0600). Every daemon verb is reachable at `POST /v1/<verb>` with the same JSON body. Routing to a session: `?session=<name>` query string or `session` body field. WebSocket live-view at `/v1/stream/<session>?token=<TOKEN>&fps=10&takeover=1`.

```bash
TOKEN=$(cat ~/.cache/patchium/rest-token)
curl -X POST http://localhost:8000/v1/go \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
curl http://localhost:8000/v1/tools \
  -H "Authorization: Bearer $TOKEN" | jq '.tools | length'
```

Docker (multi-stage build, image under 1 GB, tini as PID 1):

```bash
docker compose up -d                            # see docker-compose.yml
docker exec patchium patchium status
```

## Architecture

```
┌──────────────┐    ┌──────────────────────────────────┐    ┌─────────────────┐
│  CLI client  │ ──▶│ Daemon (Unix socket RPC)         │───▶│ Patchwright /   │
│  shell user  │    │ ──────────────────────────────── │    │ nodriver        │
└──────────────┘    │  • async asyncio server          │    │ real Chrome     │
┌──────────────┐    │  • SessionRegistry + ContextVar  │───▶│ session A       │
│ MCP server   │ ──▶│  • Shared Playwright driver      │    │ persistent ctx  │
│ Claude Code  │    │  • per-session asyncio.Lock      │    └─────────────────┘
└──────────────┘    │  • AX snapshot + @eN ref cache   │    ┌─────────────────┐
┌──────────────┐    │  • Warm pool / checkpoints       │───▶│ Chrome session B│
│ REST shim    │ ──▶│  • Vault, vision, safety, route  │    │ persistent ctx  │
│ FastAPI :8k  │    │  • Liveview WS frame stream      │    │ + proxy + UA    │
└──────────────┘    └──────────────────────────────────┘    └─────────────────┘
```

One long-lived daemon hosts N persistent Chrome sessions and routes calls through a ContextVar so existing handlers stay session-agnostic. Page handles, accessibility snapshots, element handles, vision cache, and network buffers all live in the daemon. State survives across CLI invocations until `patchium shutdown`. Socket path: `$XDG_RUNTIME_DIR/patchium/daemon.sock` (or `~/.cache/patchium/daemon.sock`).

## Stealth posture (honest)

Default stack (Patchright's canonical config + measured additions):

- **`channel="chrome"`** — real Google Chrome binary, real TLS fingerprint
- **`launch_persistent_context`** with on-disk user-data-dir — real-profile cookie/storage continuity
- **`headless=False`** — headed mode (canonical Patchright recommendation; use `--headless` only to opt out)
- **`no_viewport=True`** — let OS window size win
- **No UA / header overrides** — explicit anti-pattern per Patchright README
- **`--disable-dev-shm-usage`** — required for headless multi-session reliability
- **WebRTC leak guard flags** auto-applied when a proxy is set

**Verified**: cleared HackerOne's Cloudflare challenge on first cold launch. Vibium's WebDriver-BiDi stack triggers Cloudflare's `Runtime.enable` trap and gets walled on the same target.

**Realistic expectations by defender** (don't claim a single percentage — depends on target's configuration):

| Defender | Cold launch | After `attach` (manual login first) |
|---|---|---|
| Cloudflare default / Bot Fight Mode | ~70–90% (HackerOne ✅) | ~95% |
| Cloudflare Under Attack / Managed Challenge | ~10–30% | ~70–85% |
| DataDome | ~20–40% (+humanize: ~50%) | ~60–80% with CDP-Patches |
| Akamai Bot Manager | ~30–50% | ~70% with humanized input |
| PerimeterX / HUMAN | ~20–40% (+humanize) | ~60% with mouse entropy |
| Kasada | ~10–30% | ~30–50% (their client-side challenge VM is the wall) |

Layers that can be added (gated behind opt-in flags):
- **`humanize on`** — Bezier mouse / gaussian dwell / sinusoidal scroll (already shipped, default OFF)
- **CDP-Patches** mouse heuristics — `pip install patchium[stealth-mouse]` (GPL, opt-in)
- **nodriver** backend — `pip install patchium[nodriver]` (AGPL, opt-in)
- **BrowserForge** for canvas/WebGL/audio diversity (future, gated)

## Agent capability reference

Complete list of the 123 daemon verbs an agent can invoke. Every verb is exposed identically over CLI, MCP, and REST. Verbs that need a target take an `@eN` ref (from the most recent `map`) or any Playwright selector (`text=...`, `role=...`, `css=...`).

### Lifecycle (5)
`ping` — daemon health check.
`start` — launch real Chrome for the current session.
`attach` — connect to an existing Chrome via `--remote-debugging-port`.
`stop` — close Chrome for the current session (cookies persist).
`shutdown` — close all sessions and exit the daemon.
`status` — current session + daemon state.

### Sessions (7)
`session_new <name>` — create profile dir + optionally pre-warm.
`session_list` — running + on-disk sessions with metadata.
`session_use <name>` / `session_switch <name>` — set the active session.
`session_close <name>` — stop Chrome; cookies stay on disk.
`session_close_all` — stop every running Chrome.
`session_delete <name>` — destroy the profile dir.

### Profiles (legacy aliases — 4)
`profile_list` / `profile_new` / `profile_use` / `profile_delete` — backwards-compatible aliases over the session API.

### Navigation (7)
`go <url>` — navigate with optional `wait_until` (load|domcontentloaded|networkidle|commit). Auto-detects walled pages.
`back` / `forward` / `reload` — history.
`url` — current URL.
`title` — page title.

### Content extraction (4)
`text [target]` — visible text (whole page or one element).
`html [target]` — outer HTML (whole page or one element).
`content` — main article text via Readability heuristics.
`eval <expr>` — run JavaScript in an isolated context.

### Element queries (6)
`map` — accessibility snapshot with `@eN` refs (YAML).
`map_compact` — one-liner-per-element compact format for token-tight prompts.
`diff_map` — structural diff vs the last snapshot.
`find <kind> <value>` — semantic find: text, label, placeholder, role, testid, xpath, alt, title, css.
`count <target>` — number of matches for a selector.
`is <target> <state>` — check state: visible, hidden, enabled, disabled, checked, focused.

### Element attributes (2)
`attr <target> <name>` — get attribute value.
`value <target>` — input value.

### Interactions (12)
`click <target> [--button] [--modifiers] [--auto-dismiss-banners]` — click.
`dblclick <target>` — double-click.
`fill <target> [--text | --use-secret <site:key>]` — fill input.
`type <target> <text> [--delay-ms]` — type with per-keystroke delay.
`hover <target>` — hover.
`focus <target>` — focus.
`press <target> <keys>` — press key combo on element.
`keys <keys>` — press key combo on document.
`check <target>` / `uncheck <target>` — checkboxes.
`select <target> <value>` — select-option.
`scroll <target> <dx> <dy>` — scroll element.

### Low-level mouse (1)
`mouse <action> [x] [y] [dx] [dy] [--button]` — actions: click, move, down, up, dblclick, wheel.

### Pages / tabs / frames (5)
`pages` — list open tabs.
`page_new` / `page_switch <idx>` / `page_close <idx>` — tab control.
`frames` — list iframes (live-only, dedupes stale).
`frame [--name|--url|--clear]` — set the active frame target.

### Element handles (5)
`eval_handle <expr>` — eval and persist the returned JS handle.
`handle_eval <id> <expr>` — eval in the context of a stored handle.
`handle_list` — list active handles.
`handle_dispose <id>` / `handle_dispose_all` — release handles.

### Visual + capture (5)
`screenshot [--full-page] [--annotate]` — PNG; annotate overlays `@eN` bounding boxes (needs Pillow).
`screenshot_annotate` — re-annotate an existing screenshot from the current map.
`highlight <target>` — flash a coloured box around an element.
`pdf [-o file.pdf]` — page export.
`viewport <w> <h>` — set viewport size.

### Vision (6)
`vision_click <description>` — Claude Haiku locates → clicks.
`vision_find <description>` — locate only, return `(x, y)`.
`vision_type <description> <text>` — locate, click, type.
`vision_stats` — tokens + cost per session.
`vision_clear_cache` — nuke the perceptual hash cache.
`vision_budget [--daily USD] [--lifetime USD]` — set/show spend caps.

### Wait helpers (6)
`wait_load [--state]` — wait for load state.
`wait_selector <selector>` — wait for selector to appear.
`wait_url <pattern>` — wait for URL to match.
`wait_response <url-pattern>` — wait for a network response.
`wait_fn <expr>` — wait for a JS expression to be truthy.
`wait_ref <ref>` — wait for an `@eN` ref to resolve.
`sleep <ms>` — explicit sleep.

### Files (4)
`upload <target> <path>` — upload to `input[type=file]`.
`download_arm` — start capturing the next download.
`download_list` — completed downloads.
`download_save <id> <path>` — write a captured download to disk.

### Dialogs (1)
`dialog_policy <accept|dismiss> [--text]` — auto-handle alerts / confirms / prompts.

### Cookie banners (1)
`dismiss_banners` — close cookie consent and GDPR popups via heuristic patterns.

### Overrides (3)
`geolocation <lat> <lng>` — spoof geolocation.
`media [--color-scheme=dark] [--reduced-motion] [--forced-colors]` — media emulation.
`fingerprint [--ua] [--timezone] [--locale] [--hardware-concurrency] [--device-memory]` — per-session fingerprint customization.

### Network (10)
`network_start [--max N]` — start ring-buffer capture.
`network_stop` — stop capture.
`network_dump [--format json|text]` — dump captured events.
`route_add <pattern> [--abort|--status|--json|--body]` — intercept + stub.
`route_list` / `route_clear` — route management.
`har_start` — begin HAR archive.
`har_stop [-o file.har]` — write HAR.
`proxy_set <url>` / `proxy_clear` / `proxy_info` — per-session proxy.

### Storage (4)
`storage_export [-o auth.json]` — cookies + per-origin LS/SS (Playwright-compatible).
`storage_restore <auth.json>` — restore.
`cookies [--domain | --add | --delete]` — direct cookie read/write.

### Checkpoints (4)
`checkpoint_save <name>` — snapshot tabs + cookies + storage.
`checkpoint_list` — checkpoints for current session.
`checkpoint_load <name> [--from-session <other>]` — restore (optionally cross-session).
`checkpoint_delete <name>` — remove a checkpoint.

### Tracing (2)
`record_start` — begin Playwright trace.
`record_stop [-o trace.zip]` — write trace (Trace Viewer compatible).

### Agent orchestration (3)
`observe <intent>` — return accessibility tree relevant to the intent.
`act <intent> [--llm]` — observe + plan + execute. Heuristic by default, LLM mode when `ANTHROPIC_API_KEY` is set. Self-healing cache.
`dismiss_banners` — see above (also called automatically by `act`).

### Stealth (1)
`fingerprint <target | --ua ...>` — measure score against sannysoft, creepjs, brotector, OR set per-session UA/timezone/locale/hardware.

### Live view (3)
`liveview_start [--port] [--takeover] [--insecure-public]` — WebSocket JPEG stream.
`liveview_stop` — stop server.
`liveview_url` — print viewer URL.

### Credentials (6)
`secret_init` — provision vault key in OS keyring.
`secret_set <site> <key> <value>` — set a secret.
`secret_list [<site>]` — list keys (never values).
`secret_delete <site> <key>` — remove a secret.
`secret_totp <site>` — generate current RFC 6238 TOTP code.
`wait_email_code <site> [--timeout] [--mark-read]` — poll IMAP for a verification code.

### Safety (3)
`safety_set <off|flag-only|wrap|redact>` — content scanning mode.
`safety_status` — current mode + last classification stats.
`safety_scan <text>` — one-shot classifier on arbitrary text.

### Humanization (3)
`humanize_on` / `humanize_off` / `humanize_status` — Bezier mouse, gaussian dwell, sinusoidal scroll.

### Evals (1)
`evals` (CLI only) — `patchium evals run --targets ... --backends ... --update-readme`.

### Server modes (2)
`mcp [--caps=...]` — stdio JSON-RPC MCP server with capability gating.
`serve [--host] [--port] [--insecure-no-auth]` — FastAPI REST + WebSocket shim.

## License

Apache-2.0 (core). Optional extras pull in their own licenses: `nodriver` (AGPL-3.0), `stealth-mouse` / CDP-Patches (GPL-3.0). These are **never** required to install patchium itself.
