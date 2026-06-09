# vibatchium

**Agent-piloted browser automation that clears Cloudflare.**
Patched Playwright + multi-session daemon + credential vault + vision clicking + prompt-injection safety. One MCP server, N parallel Chromes, persistent per-session profiles.

```
pipx install vibatchium     # or: pip install vibatchium
patchright install chrome
vb setup                    # register MCP + an auto-discoverable skill so agents reach for vb (idempotent)
```

> Bleeding edge from `master`: `pipx install git+https://github.com/trueoriginlabs/vibatchium`

> **Coding agents (Codex / Cursor / Claude Code):** read [`AGENTS.md`](AGENTS.md) first — it has the one-call recipes (`explore`, `research`) and the env-discovery traps to skip.

```
vb explore https://example.com                      # one-call: text + screenshot
vb research --target https://example.com \          # parallel fan-out, N intents
  --intent "pricing model" --intent "customers" --intent "tech stack"
```

**Status:** active development, alpha. 526 tests green. 31/31 on bot.sannysoft.com. Cleared HackerOne Cloudflare cold-launch. Apache-2.0 (GPL/AGPL only via opt-in extras).

## Updating

```bash
vb update                  # upgrade to the latest PyPI release + restart the daemon
vb update --version 0.6.2  # or pin a specific version
```

`vb update` detects how vibatchium was installed (pipx or pip, with a PEP-668
`--break-system-packages` fallback) and then **stops the running daemon** so the
next command loads the new code. Manual equivalent:

```bash
pipx upgrade vibatchium    # or: pip install -U vibatchium
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
| `research` command (parallel fan-out) | ❌ | ❌ | ❌ | ✅ |

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

Active-session resolution: `--session FLAG` → `$VIBATCHIUM_SESSION` env → `~/.config/vibatchium/active-session` → `default`. Cap via `VIBATCHIUM_MAX_SESSIONS=4` (default 4).

## Documentation

- [`AGENTS.md`](AGENTS.md) — coding-agent contract (Codex / Cursor / Claude Code)

## Server modes

| Mode | Surface | Auth |
|---|---|---|
| `vb mcp` | stdio JSON-RPC; `--caps=...` gates the bucket set | n/a (stdio) |
| `vb serve` | FastAPI on `127.0.0.1:8000`; every verb at `POST /v1/<verb>`; WebSocket live-view at `/v1/stream/<session>` | bearer token (`~/.cache/vibatchium/rest-token`, mode 0600) |

**REST capability gating**: `vb serve --caps=core,nav,input,vision` restricts the HTTP surface the same way `mcp --caps` does. Without it, REST grants local-code-equivalent access (eval + secret_* + file-writing verbs all exposed) — safe for localhost dev, **not** for hosted/multi-tenant.

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

Credentials never appear in logs, HAR captures, observe cache, or agent-visible response fields (grep-tested in CI). Vault uses XSalsa20-Poly1305 with key from OS keyring or `VIBATCHIUM_SECRETS_KEY`. All vibatchium-written files are 0600; directories 0700.

For the REST shim: without `--caps`, the bearer token grants every verb including `eval`, `secret_*`, and file-writing verbs. Local-code-equivalent — always pass `--caps=...` for hosted-mode. Live-view binds 127.0.0.1 only by default (`--insecure-public` to override).

## Honest limits

- **5+ concurrent sessions = 1-2GB RAM.** Each persistent-context Chrome is ~200-400MB. Bump cap with `VIBATCHIUM_MAX_SESSIONS=8`.
- **Vision spend cap is process-wide.** N fan-out agents share one daily/lifetime budget.
- **Init scripts don't work on patchright backend.** `chrome.runtime` stays `undefined` — accepted trade for stealth wins.
- **Login walls (X, LinkedIn) require attach mode.** Cold-launch fan-out can't defeat sites requiring authenticated sessions.
- **Single daemon = single point of failure.** No HA built in.
- **PyPI version (0.1.0) is stale.** Install from the git URL above for the current feature surface.

## License

Apache-2.0 (core). Optional extras pull their own licenses: `nodriver` (AGPL-3.0). CDP-Patches (GPL-3.0) installs separately (not a pip extra — PyPI forbids `git+https://` deps): `pip install git+https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches.git@main`. Never required for the base install.
