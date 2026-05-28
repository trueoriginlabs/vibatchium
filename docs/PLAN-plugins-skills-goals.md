# Plan: Plugins, Skills, and Goals

Roadmap for three composable features that extend vibatchium toward the useful parts of browser-use's product surface, while keeping its toolkit identity.

## Terminology (read this first)

Earlier drafts conflated two different things under "skills." They are distinct features:

| Term | What it is | Analogy | Execution |
|---|---|---|---|
| **Plugin** | A pluggable Python package that registers deterministic verbs (`vb x.search`, `vb x.post`) | a library / browser extension | deterministic, no LLM at call time |
| **Skill** | A per-host Markdown field-note the agent **reads as context** before driving an unfamiliar site | CLAUDE.md / AGENTS.md / Claude Code Skills | LLM reads, then writes its own actions |
| **Goal** | A durable, long-running, resumable operation that may call plugins or be informed by skills | a job/task with a state machine | async; emits an event stream |

**This is the key correction:** browser-use's "Browser Skills" are *Markdown field-notes written by the agent*, not executable modules (verified against `github.com/browser-use/browser-harness` — 109 `.md` files across 97 domains, matched by dumb hostname lookup, opt-in via `BH_DOMAIN_SKILLS=1`). So our Skills feature (section 2) is the true analog; our Plugins feature (section 1) is a separate plugin system that browser-use doesn't have. We keep both, named distinctly, so nobody coming from browser-use is confused.

## The spine

All three rest on the same primitive: **a named operation that emits typed events and consumes/produces typed data.** Plugins register verbs; Goals orchestrate verbs over time and emit events; Skills feed context into whatever drives them. One event schema (defined in Goals) serves all three.

Build order: **Goals → Plugins → Skills.** Goals introduces the persistent event schema; designing it last forces retrofits. Plugins prove the extension model with a real package (xscraper). Skills is the lightest and benefits from both.

---

## 1. Plugins — pluggable modules that add verbs

### Contract

A plugin is a Python package (or local directory) exposing a `register(daemon)` function. The daemon calls it once at startup; the plugin registers its verbs. The plugin's namespace becomes a first-class part of the verb surface — addressable from CLI, MCP, and REST exactly like the existing 127 verbs.

No YAML DSL, no interpreter, no auto-healing framework. Just modules. "Browser extensions for the daemon."

### Three ways to load a plugin

| Mechanism | When | Distribution |
|---|---|---|
| **Local directory** | personal/private, prototyping | `~/.config/vibatchium/plugins/<name>/__init__.py` |
| **Pip package + entry point** | publishable, shareable | `pip install <pkg>`; declares `[project.entry-points."vibatchium.plugins"]` |
| **Git install** | unpublished but shareable | `vb plugin install git+https://github.com/...` (thin wrapper over pip) |

Daemon discovers all three at startup via filesystem scan + `importlib.metadata.entry_points()`.

### Plugin shape

```
xscraper/
  pyproject.toml         # [project.entry-points."vibatchium.plugins"] xscraper = "xscraper.plugin:register"
  xscraper/
    __init__.py
    plugin.py            # def register(daemon): daemon.add_verb("x.search", search_fn) ...
    scraper.py           # existing implementation, unchanged
    ...
```

The `register` function is the only contract:

```python
def register(daemon: VibatchiumDaemon) -> None:
    daemon.add_verb(
        name="x.search",
        handler=search_fn,
        inputs_schema={"query": "string", "count": "integer"},
        outputs_schema={"tweets": "array"},
        caps_required=["nav", "input"],
        secrets_required=["x.com/auth"],
        description="Search X with the logged-in account.",
    )
    daemon.add_verb("x.user_tweets", user_tweets_fn, ...)
    daemon.add_verb("x.about", about_fn, ...)
```

`caps_required` / `secrets_required` are **descriptive metadata, not enforced** — they tell operators what the plugin says it needs. A plugin is Python running as your user; it can read the vault DB off disk, query the keyring, or read `/proc/<daemon>/environ` directly, sidestepping the verb layer entirely. Trust model is exactly pip-package trust. (Caps gating still works against *external* callers over the socket — MCP/REST/CLI clients — just not against plugin code itself. Real enforcement would need subprocess-per-plugin with a separate UID, namespaces, or WASM — a separate, much bigger project.)

