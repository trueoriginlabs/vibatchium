"""Wave 6.3d — vision-first primitive tests.

Verifies:
- find_element returns coords from a mocked Claude call
- Cache hit on second identical call skips the API (mock not called)
- Low confidence raises VisionLowConfidence
- Rate limiter raises after N calls/minute
- estimate_cost_usd computes Haiku pricing
- Cache clear removes all entries
"""
from __future__ import annotations

import time
from collections import deque

import pytest

from vibatchium.vision import (
    VisionLowConfidence, VisionRateLimited,
    cache_clear, cache_get, cache_put, check_rate_limit,
    estimate_cost_usd, find_element,
)


# ─── pure unit tests ────────────────────────────────────────────────────


def test_estimate_cost():
    # 1M input + 1M output at $1 + $5 = $6
    assert estimate_cost_usd(1_000_000, 1_000_000) == pytest.approx(6.0)
    assert estimate_cost_usd(500, 200) == pytest.approx(
        500 / 1_000_000 + 200 / 1_000_000 * 5
    )


def test_rate_limit_under_threshold_ok():
    log = deque()
    for _ in range(5):
        check_rate_limit(log, max_per_minute=10)
    assert len(log) == 5


def test_rate_limit_at_threshold_raises():
    log = deque()
    for _ in range(3):
        check_rate_limit(log, max_per_minute=3)
    with pytest.raises(VisionRateLimited):
        check_rate_limit(log, max_per_minute=3)


def test_rate_limit_evicts_old_entries():
    log = deque()
    log.append(time.time() - 120)  # 2 minutes old
    log.append(time.time() - 90)
    # Old entries evicted on next check
    check_rate_limit(log, max_per_minute=3)
    assert len(log) == 1  # only the new entry


# ─── cache round-trip ───────────────────────────────────────────────────


def test_cache_roundtrip(tmp_path, monkeypatch):
    from vibatchium import vision as _vision
    # Redirect cache to tmp
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")
    img = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # fake PNG
    cache_put(img, "increment button",
              {"x": 100, "y": 200, "confidence": 0.9, "rationale": "test"})
    got = cache_get(img, "increment button")
    assert got is not None
    assert got["x"] == 100 and got["y"] == 200
    # Different intent → cache miss
    assert cache_get(img, "different intent") is None


def test_cache_ttl_expires(tmp_path, monkeypatch):
    from vibatchium import vision as _vision
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")
    img = b"\x89PNG" + b"\x01" * 50
    cache_put(img, "intent", {"x": 1, "y": 2, "confidence": 0.9, "rationale": ""})
    # Default TTL: 7 days. Force expire with 0s.
    assert cache_get(img, "intent", ttl_s=0) is None


def test_cache_clear(tmp_path, monkeypatch):
    from vibatchium import vision as _vision
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")
    cache_put(b"a", "x", {"x": 1, "y": 1, "confidence": 0.9, "rationale": ""})
    cache_put(b"b", "y", {"x": 1, "y": 1, "confidence": 0.9, "rationale": ""})
    n = cache_clear()
    assert n == 2
    assert cache_get(b"a", "x") is None


# ─── find_element with mocked Claude ────────────────────────────────────


class _FakePage:
    """Stand-in for Playwright Page — supports screenshot() + evaluate()."""
    def __init__(self, screenshot_bytes: bytes, dpr: float = 1.0):
        self._png = screenshot_bytes
        self._dpr = dpr
    async def screenshot(self, *, type='png'):
        return self._png
    async def evaluate(self, expr):
        if "devicePixelRatio" in expr:
            return self._dpr
        return None


@pytest.mark.asyncio
async def test_find_element_uses_mocked_claude(tmp_path, monkeypatch):
    from vibatchium import vision as _vision
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")

    calls = []
    async def fake_locate(png, desc):
        calls.append(desc)
        return {"x": 250, "y": 100, "confidence": 0.95,
                "rationale": "blue button bottom-left",
                "tokens": {"input": 1000, "output": 50}}

    page = _FakePage(b"\x89PNG_screenshot_one")
    result = await find_element(page, "the submit button",
                                 _claude_locate=fake_locate)
    assert calls == ["the submit button"]
    assert result["x"] == 250 and result["y"] == 100
    assert result["via"] == "vision"
    assert result["cost_usd"] > 0


