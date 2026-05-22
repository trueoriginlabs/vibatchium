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
@click.version_option(__version__, "--version")
@click.pass_context
def cli(ctx: click.Context, json_mode: bool) -> None:
    """Patchium — agentic browser CLI (Patchwright stealth + Vibium ergonomics)."""
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_mode


# ─── lifecycle ─────────────────────────────────────────────────────────────

@cli.command()
@click.option("--profile", default=None,
              help="Profile name or absolute path (defaults to active profile).")
@click.option("--headless", is_flag=True, help="Headless mode (NOT recommended for stealth).")
@click.pass_context
def start(ctx, profile, headless):
    """Start a browser session (cold launch real Chrome + persistent context)."""
    args = {}
    if profile:
        args["profile"] = profile
    if headless:
        args["headless"] = True
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


# ─── profile ─────────────────────────────────────────────────────────

@cli.group()
def profile():
    """Manage named browser profiles (cookies/storage/identity isolation)."""


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
@click.pass_context
def click_cmd(ctx, target, timeout_ms):
    """Click an @eN ref or selector."""
    _emit(call("click", {"target": target, "timeout_ms": timeout_ms}),
          ctx.obj["json"], "clicked")


@cli.command()
@click.argument("target")
@click.argument("text_arg", metavar="TEXT")
@click.option("--timeout", "timeout_ms", default=30_000, type=int)
@click.pass_context
def fill(ctx, target, text_arg, timeout_ms):
    """Clear an input and fill it with text (React-input-safe via Locator.fill)."""
    _emit(call("fill", {"target": target, "text": text_arg, "timeout_ms": timeout_ms}),
          ctx.obj["json"], "filled")


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


# ─── observe / act (intent → plan) ────────────────────────────────────────

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


# ─── MCP server ───────────────────────────────────────────────────────────

@cli.command()
def mcp():
    """Run the MCP server (stdio JSON-RPC) — wires every CLI verb as an MCP tool."""
    from .mcp_server import _entrypoint
    _entrypoint()


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
