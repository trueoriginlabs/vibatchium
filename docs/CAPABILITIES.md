# vibatchium capability reference

Complete list of the 127 daemon verbs an agent can invoke. Every verb is exposed identically over CLI, MCP, and REST. Verbs that need a target take an `@eN` ref (from the most recent `map`) or any Playwright selector (`text=...`, `role=...`, `css=...`).

## Lifecycle (6)
- `ping` — daemon health check
- `start` — launch real Chrome for the current session
- `attach` — connect to an existing Chrome via `--remote-debugging-port`
- `stop` — close Chrome for the current session (cookies persist)
- `shutdown` — close all sessions and exit the daemon
- `status` — current session + daemon state

## Sessions (7)
- `session_new <name>` — create profile dir + optionally pre-warm
- `session_list` — running + on-disk sessions with metadata
- `session_use <name>` / `session_switch <name>` — set the active session
- `session_close <name>` — stop Chrome; cookies stay on disk
- `session_close_all` — stop every running Chrome
- `session_delete <name>` — destroy the profile dir

Legacy aliases: `profile_list` / `profile_new` / `profile_use` / `profile_delete`.

## Navigation (8)
- `go <url>` — navigate with optional `wait_until` (load|domcontentloaded|networkidle|commit). Auto-detects walled pages.
- `verify_url <url>` — fast DNS / optional HTTP HEAD pre-check. ~50ms for ok, 3s timeout on dead DNS. Use before `go` to avoid burning 30s navigation timeouts on bad guesses.
- `back` / `forward` / `reload` — history
- `url` — current URL
- `title` — page title

## Content extraction (4)
- `text [target]` — visible text (whole page or one element)
- `html [target]` — outer HTML
- `content` — main article text via Readability heuristics
- `eval <expr>` — run JavaScript in an isolated context

## Element queries (6)
- `map` — accessibility snapshot with `@eN` refs (YAML)
- `map_compact` — one-liner-per-element compact format for token-tight prompts
- `diff_map` — structural diff vs the last snapshot
- `find <kind> <value>` — semantic find: text, label, placeholder, role, testid, xpath, alt, title, css
- `count <target>` — number of matches for a selector
- `is <target> <state>` — check state: visible, hidden, enabled, disabled, checked, focused

## Element attributes (2)
- `attr <target> <name>` — get attribute value
- `value <target>` — input value

## Interactions (12)
- `click <target> [--button] [--modifiers] [--auto-dismiss-banners]`
- `dblclick <target>`
- `fill <target> [--text | --use-secret <site:key>]`
- `type <target> <text> [--delay-ms]`
- `hover <target>` / `focus <target>`
- `press <target> <keys>` / `keys <keys>`
- `check <target>` / `uncheck <target>`
- `select <target> <value>`
- `scroll <target> <dx> <dy>`

## Low-level mouse (1)
- `mouse <action> [x] [y] [dx] [dy] [--button]` — actions: click, move, down, up, dblclick, wheel

## Pages / tabs / frames (5)
- `pages` — list open tabs
- `page_new` / `page_switch <idx>` / `page_close <idx>` — tab control
- `frames` — list iframes (live-only, dedupes stale)
- `frame [--name|--url|--clear]` — set the active frame target

## Element handles (5)
- `eval_handle <expr>` — eval and persist the returned JS handle
- `handle_eval <id> <expr>` — eval in the context of a stored handle
- `handle_list` — list active handles
- `handle_dispose <id>` / `handle_dispose_all` — release

## Visual + capture (5)
- `screenshot [--full-page] [--annotate]` — PNG; annotate overlays `@eN` bounding boxes (needs Pillow)
- `screenshot_annotate` — re-annotate an existing screenshot from the current map
- `highlight <target>` — flash a coloured box around an element
- `pdf [-o file.pdf]` — page export
- `viewport <w> <h>` — set viewport size

## Vision (6) — Claude Haiku
- `vision_click <description>` — locate → click
- `vision_find <description>` — locate only, return `(x, y)`
- `vision_type <description> <text>` — locate, click, type
- `vision_stats` — tokens + cost per session
- `vision_clear_cache` — nuke the perceptual hash cache
- `vision_budget [--daily USD] [--lifetime USD]` — set/show spend caps

## Wait helpers (7)
- `wait_load [--state]` — load state
- `wait_selector <selector>` — selector appears
- `wait_url <pattern>` — URL matches
- `wait_response <url-pattern>` — network response
- `wait_fn <expr>` — JS expression truthy
- `wait_ref <ref>` — `@eN` ref resolves
- `sleep <ms>`

## Files (4)
- `upload <target> <path>` — upload to `input[type=file]`
- `download_arm` — start capturing the next download
- `download_list` — completed downloads
- `download_save <id> <path>` — write a captured download to disk