@pytest.mark.asyncio
async def test_find_element_cache_hit_skips_call(tmp_path, monkeypatch):
    from vibatchium import vision as _vision
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")

    calls = []
    async def fake_locate(png, desc):
        calls.append(desc)
        return {"x": 50, "y": 50, "confidence": 0.9, "rationale": "",
                "tokens": {"input": 100, "output": 10}}

    page = _FakePage(b"identical_png")
    # First call hits the mock
    await find_element(page, "intent x", _claude_locate=fake_locate)
    # Second call (same screenshot, same intent) → cache hit
    result2 = await find_element(page, "intent x", _claude_locate=fake_locate)
    assert len(calls) == 1, "second call should have hit cache"
    assert result2["via"] == "cache"
    assert result2["cost_usd"] == 0


@pytest.mark.asyncio
async def test_find_element_low_confidence_raises(tmp_path, monkeypatch):
    from vibatchium import vision as _vision
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")
    async def fake_locate(png, desc):
        return {"x": 0, "y": 0, "confidence": 0.3, "rationale": "uncertain",
                "tokens": {"input": 100, "output": 10}}
    page = _FakePage(b"low_conf_png")
    with pytest.raises(VisionLowConfidence):
        await find_element(page, "vague", min_confidence=0.6,
                            _claude_locate=fake_locate)


@pytest.mark.asyncio
async def test_find_element_rate_limit_raises(tmp_path, monkeypatch):
    from vibatchium import vision as _vision
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")
    async def fake_locate(png, desc):
        return {"x": 1, "y": 1, "confidence": 0.9, "rationale": "",
                "tokens": {"input": 100, "output": 10}}
    page = _FakePage(b"pp")
    log = deque()
    # Under limit OK
    await find_element(page, "i1", cache_log=log, max_per_minute=2,
                       _claude_locate=fake_locate)
    await find_element(page, "i2", cache_log=log, max_per_minute=2,
                       _claude_locate=fake_locate)
    with pytest.raises(VisionRateLimited):
        await find_element(page, "i3", cache_log=log, max_per_minute=2,
                           _claude_locate=fake_locate)


@pytest.mark.asyncio
async def test_find_element_dpr_scaling(tmp_path, monkeypatch):
    from vibatchium import vision as _vision
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")
    async def fake_locate(png, desc):
        return {"x": 200, "y": 100, "confidence": 0.95, "rationale": "",
                "tokens": {"input": 100, "output": 10}}
    page = _FakePage(b"retina", dpr=2.0)
    res = await find_element(page, "x", _claude_locate=fake_locate)
    assert res["devicePixelRatio"] == 2.0
    # Caller scales the click coords by dpr themselves


# ─── Wave 7.2: cost cap tests ──────────────────────────────────────────


@pytest.fixture
def _isolate_spend(tmp_path, monkeypatch):
    """Redirect both the vision cache AND the spend log to tmp + clear env caps."""
    from vibatchium import vision as _vision
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")
    monkeypatch.setattr(_vision, "_spend_path", lambda: tmp_path / "vs.json")
    monkeypatch.delenv("VIBATCHIUM_VISION_MAX_DAILY_USD", raising=False)
    monkeypatch.delenv("VIBATCHIUM_VISION_MAX_LIFETIME_USD", raising=False)
    return tmp_path


def test_spend_persists_across_loads(_isolate_spend):
    from vibatchium.vision import add_spend, get_today_spend, get_lifetime_spend
    assert get_today_spend() == 0.0
    add_spend(0.001)
    add_spend(0.002)
    assert get_today_spend() == pytest.approx(0.003)
    assert get_lifetime_spend() == pytest.approx(0.003)


