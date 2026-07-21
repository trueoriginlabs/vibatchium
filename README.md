# vibatchium

<!-- mcp-name: io.github.trueoriginlabs/vibatchium -->

**Agent-piloted browser automation that clears Cloudflare.**
Patched Playwright + multi-session daemon + credential vault + vision clicking + prompt-injection safety. One MCP server, N parallel Chromes, persistent per-session profiles.

```
pipx install vibatchium             # core: browse / extract / screenshot / N parallel sessions
# want the stealth HTTP fetch lane (vb fetch), the credential vault, VLM read, or the REST shim?
pipx install 'vibatchium[all]'      # everything; or pick extras: vibatchium[fetch], [secrets], [llm], [rest]
patchright install chrome
vb setup                    # register MCP + an auto-discoverable skill so agents reach for vb (idempotent)
```

Core install covers all browsing. `vb fetch` (the curl_cffi TLS-fingerprint lane) is the
`[fetch]` extra; `vb install` reports which optional lanes are available. On a **uv** venv
(no pip), add an extra with `uv pip install --python <venv>/bin/python curl_cffi`.

> Bleeding edge from `master`: `pipx install 'git+https://github.com/trueoriginlabs/vibatchium#egg=vibatchium[all]'`

> **Coding agents (Codex / Cursor / Claude Code):** read [`AGENTS.md`](AGENTS.md) first — it has the one-call recipes (`explore`, `research`) and the env-discovery traps to skip.

```
vb explore https://example.com                      # one-call: text-first (screenshot only as a fallback)
vb research --target https://example.com \          # parallel fan-out, N intents
  --intent "pricing model" --intent "customers" --intent "tech stack"
```

**Status:** active development, alpha. 606 tests green. 31/31 on bot.sannysoft.com. Cleared HackerOne Cloudflare cold-launch. Apache-2.0 (AGPL only via the opt-in `nodriver` extra).

## Updating

```bash
vb update                  # upgrade to the latest PyPI release + restart the daemon
vb update --version 0.6.8  # or pin a specific version
```

`vb update` detects how vibatchium was installed (pipx, `uv tool install`,
a pip-less uv venv, or pip with a PEP-668 `--break-system-packages` fallback)
and then **stops the running daemon** so the next command loads the new code.
Manual equivalent:

```bash
pipx upgrade vibatchium    # or: uv tool upgrade vibatchium / pip install -U vibatchium
vb shutdown                # bounce the daemon — it serves old code until you do
vb --version               # confirm
```

> The daemon-restart step is the one people miss: the long-running daemon keeps
> serving the **old** version until it's bounced. `vb update` does it for you;
> if you upgrade by hand, run `vb shutdown` (the next `vb` call auto-respawns the
> new version). Optional features upgrade via `pipx install 'vibatchium[all]' --force`.

## Why vibatchium

|  | Vibium | Patchwright | Browser-Use | vibatchium |
|---|---|---|---|---|
| LLM-friendly `@eN` refs + `map` / `diff map` | ✅ | ❌ | ❌ | ✅ |
| Cloudflare CDP-leak patches | ❌ | ✅ | ❌ | ✅ |
| **Multiple parallel browsers, one daemon** | ❌ | manual | ❌ | ✅ |
| Per-session persistent profile (cookies, login) | ✅ | manual | manual | ✅ |
| CDP-attach to manually-logged-in Chrome | ❌ | manual | ❌ | ✅ |
| **Encrypted credential vault** (passwords + TOTP) | ❌ | ❌ | ❌ | ✅ |
| **IMAP email-code polling** (2FA) | ❌ | ❌ | ❌ | ✅ |
| Per-session proxy + WebRTC leak guard | ❌ | manual | ❌ | ✅ |
| Vision-first clicking with spend cap | ❌ | ❌ | ✅ | ✅ |
| **Prompt-injection classifier on scraped content** | ❌ | ❌ | ❌ | ✅ (0% FP / 204 samples) |
| Live-view stream with takeover (WebSocket) | ❌ | ❌ | partial | ✅ |
| Bearer-token REST shim + caps gating | ❌ | ❌ | manual | ✅ |
| `research` command (parallel fan-out) — CLI only | ❌ | ❌ | ❌ | ✅ |

