"""patchium CLI — thin wrapper over the daemon RPC.

All commands talk to a single long-lived daemon process. The daemon auto-spawns
on the first command if it isn't already running.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import __version__
from .client import call, daemon_is_running, DaemonError, DaemonNotRunning


def _emit(result, json_mode: bool, fallback_key: str | None = None):
    """Print a result: --json prints the whole dict; otherwise pluck a friendly value."""
    if json_mode:
        click.echo(json.dumps(result, indent=2))
        return
    if isinstance(result, dict):
        if fallback_key and fallback_key in result:
            val = result[fallback_key]
            if val is not None:
                click.echo(val)
            return
        # default: dump as compact json so users see the shape
        click.echo(json.dumps(result))
        return
    click.echo(result)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "json_mode", is_flag=True, help="Emit responses as JSON.")
@click.option("--session", "session_name", default=None,
              help="Target session name (also via PATCHIUM_SESSION env). "
                   "Defaults to the active session on disk → 'default'.")
@click.version_option(__version__, "--version")
@click.pass_context
def cli(ctx: click.Context, json_mode: bool, session_name: str | None) -> None:
    """Patchium — agentic browser CLI (Patchwright stealth + Vibium ergonomics).

    Multi-session: `patchium --session work click @e3` addresses the 'work'
    session. Without --session, the active session on disk is used (default
    'default'). Pin a session for a sub-shell via `export PATCHIUM_SESSION=work`.
    """
    import os as _os
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_mode
    # Export the chosen session via env so client.call() picks it up without
    # every subcommand needing to thread `session=` explicitly. Done before
    # any subcommand dispatch.
    if session_name:
        _os.environ["PATCHIUM_SESSION"] = session_name
    ctx.obj["session"] = session_name or _os.environ.get("PATCHIUM_SESSION")


# ─── lifecycle ─────────────────────────────────────────────────────────────

@cli.command()
@click.option("--profile", default=None,
              help="Profile name or absolute path (defaults to active session/profile).")
@click.option("--headless", is_flag=True, help="Headless mode (NOT recommended for stealth).")
@click.option("--stealth-mouse", is_flag=True,
              help="Layer humanized mouse via CDP-Patches (needs patchium[stealth-mouse]).")
@click.option("--backend", default="patchright",
              type=click.Choice(["patchright", "nodriver", "auto"]),
              help="Stealth backend. patchright (default) = current Patchright stack. "
                   "nodriver = hardened launch via nodriver lib (needs patchium[nodriver]); "
                   "better on Cloudflare Turnstile interactive challenges per 2026 benchmark. "
                   "auto = start with patchright, advisory on first wall.")
@click.pass_context
def start(ctx, profile, headless, stealth_mouse, backend):
    """Start a browser session (cold launch real Chrome + persistent context)."""
    args = {}
    if profile:
        args["profile"] = profile
    if headless:
        args["headless"] = True
    if stealth_mouse:
        args["stealth_mouse"] = True
    if backend != "patchright":
        args["backend"] = backend
    _emit(call("start", args), ctx.obj["json"])


# ─── install bootstrap ────────────────────────────────────────────────

@cli.command()
@click.option("--skip-chrome", is_flag=True, help="Skip patchright Chrome install.")
@click.pass_context
def install(ctx, skip_chrome):
    """One-time setup: install Chrome via patchright, check Xvfb/display, verify deps."""
    import os
    import shutil
    import subprocess
    from .daemon.paths import CACHE_DIR, PROFILES_DIR, CONFIG_DIR, DEFAULT_PROFILE_DIR

    out = {"checks": []}

    def check(name, ok, note=""):
        out["checks"].append({"name": name, "ok": ok, "note": note})
        click.echo(f"[{'+' if ok else '!'}] {name}: {note}")

    # 1) Python version
    import sys as _sys
    py = _sys.version.split()[0]
    check("python", True, f">=3.11 ok ({py})") if _sys.version_info >= (3, 11) \
        else check("python", False, f"need >=3.11 (have {py})")

    # 2) Real Chrome via patchright
    if skip_chrome:
        check("chrome", True, "skipped per --skip-chrome")
    else:
        try:
            subprocess.run(["patchright", "install", "chrome"],
                           check=False, capture_output=True, text=True, timeout=300)
            check("chrome", True, "patchright install chrome ran (idempotent)")
        except FileNotFoundError:
            check("chrome", False, "patchright binary not found — pip install patchium")

    # 3) DISPLAY (Xvfb hint)
    display = os.environ.get("DISPLAY")
    if display:
        check("display", True, f"DISPLAY={display}")
    else:
        check("display", False,
              "no DISPLAY — headed mode needs one. Try: "
              "`Xvfb :99 -screen 0 1920x1080x24 &` then `export DISPLAY=:99`")

    # 4) Pillow (for screenshot --annotate)
    try:
        import PIL  # noqa: F401
        check("pillow", True, "available — annotated screenshots enabled")
    except ImportError:
        check("pillow", False, "missing — `pip install pillow` for screenshot --annotate")

    # 5) Cache / profile paths writable
    for label, p in [("cache_dir", CACHE_DIR), ("config_dir", CONFIG_DIR),
                     ("profiles_dir", PROFILES_DIR), ("default_profile", DEFAULT_PROFILE_DIR)]:
        ok = os.access(p, os.W_OK)
        check(label, ok, str(p))

    # 6) MCP SDK importable
    try:
        import mcp  # noqa: F401
        check("mcp", True, "available — patchium mcp server runnable")
    except ImportError:
        check("mcp", False, "missing — `pip install mcp` for the MCP server")

    overall_ok = all(c["ok"] for c in out["checks"])
    out["ok"] = overall_ok
    click.echo("")
    click.echo("[+] all checks passed — patchium ready" if overall_ok
               else "[!] some checks failed — see notes above")
    if ctx.obj["json"]:
        click.echo(json.dumps(out, indent=2))


# ─── session (Wave 5: multi-session first-class group) ───────────────

@cli.group()
def session():
    """Manage concurrent browser sessions (1:1 with profiles).

    Sessions are independent Chrome processes — separate cookies, separate
    fingerprint, can run in parallel. Each session is tied to a profile dir
    under ~/.config/patchium/profiles/<name>/ that persists on disk.

    Patterns:
        patchium session new work               # create work profile dir
        patchium --session work start           # launch Chrome for it
        patchium --session work go https://...  # use it
        patchium session list                   # see running + on-disk
        patchium session close work             # stop Chrome; keep profile
        patchium session delete work            # destroy profile dir
    """


@session.command("new")
@click.argument("name")
@click.pass_context
def session_new(ctx, name):
    """Create a new session/profile dir. Does NOT launch Chrome — run
    `patchium --session NAME start` to actually open."""
    _emit(call("session_new", {"name": name}), ctx.obj["json"])


@session.command("list")
@click.pass_context
def session_list_cmd(ctx):
    """List every on-disk session + whether it's running."""
    res = call("session_list")
    if ctx.obj["json"]:
        _emit(res, True)
        return
    click.echo(f"active: {res['active']}")
    for s in res["sessions"]:
        marker = "*" if s["name"] == res["active"] else " "
        running = "[running]" if s["running"] else "[stopped]"
        url = f" url={s.get('url')}" if s.get("url") else ""
        click.echo(f"{marker} {s['name']:24s} {running}{url}")


@session.command("use")
@click.argument("name")
@click.pass_context
def session_use(ctx, name):
    """Set the active session for subsequent CLI calls (no --session needed)."""
    _emit(call("session_use", {"name": name}), ctx.obj["json"])


