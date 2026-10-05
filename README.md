<!-- Absolute URL on purpose: PyPI renders this same README and cannot resolve
     repo-relative paths. Pinned to master so it survives tag churn. -->
<p align="center">
  <img src="https://raw.githubusercontent.com/trueoriginlabs/vibatchium/master/assets/vb-logo.png" alt="vibatchium" width="180">
</p>

# vibatchium

<!-- mcp-name: io.github.trueoriginlabs/vibatchium -->

**The browser layer for an always-on box.** Many logged-in identities, running
unattended, all at once — on real Chrome, with stealth on by default, and with a
security model for letting an AI near the credentials.

One daemon, N persistent Chrome sessions, a `vb` CLI and an MCP server.
Self-hosted, free, Apache-2.0.

```
pipx install 'vibatchium[all]'
patchright install chrome
vb setup                     # register the MCP server + agent skill (Claude Code, Codex, Cursor)
vb explore https://example.com
```

## Is this for you?

| You want… | Use |
|---|---|
| An agent driving **your** signed-in browser while you watch | Claude in Chrome, Chrome Auto Browse — first-party, free, better integrated |
| The **stealthiest single browser** you can buy | [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) — fingerprints patched in Chromium's C++ (paid for concurrency) |
| The **leanest driver** for one agent | [bladebro](https://github.com/dondai44423/bladebro) — five tools, one Rust binary |
| Browser automation without anti-bot pressure | [playwright-mcp](https://github.com/microsoft/playwright-mcp), [agent-browser](https://github.com/vercel-labs/agent-browser) — bigger, older, better tested |
| **Many accounts on one box, running unattended, against sites that fight automation** | **vibatchium** |

That last row is the whole wedge. Supervised agents hand control back to you at
every login wall and CAPTCHA; a library leaves the fleet — sessions, budgets,
crash recovery, credentials — for you to build. vibatchium is that fleet layer.

## How it compares

As of 2026-10-06, from each project's own README, source and release notes
(the MCP-cost row is our own measurement¹). Rows where we lose are included on
purpose.

| | **vibatchium** | CloakBrowser | bladebro | agent-browser | playwright-mcp |
|---|---|---|---|---|---|
| N persistent logged-in sessions at once | **yes, free, one daemon** | 1 free (latest build) · more on Pro | one Chrome per agent session | yes, one daemon per session | one per profile dir |
| Unattended ops: crash self-heal, leases, budgets, idle freeze, goals | **all of them** | — | partial (crash relaunch, idle timeout) | partial (idle timeout, restore) | idle timeout |
| Agent can *use* a secret but not *read* it (origin-bound fill, read-back refused, TOTP) | **yes** | — | — | vault, origin check | `--secrets` redaction |
| Prompt-injection scanning of page content | **yes** | — | — | boundary markers (opt-in), no scanning | — |
| Browserless HTTP on the session's own cookies, Chrome TLS fingerprint · keyless web search | **yes** | — | — | — | — |
| Browser | stock Chrome | own Chromium build | stock Chromium | Chrome for Testing default | stock Chrome |
| Stealth layer | CDP-level (Patchright) | **engine-level (87 C++ patches)** | CDP-level ("6-layer"), no `Runtime.enable` | none in core; via cloud providers | none |
| Published stealth score | not yet | 30/30 on its own detector list | **68/80 Stealth Bench V1 (self-run)** | — | — |
| MCP startup cost in Claude Code¹ | ~2.7k tokens (87 tools, 5 always loaded) | n/a | ~3.4k tokens (5 tools) | not measured | not measured |
| Platforms | Linux (macOS: CI probe, unsupported) | Linux · macOS · Windows | Linux · macOS · Windows | all | all |

<sub>¹ bytes/4 of `tools/list` plus server instructions, measured over stdio. Clients without tool search load every tool: `vb mcp --caps min` is 12 tools, ~3.8k tokens. bladebro's own README says ~1.9k.</sub>

**Where the others are genuinely better:** CloakBrowser's engine-level patches
go deeper than any CDP-level layer can — and `vb start --browser-binary` will
drive a Chromium build you bring. bladebro has a stealth benchmark number and we
don't; until we do, read our stealth claims as measured on scoreboards, below,
not on production walls. Both run on more operating systems.

## What only vibatchium does

**1. A fleet of logged-in identities on one box.** Each session is its own
Chrome and profile, with its own proxy, timezone and GPU node, and — opt-in
with `vb persona set` — its own screen and window, because by default every
headless session on one machine reports the same 800×600 screen. `vb
fleet-check` measures how alike your sessions still look. They run in parallel
on one daemon that keeps them alive: a crashed renderer is relaunched on the
same profile, parked sessions have their renderers stopped and thawed on the
next call, leases stop two agents clobbering one session, and goals give a task
a step/spend/time budget that survives a daemon restart.

```
vb --session alice start && vb --session alice go https://x.com      # log in once, stays logged in
vb --session bob   start && vb --session bob   go https://x.com
vb --session alice act "open notifications" & vb --session bob extract & wait
vb fleet-check --sessions alice,bob                                  # how twinned are they?
```

**2. Credentials an agent can use but never see.** The vault fills a password
or a live TOTP code into the page without the value reaching the model's
context, a screenshot or a log — and only into the site it belongs to. Over
MCP, a prompt-injected agent can't type your GitHub password into a lookalike
page, can't read it back with `value` or `eval` while it sits in the field, and
can't edit the vault or pull a code. An agent with a *shell* is the operator —
the CLI has deliberate escape hatches — so give agents MCP, not Bash, if that
boundary matters to you.

```
printf %s "$GH_PASSWORD" | vb secret set github.com password --stdin
printf %s "$GH_TOTP_SEED" | vb secret set github.com totp-seed --stdin
vb --session work fill @e7 --use-secret github.com:password
vb --session work fill @e9 --use-secret github.com:totp
vb wait-email-code github.com      # operator CLI: polls IMAP and prints the code; refused over MCP
```

**3. Page text is treated as untrusted.** Every session scans the page text it
returns for instructions aimed at the agent and flags them by default; `wrap`
fences them off and `redact` removes them. Caller-supplied file paths are
confined too, so an injected agent can't upload `~/.ssh` or write into
`.git/hooks`.

**4. A browserless lane on the same identity.** `vb fetch` reuses a session's
cookies and proxy with Chrome's TLS fingerprint and client hints — JSON
endpoints behind your login at curl speed, and it tells you when it can't match
the browser. `vb search` finds URLs with no API key and no per-session budget.

```
vb --session work fetch https://api.example.com/v1/me
vb search "site reliability postmortem" -n 20 --urls
```

**Status:** alpha, active development. **1,963 tests** green in CI (Linux,
Python 3.11–3.14). Coding agents: read [`AGENTS.md`](AGENTS.md) first — the
one-call recipes and the traps worth skipping.

<sub>Detector scores quoted below (bot.sannysoft, CreepJS, Cloudflare cold-launch) are **manual observations, not CI-asserted** — no test in the suite gates on them, and they are only as current as the last hand-run. The generated block under [Measured scores](#measured-scores) is the one to trust.</sub>

## Install

```
pipx install vibatchium             # core: browse / extract / screenshot / N parallel sessions
pipx install 'vibatchium[all]'      # + fetch/search lanes, credential vault, VLM read, REST shim
                                    #   (or pick extras: [fetch], [secrets], [llm], [rest])
patchright install chrome
vb setup                            # register MCP + an auto-discoverable skill (idempotent)
vb install                          # report which optional lanes are available
```

On a **uv** venv (no pip), add an extra with
`uv pip install --python <venv>/bin/python curl_cffi`. Bleeding edge from
`master`: `pipx install 'git+https://github.com/trueoriginlabs/vibatchium#egg=vibatchium[all]'`.

## Install as a skill / plugin

`vb setup` wires up the agents on *this* machine. To pull just the agent skill
into any agent [skills.sh](https://skills.sh) supports (Claude Code, Codex,
Cursor, Gemini CLI, Copilot, …), or to install skill + MCP server as one Claude
Code plugin:

```
npx skills add trueoriginlabs/vibatchium                  # skill only, any agent

/plugin marketplace add trueoriginlabs/vibatchium         # Claude Code: skill + `vb mcp`
/plugin install vibatchium@vibatchium
```

Both still need the `vb` CLI on `PATH` (`pipx install 'vibatchium[all]'`) — the
skill drives it and the plugin's MCP server *is* `vb mcp`. On Claude Code, pick
the plugin **or** `vb setup`, not both, or you register the server twice. The
skill file is generated from `vibatchium/setup_cmd.py`; edit there and run
`python scripts/sync_skill.py`.

## Quick start

```
vb explore https://example.com                      # one call: text-first, auto-closes
vb research --target https://example.com \          # parallel fan-out, N intents
  --intent "pricing model" --intent "customers" --intent "tech stack"
```

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
WebGL readback are byte-identical across navigations, and CreepJS reported **0 %
stealth-tampering** (no synthetic-environment signatures) when last run by hand.
That figure is not regression-tested — treat it as an observation, not a guarantee.

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

On **one** shared daemon, sessions are isolated in state (separate Chromes and
profiles, no cookie bleed) but not automatically in fingerprint — on one box
they report the same screen and, without `--gpu --node`, the same software GPU
unless you set a `vb persona`; `vb fleet-check` measures it. They also share the
host: the session count budget, the
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

Freezing rather than closing is a deliberate trade, and it cuts both ways.
playwright-mcp's `--idle-timeout` (v0.0.81, 1 h default for headless) *closes*
an idle browser: that reclaims its memory, and its persistent profile keeps the
cookies, but the open tabs and in-page state are gone, and the next call pays a
cold launch. A frozen session keeps its resident memory and resumes on the next
verb with its tabs, page state and login intact — which is the behaviour a
long-lived authenticated session mid-flow needs. Memory is bounded by the other two levers above,
the RAM floor and the cgroup, rather than by teardown.

## Stealth tiers — what clears what

Stealth is a ladder, not a boolean. Pick the lowest tier that clears your target
(higher tiers cost more setup / a visible browser / a manual login). vibatchium
does **not** claim cold-launch defeat of behavioral walls — those need a real
human-driven session, and attach-mode is the honest answer.

> **Architecture caveat.** Every tier below was measured on **x86-64 Linux**.
> Patchright has a known open arm64 / Apple-Silicon detection gap, so these
> results should not be assumed to carry to an ARM host. If you run there,
> measure before you rely on it.

| Tier | How | Clears | Doesn't clear |
|---|---|---|---|
| **Standard** (default) | headless cold launch, real `channel=chrome`, de-Headless'd UA | Cloudflare IUAM / managed challenge, `bot.sannysoft` 31/31, JS-runtime fingerprinting | aggressive Turnstile, DataDome/Kasada, anything behind a login |
| **Hardened** | retry `--headed`; `vb humanize on` + `humanize ambient on` (pointer activity between actions); `vb persona set` (a per-identity screen); `--backend nodriver` (`pip install vibatchium[nodriver]`, AGPL) for the hardest Cloudflare gates | aggressive Cloudflare/Turnstile, GPU/screen tells that headless leaves | behavioral biometrics, DataDome/Kasada sensor-fusion |
| **Attach** | `vb attach` to a Chrome **you** launched and logged into | DataDome / Kasada / HUMAN behavioral walls, and any authenticated session — your real fingerprint + cookies | nothing here is automated cold; it needs the human login first |

### Measured scores

`vb evals run --update-readme` writes measured numbers into the block below, so
what we publish is generated rather than asserted. It is empty until someone
runs it — an empty block is honest; a number with no run behind it is not.

Run on x86-64 Linux, 25 Aug 2026, Chrome 150, `--gpu` (real render node).
Pass `--gpu` yourself: headless Chrome falls back to SwiftShader when the GPU
path doesn't take, and software GL is itself a detection signal, so numbers
measured without it are a floor rather than what a GPU-backed deployment gets.

<!-- vibatchium-evals -->
| Target | Backend | Humanize | GPU | Score | Status | Time |
|---|---|---|---|---|---|---|
| sannysoft | patchright | off | real | 100 | OK | 17.78s |
| creepjs | patchright | off | real | 44 | OK | 7.88s |
| brotector | patchright | off | real | 10 | OK | 7.13s |
| sannysoft | nodriver | off | real | 100 | OK | 19.85s |
| creepjs | nodriver | off | real | 44 | OK | 9.72s |
| brotector | nodriver | off | real | 10 | OK | 7.73s |
<!-- /vibatchium-evals -->

**Read these honestly — two of the three are bad.**

- **sannysoft 100** is the floor everyone in this category clears; it is not a
  differentiator.
- **creepjs 44** is mediocre. CreepJS is an adversarial *lie-detector*: it
  cross-checks main-thread against worker-thread claims and grades confidence,
  so a middling score means our environment is coherent but not indistinguishable.
- **brotector 10** is poor, and we know exactly why. The signal firing is
  `UA_Override / HighEntropyValues.empty`, and **our own de-Headless fix causes
  it.** Measured on Chrome 150, headless, same profile, with and without the
  `--user-agent` flag we set to strip `HeadlessChrome`:

  | | `architecture` | `bitness` | `uaFullVersion` | UA string |
  |---|---|---|---|---|
  | without the flag | `x86` | `64` | `150.0.7871.114` | says **HeadlessChrome** |
  | with the flag | *empty* | *empty* | *empty* | says **Chrome** |

  Passing an explicit UA makes Chrome stop deriving high-entropy client hints,
  so we trade a UA-string tell for a UA-CH-emptiness tell. It is not fixed: the
  obvious repair (`Emulation.setUserAgentOverride` with `userAgentMetadata`) is
  target-scoped and would reintroduce a main-vs-worker mismatch that is a
  *stronger* tell than either leak alone. Publishing this rather than dropping
  the target is the point — a suite that only reports its wins is marketing.
- **`nodriver` scores identically to `patchright` on all three.** The escalation
  tier buys nothing measurable *on static scoreboards*; its case rests on the
  automation-protocol axis these targets don't probe. An earlier run had
  `nodriver` slightly *ahead* on creepjs (50 vs 44) — but that run silently
  denied it the GPU while patchright got a real one, so it was scoring from
  behind. Once both arms get a real renderer the difference disappears.
  Worth knowing before reaching for the tier: upstream has not published since
  **nodriver 0.50.3 (2026-05-13)**, so what the `>=0.50.0,<1.0` pin resolves to
  is now several months old. The rung still works; it is no longer moving.

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

## Captures — 2× screenshots of states you had to click to reach

```
vb --session shots start --scale 2          # devicePixelRatio 2 (1–4, persisted; --scale 1 clears)
vb --session shots go https://example.com
vb --session shots click @e3                # open the modal…
vb --session shots screenshot -o card.png   # …then capture it at 2560×1600
                                            # (2× the 1280×800 pin; vb viewport W H to resize)
```

The default session captures one image pixel per CSS pixel, so a crop meant for a
retina asset lands at half the resolution it needs. `chrome
--force-device-scale-factor=2 --screenshot` is faithful but can't click, so it can
never capture a modal, a hover, or a logged-in view. `--scale` does both.

**It's a capture posture, not a browsing one.** Chromium only takes
`deviceScaleFactor` at context creation and Playwright refuses it alongside
`no_viewport`, so a scaled session pins a viewport and emulates device metrics —
`screen == viewport`, and `start` says so with `screen_coherent: false`. Don't
point a scaled session at a Cloudflare/DataDome wall; browse on a normal one and
scale a separate session for the picture. Cold start only (`scale_pending` on a
live session), Patchright only (`scale_ignored` on nodriver), and
`max_screenshot_px` is denominated in *device* pixels — at 2× a tall page
truncates at half the CSS height. Details in [`AGENTS.md`](AGENTS.md), under *Retina / 2× captures*.

## Search — find the URL, not just read it

Reading a walled page is only half a research loop; the other half is discovery,
and search engines are anti-bot walled like everything else. `vb search` runs the
SERP over the same curl_cffi lane — **no browser, no session, no API key, and no
per-session call budget to run out of mid-run.**

```
vb search "playwright stealth detection" -n 5
vb search "cdp leak" --site github.com -n 20
vb search "postmortem" --urls | xargs -I{} vb fetch --no-cookies {}
```

Engines are tried as a ladder (`ddg → ddg-lite → bing`) until one answers,
because reachability moves: the endpoint serving results now may rate-limit
(HTTP 202) on the next call. `--json` returns an `attempts` array naming every
engine that declined and why, and a `reason` separating *all engines are walled*
from *the web has nothing* — different problems, different fixes, and the CLI
exits non-zero only for the first.

It never reuses session cookies (a SERP needs no login, and attaching one
deanonymises the request), which is why it gets its own `search` cap instead of
riding on `fetch`. No date filter is exposed on purpose: DuckDuckGo's mislabels
article dates badly enough to corrupt a timeline. Engines rate-limit per IP — see
proxies, below.

## Proxies — per-request egress on both stealth lanes

Egress is the axis most people get wrong, so it gets stated precisely rather than
implied. Both curl_cffi lanes take `--proxy scheme://[user:pass@]host:port`:

```
vb fetch --no-cookies https://api.example/v1 --proxy http://user:pass@gw:12323
vb search "site reliability postmortem" -n 20 --proxy http://user:pass@gw:12323
vb --session work proxy set http://user:pass@gw:12323    # or per-session, for the browser
```

Measured, not asserted — same box, same command, only `--proxy` differing:

| | egress IP |
|---|---|
| direct | `115.70.50.70` |
| `--proxy` (authenticated gateway) | `212.69.0.85` |

Four things worth knowing:

- **Unset means direct, and that is enforced.** The daemon hands libcurl an
  explicit empty proxy, which is the only value that stops it reading
  `HTTP(S)_PROXY` / `ALL_PROXY` out of the environment it was spawned with. A
  long-lived daemon inherits whatever shell first started it, so without this a
  stray `HTTPS_PROXY` would silently reroute every request while the response
  claimed direct egress. (It did, until 0.19.0 — see the changelog.)
- **It matters most for `search`.** Engines rate-limit *per IP*, so a wide
  research fan-out from one address is the fastest way to push every query onto
  the last rung of the engine ladder. The response reports `proxied: true|false`
  so you can tell which IP a thin result set came from.
- **The proxy address is SSRF-guarded** like the target URL, with
  `--allow-internal` to opt in to a proxy on your own LAN. An unguarded proxy
  reaches internal services and returns their response bodies, not just
  connection errors.
- **A bad proxy is an error, never a fallback.** Silently egressing from the host
  IP when you asked for a specific one is worse than failing, because the whole
  point of asking was that the host IP must not be used. Proxy URLs are redacted
  from the verb log; responses carry the boolean, never the URL.

With a browser session, `vb proxy set` also wires the **WebRTC leak guard** — a
tunnelled HTTP request still leaks the real IP via STUN without it.

## Attach mode — the practical Cloudflare workaround

For DataDome / Kasada / hardened auth that walls cold-launch automation:

```
google-chrome --remote-debugging-port=9222 \
              --disable-blink-features=AutomationControlled \
              --user-data-dir=/tmp/cdp-profile &
# log into the walled site by hand
vb attach http://localhost:9222
vb go https://target.example.com        # now reads as your real browser
```

Patchright's CDP-layer stealth still applies over `connect_over_cdp` — attach mode gets the same protocol-level patches as cold launch, plus your real-browser fingerprint and any cookies from the manual login.

> **Launch flags are yours on this tier.** On cold launch the backend supplies
> `--disable-blink-features=AutomationControlled` for you. Attach connects to a
> Chrome that is *already running*, so nothing vibatchium does can add a launch
> flag after the fact — if you started Chrome without it, that tell is present
> for the whole session. Include it in the command above.

> **`--remote-debugging-port` is an open door.** It grants full browser control
> to any process on the machine, and a page you visit can probe localhost to
> discover it. Use it on a machine you trust, and close Chrome when you're done.

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
- **Secrets are origin-bound.** `fill --use-secret github.com:password` only writes
  into a document on that site — https, `github.com` or a subdomain, judged by the
  frame that owns the field (so an iframe can't borrow the top page's origin) —
  or into the entry's explicit `origins` list. Anywhere else it refuses *before*
  the secret is resolved, so a prompt-injected agent can't type your password
  into a lookalike page. Framed login pages, about:blank frames and pages the
  agent already ran `eval` in are refused too; while the secret sits in a field,
  `value` / `eval` are refused so it can't be read back; and the write uses the
  field's own value setter, never focus + keyboard, so page script can't
  redirect it. If the field is swapped mid-fill, it's cleared and the call
  fails. The escape hatches (`--allow-cross-origin`, two daemon env vars) are
  shell-only; MCP refuses them, along with vault edits and `secret totp`.
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
- **Behavioural detection now targets the humanizer directly.** Cloudflare's 2026
  bot-detection work names mathematically ideal Bézier cursor paths and superhuman
  click precision as tells. `humanize` improves on nothing-at-all, but it is a
  pointer-trajectory model, not a physiological one — and it is **off by default**,
  which on a behaviourally-scored site is the louder of the two states.
- **One burned profile can taint every account that shares it.** Vendors now link
  device telemetry across sessions *and* accounts. Use one profile per account,
  never share a profile between identities, and don't reuse a profile that has
  already been challenged.

- **The `fetch` and `search` lanes are outside prompt-injection scanning.** The
  scanner covers page-content verbs (`text`, `html`, `extract`, `map`, …). SERP
  titles and fetched bodies are third-party text going straight into an agent's
  context and are *not* scanned today. Treat them as untrusted input.
- **Cloudflare's 2026-09-15 default is policy, not detection.** For new domains
  and free-plan zones, Cloudflare now blocks *Training* and *Agent* crawlers by
  default on ad-monetized pages. It targets crawlers that **declare themselves**
  (user-agent or Web Bot Auth signature), so it doesn't change what a real
  Chrome session looks like — but the fingerprint and behavioural scoring above
  is a separate axis that moves independently of anything announced in a blog
  post.

## Updating

```bash
vb update                  # upgrade + bounce the daemon + refresh the agent skill
vb update --version 0.19.0   # or pin a specific version
```

`vb update` detects how vibatchium was installed (pipx, `uv tool install`,
a pip-less uv venv, or pip with a PEP-668 `--break-system-packages` fallback),
**stops the running daemon** so the next command loads the new code, and
**rewrites the agent skill / docs blocks** so a coding agent is actually told
about the verbs the new version ships (`--no-restart` / `--no-setup` opt out).
Manual equivalent:

```bash
pipx upgrade vibatchium    # or: uv tool upgrade vibatchium / pip install -U vibatchium
vb shutdown                # bounce the daemon — it serves old code until you do
vb setup                   # refresh the agent skill + docs
vb --version               # confirm
```

> The daemon-restart step is the one people miss: the long-running daemon keeps
> serving the **old** version until it's bounced. `vb update` does it for you;
> if you upgrade by hand, run `vb shutdown` (the next `vb` call auto-respawns the
> new version). Optional features upgrade via `pipx install 'vibatchium[all]' --force`.

### Running from a git checkout

`git pull` updates the **source**; whether it updates what `vb` actually runs
depends on the install, and two of the three ways it can fail are silent:

```bash
vb --version && git describe --tags   # do they agree? if not, the install COPIED
                                      # the source — reinstall editable:
                                      #   uv pip install -e '.[all]'
vb status                             # warns when the daemon predates the source
                                      # ("stale_code") — bounce with `vb shutdown`
```

A version-string compare cannot catch a checkout: `git pull` changes the code
without changing `__version__`. `vb status` compares the daemon's boot time
against the newest source file instead, and `vb update` bounces the daemon only
when it is provably behind — so it never drops live sessions for nothing.

### New cap buckets need a re-register

The MCP server's `--caps` list is frozen into your agent's config at first
registration, so a bucket added by a later release (0.19.0 added `search`) stays
invisible no matter how many times you upgrade. Re-running `vb setup` reports
the drift but deliberately won't overwrite a `--caps` you set by hand:

```bash
vb setup                              # reports any cap drift, changes nothing
vb setup --force --caps lean,search   # apply it — exposes `vb search` as a tool
```

Restart the agent session afterwards: the MCP tool list is read once, at start.

## Server modes

| Mode | Surface | Auth |
|---|---|---|
| `vb mcp` | stdio JSON-RPC; defaults to the **lean** 87-verb profile (`--caps min` = 12 tools for clients without tool search; `--caps=full`/`all` = 163; `--caps=...` for a custom bucket set) | n/a (stdio) |
| `vb serve` | FastAPI on `127.0.0.1:8000`; every verb at `POST /v1/<verb>`; WebSocket live-view at `/v1/stream/<session>` | bearer token (`~/.cache/vibatchium/rest-token`, mode 0600) |

**REST capability gating**: `vb serve --caps=core,nav,input,vision` restricts the HTTP surface the same way `mcp --caps` does. Without it, REST grants local-code-equivalent access (eval + secret_* + file-writing verbs all exposed) — safe for localhost dev, **not** for hosted/multi-tenant.

## Documentation

- [`AGENTS.md`](AGENTS.md) — coding-agent contract (Codex / Cursor / Claude Code)

## Authorized use

vibatchium is built to drive sessions **you own**, with **your** credentials, on
**your** machine — your accounts, your employer's, or a client's with their
written permission. It is a tool for automating access you already have.

That boundary is not a formality, though the law around it moved in 2026. A US
district court had granted a preliminary injunction against an AI agent that
accessed password-protected pages *through the user's own logged-in account*,
holding that the user's permission is not the platform's authorization. On
4 August 2026 the Ninth Circuit **vacated** that injunction and remanded
(*Amazon.com Services, LLC v. Perplexity AI, Inc.*, No. 26-1444, published),
concluding that the operator had not "accessed" the plaintiff's computers under
the CFAA at all — "it was the user who accessed [them], with the help of
[the] AI agent." The California CDAFA claim failed for the same reason.

Read that narrowly. It decides **who** accessed a computer, not whether evading
a technical block is access "without authorization"; the panel never reached
circumvention. A vacated preliminary injunction on remand is not a merits
ruling, and it leaves contract, terms-of-service, trespass and copyright
theories entirely untouched. What it does support is the shape of the tool:
the browser runs on your machine, under your login, and the reasoning leaned on
exactly that — no operator computer ever touched the other side's servers.

Scraping a site's public pages, evading a wall you have no account behind, or
automating an account whose terms forbid it remain decisions you are making, and
the consequences are yours.

Check the terms of the site you are automating. If you are acting for someone
else, get it in writing.

## License

Apache-2.0 core. Every default-install extra is permissive too — the `fetch` lane's curl_cffi is **MIT**. The only copyleft option is the opt-in `nodriver` backend (AGPL-3.0) — consult licensing before integrating it commercially. Nothing GPL/AGPL ships in the base install or `[all]`.
