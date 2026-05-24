# Operations playbook

Cookbook for actually running patchium in production-shaped workflows. Distilled from real dogfood runs (geminixprize 5-thread fan-out, ideation iterative single-session, mixed-tool 12-angle competitor scan).

If you're new, read [README.md](../README.md) first. This doc is for operators who already have patchium installed and want to know what to actually do with it.

## Why mix tools

Numbers from real runs:

| Pattern | Wall clock | Notes |
|---|---|---|
| Pure WebFetch / WebSearch | ~5 min | Misses SPA pages, can't probe domain availability, no audit trail, gets walled on bot-targeted surfaces |
| Pure patchium, single session iterative | **53 min** | Drowned the operator in `start && go && text` loops for queries WebSearch could have answered |
| Pure patchium, 5-thread fan-out | 10 min | Right for breadth, wrong when each thread builds on the prior |
| **Mixed (WebSearch breadth + patchium for SPA/walls/domains)** | **11 min for 12 angles** | **~13× throughput** vs pure-patchium single-session. The right default. |

The mixed-tool win came from one specific architectural decision: **parent-level `verify_url` ran 10 candidate domains in 1.5s and killed them before the subagent dispatched** — saving 10 × 30s nav timeouts the subagent would have eaten. 1200× ROI on a 50-line verb.

## Environment variables

Set these at daemon-bootstrap time (`patchium daemon start`) or in the daemon's environment. None are required; defaults assume single-session interactive use.

| Variable | Default | When to set |
|---|---|---|
| `PATCHIUM_MAX_SESSIONS` | `4` | Fan-out runs with ≥5 parallel sessions |
| `PATCHIUM_DEFAULT_HEADLESS` | unset (= headed) | Fan-out / background scraping — keeps Chrome windows off your desktop |
| `PATCHIUM_DEFAULT_SAFETY` | `flag-only` | Set `off` to skip prompt-injection scanning entirely; `wrap`/`redact` for higher-stakes flows |
| `PATCHIUM_LOG_VERBS` | `0` | Set `1` for full per-verb audit trail — pair with `PATCHIUM_LOG_LEVEL=DEBUG` |
| `PATCHIUM_LOG_LEVEL` | `INFO` | `DEBUG` enables verb logging when `PATCHIUM_LOG_VERBS=1` |
| `PATCHIUM_WARM` | `both` | `eager` / `opportunistic` / `both` / `off` — controls pre-warm pool |
| `PATCHIUM_WARM_RECYCLE` | `0` | Set `1` for "close/reopen same session name" workflows — re-prewarms on close |
| `PATCHIUM_SECRETS_KEY` | unset | Base64 32-byte vault key when not using OS keyring |
| `PATCHIUM_DISABLE_SANDBOX` | `0` | Last-resort opt-out of Chrome sandbox (Docker w/o user namespaces only) |

One-shot bootstrap for fan-out research:

```
patchium daemon start \
  --max-sessions 8 \
  --default-headless \
  --default-safety wrap \
  --log-verbs
```

That gives you 8 parallel slots, headless by default, prompt-injection wrapping on every scraped field, and a full audit trail in `$XDG_RUNTIME_DIR/patchium/daemon.log`.

## Recipe 1: Fan-out research (parallel breadth)

When the question has N independent sub-questions that don't build on each other.

```
patchium daemon start --max-sessions 8 --default-headless

# Parent-level pre-check: kill dead candidates before dispatching subagent
for url in <candidates>; do patchium verify_url --url "$url"; done

# One-shot fan-out
patchium research \
  --target https://<surviving-candidate>.com \
  --intent "<sub-question 1>" \
  --intent "<sub-question 2>" \
  --intent "<sub-question 3>" \
  --intent "<sub-question 4>" \
  --intent "<sub-question 5>" \
  --output-dir ./research-out

# Watch live (separate terminal)
patchium logs --since 10m --tail 50
```

**Measured throughput**: 5 parallel sessions, ~10 min wall clock, 5 markdown files + 5 landing screenshots + 1 index.md.

## Recipe 2: Iterative deep-dive (single-session refinement)

When each query informs the next — reading a regulator page, drilling into one specific document, following a chain of links.

```
# One-time bootstrap for full audit visibility
patchium daemon start --log-verbs

# Single session, manual iteration
patchium session_new deepdive
patchium --session deepdive start --headless   # explicit; or set DEFAULT_HEADLESS
patchium --session deepdive go <starting-url>
patchium --session deepdive text > round-1.txt

# ... read, decide next URL ...
patchium --session deepdive go <next-url>
patchium --session deepdive text > round-2.txt

# Done
patchium --session deepdive session_close

# Find errors / latency after the fact
patchium logs --session deepdive --tail 100 --errors-only
```

## Recipe 3: Mixed-tool fan-out (the recommended default)

The pattern that delivered 13× throughput in the real run. Use WebSearch for breadth, patchium for the bits only a real browser can do.

