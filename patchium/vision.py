"""Wave 6.3d — vision-first primitive.

`vision_click "the blue submit button"`: bypass the AX-tree entirely. Take
a screenshot, ask Claude (Haiku 4.5 vision) for pixel coordinates of the
described element, click them. The fallback for pages where the AX-tree
is useless (Figma, Tldraw, Flutter web, Unity WebGL).

Flow:
  1. `page.screenshot()`
  2. anthropic.messages.create(model="claude-haiku-4-5-...", ...) with
     image + description, returns JSON `{x, y, confidence}`
  3. Validate `confidence >= min_confidence` (default 0.6); else error
  4. Apply devicePixelRatio scaling
  5. `page.mouse.click(x, y)` (or just return for `vision_find`)

Caching:
  - Key: sha256(screenshot_bytes)[:16] + sha256(intent)[:16]
  - Value: `{x, y, confidence, ts}`
  - Stored in `~/.cache/patchium/vision-cache.json` (TTL: 1 week)
  - Repeat visits on identical pages skip the API call entirely.

Rate-limiting:
  - Per-session counter in `entry.flags['vision_rate']`
  - Default 30 calls/minute; sliding window
  - Raises VisionRateLimited on excess

Cost tracking:
  - Per-session total input + output tokens stashed on entry.flags['vision_stats']
  - `vision_stats` handler returns the running total + estimated $

Requires `[llm]` extra (`anthropic` SDK). Clean error without.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

log = logging.getLogger("patchium.vision")


# Pricing as of 2026-05 (per million tokens), Haiku 4.5
HAIKU_INPUT_PRICE_USD_PER_M = 1.00
HAIKU_OUTPUT_PRICE_USD_PER_M = 5.00


class VisionRateLimited(RuntimeError):
    pass


class VisionLowConfidence(RuntimeError):
    pass


def _cache_path() -> Path:
    from .daemon.paths import CACHE_DIR
    return CACHE_DIR / "vision-cache.json"


def _cache_key(screenshot_bytes: bytes, intent: str) -> str:
    sh = hashlib.sha256(screenshot_bytes).hexdigest()[:16]
    ih = hashlib.sha256(intent.encode()).hexdigest()[:16]
    return f"{sh}-{ih}"


def cache_load() -> dict:
    p = _cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def cache_save(data: dict) -> None:
    _cache_path().write_text(json.dumps(data))


def cache_get(screenshot_bytes: bytes, intent: str,
              ttl_s: int = 7 * 24 * 3600) -> dict | None:
    data = cache_load()
    key = _cache_key(screenshot_bytes, intent)
    entry = data.get(key)
    if entry and time.time() - entry.get("ts", 0) < ttl_s:
        return entry
    return None


def cache_put(screenshot_bytes: bytes, intent: str, value: dict) -> None:
    data = cache_load()
    key = _cache_key(screenshot_bytes, intent)
    data[key] = {**value, "ts": time.time()}
    cache_save(data)


def cache_clear() -> int:
    data = cache_load()
    n = len(data)
    _cache_path().unlink(missing_ok=True)
    return n


# ─── rate limiter ──────────────────────────────────────────────────────


def check_rate_limit(call_log: deque, *, max_per_minute: int = 30) -> None:
    """Sliding-window rate limiter. Mutates the deque in place — caller
    typically stashes it on `entry.flags['vision_rate']`."""
    now = time.time()
    while call_log and call_log[0] < now - 60:
        call_log.popleft()
    if len(call_log) >= max_per_minute:
        raise VisionRateLimited(
            f"vision rate limit hit: {max_per_minute} calls/minute. "
            f"Wait or raise the limit."
        )
    call_log.append(now)


# ─── Claude vision call ────────────────────────────────────────────────


_VISION_SYSTEM = """\
You analyze a screenshot and return the pixel coordinates of a described UI element.

Respond ONLY with a single JSON object on one line, no markdown fences:
{"x": <int>, "y": <int>, "confidence": <float 0..1>, "rationale": "<one sentence>"}

