"""0.10.x — the MCP server `instructions` field + entry-point WHEN-framing.

These guard the tool-SELECTION fix. vibatchium's stealth is useless if a
connecting agent never *reaches* for it: the default failure was calling the
built-in WebFetch, getting blocked (403 / Cloudflare / JS-shell), and giving up.
The `instructions` field (surfaced to the model at connect, in the
InitializeResult, regardless of caps) plus the explore/go descriptions must
carry the escalation trigger so vb wins the selection battle.

Pure/deterministic: no daemon, no Chrome — just the static MCP surface.
"""
from __future__ import annotations

import anyio
from mcp.client.session import ClientSession
from mcp.server.models import InitializationOptions
from mcp.shared.memory import create_client_server_memory_streams

from vibatchium import mcp_server as M


def test_server_ships_nonempty_instructions():
    instr = M.server.instructions
    assert instr and isinstance(instr, str)
    # the behavioral core: trigger + the no-stop rule + the entry point
    assert "WebFetch" in instr
    assert "explore" in instr
    assert "DO NOT report failure" in instr


def test_instructions_name_the_block_triggers():
    instr = M._INSTRUCTIONS
    # the symptoms an agent actually sees when blocked
    assert "403" in instr
    assert "Cloudflare" in instr and "DataDome" in instr
    assert "JavaScript is required" in instr


def test_instructions_keep_the_cheap_default_carveout():
    # must NOT tell the agent to use vb for everything — plain HTML stays WebFetch
    instr = M._INSTRUCTIONS.lower()
    assert "plain static html" in instr or "plain html" in instr
    assert "cheaper" in instr or "faster" in instr


def test_instructions_do_not_overclaim():
    instr = M._INSTRUCTIONS
    # honest hedge present; no "defeats any wall" absolutism
    assert "clears most" in instr
    assert "can still fail" in instr
    assert "always works" not in instr.lower()
    assert "any wall" not in instr.lower()
    # the fresh-eyes nit: the "always-on escalations" wording was false under a
    # custom --caps that drops core/nav — it's gone (instructions are caps-aware).
    assert "always-on" not in instr


def test_init_options_carry_instructions_load_bearing_path():
    # ServerSession reads `instructions` from the InitializationOptions we hand
    # to server.run() (NOT the Server() ctor), so it must be present THERE or
    # the client never sees it. This is the channel that actually reaches a
    # connecting agent.
    opts = M._init_options()
    assert isinstance(opts, InitializationOptions)
    assert opts.instructions == M._build_instructions(M._ACTIVE_CAPS)
    assert "WebFetch" in (opts.instructions or "")


def test_go_description_carries_blocked_trigger():
    desc = M._TOOL_BY_NAME["go"][1]
    assert "blocked" in desc.lower()
    assert "WebFetch" in desc
    assert desc != "Navigate to a URL."   # regression: must be upgraded from the bare WHAT


def test_explore_description_carries_blocked_trigger_and_keeps_original():
    desc = M._TOOL_BY_NAME["explore"][1]
    assert "BLOCKED" in desc
    assert "WebFetch" in desc
    # the prefix is PREPENDED — the original 80%-case framing must survive
    assert "ONE-CALL" in desc
    assert "OFF-BUDGET" in desc


# ─── fresh-eyes remediation: caps-awareness + the load-bearing wire path ─────

def test_lean_default_exposes_every_headlined_verb():
    # the instructions headline explore/go/extract/screenshot — every one must be
    # in the lean profile the server ships by default, else the guidance would
    # name a tool the agent doesn't have (the give-up failure this feature kills).
    exposed = {t[0] for t in M._filter_tools(M._resolve_caps("lean"))}
    for verb in ("explore", "go", "extract", "screenshot"):
        assert verb in exposed, f"{verb!r} dropped from lean — instructions would name an absent tool"


def test_instructions_are_caps_aware():
    # never name a browse verb the active surface doesn't expose.
    # vision-only / content-only have no explore/go → nothing to escalate to → None.
    assert M._build_instructions(M._resolve_caps("vision")) is None
    assert M._build_instructions(M._resolve_caps("content")) is None
    # explore+go present but screenshot dropped → ship instructions naming
    # explore/go, but do NOT list screenshot(tiles=true).
    txt = M._build_instructions(M._resolve_caps("core,nav,content"))
    assert txt is not None
    assert "explore(url)" in txt and "go(url)" in txt
    assert "screenshot(tiles=true)" not in txt
    # the default (full) surface names all three entry points.
    full = M._build_instructions(None)
    assert "explore(url)" in full and "go(url)" in full and "screenshot(tiles=true)" in full


async def test_initialize_handshake_delivers_instructions_over_the_wire():
    """The load-bearing path, end to end: drive a real ClientSession.initialize()
    against the actual `server` + `_init_options()` and assert the InitializeResult
    carries the escalation guidance. Input-only assertions would miss a regression
    like a switch to create_initialization_options() or an SDK change in how
    InitializationOptions feeds InitializeResult. initialize() never dispatches a
    tool, so no daemon/Chrome is spawned — this stays in the deterministic tier.
    """
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: M.server.run(server_read, server_write, M._init_options()))
            async with ClientSession(client_read, client_write) as client:
                result = await client.initialize()
                instr = result.instructions or ""
                assert instr, "InitializeResult carried no instructions over the wire"
                assert "WebFetch" in instr
                assert "explore" in instr
                assert "DO NOT report failure" in instr
            tg.cancel_scope.cancel()