## Real Chrome vs fake Chrome

A wave of "headless browser for AI agents" tools rebuild the browser from scratch
(Rust + V8, no Blink/Skia) to hit tiny memory and sub-100ms page loads. The catch
is structural: **with no rendering engine, they can't produce a real device's
fingerprint — they synthesize one.** And synthetic fingerprints don't hold still.

vibatchium drives *real* Google Chrome, so its fingerprints are real — and, more
to the point, **stable**. The single test that separates the two is fingerprint
stability across navigations. Run the same canvas + WebGL probe on two pages in
one session:

| | vibatchium (real Chrome) | synthesized-fingerprint engines |
|---|---|---|
| canvas hash, page A → page B | **identical** | reseeded per navigation |
| WebGL `readPixels` | real, **deterministic** pixels | often `Math.random()` |
| WebGL renderer | a real ANGLE renderer¹ | stub / zeros |

<sub>¹ Chrome's own software renderer (SwiftShader) by default — still a coherent, deterministic Chrome value, not a stub. A hardware-GPU string (e.g. `ANGLE (Intel …)`) needs the opt-in `--gpu` flag.</sub>

A real device returns the same fingerprint every page load; a fingerprint keyed
off `Date.now()` does not — and *that inconsistency* is exactly what lie-detection
fingerprinters (CreepJS and friends) flag. Measured: vibatchium's canvas hash and
WebGL readback are byte-identical across navigations, and CreepJS reports **0 %
stealth-tampering** (no synthetic-environment signatures).

