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
@click.option("--profile", type=click.Path(), default=None, help="Persistent profile dir.")
@click.option("--headless", is_flag=True, help="Headless mode (NOT recommended for stealth).")
@click.pass_context
def start(ctx, profile, headless):
    """Start a browser session (cold launch real Chrome + persistent context)."""
    args = {}
    if profile:
        args["profile"] = profile
    if headless:
        args["headless"] = True
    _emit(call("start", args), ctx.obj["json"], "mode")


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
@click.pass_context
def screenshot(ctx, output, full_page):
    """Capture a screenshot."""
    path = str(Path(output).resolve())
    _emit(call("screenshot", {"path": path, "full_page": full_page}),
          ctx.obj["json"], "path")


# ─── element model: map + interactive verbs ──────────────────────────────

@cli.command(name="map")
@click.option("--indent", is_flag=True, help="Show structural depth via leading spaces.")
@click.option("--verbose", is_flag=True, help="Include full entry list in JSON output.")
@click.pass_context
def map_cmd(ctx, indent, verbose):
    """Snapshot the page's actionable elements and assign @eN refs."""
    result = call("map", {"indent": indent, "verbose": verbose})
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