@session.command("switch")
@click.argument("name")
@click.pass_context
def session_switch(ctx, name):
    """Alias for `session use`."""
    _emit(call("session_switch", {"name": name}), ctx.obj["json"])


@session.command("close")
@click.argument("name", required=False)
@click.option("--all", "close_all", is_flag=True, help="Close every running session.")
@click.pass_context
def session_close(ctx, name, close_all):
    """Stop Chrome for one session (or --all). Profile dir is preserved."""
    if close_all:
        _emit(call("session_close_all"), ctx.obj["json"])
        return
    if not name:
        # default to the active session
        name = ctx.obj.get("session")
    _emit(call("session_close", {"name": name} if name else {}), ctx.obj["json"])


@session.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="really delete this profile dir?")
@click.pass_context
def session_delete(ctx, name):
    """Delete the on-disk profile dir (cannot delete active or 'default')."""
    _emit(call("session_delete", {"name": name}), ctx.obj["json"])


# ─── profile (legacy aliases — kept for backwards compat) ────────────

@cli.group()
def profile():
    """Manage named browser profiles. Aliased to `session` (1:1 model)."""


@profile.command("list")
@click.pass_context
def profile_list(ctx):
    """List all profiles + the active one."""
    res = call("profile_list")
    if ctx.obj["json"]:
        _emit(res, True)
        return
    click.echo(f"active: {res['active']}")
    for p in res["profiles"]:
        marker = "* " if p == res["active"] else "  "
        click.echo(f"{marker}{p}")


@profile.command("new")
@click.argument("name")
@click.pass_context
def profile_new(ctx, name):
    """Create a new named profile."""
    _emit(call("profile_new", {"name": name}), ctx.obj["json"])


@profile.command("use")
@click.argument("name")
@click.pass_context
def profile_use(ctx, name):
    """Set the active profile (applies on next `start`)."""
    _emit(call("profile_use", {"name": name}), ctx.obj["json"])


@profile.command("delete")
@click.argument("name")
@click.confirmation_option(prompt="really delete this profile?")
@click.pass_context
def profile_delete(ctx, name):
    """Delete a profile directory (cannot delete active or 'default')."""
    _emit(call("profile_delete", {"name": name}), ctx.obj["json"])


@cli.command()
@click.argument("cdp_url", default="http://localhost:9222")
@click.pass_context
def attach(ctx, cdp_url):
    """Attach to a running Chrome over CDP. Pre-req: launch Chrome with
    --remote-debugging-port=9222, log into the target site by hand, then run this.
    """
    _emit(call("attach", {"cdp_url": cdp_url}), ctx.obj["json"], "cdp_url")


@cli.command()
@click.pass_context
def stop(ctx):
    """Stop the browser session. Daemon stays up."""
    _emit(call("stop"), ctx.obj["json"], "stopped")


@cli.command()
@click.pass_context
def shutdown(ctx):
    """Stop the browser AND tell the daemon to exit."""
    try:
        result = call("shutdown")
    except DaemonNotRunning:
        click.echo("daemon not running")
        return
    _emit(result, ctx.obj["json"], "shutting_down")


@cli.command()
@click.pass_context
def status(ctx):
    """Daemon + session status."""
    if not daemon_is_running():
        result = {"daemon": False, "session": False}
    else:
        result = call("status")
        result["daemon"] = True
    _emit(result, ctx.obj["json"])


# ─── navigation ───────────────────────────────────────────────────────────

@cli.command()
@click.argument("url")
@click.option("--wait-until", default="domcontentloaded",
              type=click.Choice(["load", "domcontentloaded", "networkidle", "commit"]))
@click.option("--timeout", "timeout_ms", default=60_000, type=int, help="Timeout in ms.")
@click.pass_context
def go(ctx, url, wait_until, timeout_ms):
    """Navigate to URL."""
    _emit(call("go", {"url": url, "wait_until": wait_until, "timeout_ms": timeout_ms}),
          ctx.obj["json"], "url")


@cli.command()
@click.pass_context
def back(ctx):
    """Browser back."""
    _emit(call("back"), ctx.obj["json"], "url")


@cli.command()
@click.pass_context
def forward(ctx):
    """Browser forward."""
    _emit(call("forward"), ctx.obj["json"], "url")


@cli.command()
@click.pass_context
def reload(ctx):
    """Reload current page."""
    _emit(call("reload"), ctx.obj["json"], "url")


@cli.command()
@click.pass_context
def url(ctx):
    """Print current URL."""
    _emit(call("url"), ctx.obj["json"], "url")


@cli.command()
@click.pass_context
def title(ctx):
    """Print current page title."""
    _emit(call("title"), ctx.obj["json"], "title")


# ─── content ───────────────────────────────────────────────────────────────

@cli.command()
@click.argument("selector", required=False)
@click.pass_context
def text(ctx, selector):
    """Get inner text (whole page or a selector)."""
    args = {"selector": selector} if selector else {}
    _emit(call("text", args), ctx.obj["json"], "text")


@cli.command()
@click.argument("selector", required=False)
@click.pass_context
def html(ctx, selector):
    """Get HTML (whole page or a selector)."""
    args = {"selector": selector} if selector else {}
    _emit(call("html", args), ctx.obj["json"], "html")


@cli.command(name="eval")
@click.argument("expr", required=False)
@click.option("--stdin", is_flag=True, help="Read expression from stdin.")
@click.pass_context
def eval_cmd(ctx, expr, stdin):
    """Evaluate JS in the page (isolated context, per Patchright default)."""
    if stdin:
        expr = sys.stdin.read()
    if not expr:
        click.echo("expr or --stdin required", err=True)
        sys.exit(2)
    _emit(call("eval", {"expr": expr}), ctx.obj["json"], "value")


@cli.group()
def handle():
    """JSHandle table — hold DOM references across calls (eval_handle / dispose / list)."""


@handle.command("create")
@click.argument("expr")
@click.pass_context
def handle_create(ctx, expr):
    """Eval JS and store the result as a handle (`h_N`)."""
    _emit(call("eval_handle", {"expr": expr}), ctx.obj["json"])


@handle.command("eval")
@click.argument("handle_id")
@click.argument("expr")
@click.pass_context
def handle_eval(ctx, handle_id, expr):
    """Run JS with a stored handle as `arg`."""
    _emit(call("handle_eval", {"handle": handle_id, "expr": expr}),
          ctx.obj["json"], "value")


@handle.command("list")
@click.pass_context
def handle_list(ctx):
    """List active handles."""
    _emit(call("handle_list"), ctx.obj["json"])


@handle.command("dispose")
@click.argument("handle_id")
@click.pass_context
def handle_dispose(ctx, handle_id):
    """Release a single handle."""
    _emit(call("handle_dispose", {"handle": handle_id}), ctx.obj["json"])


@handle.command("clear")
@click.pass_context
def handle_clear(ctx):
    """Dispose ALL active handles."""
    _emit(call("handle_dispose_all"), ctx.obj["json"])


@cli.command()
@click.argument("selector")
@click.argument("name")
@click.pass_context
def attr(ctx, selector, name):
    """Get an HTML attribute value."""
    _emit(call("attr", {"selector": selector, "name": name}), ctx.obj["json"], "value")


@cli.command()
@click.argument("selector")
@click.pass_context
def value(ctx, selector):
    """Get the current value of a form element."""
    _emit(call("value", {"selector": selector}), ctx.obj["json"], "value")


# ─── input ────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("keys", required=True)
@click.pass_context
def keys(ctx, keys):
    """Press a key combination (e.g. 'Enter', 'Control+a', 'Shift+Tab')."""
    _emit(call("keys", {"keys": keys}), ctx.obj["json"], "pressed")