```
# Parent (you or a top-level Claude):
#   1. WebSearch × 15-20 queries → ranked shortlist of 10-15 angles
#   2. Parent-level `verify_url` × all candidate domains (~150ms total)
#   3. Dispatch subagent with ONLY validated URLs + only patchium-shaped sub-tasks

# Subagent brief should explicitly route per-task:
#   - "Use WebSearch for: Google searches, news lookup, Reddit threads"
#   - "Use patchium for: Xero App Store, live competitor domains, gov SPAs"
#   - "Use verify_url first for any domain not already validated"

# Wall-clock target: 10-12 min for 10-15 candidate angles fully scanned
```

## Tool routing decision tree

| Target shape | Tool | Why |
|---|---|---|
| Plain HTML, known URL, single fetch | WebFetch | No Chrome RAM, faster |
| Google search / SERP iteration | WebSearch | Pre-summarized for LLM, no daemon |
| News / Reddit / forum threads | WebSearch | Faster snippets; patchium is overkill |
| JS-rendered SPA (regulator dashboards, hosted-app pricing pages, marketplaces) | **patchium** | WebFetch returns boilerplate |
| Cloudflare / Datadome / Akamai walled | **patchium** | Stealth backend clears most cold; WebFetch gets 403 |
| Live competitor domain — "does X.com.au actually run a SaaS?" | **patchium** | One-line answer; WebSearch snippets are ambiguous |
| Candidate domain availability check | **patchium `verify_url`** | 50ms DNS check; WebSearch can't |
| Marketplace listings (Xero App Store, Devpost, etc.) | **patchium** | Search hides newly-launched competitors via index lag |
| Need verifiable screenshot of "did the regulator really say X" | **patchium** | Audit trail; WebFetch can't produce these |
| Auth-gated sites (X, LinkedIn) | **patchium attach mode** | Cold-launch fan-out can't defeat auth walls |

## Anti-patterns from real runs

**1. Dead-DNS without `verify_url`** (cost: 30s per bad guess × N candidates)

Direct `go https://<guessed-domain>` burns the full 30s nav timeout if the domain doesn't resolve. **Always** run `verify_url` first on caller-supplied or LLM-generated URLs. The real run saw 1200× ROI from this one habit (250ms saved 300s).

**2. Headed mode in fan-out workflows** (cost: desktop clutter + 2 corrections per run)

Default `start` is headed. For background scraping you almost never want this. Either pass `headless: true` on every call (operator discipline that didn't stick across runs) or set `PATCHIUM_DEFAULT_HEADLESS=1` once at daemon bootstrap.

**3. Sequential single-session for breadth questions** (cost: 5× wall clock)

When the N sub-questions are independent, fan out. The today-vs-yesterday comparison was stark: 53 min single-session iterative vs 10 min 5-thread fan-out for comparable breadth. `patchium research` is built for this; reach for it before defaulting to `session_new && start && go && text` loops.

**4. Patchium for what WebSearch does well** (cost: ~3× wall clock)

If the task is "Google search → extract snippets → iterate," WebSearch wins on speed every time. Patchium's edge is specifically the things WebSearch can't do: SPA execution, walled-page bypass, live-domain probing, audit trails. ~70% of an iterative-research session is typically WebSearch territory.

**5. Trusting subagent TL;DRs without reading the chain** (cost: shipping a wrong recommendation)

Real example: subagent claimed "FranchiseFile's annual cycle is perfectly aligned with prize launch" — the patchium-extracted regulator page text had the correct dates, the subagent's synthesis was wrong about which cycle peaked when. **Tooling doesn't fix bad inference.** Always read the subagent's reasoning chain, not just its conclusion. If you don't have time, you don't have time to dispatch the subagent.

## Observability rituals

After any non-trivial run:

```
# What sessions ran, when?
patchium logs --tail 100 | grep "session created\|session closed"

# Were any pages walled?
patchium logs --since 1h | grep "walled-page detected"

# What errored?
patchium logs --since 1h --errors-only

# Per-session activity (requires PATCHIUM_LOG_VERBS=1 set at bootstrap)
patchium logs --session <name> --tail 200

# Clean up stale sessions left by previous runs
patchium session prune --pattern <prefix> --dry-run
patchium session prune --pattern <prefix>     # actually delete
```

## What patchium can't save you from

- **Synthesis errors** — bad inference at the LLM layer. Tooling captures source text faithfully; the reasoning on top is your problem.
- **Auth walls** — X, LinkedIn, hardened logins. Use `attach` mode with a manual login first.
- **CAPTCHAs requiring solving** — Cloudflare Turnstile, hCaptcha challenge. Out of scope; use a CAPTCHA service if you must.
- **Search index lag** — WebSearch can hide newly-launched competitors. For high-stakes "is there an incumbent" questions, do a second pass via patchium against the actual marketplace.
- **Concurrent daemon use** — one daemon per user. Two Claude sessions on the same machine share the daemon and can clobber each other's state. Run separate daemons under different `XDG_RUNTIME_DIR` if you need isolation.
