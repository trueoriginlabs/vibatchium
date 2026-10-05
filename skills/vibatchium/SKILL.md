---
name: vibatchium
description: Drive a real stealth Chrome when plain HTTP/WebFetch won't work — sites behind Cloudflare/DataDome/PerimeterX, JavaScript SPAs, logged-in or authenticated pages, multi-step form/checkout flows, and parallel multi-site automation. Use whenever the user wants to browse, scrape, research, screenshot, log into, or click through a website that blocks bots or needs a real browser — including when a plain fetch came back 403, a "Just a moment..." challenge, or an empty JavaScript shell. Also does bulk web SEARCH (`vb search`) without spending the host's capped WebSearch budget. Runs via the `vb` CLI + MCP tools.
---

# vibatchium — agentic stealth browser

Everything below runs through the `vb` CLI. Check it's there with
`vb --version`; if it isn't, install it once (Python 3.11+ on Linux or another
Unix; Chrome downloads itself on first launch):

    pipx install 'vibatchium[all]'    # or: uv tool install 'vibatchium[all]'
    vb setup                          # optional: register the MCP server too

**Reach for this when** a page is walled (Cloudflare/DataDome/PerimeterX), is a
JavaScript SPA, needs a login/session, or the task is multi-step. For plain
static HTML or a Google/news lookup, use WebFetch/WebSearch — it's cheaper.

## The 80% — one-call verbs
- `vb explore <url>` — look at a page: text + screenshot, auto-closes.
- `vb research --target <url> --intent "..." --intent "..."` — parallel fan-out.
- `vb observe` then `vb act "<instruction>"` — semantic see-then-do.
- `vb search "<query>"` — find URLs. No browser, no API key, no host budget.

## Four rules that decide whether this is fast or slow

Measured across thousands of real agent runs. Each of these is the single most
common way sessions get slow or flaky.

**1. Never `sleep` after a navigation or click. Wait for the thing.**

    vb --session s wait selector "text=Add to cart" --state visible --timeout 20000

A blind `sleep 8` is both slower and less reliable than a 0.2s wait that
returns the moment the element is there. On one measured flow, deleting sleeps
took a per-item cycle from ~34s to ~7s — that swap *was* almost the whole win.

**2. Don't reach for `eval`. There is almost always a verb.**

| You were about to `eval`… | Use instead |
|---|---|
| `innerText` / `textContent` | `vb text <sel>` · `vb extract` |
| `querySelectorAll(...).length` | `vb count <sel>` · `vb candidates <sel>` |
| `.click()` | `vb click <sel>` · `vb act "<intent>"` |
| reading `href` / attributes | `vb attr <sel> href` · `vb extract-fields` |
| `window.scrollTo(...)` | `vb scroll` |

The verbs are faster, and they report failure instead of returning `undefined`.
Keep `eval` for genuine JS with no verb. And **don't `eval` on a busy page** —
isolated-world evaluation still needs the main thread, so an ad-heavy page can
hang it; confirm with `screenshot` instead.

**3. Prefer `act` over probe-then-click.**

    vb --session s act "click the Add to cart button"

One call instead of count→inspect→click. It runs on the **heuristic** tier by
default — no API key, no inference cost (`--llm` opts into the model) — and
returns the `@eN` it chose, a rationale, and a durable locator that keeps
working when the DOM shifts.

**4. Assert the side effect, not the exit code.** A click that "succeeded" in
4ms did not happen. Check the URL changed, the count rose, the element appeared.

## Finding pages (`vb search`)

Your built-in WebSearch has a per-session call budget shared with every
subagent — a wide fan-out drains it and the lanes that start afterwards get
nothing. `vb search` has no such budget, so use it for bulk discovery and save
WebSearch for what it can't serve.

    vb search "playwright stealth detection" -n 5
    vb search "cdp leak" --site github.com --urls | xargs -I{} vb fetch --no-cookies {}