# ─── screenshot ───────────────────────────────────────────────────────────

@cli.command()
@click.option("-o", "--output", default="screenshot.png", help="Output file path.")
@click.option("--full-page", is_flag=True, help="Full-page screenshot (not just viewport).")
@click.option("--annotate", is_flag=True,
              help="Overlay @eN bounding boxes (needs Pillow).")
@click.pass_context
def screenshot(ctx, output, full_page, annotate):
    """Capture a screenshot, optionally annotated with @eN box overlays."""
    path = str(Path(output).resolve())
    cmd = "screenshot_annotate" if annotate else "screenshot"
    _emit(call(cmd, {"path": path, "full_page": full_page}),
          ctx.obj["json"], "path")


# ─── element model: map + interactive verbs ──────────────────────────────

@cli.command(name="map")
@click.option("--indent/--no-indent", default=True, help="Preserve YAML indent (on by default).")
@click.option("--compact", is_flag=True,
              help="One-liner per actionable element (token-efficient).")
@click.option("--depth", default=None, type=int, help="Limit snapshot depth.")
@click.pass_context
def map_cmd(ctx, indent, compact, depth):
    """Snapshot the page's actionable elements and assign @eN refs.

    Default output is Playwright's aria_snapshot YAML with `@eN` ref notation.
    `--compact` switches to browser-use-style one-liner output (~3x cheaper in tokens).
    """
    args = {"indent": indent}
    if depth is not None:
        args["depth"] = depth
    cmd = "map_compact" if compact else "map"
    result = call(cmd, args)
    if ctx.obj["json"]:
        _emit(result, True)
        return
    click.echo(result.get("text", ""))


@cli.command(name="diff")
@click.argument("subcmd", type=click.Choice(["map"]))
@click.pass_context
def diff_cmd(ctx, subcmd):
    """Diff current state vs previous snapshot. Currently supports `diff map`."""
    if subcmd != "map":
        click.echo("only `diff map` supported", err=True); sys.exit(2)
    result = call("diff_map")
    if ctx.obj["json"]:
        _emit(result, True)
        return
    click.echo(result.get("text", ""))


@cli.command(name="click")
@click.argument("target")
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.option("--auto-dismiss-banners", is_flag=True,
              help="On 'intercepted' failure, try to dismiss banners and retry once.")
@click.pass_context
def click_cmd(ctx, target, timeout_ms, auto_dismiss_banners):
    """Click an @eN ref or selector."""
    _emit(call("click", {"target": target, "timeout_ms": timeout_ms,
                          "auto_dismiss_banners": auto_dismiss_banners}),
          ctx.obj["json"], "clicked")


@cli.command()
@click.argument("target")
@click.argument("text_arg", metavar="TEXT", required=False)
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.option("--use-secret", "use_secret", default=None,
              help="Resolve value from vault: 'site:key' (or 'site:totp' for TOTP).")
@click.pass_context
def fill(ctx, target, text_arg, timeout_ms, use_secret):
    """Clear an input and fill it with text (React-input-safe via Locator.fill).

    With --use-secret site:key, value comes from the encrypted vault — never
    appears in command line, response, or logs.
    """
    args = {"target": target, "timeout_ms": timeout_ms}
    if use_secret:
        args["use_secret"] = use_secret
    else:
        if not text_arg:
            click.echo("error: TEXT or --use-secret required", err=True)
            sys.exit(2)
        args["text"] = text_arg
    _emit(call("fill", args), ctx.obj["json"], "filled")


@cli.command(name="type")
@click.argument("target")
@click.argument("text_arg", metavar="TEXT")
@click.option("--delay", "delay_ms", default=0, type=int, help="Per-keystroke delay (ms).")
@click.pass_context
def type_cmd(ctx, target, text_arg, delay_ms):
    """Type text (key-by-key) into an element."""
    _emit(call("type", {"target": target, "text": text_arg, "delay_ms": delay_ms}),
          ctx.obj["json"], "typed")


@cli.command()
@click.argument("target")
@click.pass_context
def hover(ctx, target):
    """Hover over an element."""
    _emit(call("hover", {"target": target}), ctx.obj["json"], "hovered")


@cli.command()
@click.argument("target")
@click.pass_context
def focus(ctx, target):
    """Focus an element."""
    _emit(call("focus", {"target": target}), ctx.obj["json"], "focused")


@cli.command()
@click.argument("target")
@click.argument("press_keys")
@click.pass_context
def press(ctx, target, press_keys):
    """Press a key on a specific element (e.g. `press @e3 Enter`)."""
    _emit(call("press", {"target": target, "keys": press_keys}),
          ctx.obj["json"], "pressed")


@cli.command()
@click.argument("target")
@click.pass_context
def check(ctx, target):
    """Check a checkbox / radio."""
    _emit(call("check", {"target": target}), ctx.obj["json"], "checked")


@cli.command()
@click.argument("target")
@click.pass_context
def uncheck(ctx, target):
    """Uncheck a checkbox."""
    _emit(call("uncheck", {"target": target}), ctx.obj["json"], "unchecked")


@cli.command()
@click.argument("target")
@click.option("--value", default=None)
@click.option("--label", default=None)
@click.option("--index", default=None, type=int)
@click.pass_context
def select(ctx, target, value, label, index):
    """Select an option in a <select> element."""
    _emit(call("select", {"target": target, "value": value, "label": label, "index": index}),
          ctx.obj["json"], "selected")


@cli.command()
@click.option("--target", default=None, help="Scroll element into view by @eN/selector.")
@click.option("--dx", default=0, type=int)
@click.option("--dy", default=0, type=int)
@click.pass_context
def scroll(ctx, target, dx, dy):
    """Scroll the page or a target element into view."""
    args = {"target": target, "dx": dx, "dy": dy}
    _emit(call("scroll", args), ctx.obj["json"])


@cli.command(name="is")
@click.argument("target")
@click.argument("state", type=click.Choice(["visible", "hidden", "enabled", "disabled",
                                            "checked", "editable"]))
@click.pass_context
def is_cmd(ctx, target, state):
    """Check element state (visible / enabled / checked / ...)."""
    _emit(call("is", {"target": target, "state": state}), ctx.obj["json"], "value")


# ─── viewport ─────────────────────────────────────────────────────────────

@cli.command()
@click.argument("width", required=False, type=int)
@click.argument("height", required=False, type=int)
@click.pass_context
def viewport(ctx, width, height):
    """Get or set viewport size."""
    args = {}
    if width and height:
        args = {"width": width, "height": height}
    _emit(call("viewport", args), ctx.obj["json"])


# ─── storage (cookies + localStorage + sessionStorage) ───────────────────

@cli.group()
def storage():
    """Export or restore browser state (cookies + per-origin localStorage)."""


@storage.command("export")
@click.option("-o", "--output", default=None, help="Output file path (else print JSON).")
@click.pass_context
def storage_export(ctx, output):
    """Export storage state to a JSON file (or print to stdout)."""
    path = str(Path(output).resolve()) if output else None
    args = {"path": path} if path else {}
    result = call("storage_export", args)
    if output:
        click.echo(result.get("path"))
    else:
        click.echo(json.dumps(result.get("state"), indent=2))


@storage.command("restore")
@click.argument("file", type=click.Path(exists=True))
@click.pass_context
def storage_restore(ctx, file):
    """Restore storage from a JSON file written by `storage export`."""
    _emit(call("storage_restore", {"path": str(Path(file).resolve())}), ctx.obj["json"])


@cli.command()
@click.pass_context
def cookies(ctx):
    """List current cookies."""
    _emit(call("cookies"), ctx.obj["json"])


