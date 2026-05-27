# Operations playbook

Cookbook for running vibatchium in production-shaped workflows. From real dogfood runs (geminixprize 5-thread fan-out, iterative single-session, mixed-tool 12-angle competitor scan).

For first-install / agent contract see [AGENTS.md](../AGENTS.md). This doc is for operators who already run vibatchium.

## Why mix tools

| Pattern | Wall clock | Notes |
|---|---|---|
| Pure WebFetch / WebSearch | ~5 min | Misses SPA pages, no domain probe, no audit trail, gets walled on bot-targeted surfaces |
| Pure vibatchium, single session iterative | **53 min** | Drowned operator in `start && go && text` loops for queries WebSearch could've answered |
| Pure vibatchium, 5-thread fan-out | 10 min | Right for breadth, wrong when threads build on each other |
| **Mixed (WebSearch breadth + vibatchium for SPA/walls/domains)** | **11 min for 12 angles** | **~13× throughput** vs pure-vibatchium single-session. The right default. |

Key insight from the mixed run: **parent-level `verify_url` killed 10 candidate domains in 1.5s before the subagent dispatched**, saving 10 × 30s nav timeouts. 1200× ROI on a 50-line verb.

## Environment variables

Set at daemon-bootstrap (`vb daemon start`) or in the daemon's env. None required.

| Variable | Default | When to set |
|---|---|---|
| `VIBATCHIUM_MAX_SESSIONS` | `4` | Fan-out runs with ≥5 parallel sessions |
| `VIBATCHIUM_DEFAULT_HEADLESS` | unset | Fan-out / background — keeps Chrome off your desktop |
| `VIBATCHIUM_DEFAULT_SAFETY` | `flag-only` | `off` to skip prompt-injection scan; `wrap`/`redact` for higher stakes |
| `VIBATCHIUM_LOG_VERBS` | `0` | `1` for full per-verb audit trail (pair with `VIBATCHIUM_LOG_LEVEL=DEBUG`) |
| `VIBATCHIUM_WARM` | `both` | `eager`/`opportunistic`/`both`/`off` — pre-warm pool behavior |
| `VIBATCHIUM_WARM_RECYCLE` | `0` | `1` for "close/reopen same session" workflows |
| `VIBATCHIUM_SECRETS_KEY` | unset | Base64 32-byte vault key when not using OS keyring |
| `VIBATCHIUM_DISABLE_SANDBOX` | `0` | Last-resort sandbox opt-out (Docker w/o user namespaces only) |

Bootstrap for fan-out research:

```
vb daemon start --max-sessions 8 --default-headless --default-safety wrap --log-verbs
```

8 parallel slots, headless, prompt-injection wrapping, full audit trail in `$XDG_RUNTIME_DIR/vibatchium/daemon.log`.

## Recipe 1: Fan-out research (parallel breadth)

When N sub-questions are independent.

```
vb daemon start --max-sessions 8 --default-headless
for url in <candidates>; do vb verify_url --url "$url"; done   # kill dead candidates
vb research \
  --target https://<surviving>.com \
  --intent "<q1>" --intent "<q2>" --intent "<q3>" \
  --output-dir ./research-out
vb logs --since 10m --tail 50    # watch live (separate terminal)
```

Measured: 5 sessions, ~10 min wall clock, 5 markdown + 5 screenshots + index.md.

## Recipe 2: Iterative deep-dive (single-session)

When each query informs the next.

```
vb daemon start --log-verbs
vb session_new deepdive
vb --session deepdive start --headless
vb --session deepdive go <starting-url>
vb --session deepdive text > round-1.txt
# ... decide next URL, repeat ...
vb --session deepdive session_close
vb logs --session deepdive --tail 100 --errors-only   # post-mortem
```

## Recipe 3: Mixed-tool fan-out (the recommended default)

The pattern that delivered 13× throughput. WebSearch for breadth, vibatchium for what only a real browser can do.

Parent (you / top-level Claude):
1. WebSearch × 15-20 queries → ranked shortlist of 10-15 angles
2. Parent-level `verify_url` × all candidate domains (~150ms total)
3. Dispatch subagent with ONLY validated URLs + vibatchium-shaped sub-tasks

Subagent brief routes per-task:
- WebSearch for: Google searches, news lookup, Reddit threads
- vibatchium for: marketplaces (Xero App Store), live competitor domains, gov SPAs
- `verify_url` first for any unvalidated domain

Wall-clock target: 10-12 min for 10-15 angles fully scanned.

## Anti-patterns from real runs

**1. Dead-DNS without `verify_url`** — 30s per bad guess × N candidates. Always pre-check LLM-generated URLs. Real run: 1200× ROI (250ms saved 300s).

**2. Headed mode in fan-out** — desktop clutter + repeated operator corrections. Either pass `headless: true` on every call or set `VIBATCHIUM_DEFAULT_HEADLESS=1` once at bootstrap.

**3. Sequential single-session for breadth questions** — 53 min vs 10 min for comparable scope. When sub-questions are independent, `vb research` instead of `session_new && start && go && text` loops.

**4. Vibatchium for what WebSearch does well** — ~3× wall clock. Vibatchium's edge: SPA execution, walled-page bypass, live-domain probing, audit trails. ~70% of iterative research is WebSearch territory.

**5. Trusting subagent TL;DRs without reading the chain** — shipped a wrong recommendation in one run because the synthesis was wrong even though extraction was correct. **Tooling doesn't fix bad inference.** Read the reasoning chain; if no time, don't dispatch.

## Observability rituals

After any non-trivial run:

```
vb logs --tail 100 | grep "session created\|session closed"
vb logs --since 1h | grep "walled-page detected"
vb logs --since 1h --errors-only
vb logs --session <name> --tail 200       # needs VIBATCHIUM_LOG_VERBS=1 at bootstrap
vb session prune --pattern <prefix> --dry-run
vb session prune --pattern <prefix>
```

## What vibatchium can't save you from

- **Synthesis errors** — bad LLM inference. Tooling captures source faithfully; reasoning on top is your problem.
- **Auth walls** — X, LinkedIn, hardened logins. Use `attach` mode with manual login.
- **CAPTCHAs needing solving** — Cloudflare Turnstile, hCaptcha. Out of scope; use a solving service.
- **Search index lag** — WebSearch can hide newly-launched competitors. Second-pass via vibatchium against the actual marketplace.
- **Concurrent daemon use** — one daemon per user. Two Claude sessions on the same machine share it. Run separate daemons under different `XDG_RUNTIME_DIR` for isolation.