## Dialogs + cookie banners (2)
- `dialog_policy <accept|dismiss> [--text]` — auto-handle alerts / confirms / prompts
- `dismiss_banners` — close cookie consent + GDPR popups via heuristic patterns

## Overrides (3)
- `geolocation <lat> <lng>` — spoof
- `media [--color-scheme=dark] [--reduced-motion] [--forced-colors]` — media emulation
- `fingerprint [--ua] [--timezone] [--locale] [--hardware-concurrency] [--device-memory]` — per-session fingerprint customization

## Network (10)
- `network_start [--max N]` — start ring-buffer capture
- `network_stop` / `network_dump [--format json|text]`
- `route_add <pattern> [--abort|--status|--json|--body]` — intercept + stub
- `route_list` / `route_clear`
- `har_start` / `har_stop [-o file.har]` — full HAR archive
- `proxy_set <url>` / `proxy_clear` / `proxy_info` — per-session proxy with WebRTC leak guard

## Storage (4)
- `storage_export [-o auth.json]` — cookies + per-origin LS/SS
- `storage_restore <auth.json>` — restore
- `cookies [--domain | --add | --delete]` — direct cookie read/write

## Checkpoints (4)
- `checkpoint_save <name>` — snapshot tabs + cookies + storage
- `checkpoint_list` — checkpoints for current session
- `checkpoint_load <name> [--from-session <other>]` — restore (optionally cross-session clone)
- `checkpoint_delete <name>`

## Tracing (2)
- `record_start` — begin Playwright trace
- `record_stop [-o trace.zip]` — write trace (Trace Viewer compatible)

## Agent orchestration (3)
- `observe <intent>` — accessibility tree relevant to the intent
- `act <intent> [--llm]` — observe + plan + execute. Heuristic by default, LLM mode when `ANTHROPIC_API_KEY` is set. Self-healing cache.
- `dismiss_banners` — called automatically by `act`

## Stealth (1)
- `fingerprint <target | --ua ...>` — measure score against sannysoft / creepjs / brotector, OR set per-session UA / timezone / locale / hardware

## Live view (3)
- `liveview_start [--port] [--takeover] [--insecure-public]` — WebSocket JPEG stream
- `liveview_stop`
- `liveview_url` — print viewer URL

## Credentials (6)
- `secret_init` — provision vault key in OS keyring
- `secret_set <site> <key> <value>` — store
- `secret_list [<site>]` — list keys (never values)
- `secret_delete <site> <key>`
- `secret_totp <site>` — current RFC 6238 TOTP code
- `wait_email_code <site> [--timeout] [--mark-read]` — poll IMAP for a verification code

## Safety (3)
- `safety_set <off|flag-only|wrap|redact>` — content scanning mode
- `safety_status` — current mode + last classification stats
- `safety_scan <text>` — one-shot classifier on arbitrary text

## Humanization (3)
- `humanize_on` / `humanize_off` / `humanize_status` — Bezier mouse, gaussian dwell, sinusoidal scroll

## Telemetry (1)
- `set_log_verbs <on|off>` — runtime toggle for the per-verb DEBUG audit log. No daemon restart needed.

## Evals (CLI only)
- `vb evals run --targets ... --backends ... --update-readme` — built-in benchmark runner

## Orchestration (CLI only)
- `vb research --target <url> --intent ... --intent ...` — parallel fan-out across N sessions; per-thread markdown + screenshot artifacts + an index.md

## Server modes (2)
- `mcp [--caps=...]` — stdio JSON-RPC MCP server with capability gating
- `serve [--host] [--port] [--insecure-no-auth] [--caps=...]` — FastAPI REST + WebSocket shim

## MCP capability buckets

```
core       start, attach, stop, status, set_log_verbs
session    session_new/list/use/switch/close/close_all/delete, profile_*
nav        go, verify_url, back, forward, reload, url, title, wait_url/load/fn
content    text, html, eval, attr, value, content, count, find
input      click, fill, type, hover, press, keys, check, uncheck, scroll,
           is_state, mouse, upload, humanize_*
element    map, map_compact, diff_map, highlight
pages      pages, page_new, page_switch, frames, frame
storage    storage_export, storage_restore, cookies,
           checkpoint_save/load/list/delete
network    network_start/stop/dump, route_add/list/clear, wait_response,
           har_start/stop, proxy_set/clear/info
dialogs    dialog_policy, download_arm/list/save
overrides  geolocation, media, viewport
vision     screenshot, screenshot_annotate, pdf,
           vision_click/find/type/stats/clear_cache/budget
devtools   record_start/stop, eval_handle, handle_eval/list/dispose
agent      observe, act, dismiss_banners
stealth    fingerprint
liveview   liveview_start/stop/url
secrets    secret_init/set/list/delete/totp, wait_email_code
safety     safety_set/status/scan
```

`status` is always exposed regardless of the cap filter.