# ─── wait family ─────────────────────────────────────────────────────────

@cli.group()
def wait():
    """Wait for a state change (element / url / load / fn / sleep)."""


@wait.command("selector")
@click.argument("selector")
@click.option("--state", default="visible",
              type=click.Choice(["visible", "hidden", "attached", "detached"]))
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.pass_context
def wait_selector(ctx, selector, state, timeout_ms):
    """Wait for a selector / @eN ref to reach a state."""
    if selector.startswith("@e") or (len(selector) > 1 and selector[0] == "e" and selector[1:].isdigit()):
        _emit(call("wait_ref", {"ref": selector, "state": state, "timeout_ms": timeout_ms}),
              ctx.obj["json"])
    else:
        _emit(call("wait_selector", {"selector": selector, "state": state, "timeout_ms": timeout_ms}),
              ctx.obj["json"])


@wait.command("url")
@click.argument("pattern")
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.pass_context
def wait_url(ctx, pattern, timeout_ms):
    """Wait until the URL matches (glob or regex)."""
    _emit(call("wait_url", {"pattern": pattern, "timeout_ms": timeout_ms}),
          ctx.obj["json"], "url")


@wait.command("load")
@click.option("--state", default="load",
              type=click.Choice(["load", "domcontentloaded", "networkidle"]))
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.pass_context
def wait_load(ctx, state, timeout_ms):
    """Wait for a page load state."""
    _emit(call("wait_load", {"state": state, "timeout_ms": timeout_ms}), ctx.obj["json"])


@wait.command("fn")
@click.argument("expr")
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.pass_context
def wait_fn(ctx, expr, timeout_ms):
    """Wait until a JS expression returns truthy."""
    _emit(call("wait_fn", {"expr": expr, "timeout_ms": timeout_ms}), ctx.obj["json"])


@cli.command()
@click.argument("ms", type=int)
@click.pass_context
def sleep(ctx, ms):
    """Pause N milliseconds."""
    _emit(call("sleep", {"ms": ms}), ctx.obj["json"])


# ─── semantic locators ────────────────────────────────────────────────────

@cli.command()
@click.argument("kind", type=click.Choice(["text", "label", "placeholder", "role",
                                           "testid", "xpath", "alt", "title", "css"]))
@click.argument("query")
@click.option("--name", default=None, help="Accessible name (role kind only).")
@click.option("--exact", is_flag=True)
@click.pass_context
def find(ctx, kind, query, name, exact):
    """Find elements by semantic locator (text/label/placeholder/role/testid/xpath/alt/title/css)."""
    args = {"kind": kind, "query": query, "exact": exact}
    if name:
        args["name"] = name
    _emit(call("find", args), ctx.obj["json"])


@cli.command()
@click.argument("target")
@click.pass_context
def count(ctx, target):
    """Count matching elements for a selector or @eN."""
    _emit(call("count", {"target": target}), ctx.obj["json"], "count")


@cli.command()
@click.argument("html_arg", metavar="HTML")
@click.option("--stdin", is_flag=True, help="Read HTML from stdin.")
@click.pass_context
def content(ctx, html_arg, stdin):
    """Replace the page HTML wholesale."""
    if stdin:
        html_arg = sys.stdin.read()
    _emit(call("content", {"html": html_arg}), ctx.obj["json"])


# ─── frames ───────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def frames(ctx):
    """List all frames (main + iframes) with name + URL."""
    _emit(call("frames"), ctx.obj["json"])


@cli.command()
@click.option("--name", default=None)
@click.option("--url", default=None)
@click.option("--clear", is_flag=True)
@click.pass_context
def frame(ctx, name, url, clear):
    """Switch active frame by name or URL substring (use --clear to go back to main)."""
    args = {}
    if not clear:
        if name: args["name"] = name
        if url:  args["url"] = url
    _emit(call("frame", args), ctx.obj["json"])


# ─── mouse ────────────────────────────────────────────────────────────────

@cli.group()
def mouse():
    """Mouse control (click / move / down / up / dblclick / wheel)."""


@mouse.command("click")
@click.argument("x", type=float)
@click.argument("y", type=float)
@click.option("--button", default="left", type=click.Choice(["left", "right", "middle"]))
@click.pass_context
def mouse_click(ctx, x, y, button):
    _emit(call("mouse", {"action": "click", "x": x, "y": y, "button": button}), ctx.obj["json"])


@mouse.command("dblclick")
@click.argument("x", type=float)
@click.argument("y", type=float)
@click.pass_context
def mouse_dblclick(ctx, x, y):
    _emit(call("mouse", {"action": "dblclick", "x": x, "y": y}), ctx.obj["json"])


@mouse.command("move")
@click.argument("x", type=float)
@click.argument("y", type=float)
@click.option("--steps", default=1, type=int)
@click.pass_context
def mouse_move(ctx, x, y, steps):
    _emit(call("mouse", {"action": "move", "x": x, "y": y, "steps": steps}), ctx.obj["json"])


@mouse.command("down")
@click.option("--button", default="left")
@click.pass_context
def mouse_down(ctx, button):
    _emit(call("mouse", {"action": "down", "button": button}), ctx.obj["json"])


@mouse.command("up")
@click.option("--button", default="left")
@click.pass_context
def mouse_up(ctx, button):
    _emit(call("mouse", {"action": "up", "button": button}), ctx.obj["json"])


@mouse.command("wheel")
@click.argument("dx", type=float)
@click.argument("dy", type=float)
@click.pass_context
def mouse_wheel(ctx, dx, dy):
    _emit(call("mouse", {"action": "wheel", "dx": dx, "dy": dy}), ctx.obj["json"])


# ─── upload / dialog / download ──────────────────────────────────────────

@cli.command()
@click.argument("target")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True))
@click.pass_context
def upload(ctx, target, files):
    """Set files on an input[type=file] element."""
    _emit(call("upload", {"target": target, "files": [str(Path(f).resolve()) for f in files]}),
          ctx.obj["json"])


@cli.command()
@click.argument("action", type=click.Choice(["accept", "dismiss"]))
@click.option("--text", default=None, help="Prompt input text (with accept).")
@click.pass_context
def dialog(ctx, action, text):
    """Set policy for the next alert/confirm/prompt dialog."""
    args = {"action": action}
    if text is not None:
        args["text"] = text
    _emit(call("dialog_policy", args), ctx.obj["json"])


@cli.group()
def download():
    """Download management (arm / list / save)."""


@download.command("arm")
@click.pass_context
def download_arm(ctx):
    """Start collecting download events. Subsequent downloads appear in `download list`."""
    _emit(call("download_arm"), ctx.obj["json"])


@download.command("list")
@click.pass_context
def download_list(ctx):
    """List captured downloads."""
    _emit(call("download_list"), ctx.obj["json"])


@download.command("save")
@click.argument("index", type=int)
@click.argument("path", type=click.Path())
@click.pass_context
def download_save(ctx, index, path):
    """Save download #N to a path."""
    _emit(call("download_save", {"index": index, "path": str(Path(path).resolve())}),
          ctx.obj["json"])


# ─── pdf / record / highlight ────────────────────────────────────────────

@cli.command()
@click.option("-o", "--output", default="page.pdf")
@click.option("--format", "page_format", default="Letter")
@click.pass_context
def pdf(ctx, output, page_format):
    """Save the current page as PDF."""
    _emit(call("pdf", {"path": str(Path(output).resolve()), "format": page_format}),
          ctx.obj["json"], "path")


@cli.group()
def record():
    """Record / stop a Playwright trace ZIP (Trace Viewer compatible)."""


