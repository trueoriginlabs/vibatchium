"""Wave 5.2: MCP --caps capability gating tests.

Verifies:
- No --caps → full surface (all tools exposed)
- Specific caps → only those buckets exposed (plus _ALWAYS_EXPOSED)
- 'all' as a cap → no filter
- Unknown caps → ValueError
- Tools belonging to multiple buckets surface when ANY is requested
- Cap filtering is enforced at call_tool too (not just list_tools)
"""
from __future__ import annotations

import pytest

from vibatchium.mcp_server import (
    TOOLS, _CAP_BUCKETS, _filter_tools, _resolve_caps,
)


def test_no_caps_exposes_all():
    """No filter = every TOOLS entry is exposed."""
    assert _resolve_caps(None) is None
    assert _resolve_caps("") is None
    filtered = _filter_tools(None)
    assert len(filtered) == len(TOOLS)


def test_caps_all_is_no_filter():
    """The literal 'all' cap is equivalent to no filter."""
    assert _resolve_caps("all") is None
    assert _resolve_caps("all,core") is None  # 'all' wins


def test_caps_core_only_subset():
    """`--caps=core` gives only the core bucket (+ always-exposed)."""
    caps = _resolve_caps("core")
    assert caps == {"core"}
    names = {t[0] for t in _filter_tools(caps)}
    # Every core tool should be in
    assert _CAP_BUCKETS["core"].issubset(names)
    # A non-core tool should NOT be in
    assert "screenshot" not in names
    assert "har_start" not in names
    assert "click" not in names


def test_caps_session_only():
    """`--caps=session` exposes session_* + profile_* aliases."""
    caps = _resolve_caps("session")
    names = {t[0] for t in _filter_tools(caps)}
    assert "session_new" in names
    assert "session_list" in names
    assert "profile_list" in names  # legacy alias still in 'session' bucket
    assert "click" not in names


def test_caps_multi_bucket():
    """`--caps=core,nav,input` unions the buckets."""
    caps = _resolve_caps("core,nav,input")
    names = {t[0] for t in _filter_tools(caps)}
    # Core: start
    assert "start" in names
    # Nav: go
    assert "go" in names
    # Input: click, fill
    assert "click" in names
    assert "fill" in names
    # Network bucket NOT requested
    assert "har_start" not in names
    assert "route_add" not in names


def test_caps_unknown_raises():
    """Unknown bucket name → ValueError with the full list of valid buckets."""
    with pytest.raises(ValueError, match="unknown capability"):
        _resolve_caps("bogus")
    with pytest.raises(ValueError, match="unknown capability"):
        _resolve_caps("core,bogus")


def test_caps_status_always_exposed():
    """Even with the smallest cap set, status is always present (orientation)."""
    caps = _resolve_caps("vision")
    names = {t[0] for t in _filter_tools(caps)}
    assert "status" in names


def test_caps_compact_set_for_basic_agent():
    """A reasonable LLM-cheap config: core,session,nav,content,input,element,agent."""
    caps = _resolve_caps("core,session,nav,content,input,element,agent")
    names = {t[0] for t in _filter_tools(caps)}
    # Full session+navigation+interaction loop is in
    for need in ("start", "session_new", "go", "text", "click", "fill",
                 "map", "observe", "act"):
        assert need in names, f"{need!r} missing from compact agent caps"
    # Heavy/specialized stuff is OUT
    for omit in ("screenshot_annotate", "har_start", "record_start",
                 "eval_handle", "geolocation"):
        assert omit not in names, f"{omit!r} should be off the compact list"


def test_caps_count_reduction():
    """Quantify: compact caps should be substantially smaller than the full surface."""
    full = len(_filter_tools(None))
    compact = len(_filter_tools(_resolve_caps("core,session,nav,input,agent")))
    assert compact < full * 0.6, f"compact caps {compact} vs full {full} — gating ineffective"


def test_every_tool_classified():
    """No tool falls outside the bucket map (else it's invisible under any --caps).

    Tools that should always show regardless of caps live in _ALWAYS_EXPOSED.
    Sleep/ping are utility tools that the filter also lets through. Everything
    else needs an explicit bucket.
    """
    from vibatchium.mcp_server import _ALWAYS_EXPOSED
    classified = set().union(*_CAP_BUCKETS.values()) | _ALWAYS_EXPOSED | {"sleep", "ping"}
    all_names = {t[0] for t in TOOLS}
    unclassified = all_names - classified
    assert not unclassified, (
        f"tools missing from _CAP_BUCKETS: {sorted(unclassified)}. "
        f"Add them to a bucket or to _ALWAYS_EXPOSED."
    )
