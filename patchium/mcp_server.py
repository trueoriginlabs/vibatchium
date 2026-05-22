"""Patchium MCP server — exposes the daemon's verbs as MCP tools over stdio.

Wire-up: `claude mcp add patchium python -m patchium.mcp_server`.

The MCP server talks to the SAME daemon that the CLI uses. A browser session
started by `patchium start` (or `patchium attach`) is immediately accessible to
Claude Code via these tools, and vice versa — single source of browser truth.

Tool naming follows the CLI verb names (go, map, click, fill, ...) so MCP
ergonomics match the CLI ergonomics.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from . import __version__
from .client import call as daemon_call, daemon_is_running, spawn_daemon


server = Server("patchium")


# ─── tool schemas ─────────────────────────────────────────────────────────


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _int(desc: str, default: int | None = None) -> dict:
    s = {"type": "integer", "description": desc}
    if default is not None:
        s["default"] = default
    return s


def _bool(desc: str, default: bool = False) -> dict:
    return {"type": "boolean", "description": desc, "default": default}


# Each entry: (name, description, json_schema for input, daemon_cmd, arg_mapper)
# arg_mapper transforms the MCP tool args dict into the daemon RPC args dict.
TOOLS: list[tuple[str, str, dict, str, Any]] = [
    ("start", "Start a browser session (cold-launch real Chrome).",
     {"type": "object", "properties": {
         "profile": _str("Persistent profile dir (default: cache dir)."),
         "headless": _bool("Headless mode (not recommended for stealth)."),
     }},
     "start", None),
    ("attach", "Attach to an existing Chrome via CDP (use after manual login on a Cloudflare-walled site).",
     {"type": "object", "properties": {"cdp_url": _str("e.g. http://localhost:9222")},
      "required": []},
     "attach", None),
    ("stop", "Stop the browser session.",
     {"type": "object", "properties": {}}, "stop", None),
    ("status", "Daemon + session status.",
     {"type": "object", "properties": {}}, "status", None),
    ("go", "Navigate to a URL.",
     {"type": "object",
      "properties": {"url": _str("Target URL."),
                     "wait_until": _str("load|domcontentloaded|networkidle|commit"),
                     "timeout_ms": _int("Timeout in ms.", 60_000)},
      "required": ["url"]},
     "go", None),
    ("back", "Browser back.",
     {"type": "object", "properties": {}}, "back", None),
    ("forward", "Browser forward.",
     {"type": "object", "properties": {}}, "forward", None),
    ("reload", "Reload current page.",
     {"type": "object", "properties": {}}, "reload", None),
    ("url", "Get current URL.", {"type": "object", "properties": {}}, "url", None),
    ("title", "Get current page title.",
     {"type": "object", "properties": {}}, "title", None),
    ("text", "Get inner text (whole page or a selector).",
     {"type": "object", "properties": {"selector": _str("Optional CSS or @eN.")}},
     "text", None),
    ("html", "Get HTML (whole page or a selector).",
     {"type": "object", "properties": {"selector": _str("Optional CSS or @eN.")}},
     "html", None),
    ("eval", "Evaluate a JS expression in the page (isolated context per Patchright default).",
     {"type": "object", "properties": {"expr": _str("JS expression.")},
      "required": ["expr"]},
     "eval", None),
    ("map", "Snapshot actionable elements and assign @eN refs (uses Playwright aria_snapshot mode='ai').",
     {"type": "object", "properties": {
         "indent": _bool("Preserve YAML indent.", True),
         "depth": _int("Limit snapshot depth."),
     }},
     "map", None),
    ("diff_map", "Diff current snapshot vs the previous one.",
     {"type": "object", "properties": {}}, "diff_map", None),
    ("click", "Click an @eN ref or CSS selector.",
     {"type": "object",
      "properties": {"target": _str("@eN ref or selector."),
                     "timeout_ms": _int("Timeout in ms.", 30_000),
                     "auto_dismiss_banners": _bool(
                         "On 'intercepted' failure, try dismiss_banners once and retry.",
                         False)},
      "required": ["target"]},
     "click", None),
    ("fill", "Clear an input and fill it with text.",
     {"type": "object",
      "properties": {"target": _str("@eN ref or selector."),
                     "text": _str("Text to fill.")},
      "required": ["target", "text"]},
     "fill", None),
    ("type", "Type text (key-by-key) into an element.",
     {"type": "object",
      "properties": {"target": _str("@eN ref or selector."),
                     "text": _str("Text to type."),
                     "delay_ms": _int("Per-keystroke delay (ms).", 0)},
      "required": ["target", "text"]},
     "type", None),
    ("hover", "Hover over an element.",
     {"type": "object", "properties": {"target": _str("@eN ref or selector.")},
      "required": ["target"]},
     "hover", None),
    ("press", "Press a key on a specific element (e.g. Enter on @e3).",
     {"type": "object",
      "properties": {"target": _str("@eN ref or selector."),
                     "keys": _str("Key combination.")},
      "required": ["target", "keys"]},
     "press", None),
    ("keys", "Press a key combination (e.g. 'Control+a', 'Enter').",
     {"type": "object", "properties": {"keys": _str("Key combo.")},
      "required": ["keys"]},
     "keys", None),
    ("check", "Check a checkbox / radio.",
     {"type": "object", "properties": {"target": _str("@eN or selector.")},
      "required": ["target"]},
     "check", None),
    ("uncheck", "Uncheck a checkbox.",
     {"type": "object", "properties": {"target": _str("@eN or selector.")},
      "required": ["target"]},
     "uncheck", None),
    ("scroll", "Scroll the page or a target element into view.",
     {"type": "object", "properties": {
         "target": _str("Optional @eN/selector to scroll into view."),
         "dx": _int("Horizontal pixels.", 0),
         "dy": _int("Vertical pixels.", 0),
     }}, "scroll", None),
    ("is_state", "Check element state (visible/enabled/checked/...).",
     {"type": "object",
      "properties": {"target": _str("@eN or selector."),
                     "state": _str("visible|hidden|enabled|disabled|checked|editable")},
      "required": ["target", "state"]},
     "is", None),
    ("screenshot", "Capture a screenshot. Returns base64 PNG if no path given.",
     {"type": "object",
      "properties": {"path": _str("Output file path."),
                     "full_page": _bool("Full page (not just viewport).")}},
     "screenshot", None),
    ("viewport", "Get or set viewport size.",
     {"type": "object",
      "properties": {"width": _int("Width."), "height": _int("Height.")}},
     "viewport", None),
    ("storage_export", "Export storage state (cookies + per-origin LS/SS).",
     {"type": "object", "properties": {"path": _str("Output file path.")}},
     "storage_export", None),
    ("storage_restore", "Restore storage state from a JSON file.",
     {"type": "object",
      "properties": {"path": _str("Input file path.")},
      "required": ["path"]},
     "storage_restore", None),
    ("cookies", "List current cookies.",
     {"type": "object", "properties": {}}, "cookies", None),
    ("wait_url", "Wait until the URL matches a glob/regex.",
     {"type": "object",
      "properties": {"pattern": _str("URL pattern."),
                     "timeout_ms": _int("Timeout in ms.", 30_000)},
      "required": ["pattern"]},
     "wait_url", None),
    ("wait_load", "Wait for a page load state (load/domcontentloaded/networkidle).",
     {"type": "object",
      "properties": {"state": _str("load|domcontentloaded|networkidle"),
                     "timeout_ms": _int("Timeout in ms.", 30_000)}},
     "wait_load", None),
    ("wait_fn", "Wait until a JS expression returns truthy.",
     {"type": "object",
      "properties": {"expr": _str("JS expression."),
                     "timeout_ms": _int("Timeout in ms.", 30_000)},
      "required": ["expr"]},
     "wait_fn", None),
    ("pages", "List all open browser tabs.",
     {"type": "object", "properties": {}}, "pages", None),
    ("page_new", "Open a new tab and switch to it.",
     {"type": "object", "properties": {}}, "page_new", None),
    ("page_switch", "Switch to tab by index.",
     {"type": "object", "properties": {"index": _int("Tab index.")},
      "required": ["index"]},
     "page_switch", None),
    ("find", "Locate elements by semantic strategy (text/label/placeholder/role/testid/xpath/alt/title/css).",
     {"type": "object",
      "properties": {"kind": _str("text|label|placeholder|role|testid|xpath|alt|title|css"),
                     "query": _str("Search query."),
                     "exact": _bool("Exact match.", False),
                     "name": _str("Accessible name (role kind only).")},
      "required": ["kind", "query"]},
     "find", None),
    ("count", "Count matching elements for a selector or @eN ref.",
     {"type": "object", "properties": {"target": _str("@eN or selector.")},
      "required": ["target"]},
     "count", None),
    ("content", "Replace the page HTML wholesale via page.set_content.",
     {"type": "object", "properties": {"html": _str("New HTML body.")},
      "required": ["html"]},
     "content", None),
    ("frames", "List all live frames with name + url + depth + active flag.",
     {"type": "object", "properties": {}}, "frames", None),
    ("frame", "Switch active frame by name or URL substring (omit both to clear).",
     {"type": "object", "properties": {"name": _str("Frame name."),
                                        "url": _str("URL substring.")}},
     "frame", None),
    ("mouse", "Mouse control at pixel coordinates.",
     {"type": "object",
      "properties": {"action": _str("click|dblclick|move|down|up|wheel"),
                     "x": {"type": "number"}, "y": {"type": "number"},
                     "button": _str("left|right|middle"),
                     "steps": _int("Move steps."),
                     "dx": {"type": "number"}, "dy": {"type": "number"}},
      "required": ["action"]},
     "mouse", None),
    ("upload", "Set files on an input[type=file] target.",
     {"type": "object",
      "properties": {"target": _str("@eN or selector."),
                     "files": {"type": "array", "items": {"type": "string"}}},
      "required": ["target", "files"]},
     "upload", None),
    ("dialog_policy", "Set how the next alert/confirm/prompt is handled.",
     {"type": "object",
      "properties": {"action": _str("accept|dismiss"),
                     "text": _str("Prompt input text on accept.")},
      "required": ["action"]},
     "dialog_policy", None),
    ("download_arm", "Start collecting page download events.",
     {"type": "object", "properties": {}}, "download_arm", None),
    ("download_list", "List captured downloads.",
     {"type": "object", "properties": {}}, "download_list", None),
    ("download_save", "Save a captured download to a path.",
     {"type": "object",
      "properties": {"index": _int("Download index."),
                     "path": _str("Save path.")},
      "required": ["index", "path"]},
     "download_save", None),
    ("pdf", "Save the current page as PDF.",
     {"type": "object",
      "properties": {"path": _str("Output path."),
                     "format": _str("Letter|A4|...")},
      "required": ["path"]},
     "pdf", None),
    ("record_start", "Start a Playwright trace recording.",
     {"type": "object",
      "properties": {"screenshots": _bool("Capture screenshots.", True),
                     "snapshots": _bool("Capture DOM snapshots.", True),
                     "sources": _bool("Capture source files.", False)}},
     "record_start", None),
    ("record_stop", "Stop tracing and write the trace ZIP.",
     {"type": "object", "properties": {"path": _str("Output ZIP path.")},
      "required": ["path"]},
     "record_stop", None),
    ("highlight", "Briefly outline an @eN ref or selector for visual debugging.",
     {"type": "object",
      "properties": {"target": _str("@eN or selector."),
                     "ms": _int("Highlight duration ms.", 3000)},
      "required": ["target"]},
     "highlight", None),
    ("geolocation", "Override geolocation (lat/lng/accuracy) or clear.",
     {"type": "object",
      "properties": {"lat": {"type": "number"}, "lng": {"type": "number"},
                     "accuracy": {"type": "number"}, "clear": _bool("Clear override.", False)}},
     "geolocation", None),
    ("media", "Override CSS media features (color-scheme, reduced-motion, print, ...).",
     {"type": "object",
      "properties": {"media": _str("screen|print|no-override"),
                     "color_scheme": _str("light|dark|no-preference|no-override"),
                     "reduced_motion": _str("reduce|no-preference|no-override"),
                     "forced_colors": _str("active|none|no-override")}},
     "media", None),
    ("network_start", "Start capturing request/response events.",
     {"type": "object", "properties": {"max": _int("Ring buffer size.", 500)}},
     "network_start", None),
    ("network_stop", "Stop network capture.",
     {"type": "object", "properties": {}}, "network_stop", None),
    ("network_dump", "Dump captured network events (optionally to a file).",
     {"type": "object", "properties": {"path": _str("Optional output JSON path.")}},
     "network_dump", None),
    ("screenshot_annotate", "Screenshot with @eN bounding-box overlays (vision-LLM friendly).",
     {"type": "object",
      "properties": {"path": _str("Output PNG path."),
                     "full_page": _bool("Capture full page.", False)},
      "required": ["path"]},
     "screenshot_annotate", None),
    ("map_compact", "One-line-per-element rendering of the snapshot (token-efficient).",
     {"type": "object", "properties": {"depth": _int("Limit snapshot depth.")}},
     "map_compact", None),
    ("observe", "Plan a verb + target for an intent without executing.",
     {"type": "object",
      "properties": {"intent": _str("Natural-language intent."),
                     "llm": _bool("Use Claude (needs ANTHROPIC_API_KEY).", False),
                     "force": _bool("Bypass cache.", False)},
      "required": ["intent"]},
     "observe", None),
    ("act", "Observe + execute the resulting plan in one shot.",
     {"type": "object",
      "properties": {"intent": _str("Natural-language intent."),
                     "llm": _bool("Use Claude.", False)},
      "required": ["intent"]},
     "act", None),
    ("profile_list", "List all profiles and the active one.",
     {"type": "object", "properties": {}}, "profile_list", None),
    ("profile_new", "Create a new named profile.",
     {"type": "object", "properties": {"name": _str("Profile name.")},
      "required": ["name"]},
     "profile_new", None),
    ("profile_use", "Set the active profile (takes effect on next start).",
     {"type": "object", "properties": {"name": _str("Profile name.")},
      "required": ["name"]},
     "profile_use", None),
    ("profile_delete", "Delete a profile directory (not the active or default).",
     {"type": "object", "properties": {"name": _str("Profile name.")},
      "required": ["name"]},
     "profile_delete", None),
    ("route_add", "Add a request-interception rule (abort/fulfill/passthrough).",
     {"type": "object",
      "properties": {"pattern": _str("Playwright URL glob, e.g. **/*.png"),
                     "mode": _str("abort|fulfill|passthrough"),
                     "body": _str("Response body (fulfill mode)."),
                     "status": _int("HTTP status (fulfill).", 200),
                     "content_type": _str("Content-Type (fulfill)."),
                     "headers": {"type": "object",
                                 "additionalProperties": {"type": "string"}}},
      "required": ["pattern"]},
     "route_add", None),
    ("route_list", "List active route rules + hit counts.",
     {"type": "object", "properties": {}}, "route_list", None),
    ("route_clear", "Clear one or all route rules.",
     {"type": "object", "properties": {"pattern": _str("Specific pattern to clear.")}},
     "route_clear", None),
    ("wait_response", "Wait for a network response matching a URL pattern.",
     {"type": "object",
      "properties": {"pattern": _str("URL substring or regex."),
                     "timeout_ms": _int("Timeout in ms.", 30_000),
                     "body": _bool("Capture response body.", False),
                     "max_body": _int("Max body bytes.", 1_000_000)},
      "required": ["pattern"]},
     "wait_response", None),
    ("dismiss_banners", "Heuristically dismiss cookie/consent/newsletter banners.",
     {"type": "object",
      "properties": {"prefer": _str("accept|reject (default reject)."),
                     "dry_run": _bool("Report candidates without clicking.", False),
                     "max_clicks": _int("Max banners to dismiss.", 1)}},
     "dismiss_banners", None),
    ("har_start", "Start HAR recording (full request+response capture).",
     {"type": "object",
      "properties": {"path": _str("HAR output path."),
                     "content": _str("embed|omit response bodies."),
                     "url_filter": _str("Only capture matching URLs.")},
      "required": ["path"]},
     "har_start", None),
    ("har_stop", "Stop HAR recording and flush.",
     {"type": "object", "properties": {}}, "har_stop", None),
    ("eval_handle", "Eval JS and return a handle id; usable with handle_eval.",
     {"type": "object", "properties": {"expr": _str("JS expression.")},
      "required": ["expr"]},
     "eval_handle", None),
    ("handle_eval", "Evaluate JS with a handle as `arg`.",
     {"type": "object",
      "properties": {"handle": _str("Handle id (h_N)."),
                     "expr": _str("JS expression.")},
      "required": ["handle", "expr"]},
     "handle_eval", None),
    ("handle_list", "List active handle ids.",
     {"type": "object", "properties": {}}, "handle_list", None),
    ("handle_dispose", "Release a single handle.",
     {"type": "object", "properties": {"handle": _str("Handle id.")},
      "required": ["handle"]},
     "handle_dispose", None),
    ("handle_dispose_all", "Release all active handles.",
     {"type": "object", "properties": {}}, "handle_dispose_all", None),
]


_TOOL_BY_NAME = {t[0]: t for t in TOOLS}


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=name, description=desc, inputSchema=schema)
        for (name, desc, schema, _cmd, _mapper) in TOOLS
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.Content]:
    entry = _TOOL_BY_NAME.get(name)
    if entry is None:
        return [types.TextContent(type="text", text=f"unknown tool: {name}")]
    _name, _desc, _schema, cmd, mapper = entry
    args = mapper(arguments) if mapper else (arguments or {})

    # Auto-spawn the daemon if it isn't running.
    if not daemon_is_running():
        spawn_daemon()

    try:
        result = await asyncio.to_thread(daemon_call, cmd, args)
    except Exception as exc:  # noqa: BLE001
        return [types.TextContent(type="text", text=f"error: {type(exc).__name__}: {exc}")]

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="patchium",
                server_version=__version__,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def _entrypoint() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    _entrypoint()
