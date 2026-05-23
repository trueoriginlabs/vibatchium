# Stealth posture, honest

What patchium does, what it doesn't, what we tested, and the explicit trade-offs.

## Default stack

- **`channel="chrome"`** — real Google Chrome binary, real TLS fingerprint
- **`launch_persistent_context`** with on-disk `user-data-dir` — real-profile cookie/storage continuity
- **`headless=False`** by default (canonical Patchright recommendation; use `--headless` to opt out)
- **`no_viewport=True`** — let OS window size win
- **No UA / header overrides** — explicit anti-pattern per Patchright README
- **`--disable-dev-shm-usage`** — required for headless multi-session reliability
- **WebRTC leak guard flags** auto-applied when a proxy is set
- **`--no-sandbox` explicitly stripped** via `ignore_default_args` (Wave 7.5c) — this flag triggers a visible yellow infobar AND is a layer-7 detector signal
- **Sandbox enabled** unless `PATCHIUM_DISABLE_SANDBOX=1` (escape hatch for Docker / restricted environments)

## Verified

- **Cleared HackerOne's Cloudflare challenge** on first cold launch (`tests/smoke_cloudflare.py`)
- **31/31 on bot.sannysoft.com** (measured, `tests/test_wave7_stealth_gate.py`)
- **`navigator.webdriver = false`** (defense-in-depth test alongside sannysoft)
- **`navigator.userAgent`** is real Chrome 147, not `HeadlessChrome`
- **`window.chrome`** present with real-Chrome sub-properties (`loadTimes`, `csi`)
- **WebGL vendor/renderer**: real GPU info, not Mesa SwiftShader
- **No `--no-sandbox` flag** in actual Chrome argv (process-level probe in `tests/test_wave7_stealth_gate.py`)

## Realistic expectations by defender

Don't claim a single percentage — depends on target's configuration.

| Defender | Cold launch | After `attach` (manual login first) |
|---|---|---|
| Cloudflare default / Bot Fight Mode | ~70–90% (HackerOne ✅) | ~95% |
| Cloudflare Under Attack / Managed Challenge | ~10–30% | ~70–85% |
| DataDome | ~20–40% (+humanize: ~50%) | ~60–80% with CDP-Patches |
| Akamai Bot Manager | ~30–50% | ~70% with humanized input |
| PerimeterX / HUMAN | ~20–40% (+humanize) | ~60% with mouse entropy |
| Kasada | ~10–30% | ~30–50% (their client-side challenge VM is the wall) |

## Trade-offs we accepted (and why)

### `chrome.runtime` is `undefined`

Real Chrome exposes `window.chrome.runtime` as an object on regular pages. Bare Patchright leaves it undefined. We could shim it via `add_init_script`, but Patchright **deliberately filters** the CDP method `add_init_script` uses (`Page.addScriptToEvaluateOnNewDocument`) because the *presence* of an init-script-on-new-document is a stronger fingerprint signal than the missing runtime object.

Verified empirically: `context.add_init_script(...)` is a silent no-op on Patchright contexts.

**Trade**: chrome.runtime stays undefined, but no CDP automation-shape leaks. Sites that hard-require chrome.runtime can use `--backend nodriver` (which doesn't filter) or attach mode (real user Chrome).

Pinned by test assertion in `tests/test_wave7_stealth_gate.py` — if the runtime ever becomes defined, the test reminds us to update this doc.

### `--disable-blink-features=AutomationControlled` triggers a yellow banner

In **headed mode**, Chrome shows a yellow "you are using an unsupported command-line flag" infobar at the top of every page because of this flag. The flag is what stops `navigator.webdriver` from being `true`.

**Trade**: headed mode shows a cosmetic warning that humans see. The banner is **not** in the page DOM — invisible to every JavaScript-based bot detector (sannysoft, CreepJS, FingerprintJS, Brotector, Cloudflare's challenge JS, DataDome, Akamai, Kasada — all probe via JS and see nothing). Not in `page.screenshot()` output either. Removing the flag silences the warning but flips `navigator.webdriver` to `true` and breaks every JS detector test.

Measured:

| | Headed + flag | Headed - flag | Headless + flag | Headless - flag |
|---|---|---|---|---|
| Yellow banner | **visible** | gone | n/a | n/a |
| `navigator.webdriver` | `false` ✓ | `true` ✗ | `false` ✓ | **`true` ✗** |
| UA contains "Headless" | no ✓ | no ✓ | no ✓ | **yes ✗** |
| Sannysoft | 31/31 ✓ | broken | 31/31 ✓ | broken |
| Cloudflare-clear | works ✓ | broken | works ✓ | broken |

We keep the flag. In headless mode (the default for agent runs), it's a non-issue: no UI to show the banner in, JS still can't see it.

## Layers we can add (opt-in)

- **`humanize on`** — Bezier mouse / gaussian dwell / sinusoidal scroll (already shipped, default OFF; only enable when targets actually fingerprint mouse behavior — DataDome, PerimeterX, HUMAN)
- **CDP-Patches** mouse heuristics — `pip install patchium[stealth-mouse]` (GPL-3.0, opt-in)
- **nodriver** backend — `pip install patchium[nodriver]` (AGPL-3.0, opt-in). Doesn't filter init scripts, so `chrome.runtime` shim is possible. Different stealth profile — best for sites where chrome.runtime is checked.
- **BrowserForge** for canvas/WebGL/audio diversity (future, gated)

## When to use what

- **Default (cold launch + cookies persist)** — most sites, including Cloudflare default config. Good for crawling, scraping, agent navigation.
- **`+humanize`** — DataDome / PerimeterX / Akamai. Adds latency.
- **`+nodriver` backend** — sites that check chrome.runtime / want SwiftShader fingerprint diversity.
- **`+attach` mode** — anything that requires real-user TLS fingerprint + login state. The protocol-layer patches still apply over `connect_over_cdp` so you get Patchright's stealth + your real-browser fingerprint.

## What we don't do

- **TLS JA3 spoofing** — we run real Chrome so this is real-Chrome's JA3. If you need to look like a non-Chrome browser, this won't help.
- **Canvas / audio fingerprint randomization** — neither patchright nor patchium does this. CreepJS will identify the GPU.
- **Browser-level user simulation (typing pauses, mouse jitter)** — that's `humanize on`. Opt-in.
- **Defeat Cloudflare Turnstile / Managed Challenge that requires JS challenge solving** — out of scope. Use a CAPTCHA-solving service if you need this.

## How to verify against your target

```
patchium fingerprint sannysoft       # baseline JS-runtime score
patchium fingerprint creepjs         # canvas/audio/timing
patchium fingerprint brotector       # Patchright authors' own gauntlet

patchium evals run --targets sannysoft,creepjs --backends patchright,nodriver
patchium evals run --min-score 80 --update-readme   # CI gate + auto-patch README
```

If your target is a specific site you can probe, the most useful thing is `patchium go <url>` and look at the response. The daemon auto-detects walled pages (14+ challenge patterns: Cloudflare, Datadome, PerimeterX, Akamai, hCaptcha, Sucuri, Imperva) and surfaces `walled: <defender>` plus an `advice` field.
