# patchium

**Agent-piloted browser automation that clears Cloudflare.**
Patched Playwright + multi-session daemon + credential vault + vision clicking + prompt-injection safety. One MCP server, N parallel Chromes, persistent per-session profiles.

```
# Install from source — active development; the PyPI package may lag.
git clone https://github.com/monodev-eth/patchium && cd patchium
pip install -e ".[all]"               # core + every advertised feature
patchright install chrome             # one-time: real Chrome (not Chromium)
patchium install                      # sanity-check the environment

# Register with Claude Code (use python3, NOT python — fails on Debian/Ubuntu)
claude mcp add patchium python3 -m patchium.mcp_server
# Then restart Claude Code for the MCP to load.
```

Then either drive from a shell or from any MCP-speaking agent:

```
patchium start                        # launch headed Chrome, persistent profile
patchium go https://example.com
patchium map                          # AX-tree snapshot with @eN refs
patchium click @e6
patchium screenshot -o page.png
patchium stop
```

For a parallel research crawl in one command:

```
patchium research --target https://example.com \
  --intent "what's the pricing model" \
  --intent "who are the customers" \
  --intent "what tech stack do they use"
# → spawns 3 parallel Chromes, writes per-thread markdown + screenshots to ./patchium-research-<ts>/
```

**Status:** active development, alpha. **Install from source — the PyPI version lags.** 347+ tests green in ~54s. 31/31 on bot.sannysoft.com. Cleared HackerOne Cloudflare cold-launch. Apache-2.0 (GPL/AGPL only via opt-in extras).

## Why patchium

|  | Vibium | Patchwright | Browser-Use | patchium |
|---|---|---|---|---|
| LLM-friendly `@eN` refs + `map` / `diff map` | ✅ | ❌ | ❌ | ✅ |
| Cloudflare CDP-leak patches | ❌ | ✅ | ❌ | ✅ |
| **Multiple parallel browsers, one daemon** | ❌ | manual | ❌ | ✅ |
| Per-session persistent profile (cookies, login) | ✅ | manual | manual | ✅ |
| CDP-attach to a manually-logged-in Chrome | ❌ | manual | ❌ | ✅ |
| **Encrypted credential vault** (passwords + TOTP) | ❌ | ❌ | ❌ | ✅ |
| **IMAP email-code polling** (2FA) | ❌ | ❌ | ❌ | ✅ |
| Per-session proxy + WebRTC leak guard | ❌ | manual | ❌ | ✅ |
| Vision-first clicking with daily/lifetime spend cap | ❌ | ❌ | ✅ | ✅ |
| **Prompt-injection classifier on scraped content** | ❌ | ❌ | ❌ | ✅ (0% FP / 204-sample) |
| Live-view stream with takeover (WebSocket) | ❌ | ❌ | partial | ✅ |
| Bearer-token REST shim + caps gating | ❌ | ❌ | manual | ✅ |
| MCP server with 18 capability buckets | partial | ❌ | ❌ | ✅ 114 tools |
| `research` command (parallel fan-out) | ❌ | ❌ | ❌ | ✅ |

## Multi-session in 10 lines

```
patchium session new work
patchium --session work start
patchium --session work go https://github.com           # log in by hand once
patchium session new banking
patchium --session banking start
patchium --session banking go https://bank.example.com
patchium --session work click @e3 &                     # truly parallel —
patchium --session banking fill @e5 hi &                # separate Chromes, no cookie bleed
wait
patchium session list
```

Active-session resolution: `--session FLAG` → `$PATCHIUM_SESSION` env → `~/.config/patchium/active-session` → `default`. Cap via `PATCHIUM_MAX_SESSIONS=4` (default 4; bump for larger fan-outs).

## Brief for another agent

Paste this to another Claude / GPT / agent that has patchium MCP installed:

```
You have patchium installed as an MCP. It's a multi-session browser
automation daemon: each session is an independent real Chrome with its
own cookies + profile.

Key verbs (these are the MCP-style names with underscores; the
CLI form uses spaces — `session new`, `safety set wrap` — but you
can paste either form into a shell, the CLI auto-translates):
  session_new <name>     create a session
  start                  launch real Chrome for current session
  go <url>               navigate (auto-detects Cloudflare/Datadome walls)
  verify_url <url>       fast DNS pre-check before committing 30s nav timeout
  map / map_compact      AX-tree snapshot with @eN refs (LLM-friendly)
  click @eN / fill @eN <text>   interact
  text [target]          visible page or element text
  content                Readability-extracted main article
  eval <expr>            run JavaScript
  screenshot --full-page  PNG artifact
  act "<intent>"         heuristic plan → execute (self-healing selectors)
  session_close <name>   clean up

Every call accepts `session` to route to a specific browser. Without it,
calls go to the active session. For parallel work pass `session=<name>`
explicitly.

For a parallel research fan-out, prefer the one-shot CLI:
  patchium research --target <url> --intent "..." --intent "..." ...
It handles spawn / safety / crawl / cleanup for N threads in one call.

For non-trivial runs, enable verb-level audit logging up front:
  patchium daemon start --max-sessions 8 --log-verbs
  # then: patchium logs --session research-2 --tail 50 --since 10m
This is the only way to debug "why was thread N slow" — without verb
logging the daemon log only shows session create/close events.

Stealth: clears Cloudflare cold on most targets, sannysoft 31/31. In
HEADED mode a yellow "--disable-blink-features unsupported" infobar
shows — cosmetic only, invisible to all JS bot detectors, ignore it.

For credential-sensitive flows (login):
  secret_init                                  provision vault key
  secret_set <site> <key> <value>              store
  fill @eN --use-secret <site>:<key>           fill without logging
  wait_email_code <site>                       poll IMAP for verification code

For protection against prompt injection on scraped content:
  safety_set wrap                              auto-flag dangerous regions

Capability buckets for MCP gating (`patchium mcp --caps=...`):
  core, session, nav, content, input, element, pages, storage, network,
  dialogs, overrides, vision, devtools, agent, stealth, liveview,
  secrets, safety
```

## What's in the box

| Surface | Verbs |
|---|---|
| **Lifecycle** | `start`, `attach`, `stop`, `status`, `shutdown` |
| **Sessions** | `session_new/list/use/switch/close/close_all/delete` |
| **Navigation** | `go`, `verify_url`, `back`, `forward`, `reload`, `url`, `title`, `wait_*` |
| **Interactions** | `click`, `fill`, `type`, `hover`, `press`, `keys`, `check/uncheck`, `scroll`, `select`, `upload`, `mouse` |
| **Element queries** | `map`, `map_compact`, `diff_map`, `find`, `count`, `is`, `attr`, `value` |
| **Content** | `text`, `html`, `content`, `eval`, `eval_handle`, `handle_eval/list/dispose` |
| **Pages/frames** | `pages`, `page_new/switch/close`, `frames`, `frame` |
| **Visual** | `screenshot [--annotate]`, `highlight`, `pdf`, `viewport` |
| **Vision (Claude Haiku)** | `vision_click/find/type/stats/clear_cache/budget` |
| **Network** | `network_start/stop/dump`, `route_add/list/clear`, `har_start/stop`, `proxy_set/clear/info` |
| **Storage** | `storage_export/restore`, `cookies`, `checkpoint_save/load/list/delete` |
| **Credentials** | `secret_init/set/list/delete/totp`, `wait_email_code` |
| **Safety** | `safety_set/status/scan` (prompt-injection classifier) |
| **Overrides** | `geolocation`, `media`, `fingerprint` |
| **Live view** | `liveview_start/stop/url` (WebSocket frame stream + takeover) |
| **Telemetry** | `set_log_verbs` (runtime per-verb DEBUG audit log) |
| **Agents** | `observe`, `act` (heuristic + LLM modes, self-healing cache) |
| **Stealth** | `fingerprint` (sannysoft/creepjs/brotector scorer + per-session UA/timezone/locale) |

- Full per-verb reference: [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md)
- Operator playbook + env vars + recipes + anti-patterns from real runs: [`docs/OPERATIONS.md`](docs/OPERATIONS.md)
- Stealth posture + trade-offs: [`docs/STEALTH.md`](docs/STEALTH.md)

## Install

```
pip install patchium                  # core CLI + MCP server + daemon
pip install "patchium[all]"           # + annotate, llm, liveview, secrets, rest
pip install "patchium[nodriver]"      # + nodriver backend (AGPL, opt-in)
pip install "patchium[stealth-mouse]" # + CDP-Patches mouse heuristics (GPL, opt-in)
patchright install chrome
patchium install                      # sanity-check
```

Linux servers without a display: `Xvfb :99 -screen 0 1920x1080x24 & export DISPLAY=:99`.

## Server modes

| Mode | Surface | Auth |
|---|---|---|
| `patchium mcp` | stdio JSON-RPC; 114 tools; `--caps=...` gates the bucket set | n/a (stdio) |
| `patchium serve` | FastAPI on `127.0.0.1:8000`; every verb at `POST /v1/<verb>`; WebSocket live-view at `/v1/stream/<session>` | bearer token (`~/.cache/patchium/rest-token`, mode 0600) |

REST capability gating: `patchium serve --caps=core,nav,input,vision` restricts the HTTP surface the same way `mcp --caps` does. Without it, REST grants local-code-equivalent access (eval + secret_* + file-writing verbs all exposed) — safe for localhost dev, NOT for hosted/multi-tenant.

## Attach mode — the practical Cloudflare workaround

For DataDome / Kasada / hardened auth that walls cold-launch automation:

```
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/cdp-profile &
# log into the walled site by hand
patchium attach http://localhost:9222
patchium go https://target.example.com        # now reads as your real browser
```