### CLI / MCP surface

```
vb plugin list                      # installed plugins + their verbs
vb plugin show <name>               # metadata, declared caps/secrets, version
vb plugin install <pkg-or-git-url>  # pip-installs into vibatchium env
vb plugin remove <name>
vb plugin reload                    # rescan without restarting daemon
```

Plugin verbs are addressable as `vb <namespace>.<verb>` — `vb x.search "$BTC"` works because xscraper registered it. MCP exposure is automatic: a registered verb appears in the MCP tool list, discovered the same way as any verb.

### xscraper retrofit (v1 reference plugin, ~half a day)

xscraper already exists as a working pip package with typed methods, fixtures, rate-limit tracking, and selector-breakage detection. Retrofit:

1. Add `[project.entry-points."vibatchium.plugins"]` in `pyproject.toml`.
2. Add `xscraper/plugin.py` with `register(daemon)` wrapping `search`, `user_tweets`, `about`, `discover_community_url` as verbs under the `x.` namespace.
3. Optionally fold the existing `xscraper login` / `xscraper check` CLI into `vb x.login` / `vb x.check`.

xscraper keeps its repo, tests, and package identity. Existing `XScraper(...)` Python consumers are unaffected (the API is unchanged). The daemon just picks it up after `pip install xscraper`.

### xposter (v2 reference plugin, new package, ~1 wk)

Same pattern. Registers `x.post`, `x.reply`, `x.dm`, `x.like`, `x.retweet`. Shares the logged-in session with xscraper because both drive the same daemon session — no coordination beyond using the same session name.

### Modules in vibatchium

```
vibatchium/plugins/
  registry.py         # discovery (entry points + local dir scan), load, reload
  api.py              # VibatchiumDaemon.add_verb signature, schema types
vibatchium/cli/plugin.py
```

### Phases

| Phase | Scope | Effort |
|---|---|---|
| 1 | `add_verb` API, entry-point + local-dir discovery, `vb plugin list/show/reload` | ~3 d |
| 2 | xscraper retrofit | ~half day |
| 3 | xposter (new package) | ~1 wk |
| 4 | `vb plugin install <git-url>` wrapper | ~1 d |

### Trade-offs