- (x, y) is the CENTER of the element, relative to the image's top-left.
- confidence reflects how certain you are the element matches the description.
- If the element is not visible, return confidence: 0.
"""


async def claude_locate(screenshot_png: bytes, description: str,
                        *, model: str = "claude-haiku-4-5-20251001") -> dict:
    """Send screenshot + description to Claude, parse the JSON response.

    Returns `{x, y, confidence, rationale, tokens: {input, output}}`.
    Raises RuntimeError if anthropic SDK not installed or API call fails.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "vision requires `pip install patchium[llm]` (anthropic SDK). "
            f"(import error: {exc})"
        ) from exc
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("vision requires ANTHROPIC_API_KEY")

    client = anthropic.Anthropic(api_key=api_key)
    img_b64 = base64.b64encode(screenshot_png).decode()
    # Run the blocking call in a worker thread; Anthropic SDK is sync.
    def _call_sync():
        resp = client.messages.create(
            model=model, max_tokens=300, system=_VISION_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64",
                                "media_type": "image/png",
                                "data": img_b64}},
                    {"type": "text",
                     "text": f"Find: {description}"},
                ],
            }],
        )
        return resp
    resp = await asyncio.to_thread(_call_sync)
    text = resp.content[0].text  # type: ignore[attr-defined]
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    data = json.loads(text)
    tokens = getattr(resp, "usage", None)
    return {
        "x": int(data["x"]),
        "y": int(data["y"]),
        "confidence": float(data.get("confidence", 0)),
        "rationale": data.get("rationale", ""),
        "tokens": {
            "input": getattr(tokens, "input_tokens", 0) if tokens else 0,
            "output": getattr(tokens, "output_tokens", 0) if tokens else 0,
        },
    }


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Estimate cost for an input/output token pair at Haiku 4.5 pricing."""
    return (input_tokens / 1_000_000) * HAIKU_INPUT_PRICE_USD_PER_M + \
           (output_tokens / 1_000_000) * HAIKU_OUTPUT_PRICE_USD_PER_M


# ─── high-level vision_find (the orchestrator) ─────────────────────────


async def find_element(page, intent: str, *,
                        min_confidence: float = 0.6,
                        cache_log: deque | None = None,
                        max_per_minute: int = 30,
                        use_cache: bool = True,
                        _claude_locate=None) -> dict:
    """Return coordinates for `intent` on the current page's screenshot.

    Caches by (screenshot hash, intent). On cache hit, no API call. On
    cache miss, calls Claude, caches the result if confidence >= threshold.

    Returns `{x, y, confidence, rationale, via, devicePixelRatio, cost_usd}`.
    Raises VisionRateLimited / VisionLowConfidence on the obvious failures.

    `_claude_locate` is for testing — pass a coroutine to replace the API call.
    """
    if cache_log is not None:
        check_rate_limit(cache_log, max_per_minute=max_per_minute)

    screenshot_bytes = await page.screenshot(type='png')

    # Try cache
    if use_cache:
        cached = cache_get(screenshot_bytes, intent)
        if cached and cached.get("confidence", 0) >= min_confidence:
            try:
                dpr = await page.evaluate("() => window.devicePixelRatio || 1")
            except Exception:  # noqa: BLE001
                dpr = 1
            return {
                "x": cached["x"], "y": cached["y"],
                "confidence": cached["confidence"],
                "rationale": cached.get("rationale", ""),
                "via": "cache", "devicePixelRatio": dpr, "cost_usd": 0.0,
            }

    # Cache miss → Claude
    locator = _claude_locate or claude_locate
    result = await locator(screenshot_bytes, intent)
    if result["confidence"] < min_confidence:
        raise VisionLowConfidence(
            f"low confidence {result['confidence']:.2f} for {intent!r}: "
            f"{result.get('rationale', '')}"
        )
    cache_put(screenshot_bytes, intent, {
        "x": result["x"], "y": result["y"],
        "confidence": result["confidence"],
        "rationale": result.get("rationale", ""),
    })
    cost = estimate_cost_usd(
        result["tokens"]["input"], result["tokens"]["output"],
    )
    try:
        dpr = await page.evaluate("() => window.devicePixelRatio || 1")
    except Exception:  # noqa: BLE001
        dpr = 1
    return {
        "x": result["x"], "y": result["y"],
        "confidence": result["confidence"],
        "rationale": result.get("rationale", ""),
        "via": "vision", "devicePixelRatio": dpr, "cost_usd": cost,
        "tokens": result["tokens"],
    }