- Engine LADDER `ddg → ddg-lite → bing`, tried until one answers. Pin with
  `--engine` when you need determinism.
- Read `attempts` in `--json`: `ok:false` means **every engine declined**, not
  that the web is empty. Those need different responses — retry/proxy vs.
  rephrase the query.
- Engines rate-limit **per IP**. On a wide fan-out pass `--proxy <url>` rather
  than hammering one address. `HTTP(S)_PROXY` is never consulted — unset always
  means direct egress.
- Sessionless by design: never attaches a logged-in session's cookies to search
  traffic. Pairs with `vb fetch --no-cookies <url>` to read what it finds.
- Needs the `[fetch]` extra and the `search` cap (see Optional lanes).

## Reading a page without a browser (`vb fetch`)

`fetch` is an HTTP lane that impersonates Chrome's TLS/HTTP2 fingerprint
(curl_cffi), so it gets through fingerprint walls that block plain
WebFetch/requests — without paying for a renderer. **No JavaScript runs**, so
it's the right tool for JSON/API endpoints and server-rendered HTML, and the
wrong one for an SPA that hydrates client-side (use `explore` there).

    vb fetch --no-cookies https://api.example.com/v1/items   # sessionless
    vb --session work fetch https://site.com/account.json    # reuses that
                                                             # session's cookies

Reusing a session's cookies is the point: log in once in the browser, then pull
authenticated endpoints at HTTP speed. `--no-cookies` when you don't need the
identity — it's the lower blast-radius default for anything public.

## Addressing elements

`observe` returns `@eN` refs, but you can also address elements directly —
no need to observe first:

    @e3                       a ref from the last observe/map
    @text:Add to cart         visible text
    @label:Email              form label
    @role:button              ARIA role
    @role:button[name=Submit] role + accessible name
    div.price > span          plain CSS also works

**The one exception, and it will bite you:** `wait selector` does NOT accept
the `@…` grammar — it passes the string to Playwright as a raw selector and
errors `Unsupported token "@text"`. Inside `wait`, use Playwright syntax
(`text=Add to cart`) instead. `wait ref @e3` does resolve refs. When several
elements match, `vb candidates <sel>` lists them with their visible text and
you pick with `--index N`.

## Multi-step / logged-in flows
    vb session new work && vb --session work start
    vb --session work go <url>      # log in by hand once; cookies persist
    vb --session work observe       # @eN element refs
    vb --session work click @e3

Filling things in:

    vb --session work fill @label:Email "me@example.com"   # set a value at once
    vb --session work type @text:Search "fpv drone"        # keystroke-by-keystroke
    vb --session work select @label:Country "Australia"
    vb --session work press @label:Search Enter
    vb --session work upload @text:Attach ./file.pdf

`fill` sets the value directly; `type` sends real keystrokes (`--delay` between
them) and is what you want when a field only reacts to key events — an
autocomplete, or a form that validates as you type.

Pulling structured data out in one round trip:

    vb --session work extract-fields --fields '{"title":"h1","prices[]":".price"}'

Grammar: `name[]` = array, `sel@attr` = attribute, bare = text.

Sessions are independent Chromes — run several in parallel, no cookie bleed.

**Close what you open.** Each live session is a real Chrome holding on the
order of a gigabyte, and it stays resident until closed — most sessions ever
started were never explicitly closed. When a task is done:

    vb session close <name>        # stop Chrome, keep the profile + cookies
    vb --session <name> stop       # same for the session you're on

Use `--ephemeral` at `start` for one-shot work that shouldn't leave a profile
behind at all, and `vb explore` (which auto-closes) whenever one look suffices.

Slow, ad-heavy pages: plain `vb go <url>` is already the right call — it waits
for `domcontentloaded` (60s cap), never for network idle, so don't weaken it
to `--wait-until commit`; follow it with `wait selector` for the element you
need. A killed or timed-out client does **not** cancel the verb — the click or
navigation may still land — so re-check the page before retrying. If a session
looks hung, `vb session close <name>` gets through without queueing behind it.