- **Code-only, no YAML/DSL.** Any real plugin needs real code (xscraper's in-browser eval consolidation is proof). YAML doubled surface area without changing the trust posture.
- **No enforced caps.** See above — plugin code runs as your user. Trust is pip-package trust.
- **No central registry.** PyPI is the marketplace; `pip` is discovery.

---

## 2. Skills — per-host Markdown field-notes (the real browser-use analog)

### Contract

A Skill is a Markdown file of accumulated field-notes about one host: which selectors work, gotchas, login quirks, "prefer the REST API here," "this site walls cold-launch — use attach mode." When the agent navigates to a host, the daemon surfaces the matching notes so the agent reads them *before* inventing an approach.

This is **memory/context**, not executable code. The agent still drives via verbs; the notes just make it competent faster on sites it (or someone else) has seen before.

### Format (deliberately loose, browser-use-compatible)

Free Markdown, light convention — matches browser-use's `domain-skills/<host>/*.md` so their public directory (~97 domains) is **importable**:

```
~/.local/share/vibatchium/skills/
  amazon.com/
    product-search.md
  github.com/
    scraping.md
  kayak.com/
    flight-search.md     # may say: "prefer the kayak plugin `vb kayak.search` — faster than the UI"
```

A note file: H1 title, a dated "verified" line, `##` task sections with prose + code snippets and confirmed selectors. No required schema. Convention (from browser-use's hard-won lessons): **no pixel coordinates** (break on layout), **no secrets** (notes are shareable).

Note that skills can point at plugins ("for X, call `vb x.search` instead of driving the UI") — the two features compose: Skills tell the agent *that a deterministic plugin exists* for a site.

### Matching

On `go <url>` (and `explore`), after navigation the daemon computes a host key (strip `www.`, take the registrable host) and looks up `skills/<host>/`. It returns the list of matching note filenames in the `go` response — and, for short notes, optionally inlines them. The agent is instructed (via MCP tool description / AGENTS.md) to read them before acting.

`go` already auto-detects walled pages, so it already has a post-navigation hook — this slots in there.

### Authoring

- **Manual:** `vb skill write <host> --title "..." --body @notes.md`, or just drop a `.md` file in the dir.
- **Agent-authored (the browser-use magic):** the agent calls `vb skill write <host> ...` when it learns something non-obvious during a run. This is a convention prompted via the MCP description, not daemon-forced.
- **Import:** `vb skill import git+https://github.com/browser-use/browser-harness#agent-workspace/domain-skills` pulls their directory into the local skills dir (format-compatible).

### Safety — vibatchium's differentiator over browser-use

Skills get **injected into the agent's context**, which makes them a prompt-injection surface. browser-use reads the files raw. vibatchium already ships `safety_scan` (0% FP / 204 samples) — run it over note content before injection and before `skill write`/`skill import`. Two checks:

1. **Injection scan** on read — flagged notes are surfaced with a `safety_flagged` marker (or withheld) so a malicious shared note can't smuggle "ignore previous instructions" into the agent.
2. **Secret scan** on write/import — refuse to persist a note containing secret-like patterns (tokens, cookies, passwords), enforcing the "no secrets" convention mechanically instead of by etiquette.

This makes vibatchium's skill-sharing safer than browser-use's by default.

### Opt-in

Default **off** (like browser-use's `BH_DOMAIN_SKILLS=1`) — set `VIBATCHIUM_SKILLS=1` or a per-session flag. Reasons: injecting notes on every navigation costs context tokens, and it's an injection surface. Opt-in keeps the default surface clean.

### CLI / MCP surface

```
vb skill list [<host>]              # notes on disk, by host
vb skill show <host>/<file>
vb skill write <host> --title ... --body ...
vb skill rm <host>/<file>
vb skill import <git-url>           # pull a note directory (e.g. browser-use's)
```

### Modules in vibatchium

```
vibatchium/skills/
  store.py            # host-keyed markdown store under ~/.local/share/vibatchium/skills/
  match.py            # hostname → note filenames (the go-time hook)
  safety.py           # safety_scan integration on read + write/import
vibatchium/cli/skill.py
```

### Phases

| Phase | Scope | Effort |
|---|---|---|
| 1 | host-keyed store, `go`-time matching + surfacing, `vb skill list/show/write/rm` | ~3 d |
| 2 | `safety_scan` on read (injection) + write/import (secrets) | ~2 d |
| 3 | `vb skill import` from browser-use's directory (format interop) | ~1 d |

### Trade-offs

- **Loose Markdown, not schema.** browser-use learned this — over-structuring notes makes them brittle and hard for the agent to author. Keep prose.
- **Agent-authored notes can drift/contradict.** No auto-dedup in v1. The agent reads all notes for a host; conflicting advice is the agent's problem to resolve, same as browser-use.
- **Off by default.** Costs context tokens and is an injection surface; opt-in is the safe default.

---

## 3. Goals — durable long-running loop

### Contract

A Goal is a persistent record with a state machine, bound to one session, with budget enforcement, crash-resumability, an event stream, and a pluggable agent driver. The daemon does **not** run the LLM by default — an external driver (Claude Code, Codex, custom) calls `goal_next` / `goal_step` in a loop. An optional **builtin driver** can be enabled later if there's demand.

### State machine

```
pending → running ⇄ paused
              ↓        ↑
         needs_input ──┘
              ↓
           done | failed | cancelled
```

- `running` holds an exclusive lock on its session.
- `paused` releases the lock and snapshots a `checkpoint_id`.
- `needs_input` is `paused` with a pending question; resumes on `goal_answer`.
- On daemon restart, all `running` → `paused` with reason `daemon_restart`.

### Storage

SQLite at `~/.local/share/vibatchium/goals.db` (mode 0600).

```sql
CREATE TABLE goals (
  id TEXT PRIMARY KEY,                   -- ULID
  description TEXT NOT NULL,
  session TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  status TEXT NOT NULL,
  budget_json TEXT NOT NULL,             -- {max_steps, max_spend_usd, max_wall_minutes}
  consumed_json TEXT NOT NULL DEFAULT '{}',
  inputs_json TEXT NOT NULL,
  outputs_json TEXT,
  notifier TEXT,                         -- webhook://... | mcp_push | stdout
  driver TEXT NOT NULL,                  -- external | builtin
  parent_id TEXT,                        -- for sub-goals
  caps TEXT NOT NULL,                    -- restricted caps for this goal
  domain_allowlist TEXT,                 -- nullable CSV of allowed origins
  current_step INTEGER NOT NULL DEFAULT 0,
  checkpoint_id TEXT,
  client_token_idx TEXT NOT NULL DEFAULT '{}'  -- token → step map for idempotent retries
);
CREATE TABLE goal_events (
  goal_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (goal_id, seq)
);
CREATE TABLE goal_artifacts (
  goal_id TEXT NOT NULL, name TEXT NOT NULL,
  path TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
  PRIMARY KEY (goal_id, name)
);
```

Artifacts on disk at `~/.cache/vibatchium/goals/<id>/`.

### Event kinds (the universal vocabulary)

`step_start | step_done | step_failed | observation | plan_update | question | user_input | artifact | model_call | budget_consumed | session_attached | session_released | checkpoint_saved | done | failed | cancelled`

Shared with notifiers and any consumer. One schema.

### External-driver flow

```
agent →  vb goal next                 # daemon picks a runnable goal, locks session, returns context
agent →  observe via vb verbs         # map, screenshot, text, plus any matching skills surfaced
agent →  vb goal step --goal-id ... --action <json> --observation <json>
        # daemon records event, charges budget, checkpoints at boundary
agent →  vb goal ask "do you have ..."  # status → needs_input, push to notifier
... user replies via notifier ...
agent →  vb goal next                 # picks it back up
agent →  vb goal done --outputs <json>
```

The agent never holds in-process state beyond one step. Daemon survives agent crashes; agent survives daemon crashes (idempotent via `--client-token`).

### Budget & safety enforcement

The daemon (not the agent) is the budget cop. Each `goal_step` decrements `steps`, `spend_usd` (model_call events carry token counts × price table), `wall_minutes`. Hard stop on exceed → `failed:budget_exceeded`, notifier informed.

The daemon runs `safety_scan` on every observation before storing it. Flagged content is included but tagged `safety_flagged=true`.

`caps` on the goal record restrict which verbs the daemon accepts from this goal's agent (`eval`, `secret_*`, `route_*` refused even if the daemon has full caps). Goals are the right boundary to enforce caps because they're driven over the verb surface — unlike plugins, which run as in-process code.

### CLI / MCP surface

```
vb goal new --description ... [--session NAME] [--notifier URI] [--budget steps=30,minutes=20,spend_usd=2] [--driver external|builtin] [--caps ...] [--allow-domains ...]
vb goal list [--status STATE]
vb goal show <id>
vb goal tail <id>                # SSE / WebSocket event stream
vb goal next
vb goal step --goal-id ... --action ... --observation ... --client-token ...
vb goal ask --goal-id ... --question "..."
vb goal answer --goal-id ... --text "..."
vb goal done --goal-id ... --outputs ...
vb goal pause <id> | resume <id> | cancel <id>
vb goal spawn --parent <id> --description ...
vb goal tree <id>
vb goal artifacts <id>
```

### Crash-resume mechanics

- Each `step_done` writes a `checkpoint_saved` event using the existing `checkpoint_save`.
- On daemon start, `status='running'` → `paused`/`reason=daemon_restart`; drop session locks.
- On next `goal_next` for a paused goal with `checkpoint_id`: restore checkpoint into a fresh session bind, emit `session_attached`, hand context back.
- If a step was mid-flight at crash, the `client_token` makes the retry idempotent.

### Notifiers (lightweight)

| Notifier | URI | Use case |
|---|---|---|
| **stdout** | `stdout://` (default) | local dev — events to daemon log |
| **webhook** | `webhook://https://example.com/hook` | external service receives POSTs |
| **mcp_push** | `mcp_push://` | events surfaced to the owning MCP client |

Notifier ABC in `vibatchium/goals/notifiers.py`, ~50 LOC each. Telegram/Slack/Discord are explicitly **out of scope** — they need their own ACL/caps/budget machinery and pull vibatchium toward end-user UX that browser-use Box already owns.

### Builtin driver (deferred, optional)

A small builtin agent loop (Anthropic/OpenAI/local) gated by `VIBATCHIUM_BUILTIN_AGENT=on`, same event schema and enforcement as the external driver. Deferred — the external-driver path is the primary value.

### Modules

```
vibatchium/goals/
  store.py            # SQLite + migrations
  engine.py           # state machine, budget, session locking, idempotency
  events.py           # shared event schema (also used by notifiers)
  notifiers.py        # stdout, webhook, mcp_push
  drivers/
    external.py
    builtin.py        # deferred
vibatchium/cli/goal.py
vibatchium/mcp/handlers/goal.py
vibatchium/rest/goal.py  # tail SSE
```

### Phases

| Phase | Scope | Effort |
|---|---|---|
| 1 | Schema + state machine + external driver, stdout notifier | ~2 wk |
| 2 | Checkpoint integration, crash-resume, idempotent retries | ~1 wk |
| 3 | Webhook + MCP-push notifiers | ~3 d |
| 4 | `goal tail` SSE, `goal tree`, artifact UX | ~1 wk |
| 5 | Builtin LLM driver (deferred) | ~2 wk |

### Trade-offs

- **External-first driver.** Preserves toolkit identity. Builtin-default would start a fight with browser-use Cloud on agent quality.
- **SQLite vs jsonl.** SQLite for cheap status queries, idempotency uniqueness, atomic writes; JSON payloads in columns.
- **One session per goal.** Hopping breaks crash-resume. Sub-goals use sibling sessions for parallelism.
- **No compensating actions on cancel.** Encourages idempotent steps.

---

## Cross-cutting decisions

- **One event schema** (`vibatchium/goals/events.py`) — Goals and (when relevant) plugins emitting progress share it.
- **Three distinct extension concepts, named distinctly.** Plugins = code/verbs. Skills = markdown memory. Goals = orchestration. Never call two of them the same thing again.
- **Persistence roots:** `~/.local/share/vibatchium/` for `goals.db` + `skills/` markdown; `~/.config/vibatchium/plugins/` for local-dir plugins; `~/.cache/vibatchium/` for artifacts. 0600 / 0700.
- **`safety_scan` everywhere content enters the agent:** goal observations, and skill notes on read/write/import. This is vibatchium's recurring edge.
- **Caps:** enforced at the verb boundary for Goals (real); descriptive-only for Plugins (Python runs as your user).
- **No new background daemons.** Plugins load in-process at startup; Goals run on-demand; Skills are file lookups. One process, one socket, one PID.

## Recommended build order

1. **Goals Phase 1+2** (~3 wk) — unlocks durable goals for external drivers; locks the event schema.
2. **Plugins Phase 1+2** (~4 d) — `add_verb` + discovery + xscraper retrofit. Proves the extension model on a real package.
3. **Plugins Phase 3** (~1 wk) — xposter (new package). Proves the pattern scales.
4. **Skills Phase 1-3** (~6 d) — markdown notes, `go`-time surfacing, safety_scan, browser-use import. The lightest feature, biggest agent-competence payoff.
5. **Goals Phase 3+4** (~1.5 wk) — notifiers, SSE tail, tree, artifacts.
6. **Goals Phase 5** (deferred) — builtin LLM driver if demand appears.

Total for the toolkit-relevant slice (durable Goals + a Plugin ecosystem + Skill memory): ~6-7 weeks. Consumer-facing channels (Telegram et al.) remain out of scope — browser-use Box's territory, and adding them dilutes positioning.