@record.command("start")
@click.option("--screenshots/--no-screenshots", default=True)
@click.option("--snapshots/--no-snapshots", default=True)
@click.option("--sources", is_flag=True)
@click.pass_context
def record_start(ctx, screenshots, snapshots, sources):
    _emit(call("record_start", {"screenshots": screenshots, "snapshots": snapshots, "sources": sources}),
          ctx.obj["json"])


@record.command("stop")
@click.option("-o", "--output", default="trace.zip")
@click.pass_context
def record_stop(ctx, output):
    _emit(call("record_stop", {"path": str(Path(output).resolve())}), ctx.obj["json"], "path")


@cli.command()
@click.argument("target")
@click.option("--ms", default=3000, type=int)
@click.pass_context
def highlight(ctx, target, ms):
    """Briefly outline an element (visual debugging)."""
    _emit(call("highlight", {"target": target, "ms": ms}), ctx.obj["json"])


# ─── geolocation / media ─────────────────────────────────────────────────

@cli.command()
@click.argument("lat", type=float, required=False)
@click.argument("lng", type=float, required=False)
@click.option("--accuracy", default=10, type=float)
@click.option("--clear", is_flag=True)
@click.pass_context
def geolocation(ctx, lat, lng, accuracy, clear):
    """Override geolocation (or --clear to remove)."""
    if clear:
        _emit(call("geolocation", {"clear": True}), ctx.obj["json"])
        return
    if lat is None or lng is None:
        click.echo("lat lng required (or --clear)", err=True); sys.exit(2)
    _emit(call("geolocation", {"lat": lat, "lng": lng, "accuracy": accuracy}), ctx.obj["json"])


@cli.command()
@click.option("--media", type=click.Choice(["screen", "print", "no-override"]))
@click.option("--color-scheme", type=click.Choice(["light", "dark", "no-preference", "no-override"]))
@click.option("--reduced-motion", type=click.Choice(["reduce", "no-preference", "no-override"]))
@click.option("--forced-colors", type=click.Choice(["active", "none", "no-override"]))
@click.pass_context
def media(ctx, media, color_scheme, reduced_motion, forced_colors):
    """Override CSS media features (color-scheme, reduced-motion, print, etc.)."""
    args = {}
    if media: args["media"] = media
    if color_scheme: args["color_scheme"] = color_scheme
    if reduced_motion: args["reduced_motion"] = reduced_motion
    if forced_colors: args["forced_colors"] = forced_colors
    _emit(call("media", args), ctx.obj["json"])


# ─── network capture ─────────────────────────────────────────────────────

@cli.group()
def network():
    """Network request/response capture (start / stop / dump)."""


@network.command("start")
@click.option("--max", "max_events", default=500, type=int)
@click.pass_context
def network_start(ctx, max_events):
    _emit(call("network_start", {"max": max_events}), ctx.obj["json"])


@network.command("stop")
@click.pass_context
def network_stop(ctx):
    _emit(call("network_stop"), ctx.obj["json"])


@network.command("dump")
@click.option("-o", "--output", default=None)
@click.pass_context
def network_dump(ctx, output):
    args = {}
    if output:
        args["path"] = str(Path(output).resolve())
    _emit(call("network_dump", args), ctx.obj["json"])


# ─── HAR (full HTTP Archive) ─────────────────────────────────────────────

@cli.group()
def har():
    """HTTP Archive (HAR 1.2) capture — request + response bodies + timings."""


@har.command("start")
@click.argument("output", type=click.Path())
@click.option("--content", default="embed", type=click.Choice(["embed", "omit"]),
              help="embed: include response bodies (default). omit: skip bodies.")
@click.option("--url-filter", default=None,
              help="Only capture entries whose URL contains this substring.")
@click.pass_context
def har_start(ctx, output, content, url_filter):
    """Start HAR recording. Writes on `har stop`."""
    args = {"path": str(Path(output).resolve()), "content": content}
    if url_filter:
        args["url_filter"] = url_filter
    _emit(call("har_start", args), ctx.obj["json"])


@har.command("stop")
@click.pass_context
def har_stop(ctx):
    """Stop HAR recording and flush to disk."""
    _emit(call("har_stop"), ctx.obj["json"])


# ─── request interception (route) ────────────────────────────────────────

@cli.group()
def route():
    """Request interception (abort/fulfill/observe URL patterns)."""


@route.command("add")
@click.argument("pattern")
@click.option("--mode", default="passthrough",
              type=click.Choice(["abort", "fulfill", "passthrough"]))
@click.option("--body", default="", help="Response body when --mode=fulfill.")
@click.option("--status", default=200, type=int)
@click.option("--content-type", default="text/plain")
@click.pass_context
def route_add(ctx, pattern, mode, body, status, content_type):
    """Add a route rule. PATTERN is a Playwright URL glob like `**/*.png`.

    Examples:
      patchium route add "**/*.{png,jpg,css}" --mode abort
      patchium route add "**/api/users" --mode fulfill --body '{"ok":true}' --content-type application/json
    """
    _emit(call("route_add", {"pattern": pattern, "mode": mode, "body": body,
                              "status": status, "content_type": content_type}),
          ctx.obj["json"])


@route.command("list")
@click.pass_context
def route_list(ctx):
    """List active route rules + hit counts."""
    _emit(call("route_list"), ctx.obj["json"])


@route.command("clear")
@click.argument("pattern", required=False)
@click.pass_context
def route_clear(ctx, pattern):
    """Clear one rule by pattern, or all rules if no pattern given."""
    args = {"pattern": pattern} if pattern else {}
    _emit(call("route_clear", args), ctx.obj["json"])


@cli.command("wait-response")
@click.argument("pattern")
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.option("--body", is_flag=True, help="Capture and return the response body.")
@click.option("--max-body", default=1_000_000, type=int)
@click.pass_context
def wait_response(ctx, pattern, timeout_ms, body, max_body):
    """Wait for a network response matching URL pattern (and optionally return the body)."""
    _emit(call("wait_response", {"pattern": pattern, "timeout_ms": timeout_ms,
                                  "body": body, "max_body": max_body}),
          ctx.obj["json"])


# ─── cookie / consent banner auto-dismiss ─────────────────────────────────

@cli.command("dismiss-banners")
@click.option("--prefer", default="reject", type=click.Choice(["reject", "accept"]),
              help="Prefer reject-class buttons (privacy default) or accept-class.")
@click.option("--dry-run", is_flag=True, help="Report candidates without clicking.")
@click.option("--max", "max_clicks", default=1, type=int)
@click.pass_context
def dismiss_banners(ctx, prefer, dry_run, max_clicks):
    """Heuristically dismiss cookie/consent/newsletter banners on the current page.

    Scans the AX snapshot for buttons matching common consent labels (Accept,
    Reject, Agree, Got it, etc.) and clicks the most direct one. Uses the
    privacy-friendly default (--prefer=reject).
    """
    _emit(call("dismiss_banners", {"prefer": prefer, "dry_run": dry_run,
                                    "max_clicks": max_clicks}),
          ctx.obj["json"])


# ─── observe / act (intent → plan) ────────────────────────────────────────

@cli.command("observe-clear-cache")
@click.pass_context
def observe_clear_cache(ctx):
    """Delete the on-disk observe→act plan cache."""
    from .daemon.paths import CACHE_DIR
    cache = CACHE_DIR / "observe-cache.json"
    if cache.exists():
        cache.unlink()
        click.echo(f"cleared {cache}")
    else:
        click.echo("no cache file")