def test_no_caps_no_gate(_isolate_spend):
    from vibatchium.vision import check_budget
    # Without env vars set, check_budget never raises
    snap = check_budget()
    assert snap["daily_cap"] is None
    assert snap["lifetime_cap"] is None


def test_daily_cap_blocks_when_exceeded(_isolate_spend, monkeypatch):
    from vibatchium.vision import add_spend, check_budget, VisionBudgetExceeded
    monkeypatch.setenv("VIBATCHIUM_VISION_MAX_DAILY_USD", "0.01")
    # Push spend up close to cap
    add_spend(0.009)
    # Estimate 0.005 → 0.009 + 0.005 > 0.01 → raise
    with pytest.raises(VisionBudgetExceeded, match="daily"):
        check_budget(estimate_usd=0.005)
    # Smaller estimate that fits in remaining budget → OK
    snap = check_budget(estimate_usd=0.0005)
    assert snap["today"] == pytest.approx(0.009)


def test_lifetime_cap_blocks_when_exceeded(_isolate_spend, monkeypatch):
    from vibatchium.vision import add_spend, check_budget, VisionBudgetExceeded
    monkeypatch.setenv("VIBATCHIUM_VISION_MAX_LIFETIME_USD", "0.10")
    add_spend(0.099)
    with pytest.raises(VisionBudgetExceeded, match="lifetime"):
        check_budget(estimate_usd=0.005)


def test_reset_spend_scopes(_isolate_spend):
    from vibatchium.vision import add_spend, reset_spend, get_today_spend, get_lifetime_spend
    add_spend(0.05)
    # reset today only
    reset_spend(scope="today")
    assert get_today_spend() == 0.0
    assert get_lifetime_spend() == pytest.approx(0.05)
    # reset lifetime
    reset_spend(scope="lifetime")
    assert get_lifetime_spend() == 0.0
    # reset all
    add_spend(0.03)
    reset_spend(scope="all")
    assert get_today_spend() == 0.0
    assert get_lifetime_spend() == 0.0


@pytest.mark.asyncio
async def test_find_element_enforces_budget(_isolate_spend, monkeypatch):
    """When the daily cap is set and exceeded, find_element refuses the call
    AND never invokes the Claude mock."""
    from vibatchium.vision import add_spend, find_element, VisionBudgetExceeded
    monkeypatch.setenv("VIBATCHIUM_VISION_MAX_DAILY_USD", "0.01")
    add_spend(0.009)
    calls = []
    async def fake_locate(png, desc):
        calls.append(desc)
        return {"x": 1, "y": 1, "confidence": 0.95, "rationale": "",
                "tokens": {"input": 100, "output": 10}}
    page = _FakePage(b"budget_test_png")
    with pytest.raises(VisionBudgetExceeded):
        await find_element(page, "intent", _claude_locate=fake_locate)
    # Claude mock NEVER called because budget gate fires first
    assert calls == []


@pytest.mark.asyncio
async def test_find_element_increments_spend_on_success(_isolate_spend):
    from vibatchium.vision import find_element, get_today_spend
    before = get_today_spend()
    async def fake_locate(png, desc):
        return {"x": 1, "y": 1, "confidence": 0.95, "rationale": "",
                "tokens": {"input": 1500, "output": 50}}
    page = _FakePage(b"spend_increments")
    await find_element(page, "intent", _claude_locate=fake_locate)
    after = get_today_spend()
    # 1500 input * $1/M + 50 output * $5/M = 0.0015 + 0.00025 = 0.00175
    assert after - before == pytest.approx(0.00175, abs=0.0001)


def test_vision_budget_handler_reports_caps():
    """End-to-end: vision_budget handler returns the snapshot the user expects.

    Note: env vars set in the test process don't propagate to the
    already-spawned daemon. We just verify the handler runs and returns
    the expected response shape.
    """
    from vibatchium.client import call as _call
    res = _call("vision_budget")
    assert "today_usd" in res
    assert "lifetime_usd" in res
    assert "daily_cap_usd" in res  # may be None on daemon side
    assert "lifetime_cap_usd" in res