## When a page is walled

`go` reports `cloudflare_walled: <defender>` and fills in an `advice` string
naming the exact commands for *this* session. **Read `advice` and follow it** —
it knows things this file can't, like whether the daemon even has a display.
The shape it recommends:

**A human solves it** (captcha / interactive challenge) — open the profile in a
real, visible window:

    vb session close <s> && vb show <s> --url <url>    # alias: vb login
    vb show --close <s>                                # when done

`vb show` is the verb for a window a person looks at. It runs a separate
windowed daemon, so live sessions are untouched. Cookies persist, so once a
human clears the wall the normal headless session can carry on.

**Automatic retry, no human** — each rung needs the session CLOSED first,
because backend and headedness are fixed at launch:

1. `vb session close <s> && vb --session <s> start --headed` — the biggest
   measured win: headless leaves GPU and screen tells a real window doesn't.
   Needs a display; on a display-less daemon `advice` will omit this rung
   rather than recommend a command that daemon would reject.
2. `vb humanize on` — human-like timing/motion for the input you send after.
3. `vb session close <s> && vb --session <s> start --backend nodriver` — a
   hardened launch path. Last rung, and the weakest; see below.

### About `--backend nodriver`

- It exists **only on `vb start`**. `explore` and `research` always run
  patchright and won't tell you so. Named-session route only:

      vb session new hard
      vb --session hard start --backend nodriver --headless
      vb --session hard go <url>

- **Set expectations honestly.** The integration is two-layer: nodriver spawns
  Chrome (its launcher replaces the usual one), then vibatchium attaches over
  CDP with patchright — so the control plane is patchright again. Our own evals
  measure *identical* detector scores for both backends. It changes the launch
  surface, not how the browser is driven. Try `--headed` first; treat nodriver
  as worth a try, not as a thing that will work.
- Needs the `[nodriver]` extra (see Optional lanes) — AGPL-3.0, deliberately
  excluded from `[all]`. `--backend auto` only prints an advisory on a wall; it
  does not switch backends for you.

## Optional lanes (and why one may be missing)

Core install covers all browsing. Some lanes are opt-in extras:
`[fetch]` (curl_cffi — powers **both** `vb fetch` and `vb search`),
`[nodriver]`, `[llm]` (the `vision` lane), `[secrets]`. **Check, don't guess:**

    vb install      # prints every lane as available / missing

If a lane is missing, two things trip people up:

- **The dependency belongs to the daemon's venv, not your shell's.** `vb` talks
  to a long-lived background daemon, and the import happens *there*. A venv with
  curl_cffi still fails if the daemon was started from one without it.
- **The install command depends on how vibatchium was installed** — `pipx
  inject`, `uv pip install --python <exe>`, or plain `pip`, and a uv venv has no
  `pip` at all. Don't guess: the error message from the failing verb names the
  correct command for *this* install. Run that, then `vb shutdown` so the next
  call respawns the daemon with the lane available.

## Notes
- Install with `pipx` / `uv tool` as above — never a bare `pip install`
  into the system Python (Debian/Ubuntu block it under PEP 668) — and don't
  `python -m vibatchium`. Call `vb`.
- `explore` is available BOTH as an MCP tool and on the CLI — prefer the MCP
  tool if you have it. `research` is **CLI only**: it fans out N parallel
  browser sessions and writes markdown artifacts to a directory, which is a
  poor fit for a single tool call on a session-capped daemon, so shell out to
  `vb research` for it.
- The MCP server exposes a curated subset of verbs; the full surface is always
  on the CLI (`vb --help`), and you can widen the MCP tools by re-registering
  the server with `--caps=all`.
- `search` and `fetch` are **not** in the default MCP tool set — network egress
  is opt-in, so an operator grants it deliberately. If you don't see a search
  tool, that is expected: shell out to `vb search` on the CLI, which always
  works. To expose them as MCP tools instead:
  `vb setup --force --caps lean,search,fetch`.