@cli.command()
@click.argument("intent")
@click.option("--llm", is_flag=True, help="Use Claude (needs ANTHROPIC_API_KEY).")
@click.option("--force", is_flag=True, help="Bypass the on-disk plan cache.")
@click.pass_context
def observe(ctx, intent, llm, force):
    """Compute a plan for an intent without executing.

    Returns the proposed verb + @eN target + rationale. Cached on disk so
    re-runs of the same (url, intent) skip inference. With --llm and
    ANTHROPIC_API_KEY set, uses Claude; otherwise falls back to a heuristic
    keyword-overlap match.
    """
    _emit(call("observe", {"intent": intent, "llm": llm, "force": force}), ctx.obj["json"])


@cli.command()
@click.argument("intent")
@click.option("--llm", is_flag=True)
@click.pass_context
def act(ctx, intent, llm):
    """Observe + execute the resulting plan in one shot."""
    _emit(call("act", {"intent": intent, "llm": llm}), ctx.obj["json"])


@cli.command()
@click.option("-n", "--lines", default=50, type=int)
@click.option("--follow", is_flag=True, help="tail -f the log.")
@click.pass_context
def logs(ctx, lines, follow):
    """Tail the daemon log."""
    from .daemon.paths import LOG_PATH
    import subprocess as _sub
    if not LOG_PATH.exists():
        click.echo(f"no log yet at {LOG_PATH}", err=True)
        sys.exit(1)
    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(str(LOG_PATH))
    _sub.call(cmd)


# ─── Wave 6.3d: vision-first primitives ──────────────────────────────────

@cli.command("vision-click")
@click.argument("intent")
@click.option("--min-confidence", default=0.6, type=float)
@click.option("--button", default="left", type=click.Choice(["left", "right", "middle"]))
@click.option("--max-per-minute", default=30, type=int)
@click.pass_context
def vision_click(ctx, intent, min_confidence, button, max_per_minute):
    """Find a UI element by description (via Claude vision) and click it.

    Use when the AX-tree is useless (canvas UIs, Flutter, Unity WebGL).
    Requires ANTHROPIC_API_KEY + `pip install patchium[llm]`.

        patchium vision-click "the blue submit button"
        patchium vision-click "the OK button in the modal" --min-confidence 0.8
    """
    _emit(call("vision_click", {
        "intent": intent, "min_confidence": min_confidence,
        "button": button, "max_per_minute": max_per_minute,
    }), ctx.obj["json"])


@cli.command("vision-find")
@click.argument("intent")
@click.option("--min-confidence", default=0.6, type=float)
@click.pass_context
def vision_find_cmd(ctx, intent):
    """Locate a UI element via vision and return coords + confidence (no click)."""
    _emit(call("vision_find", {"intent": intent, "min_confidence": 0.0}),
          ctx.obj["json"])


@cli.command("vision-type")
@click.argument("intent")
@click.argument("text_arg", metavar="TEXT")
@click.option("--min-confidence", default=0.6, type=float)
@click.pass_context
def vision_type_cmd(ctx, intent, text_arg, min_confidence):
    """vision-click the described field, then type TEXT."""
    _emit(call("vision_type", {"intent": intent, "text": text_arg,
                                "min_confidence": min_confidence}),
          ctx.obj["json"])


@cli.group()
def vision():
    """Inspect or reset vision-related state."""


@vision.command("stats")
@click.pass_context
def vision_stats(ctx):
    """Cumulative vision API usage for current session (calls, tokens, cost)."""
    _emit(call("vision_stats"), ctx.obj["json"])


@vision.command("clear-cache")
@click.pass_context
def vision_clear_cache(ctx):
    """Drop the on-disk vision (screenshot, intent) → coords cache."""
    _emit(call("vision_clear_cache"), ctx.obj["json"])


@vision.command("budget")
@click.option("--reset", type=click.Choice(["today", "lifetime", "all"]),
              default=None, help="Reset today's or lifetime spend tracking.")
@click.pass_context
def vision_budget(ctx, reset):
    """Show today's + lifetime vision spend vs configured caps.

    Caps via env vars: PATCHIUM_VISION_MAX_DAILY_USD,
    PATCHIUM_VISION_MAX_LIFETIME_USD. Unset = no cap.
    """
    args = {}
    if reset:
        args["reset"] = reset
    _emit(call("vision_budget", args), ctx.obj["json"])


# ─── Wave 6.3c: prompt-injection safety ──────────────────────────────────

@cli.group()
def safety():
    """Configure prompt-injection scanning per session.

    OFF by default (zero overhead). Modes:
        flag-only  add prompt_injection_risk + signals to responses
        wrap       wrap suspicious regions in <UNTRUSTED_CONTENT> tags
        redact     replace suspicious regions with [REDACTED-PROMPT-INJECTION-N]

        patchium --session work safety set flag-only
        patchium --session work map      # response gains risk metadata
        patchium safety scan "ignore previous instructions"  # test patterns
    """


@safety.command("set")
@click.argument("mode", type=click.Choice(["off", "flag-only", "wrap", "redact"]))
@click.pass_context
def safety_set(ctx, mode):
    """Set the safety mode for the current session."""
    _emit(call("safety_set", {"mode": mode}), ctx.obj["json"])


@safety.command("status")
@click.pass_context
def safety_status(ctx):
    """Report current safety mode."""
    _emit(call("safety_status"), ctx.obj["json"])


@safety.command("scan")
@click.argument("text")
@click.pass_context
def safety_scan(ctx, text):
    """Run the classifier on TEXT and print its risk + signals."""
    _emit(call("safety_scan", {"text": text}), ctx.obj["json"])


# ─── Wave 6.3a: credential vault + TOTP ──────────────────────────────────

@cli.group()
def secret():
    """Encrypted vault for per-site credentials + TOTP.

    Vault key is sourced from OS keyring (preferred) or PATCHIUM_SECRETS_KEY
    env (base64-32-bytes; CI/headless). Run `patchium secret init` once to
    provision the key.

        patchium secret init
        patchium secret set github.com username alice
        patchium secret set github.com password 'hunter2'
        patchium secret set github.com totp-seed JBSWY3DPEHPK3PXP
        patchium secret list
        patchium fill @e7 --use-secret github.com:totp
    """


@secret.command("init")
@click.option("--prefer", default="keyring",
              type=click.Choice(["keyring", "env"]),
              help="Where to store the generated key.")
@click.option("--print-key", is_flag=True,
              help="Echo the key (for env-var setups).")
@click.pass_context
def secret_init(ctx, prefer, print_key):
    """Generate and provision a vault key."""
    args = {"prefer": prefer}
    if print_key:
        args["print_key"] = True
    _emit(call("secret_init", args), ctx.obj["json"])


@secret.command("set")
@click.argument("site")
@click.argument("key")
@click.argument("value", required=False)
@click.option("--stdin", is_flag=True, help="Read value from stdin (recommended for passwords).")
@click.pass_context
def secret_set(ctx, site, key, value, stdin):
    """Store a secret. Use --stdin to avoid shell history."""
    if stdin:
        value = sys.stdin.read().strip()
    if not value:
        click.echo("error: value required (use VALUE arg or --stdin)", err=True)
        sys.exit(2)
    _emit(call("secret_set", {"site": site, "key": key, "value": value}),
          ctx.obj["json"])


@secret.command("list")
@click.argument("site", required=False)
@click.pass_context
def secret_list(ctx, site):
    """List secrets in masked form."""
    args = {"site": site} if site else {}
    _emit(call("secret_list", args), ctx.obj["json"])