Patchright's CDP-layer stealth still applies over `connect_over_cdp` — attach mode gets the same protocol-level patches as cold launch, plus your real-browser fingerprint and any cookies from the manual login.

## Stealth posture, honest

Default stack: real Chrome binary (`channel="chrome"`), `launch_persistent_context`, headed by default, `no_viewport=True`, no UA/header overrides, WebRTC leak-guard when proxy is set, sandbox enabled, `--no-sandbox` explicitly stripped.

| Defender | Cold launch | After `attach` (manual login first) |
|---|---|---|
| Cloudflare default / Bot Fight Mode | ~70–90% (HackerOne ✅) | ~95% |
| Cloudflare Under Attack / Managed Challenge | ~10–30% | ~70–85% |
| DataDome | ~20–40% (+humanize: ~50%) | ~60–80% with CDP-Patches |
| Akamai Bot Manager | ~30–50% | ~70% with humanized input |
| PerimeterX / HUMAN | ~20–40% (+humanize) | ~60% with mouse entropy |
| Kasada | ~10–30% | ~30–50% |

Full posture + trade-offs documented in [`docs/STEALTH.md`](docs/STEALTH.md).

## Security model — REST shim

Without `--caps`, any client holding the bearer token can invoke **every** verb including `eval`, `secret_*`, file-writing verbs (`screenshot`, `storage_export`, `download_save`, `pdf`, `har_stop`, `network_dump`, `record_stop`). That's local-code-equivalent access. For hosted-mode deployments **always** pass `--caps=...`. Live-view binds 127.0.0.1 only by default (`--insecure-public` to override).

Credentials never appear in logs, HAR captures, observe cache, or agent-visible response fields (grep-tested in CI). Vault uses XSalsa20-Poly1305 with key from OS keyring or `PATCHIUM_SECRETS_KEY` env. All patchium-written files are 0600; directories 0700 (enforced + tested in `tests/test_wave7_stealth_gate.py`).

## When to use patchium vs WebFetch / requests

Honest sizing — patchium isn't always the right tool. From real dogfood runs:

| Target | Recommended | Why |
|---|---|---|
| Known URL, plain HTML, single fetch | **WebFetch** | Faster, no daemon, no Chrome RAM |
| JS-rendered SPA (Antigravity, Devpost, paydaysuper.com.au, dashboards) | **patchium** | WebFetch returns boilerplate / empty; real browser executes the JS |
| Cloudflare / Datadome / Akamai walled | **patchium** | Stealth posture clears most walls cold-launch; WebFetch gets 403 |
| Need screenshot audit trail | **patchium** | `screenshot --full-page` produces verifiable PNGs WebFetch can't |
| Parallel multi-angle research (5+ independent questions) | **patchium research** | Sequential WebFetch costs N × per-fetch latency; daemon fans 5+ Chromes truly in parallel |
| Sequential queries against the same target (6 competitors back-to-back) | **patchium with `PATCHIUM_WARM_RECYCLE=1`** | Pre-warm recycle re-spawns the same profile so each next session finds it warm |
| Auth-gated sites (X, LinkedIn, hardened logins) | **patchium attach mode** | Manual login first, attach daemon; cold-launch fan-out can't defeat auth walls |
| Tiny one-shot scrape, no follow-up | **`curl` or WebFetch** | Don't spin up Chrome for one HTML fetch |

The asymmetric win for patchium is **primary-source extraction from JS-heavy sites + screenshot audit trail**. For the rest, WebFetch is competitive and often faster.

## Honest limits

- **5+ concurrent sessions = 1-2GB RAM.** Each persistent-context Chrome is ~200-400MB. Bump the cap with `patchium daemon start --max-sessions 8` (or set `PATCHIUM_MAX_SESSIONS=8` in the daemon env).
- **Vision spend cap is process-wide.** N fan-out agents share one daily/lifetime budget.
- **Init scripts don't work on patchright backend.** Patchright filters `Page.addScriptToEvaluateOnNewDocument` because the CDP call is itself a fingerprint signal. Consequence: `chrome.runtime` stays `undefined` (we trade this for the bigger stealth win — documented in `docs/STEALTH.md`).
- **Login walls (X, LinkedIn, etc.) require attach mode.** Cold-launch fan-out cannot defeat sites that require an authenticated session — even with patchium's stealth posture, you'll hit a login screen. Workaround: launch Chrome manually, log in by hand, then `patchium attach http://localhost:9222`. The credential vault feature can automate the typed-credentials step but you still need a real account on the target site.
- **Single daemon = single point of failure.** No HA built in.
- **PyPI version (0.1.0) is stale.** Active development; the `pip install patchium` package on PyPI is months behind. Install from source for the current feature surface — see the Quickstart block above.

## License

Apache-2.0 (core). Optional extras pull their own licenses: `nodriver` (AGPL-3.0), `stealth-mouse` / CDP-Patches (GPL-3.0). These are **never** required to install patchium itself.
