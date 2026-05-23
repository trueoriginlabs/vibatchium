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

from patchium.vision import (
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
    from patchium import vision as _vision
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
    from patchium import vision as _vision
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")
    img = b"\x89PNG" + b"\x01" * 50
    cache_put(img, "intent", {"x": 1, "y": 2, "confidence": 0.9, "rationale": ""})
    # Default TTL: 7 days. Force expire with 0s.
    assert cache_get(img, "intent", ttl_s=0) is None


def test_cache_clear(tmp_path, monkeypatch):
    from patchium import vision as _vision
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
    from patchium import vision as _vision
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
    from patchium import vision as _vision
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
    from patchium import vision as _vision
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
    from patchium import vision as _vision
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
    from patchium import vision as _vision
    monkeypatch.setattr(_vision, "_cache_path", lambda: tmp_path / "vc.json")
    async def fake_locate(png, desc):
        return {"x": 200, "y": 100, "confidence": 0.95, "rationale": "",
                "tokens": {"input": 100, "output": 10}}
    page = _FakePage(b"retina", dpr=2.0)
    res = await find_element(page, "x", _claude_locate=fake_locate)
    assert res["devicePixelRatio"] == 2.0
    # Caller scales the click coords by dpr themselves