**This is not a claim of invisibility.** The moat is fingerprint *authenticity*,
not hiding that a browser is automated — vibatchium still reads as headless on the
headless-specific tells (see [Honest limits](#honest-limits)), and real-GPU WebGL
(`--gpu`) is opt-in. But real, consistent fingerprints pass the consistency tier
that synthetic ones fail *by construction* — and that tier is what stands between
you and a login wall.

## Multi-session in 10 lines

```
vb session new work
vb --session work start
vb --session work go https://github.com           # log in by hand once
vb session new banking
vb --session banking start
vb --session banking go https://bank.example.com
vb --session work click @e3 &                     # truly parallel —
vb --session banking fill @e5 hi &                # separate Chromes, no cookie bleed
wait
vb session list
```

Active-session resolution: `--session FLAG` → `$VIBATCHIUM_SESSION` env → `~/.config/vibatchium/active-session` → `default`. Cap via `VIBATCHIUM_MAX_SESSIONS=8` (default 8).

### Multi-agent: shared sessions vs a private daemon

On **one** shared daemon, sessions give real fingerprint isolation (separate
Chromes, no cookie bleed) but share the host: the session count budget, the
memory, and the blast radius of an OOM or a daemon bounce. Two models, pick per
trust level:

- **Cooperating agents (your own fleet):** the shared daemon is right — just give
  each concurrent agent a **unique `--session` name** so stateful flows don't
  collide on `default`. `vb session lease` coordinates a shared name.
- **A private blast radius:** a **per-agent daemon** on its own socket + `HOME`
  — separate profiles/config/state, its own session budget, zero contact with
  the shared daemon. `vb daemon start --isolated` prints the `XDG_RUNTIME_DIR`/
  `HOME` to export for subsequent calls; `vb mcp --isolated` runs the MCP server
  on its own private daemon directly. `vb daemon reap` cleans up abandoned ones.
  (Same UID = same trust domain — this bounds *blast radius*, not a security
  boundary between distrusting tenants; for that, separate UIDs/containers.)

**Resource governance.** The session cap bounds process *count*, not bytes. On a
shared box, set `VIBATCHIUM_SESSION_RAM_FLOOR_MB` to refuse a new launch when free
memory is low (a portable admission belt). For a hard ceiling, run the daemon
under a cgroup — `systemd-run --user --scope -p MemoryMax=4G vb daemon start` puts
the daemon **and all its Chromes** in one cgroup sharing the limit: an *aggregate*
daemon-wide cap (not per-renderer), and a breach OOM-kills inside the scope, which
can include the daemon. It's the only non-racy memory bound, so size it for the
whole fan-out.

**Idle CPU.** Parked sessions can't burn cores either: the daemon SIGSTOPs a
launched session's renderer processes after `VIBATCHIUM_IDLE_FREEZE_AFTER` seconds
with no verb (default 90) and thaws them on the next call, so an idle WebGL /
animation page drops to zero CPU without a teardown (default on;
`VIBATCHIUM_IDLE_FREEZE=0` disables).

## Documentation

- [`AGENTS.md`](AGENTS.md) — coding-agent contract (Codex / Cursor / Claude Code)

## Server modes

| Mode | Surface | Auth |
|---|---|---|
| `vb mcp` | stdio JSON-RPC; defaults to the **lean** ~80-verb profile (`--caps=full`/`all` for the full surface; `--caps=...` for a custom bucket set) | n/a (stdio) |
| `vb serve` | FastAPI on `127.0.0.1:8000`; every verb at `POST /v1/<verb>`; WebSocket live-view at `/v1/stream/<session>` | bearer token (`~/.cache/vibatchium/rest-token`, mode 0600) |

**REST capability gating**: `vb serve --caps=core,nav,input,vision` restricts the HTTP surface the same way `mcp --caps` does. Without it, REST grants local-code-equivalent access (eval + secret_* + file-writing verbs all exposed) — safe for localhost dev, **not** for hosted/multi-tenant.

## Stealth tiers — what clears what

Stealth is a ladder, not a boolean. Pick the lowest tier that clears your target
(higher tiers cost more setup / a visible browser / a manual login). vibatchium
does **not** claim cold-launch defeat of behavioral walls — those need a real
human-driven session, and attach-mode is the honest answer.

| Tier | How | Clears | Doesn't clear |
|---|---|---|---|
| **Standard** (default) | headless cold launch, real `channel=chrome`, de-Headless'd UA | Cloudflare IUAM / managed challenge, `bot.sannysoft` 31/31, JS-runtime fingerprinting | aggressive Turnstile, DataDome/Kasada, anything behind a login |
| **Hardened** | retry `--headed`; `vb humanize on`; `--backend nodriver` (`pip install vibatchium[nodriver]`, AGPL) for the hardest Cloudflare gates | aggressive Cloudflare/Turnstile, GPU/screen tells that headless leaves | behavioral biometrics, DataDome/Kasada sensor-fusion |
| **Attach** | `vb attach` to a Chrome **you** launched and logged into | DataDome / Kasada / HUMAN behavioral walls, and any authenticated session — your real fingerprint + cookies | nothing here is automated cold; it needs the human login first |

### Measured scores

`vb evals --update-readme` writes measured numbers into the block below, so
what we publish is generated rather than asserted. It is empty until someone
runs it — an empty block is honest; a number with no run behind it is not.

<!-- vibatchium-evals -->
_No eval run has been published yet. Generate with:_ `vb evals --update-readme`
<!-- /vibatchium-evals -->

> **What these do and don't cover.** These are *fingerprint scoreboards* —
> the static axis. Through 2026 the major anti-bot vendors moved to
> session-lifetime **behavioural** scoring, which none of these targets
> measure, and which we have **not** measured against any commercial vendor.
> Treat a good score here as evidence about environment coherence only.

For the behavioural axis itself, `vb oracle run` is a self-hosted probe: it drives a
page with `humanize` off then on and grades trajectory curvature, dwell, keystroke
cadence and scroll dynamics against a human-plausible band (`vb oracle record`
captures a real-operator baseline; literature defaults until you do). It measures
*our* model of human rather than a named vendor — but it turns "we humanize" into a
measured on/off delta, and it's honest about the one axis synthetic input can't
reach: CDP input emits no raw-pointer / coalesced events, which only attach-mode
against real hardware closes.

Escalation ladder when a wall trips: **headless → `--headed` → `humanize on` →
`--backend nodriver` → attach-mode after a manual login.** Patchright's CDP-layer
patches apply in *all* tiers, including attach (`connect_over_cdp`).

> The `fetch` verb is an orthogonal fast-path, not a tier: once you're past a
> wall in the browser, `vb fetch` reuses that session's cookies+proxy to hit
> JSON/API endpoints at TLS-fingerprint-correct speed — but it runs no JS, so it
> can't *clear* a JS challenge itself.

## Attach mode — the practical Cloudflare workaround

For DataDome / Kasada / hardened auth that walls cold-launch automation:

```
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/cdp-profile &
# log into the walled site by hand
vb attach http://localhost:9222
vb go https://target.example.com        # now reads as your real browser
```

Patchright's CDP-layer stealth still applies over `connect_over_cdp` — attach mode gets the same protocol-level patches as cold launch, plus your real-browser fingerprint and any cookies from the manual login.

## Security model

vibatchium is built to drive *real* logins from an untrusted agent loop, so the
threat model is "a credential must never reach the model, a screenshot, or a log":

- **Encrypted vault.** Passwords and TOTP secrets live in an XSalsa20-Poly1305
  vault keyed from the OS keyring or `VIBATCHIUM_SECRETS_KEY`. A resolved secret
  never appears in logs, HAR captures, the observe cache, or any agent-visible
  response field (grep-tested in CI).
- **Secrets are never rendered in the clear.** `fill --use-secret` masks the field
  *in the page* (`-webkit-text-security`), applied before the value is written, so
  every path that turns the viewport into bytes — the `screenshot` verb, the 5 fps
  live-view stream, and VLM `vision_*` calls that ship the frame to a model —
  captures dots, not the value. The mask **fails closed** (no write if it can't be
  confirmed), covers password fields so a show-password toggle can't unmask, and
  the accessibility snapshot returned by `map` / `diff_map` strips masked values so
  the secret can't leak into the model's context as text either.
- **Live-view is authenticated.** The WebSocket requires a per-server token and
  rejects foreign-`Origin` connections (the CSWSH class), and *driving* the page is
  a separate token from watch-only — a read-only link can be shared without handing
  over the keyboard. Binds `127.0.0.1` by default (`--insecure-public` to override).
- **Scraped content is marked untrusted.** MCP verbs that return page-derived text
  carry `openWorldHint`, so a host can taint the output against prompt injection
  instead of treating a scraped page as instructions; pure probes are `readOnlyHint`
  and mutating verbs (`stop`, `secret_delete`, `storage_restore`) are
  `destructiveHint`.
- **REST shim.** Without `--caps`, the bearer token grants every verb including
  `eval`, `secret_*`, and file-writing verbs — local-code-equivalent, so always
  pass `--caps=...` in hosted mode. All vibatchium-written files are `0600`;
  directories `0700`.

## Honest limits

- **5+ concurrent sessions = 1-2GB RAM.** Each persistent-context Chrome is ~200-400MB. Bump cap with `VIBATCHIUM_MAX_SESSIONS=8`.
- **Vision spend cap is process-wide.** N fan-out agents share one daily/lifetime budget.
- **Init scripts don't work on patchright backend.** `chrome.runtime` stays `undefined` — accepted trade for stealth wins.
- **Login walls (X, LinkedIn) require attach mode.** Cold-launch fan-out can't defeat sites requiring authenticated sessions.
- **Synthetic input has a CDP coordinate signature.** Every `click`/`type`/`hover`/`scroll` rides Playwright over CDP `Input.dispatchMouseEvent`/`dispatchKeyEvent` (`pageX==screenX`, no `CoalescedEvents`). Patchright patches the JS-context leaks, not the Input domain, and `humanize on` improves trajectory/timing realism but does **not** change the per-event signature. Behavioral walls that fingerprint it (DataDome/Kasada/HUMAN) want **attach-mode against a real headful Chrome you drive** — OS-level synthetic input (CDP-Patches) is headful + active-tab only and doesn't fit a headless, N-parallel daemon.
- **`fetch` is a static-fingerprint lane, not a browser.** The curl_cffi `fetch` verb matches Chrome's JA3/HTTP2 but runs no JavaScript — it clears TLS-fingerprint gates, not DataDome/Kasada/Turnstile JS challenges. Fall back to `go` for those.
- **Single daemon = single point of failure.** No HA built in.

## License

Apache-2.0 core. Every default-install extra is permissive too — the `fetch` lane's curl_cffi is **MIT**. The only copyleft option is the opt-in `nodriver` backend (AGPL-3.0) — consult licensing before integrating it commercially. Nothing GPL/AGPL ships in the base install or `[all]`.