@secret.command("delete")
@click.argument("site")
@click.argument("key", required=False)
@click.confirmation_option(prompt="really delete this secret?")
@click.pass_context
def secret_delete(ctx, site, key):
    """Delete a single key, or all keys for the site if KEY omitted."""
    args = {"site": site}
    if key:
        args["key"] = key
    _emit(call("secret_delete", args), ctx.obj["json"])


@secret.command("totp")
@click.argument("site")
@click.pass_context
def secret_totp(ctx, site):
    """Print the current TOTP code for SITE's stored totp-seed."""
    _emit(call("secret_totp", {"site": site}), ctx.obj["json"], "code")


@cli.command("wait-email-code")
@click.argument("site")
@click.option("--timeout", default=60, type=int)
@click.option("--max-age", default=300, type=int,
              help="Skip emails older than this many seconds.")
@click.option("--mark-read", is_flag=True, help="Mark the source email as read.")
@click.pass_context
def wait_email_code(ctx, site, timeout, max_age, mark_read):
    """Poll IMAP for an email matching SITE's stored email-poll filter,
    return the extracted code.

    Set the poll URL once via:
        patchium secret set example.com email-poll \\
          'imaps://user:pass@imap.gmail.com:993?regex=\\d{6}&from=*@example.com'
    """
    args = {"site": site, "timeout": timeout, "max_age": max_age,
            "mark_read": mark_read}
    _emit(call("wait_email_code", args, timeout=timeout + 10),
          ctx.obj["json"], "code")


# ─── Wave 6.2c: evals benchmark suite ────────────────────────────────────

@cli.group()
def evals():
    """Run the stealth-eval benchmark matrix and emit markdown/JSON tables.

    Replaces the README's '70-90%' guesses with measured numbers per backend
    and per humanize-on/off. Use in CI with --min-score to catch regressions.

        patchium evals run                                # default matrix → markdown
        patchium evals run --backends patchright,nodriver --humanize on,off
        patchium evals run --json --out evals.json
        patchium evals run --update-readme                # patches README in-place
        patchium evals run --min-score 80                 # exit 1 if any cell <80
    """


@evals.command("run")
@click.option("--targets", default="sannysoft",
              help="Comma-separated target names or URLs (default: sannysoft).")
@click.option("--backends", default="patchright",
              help="Comma-separated backend names. nodriver requires patchium[nodriver].")
@click.option("--humanize", default="off",
              help="Comma-separated 'on','off' modes (default: off).")
@click.option("--settle-ms", default=5000, type=int)
@click.option("--out", "out_path", default=None, type=click.Path(),
              help="Write output to file instead of stdout.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of markdown.")
@click.option("--update-readme", "update_readme_flag", is_flag=True,
              help="Patch README.md between <!-- patchium-evals --> markers.")
@click.option("--min-score", "min_score_arg", default=None, type=int,
              help="Exit non-zero if any cell scored below this (CI gate).")
@click.pass_context
def evals_run(ctx, targets, backends, humanize, settle_ms, out_path,
              as_json, update_readme_flag, min_score_arg):
    """Run the eval matrix and emit a results table."""
    from . import evals as _evals
    targets_list = [t.strip() for t in targets.split(",") if t.strip()]
    backends_list = [b.strip() for b in backends.split(",") if b.strip()]
    humanize_modes = []
    for m in humanize.split(","):
        m = m.strip().lower()
        if m == "on": humanize_modes.append(True)
        elif m == "off": humanize_modes.append(False)
    if not humanize_modes:
        humanize_modes = [False]

    rows = _evals.run_eval_matrix(
        call, targets=targets_list, backends=backends_list,
        humanize_modes=humanize_modes, settle_ms=settle_ms,
    )

    if as_json:
        output = _evals.render_json(rows)
    else:
        output = _evals.render_markdown(rows)

    if update_readme_flag:
        from pathlib import Path as _P
        readme = _P.cwd() / "README.md"
        if not readme.exists():
            # Try relative to the package install (dev mode)
            import patchium as _pm
            readme = _P(_pm.__file__).resolve().parent.parent / "README.md"
        if readme.exists():
            changed = _evals.update_readme(readme, _evals.render_markdown(rows))
            click.echo(f"README updated: {changed} ({readme})", err=True)
        else:
            click.echo(f"README.md not found", err=True)

    if out_path:
        from pathlib import Path as _P
        _P(out_path).write_text(output)
        click.echo(f"wrote {out_path}", err=True)
    else:
        click.echo(output)

    if min_score_arg is not None:
        lowest = _evals.min_score(rows)
        if lowest is None:
            click.echo("error: no scored cells (all errored)", err=True)
            sys.exit(1)
        if lowest < min_score_arg:
            click.echo(f"FAIL: min score {lowest} < {min_score_arg}", err=True)
            sys.exit(1)


# ─── Wave 6.2b: humanization ─────────────────────────────────────────────

@cli.group()
def humanize():
    """Toggle human-like mouse trajectories + dwell + scroll inertia per session.

    OFF by default (Bezier paths are visible entropy — only enable when the
    target actually fingerprints mouse behavior, e.g. DataDome, PerimeterX).

        patchium --session work humanize on
        patchium --session work click @e3       # uses humanized click
        patchium --session work humanize off
    """


@humanize.command("on")
@click.pass_context
def humanize_on(ctx):
    _emit(call("humanize_on"), ctx.obj["json"])


@humanize.command("off")
@click.pass_context
def humanize_off(ctx):
    _emit(call("humanize_off"), ctx.obj["json"])


@humanize.command("status")
@click.pass_context
def humanize_status(ctx):
    _emit(call("humanize_status"), ctx.obj["json"])


# ─── Wave 6.2a: per-session proxy ────────────────────────────────────────

@cli.group()
def proxy():
    """Per-session proxy configuration.

    Set a proxy that will be applied next time the session launches:

        patchium --session work proxy set "http://user:pass@127.0.0.1:8888"
        patchium --session work proxy set --path ~/.config/patchium-proxy.txt
        patchium --session work start          # uses the configured proxy
        patchium --session work proxy info     # exit IP, latency
        patchium --session work proxy clear

    Built-in providers (URL prefixes):
      http / socks5     generic
      brightdata        Bright Data residential/datacenter
      iproyal           IPRoyal residential + sticky sessions
      decodo            Decodo residential
    """


@proxy.command("set")
@click.argument("url", required=False)
@click.option("--path", default=None,
              help="Read the proxy URL from a 0600 file (cred hygiene).")
@click.pass_context
def proxy_set(ctx, url, path):
    """Persist a proxy URL for the current session."""
    if not url and not path:
        click.echo("error: URL or --path required", err=True); sys.exit(2)
    args = {}
    if url:
        args["url"] = url
    if path:
        args["path"] = str(Path(path).resolve())
    _emit(call("proxy_set", args), ctx.obj["json"])


@proxy.command("clear")
@click.pass_context
def proxy_clear(ctx):
    """Remove the proxy from the current session (takes effect on next start)."""
    _emit(call("proxy_clear"), ctx.obj["json"])


@proxy.command("info")
@click.pass_context
def proxy_info(ctx):
    """Show configured proxy + current exit IP (if session running)."""
    _emit(call("proxy_info"), ctx.obj["json"])


# ─── Wave 6.1c: session checkpoint / restore ────────────────────────────

@cli.group()
def checkpoint():
    """Save & restore complete session state — tabs, cookies, storage.

    A checkpoint captures everything needed to recreate a logged-in browser
    state later, even in a different session (Browserbase Contexts parity).

        patchium --session work checkpoint save logged-in
        patchium --session work checkpoint list
        patchium --session work-2 checkpoint load logged-in --from-session work
        patchium --session work checkpoint delete logged-in
    """


