"""`--caps min` — the smallest MCP surface, for clients that load every tool
schema up front (no tool search).

The resolution tests pin the exact set (each verb is justified in caps.py; a
change here should be a decision, not drift). The handshake test drives the real
`vb mcp --caps min` over stdio — initialize → notifications/initialized →
tools/list — and measures what a client actually pays per turn.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

from vibatchium import caps as C
from vibatchium import mcp_server as M

MIN_TOOLS = {"explore", "go", "extract", "screenshot", "act", "map", "click",
             "fill", "press", "expect", "session_close", "status"}


# ─── resolution ─────────────────────────────────────────────────────────


def test_min_is_a_named_profile():
    assert "min" in C.CAP_PROFILES
    assert C.resolve_caps("min") == {"min"}
    assert C.resolve_caps("MIN") == {"min"}


def test_min_exposes_exactly_the_justified_set():
    names = {t[0] for t in M._filter_tools(C.resolve_caps("min"))}
    assert names == MIN_TOOLS
    assert len(names) <= 12


def test_min_is_a_subset_of_lean():
    # Nothing in min is an opt-in lane: a client moving min → lean only gains.
    lean = {t[0] for t in M._filter_tools(C.resolve_caps("lean"))}
    assert MIN_TOOLS <= lean


def test_min_covers_the_always_loaded_core():
    # The tool-search clients' always-loaded core must be a subset — a client
    # without tool search shouldn't get LESS than one with it.
    assert set(M._ALWAYS_LOAD) <= MIN_TOOLS


def test_every_min_verb_is_a_real_tool():
    assert C.MIN_VERBS <= {t[0] for t in M.TOOLS}


def test_min_composes_with_other_buckets():
    names = {t[0] for t in M._filter_tools(C.resolve_caps("min,search"))}
    assert names == MIN_TOOLS | {"search"}


def test_min_gates_call_tool_like_any_profile():
    caps = C.resolve_caps("min")
    assert C.verb_in_caps("fill", caps)
    assert C.verb_in_caps("status", caps)
    assert not C.verb_in_caps("eval", caps)
    assert not C.verb_in_caps("start", caps)
    assert not C.verb_in_caps("x.search", caps)


def test_min_instructions_name_no_absent_tool():
    txt = M._build_instructions(C.resolve_caps("min"))
    assert txt and "explore(url)" in txt
    # session_lease isn't in min, so the concurrency advice must not name it...
    assert "session_lease" not in txt
    # ...while lean, which has it, still gets the full advice.
    assert "session_lease" in M._build_instructions(C.resolve_caps("lean"))


# Tool names that are also ordinary English words — prose may use them ("page
# text", "the URL", "back"), but never as a CALL: not in backticks, not as
# `name(`, not inside a slash-list like extract/text/html.
_ENGLISH_TOOL_NAMES = {"back", "check", "clean", "content", "count", "fetch",
                       "pages", "scroll", "start", "stop", "text", "type", "url",
                       "value", "viewport", "find", "title", "select", "focus",
                       "reload", "forward", "frame", "keys", "media", "search"}


def _descriptions(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "description" and isinstance(v, str):
                yield v
            else:
                yield from _descriptions(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _descriptions(v)


def test_min_tool_text_names_no_absent_tool():
    # Best-effort scan: a --caps min client must never be pointed at a tool it
    # doesn't have (`candidates`, `dismiss_banners`, `go` → "pair with
    # extract/text/html/map" were the review's examples).
    import re
    caps = C.resolve_caps("min")
    tools = M._filter_tools(caps)
    exposed = {t[0] for t in tools}
    absent = {t[0] for t in M.TOOLS} - exposed
    chunks = [M._build_instructions(caps)]
    for t in tools:
        chunks.append(t[1])
        chunks += list(_descriptions(M._augment_schema_with_session(t[2])))
    txt = "\n".join(chunks)
    names = {t[0] for t in M.TOOLS}
    # a slash-list of TOOL names ("extract/text/html/map"), not prose
    # ("text/selectors")
    slash_lists = [set(m.split("/")) for m in
                   re.findall(r"[A-Za-z_]+(?:/[A-Za-z_]+)+", txt)]
    hits = set()
    for w in set(re.findall(r"[A-Za-z_]+", txt)) & absent:
        if w not in _ENGLISH_TOOL_NAMES:
            hits.add(w)
            continue
        w_ = re.escape(w)
        if re.search(rf"`{w_}`|\b{w_}\(", txt) or any(
                w in grp and len(grp & names) > 1 for grp in slash_lists):
            hits.add(w)
    assert not hits, f"min tool text names absent tools: {sorted(hits)}"


def test_setup_registers_min(tmp_path, monkeypatch):
    # `vb setup --caps min` validates through the same resolver and registers
    # `vb mcp --caps min` verbatim.
    import shutil
    from pathlib import Path
    from vibatchium import setup_cmd
    from tests.test_setup_cmd import _capture_mcp_add
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which",
                        lambda n: "/fake/claude" if n == "claude" else None)
    calls = _capture_mcp_add(monkeypatch)
    res = setup_cmd.setup_claude("/opt/vb", dry_run=False, write_docs=False,
                                 caps="min")
    assert res.mcp == "registered"
    add = next(c for c in calls if "add" in c)
    assert add[-3:] == ["mcp", "--caps", "min"]
    # ...and the top-level entry accepts it (an unknown name raises CapsError
    # before anything is written).
    assert setup_cmd.run_setup([], dry_run=True, write_docs=False,
                               caps="min")["caps"] == "min"
    with pytest.raises(C.CapsError):
        setup_cmd.run_setup([], dry_run=True, write_docs=False, caps="mini")


# ─── the real stdio handshake ───────────────────────────────────────────


def _handshake(caps: str) -> dict:
    """Run `vb mcp --caps <caps>` and return {tools, tools_bytes, instr_bytes}.

    Private HOME + XDG_RUNTIME_DIR: list_tools only talks to a daemon when the
    plugins bucket is on (it isn't for min/lean), but the env is isolated anyway
    so this can never reach a shared daemon.
    """
    env = dict(os.environ)
    env["HOME"] = tempfile.mkdtemp(prefix="vbcaps-home-")
    env["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="vbcaps-rt-")
    env.pop("XDG_CONFIG_HOME", None)
    proc = subprocess.Popen(
        [sys.executable, "-c", "from vibatchium.cli import main; main()",
         "mcp", "--caps", caps],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, text=True)

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv(want_id):
        while True:
            line = proc.stdout.readline()
            assert line, "MCP server closed stdout"
            msg = json.loads(line)
            if msg.get("id") == want_id:
                return line, msg

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "pytest", "version": "1"}}})
        _, init = recv(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        raw, listed = recv(2)
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    tools = listed["result"]["tools"]
    instr = init["result"].get("instructions") or ""
    return {"tools": [t["name"] for t in tools],
            # the tools array as the client receives it (what it puts in context)
            "tools_bytes": len(json.dumps(tools, separators=(",", ":"))),
            "raw_bytes": len(raw.encode()),
            "instr_bytes": len(instr.encode())}


@pytest.mark.timeout(60)
def test_min_handshake_lists_twelve_tools_at_a_fraction_of_lean():
    mn = _handshake("min")
    lean = _handshake("lean")
    assert set(mn["tools"]) == MIN_TOOLS
    assert len(lean["tools"]) > 4 * len(mn["tools"])
    # ~tokens = bytes/4. min must be a real diet, not a rounding error.
    assert mn["tools_bytes"] * 4 < lean["tools_bytes"], (mn, lean)
    print(f"\n[caps footprint] min: {len(mn['tools'])} tools, "
          f"{mn['tools_bytes']} B (~{mn['tools_bytes'] // 4} tok); "
          f"lean: {len(lean['tools'])} tools, {lean['tools_bytes']} B "
          f"(~{lean['tools_bytes'] // 4} tok); instructions min/lean "
          f"{mn['instr_bytes']}/{lean['instr_bytes']} B")