@checkpoint.command("save")
@click.argument("name")
@click.pass_context
def checkpoint_save(ctx, name):
    """Save the current session's tabs + cookies + storage as <name>."""
    _emit(call("checkpoint_save", {"name": name}), ctx.obj["json"])


@checkpoint.command("load")
@click.argument("name")
@click.option("--from-session", default=None,
              help="Load from a different session's checkpoint dir (cross-session clone).")
@click.pass_context
def checkpoint_load(ctx, name, from_session):
    """Restore checkpoint <name> into the current session."""
    args = {"name": name}
    if from_session:
        args["from_session"] = from_session
    _emit(call("checkpoint_load", args), ctx.obj["json"])


@checkpoint.command("list")
@click.option("--from-session", default=None, help="List a different session's checkpoints.")
@click.pass_context
def checkpoint_list(ctx, from_session):
    """List checkpoints for the current (or specified) session."""
    args = {}
    if from_session:
        args["from_session"] = from_session
    res = call("checkpoint_list", args)
    if ctx.obj["json"]:
        _emit(res, True)
        return
    import time as _time
    if not res["checkpoints"]:
        click.echo("no checkpoints")
        return
    for c in res["checkpoints"]:
        ts = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(c["ts"]))
        click.echo(f"{c['name']:24s} {ts}  {c['tabs']} tabs  {c['cookies']} cookies  {c['bytes']}B")


@checkpoint.command("delete")
@click.argument("name")
@click.pass_context
def checkpoint_delete(ctx, name):
    """Delete checkpoint <name> from the current session."""
    _emit(call("checkpoint_delete", {"name": name}), ctx.obj["json"])


# ─── Wave 6.1a: live-view ────────────────────────────────────────────────

@cli.group()
def liveview():
    """Stream browser frames over WebSocket for a regular-browser viewer.

    Watch what an agent is doing in real time. Read-only by default;
    --takeover forwards your clicks/keystrokes back into the session.

        patchium liveview start                # bind 127.0.0.1:9223
        patchium liveview start --takeover     # mouse/keyboard takeover mode
        patchium liveview url                  # print viewer URL
        # open the URL in any browser
        patchium liveview stop
    """


@liveview.command("start")
@click.option("--port", default=9223, type=int)
@click.option("--host", default="127.0.0.1",
              help="Bind address. 127.0.0.1 is the only safe default.")
@click.option("--fps", default=5, type=int)
@click.option("--jpeg-quality", default=60, type=int)
@click.option("--takeover", is_flag=True,
              help="Forward viewer clicks/keystrokes to the session.")
@click.option("--insecure-public", is_flag=True,
              help="Required acknowledgement to bind a non-loopback host.")
@click.pass_context
def liveview_start(ctx, port, host, fps, jpeg_quality, takeover, insecure_public):
    """Start the live-view server."""
    args = {"port": port, "host": host, "fps": fps,
            "jpeg_quality": jpeg_quality, "takeover": takeover}
    if insecure_public:
        args["insecure_public"] = True
    _emit(call("liveview_start", args), ctx.obj["json"])


@liveview.command("stop")
@click.pass_context
def liveview_stop(ctx):
    """Stop the live-view server."""
    _emit(call("liveview_stop"), ctx.obj["json"])


@liveview.command("url")
@click.option("--session", "session_name", default=None,
              help="Specific session to link (omit for current default).")
@click.pass_context
def liveview_url(ctx, session_name):
    """Print the viewer URL for the current or specified session."""
    args = {}
    if session_name:
        args["session"] = session_name
    res = call("liveview_url", args)
    if ctx.obj["json"]:
        _emit(res, True)
        return
    if not res.get("running"):
        click.echo("live-view not running — `patchium liveview start` first", err=True)
        sys.exit(1)
    target = res.get("session_url") or res.get("url")
    click.echo(target)


# ─── Wave 5.4b: fingerprint scorer ────────────────────────────────────────

@cli.command()
@click.argument("target", default="sannysoft")
@click.option("--url", default=None, help="Override the URL (for custom detectors).")
@click.option("--extract", default=None,
              help="JS expression to extract score (for custom detectors).")
@click.option("--settle-ms", default=5000, type=int,
              help="Ms to wait after networkidle for JS to render the report.")
@click.pass_context
def fingerprint(ctx, target, url, extract, settle_ms):
    """Open a bot-detection page and extract a numeric stealth score.

    Built-in TARGETs:
      sannysoft  — bot.sannysoft.com
      creepjs    — CreepJS canvas/audio/timing detector
      brotector  — Brotector (Patchright authors' own gauntlet)

    Use to replace the README's '70-90%' guesses with measured numbers per
    backend. Run with `--backend nodriver` (via `patchium start --backend ...`)
    to compare stealth stacks on the same target.
    """
    args = {"target": target, "settle_ms": settle_ms}
    if url:
        args["url"] = url
    if extract:
        args["extract"] = extract
    _emit(call("fingerprint", args), ctx.obj["json"])


# ─── MCP server ───────────────────────────────────────────────────────────

@cli.command()
@click.option("--caps", default=None,
              help="Comma-separated capability list to expose "
                   "(default: all). Available: core,session,nav,content,input,"
                   "element,pages,storage,network,dialogs,overrides,vision,"
                   "devtools,agent. Example: `--caps=core,session,nav,input,agent` "
                   "exposes only the basics (cuts prompt-token tax for LLMs).")
def mcp(caps):
    """Run the MCP server (stdio JSON-RPC) — wires every CLI verb as an MCP tool."""
    from .mcp_server import _entrypoint
    _entrypoint(caps=caps)


# ─── Wave 6.4a: REST shim ────────────────────────────────────────────────

@cli.command()
@click.option("--host", default="127.0.0.1",
              help="Bind address. 127.0.0.1 is the default; --insecure-no-auth needed for 0.0.0.0.")
@click.option("--port", default=8000, type=int)
@click.option("--insecure-no-auth", is_flag=True,
              help="Disable bearer-token auth (dev only).")
def serve(host, port, insecure_no_auth):
    """Run the FastAPI REST shim mirroring every daemon verb at POST /v1/<verb>.

    Bearer token persists at ~/.cache/patchium/rest-token (mode 0600).
    Set the same token in the Authorization header from any HTTP client.
    """
    if host not in ("127.0.0.1", "::1", "localhost") and not insecure_no_auth:
        # Public bind WITH auth is fine, but we want the user to think about it
        click.echo(f"warning: binding non-loopback {host!r}; ensure firewall is set", err=True)
    from .rest import serve as _serve
    _serve(host=host, port=port, require_auth=not insecure_no_auth)


# ─── pages ────────────────────────────────────────────────────────────────

@cli.group()
def page():
    """Manage browser pages (new / switch / close)."""


@page.command("new")
@click.pass_context
def page_new(ctx):
    """Open a new tab and switch to it."""
    _emit(call("page_new"), ctx.obj["json"], "url")


@page.command("switch")
@click.argument("index", type=int)
@click.pass_context
def page_switch(ctx, index):
    """Switch to tab by index."""
    _emit(call("page_switch", {"index": index}), ctx.obj["json"], "url")


@page.command("close")
@click.pass_context
def page_close(ctx):
    """Close the current tab."""
    _emit(call("page_close"), ctx.obj["json"])


@cli.command()
@click.pass_context
def pages(ctx):
    """List all open pages."""
    _emit(call("pages"), ctx.obj["json"])


# ─── error wrapper ────────────────────────────────────────────────────────

def main():
    try:
        cli(obj={})
    except DaemonError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except DaemonNotRunning as exc:
        click.echo(f"daemon not running: {exc}", err=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
