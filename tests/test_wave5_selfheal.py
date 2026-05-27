"""Wave 5.3: self-healing selector cache tests.

Verifies:
- Heuristic plan steps include `_durable` (role+name selector) for self-heal
- `observe` enriches LLM-style plans with durable info by snapshot lookup
- `act` on cache hit prefers the durable selector → `via: durable`
- `act` falls back to @eN via re-observe when durable fails (`self_healed: true`)
- `cache_invalidate` removes a single (url, intent) entry
- `build_durable_selector` produces well-formed Playwright role/text selectors
"""
from __future__ import annotations

import pytest

from vibatchium.client import call, DaemonError
from vibatchium.daemon.observe import (
    build_durable_selector,
    cache_invalidate,
    cache_get,
)
from vibatchium.daemon.paths import PROFILES_DIR


def _ensure_clean(name: str) -> None:
    """Close + remove a session if it lingers from a prior test failure."""
    import shutil
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    p = PROFILES_DIR / name
    if p.exists():
        try:
            shutil.rmtree(p)
        except Exception:  # noqa: BLE001
            pass


# ─── pure helpers (no daemon needed) ─────────────────────────────────────


def test_build_durable_selector_role_and_name():
    assert build_durable_selector("button", "Submit") == 'role=button[name="Submit"]'


def test_build_durable_selector_escapes_quotes():
    assert build_durable_selector("link", 'click "here"') == 'role=link[name="click \\"here\\""]'


def test_build_durable_selector_text_fallback():
    assert build_durable_selector(None, "Submit") == 'text="Submit"'


def test_build_durable_selector_none_when_nameless():
    assert build_durable_selector("button", None) is None
    assert build_durable_selector(None, None) is None


# ─── live daemon tests ───────────────────────────────────────────────────


def test_observe_plan_includes_durable_metadata(local_server):
    """Heuristic plan steps should carry _role, _name, _durable."""
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("observe", {"intent": "click the Increment button", "force": True})
    assert res["plan"], "expected at least one step"
    step = res["plan"][0]
    assert step.get("_role") == "button"
    assert "Increment" in (step.get("_name") or "")
    assert step.get("_durable", "").startswith("role=button[name=")


def test_act_cache_hit_uses_durable(local_server):
    """Second act on the same intent should prefer durable selector."""
    call("go", {"url": f"{local_server}/simple.html"})
    # prime the cache
    res1 = call("act", {"intent": "press the Increment button"})
    # second call — should be cached, use durable
    res2 = call("act", {"intent": "press the Increment button"})
    assert res2["steps"][0].get("via") in {"durable", "ref"}
    # On cache hit, when durable exists, we expect 'durable'. The role-based
    # selector should resolve `Increment` reliably.
    if res2["steps"][0].get("via") != "durable":
        pytest.fail(
            f"expected durable on cache hit, got {res2['steps'][0].get('via')!r}; "
            f"step={res2['steps'][0]['step']}"
        )


def test_act_self_heals_when_durable_breaks(local_server):
    """If the cached durable selector no longer matches, act re-observes.

    Uses the default session and mutates a button's accessible name in-place
    so the cached durable role-selector no longer matches. Validates that
    `act` returns `self_healed=True` and the cache was invalidated.
    """
    call("go", {"url": f"{local_server}/simple.html"})
    # Prime cache for an intent
    call("act", {"intent": "press the Increment button"})
    url = call("url")["url"]
    assert cache_get(url, "press the Increment button") is not None
    # Mutate the button text so role=button[name="Increment"] no longer resolves
    call("eval", {
        "expr": "document.getElementById('counter-btn').textContent = 'XyzNeverMatch'"
    })
    # Re-act with the same intent — durable fails, self-heal triggers
    res = call("act", {"intent": "press the Increment button"})
    assert res.get("self_healed") is True, \
        f"expected self_healed=True; got {res}"
    # cache should be either empty (re-observe found nothing matching 'Increment')
    # OR replaced with a fresh entry (whatever new selector applies)
    new_cached = cache_get(url, "press the Increment button")
    # if there's a new cache entry, it should not still claim the durable for
    # the old name (since the page changed)
    if new_cached is not None:
        for step in new_cached.get("plan", []):
            assert step.get("_name") != "Increment ", \
                "stale 'Increment' name persisted after self-heal"


def test_cache_invalidate_removes_single_entry():
    """cache_invalidate drops only the targeted (url, intent) row.

    Pure-function test — talks to the on-disk cache directly via observe.cache_put
    so it doesn't depend on the daemon's page state (which can be polluted by
    previous act tests).
    """
    from vibatchium.daemon.observe import cache_put
    url = "http://test.local/cache_invalidate_demo"
    intent_a = "click increment ZZZ"
    intent_b = "press submit ZZZ"
    plan_stub = [{"verb": "click", "target": "@e1"}]
    cache_put(url, intent_a, {"intent": intent_a, "url": url, "plan": plan_stub,
                              "source": "test"})
    cache_put(url, intent_b, {"intent": intent_b, "url": url, "plan": plan_stub,
                              "source": "test"})
    assert cache_get(url, intent_a) is not None
    assert cache_get(url, intent_b) is not None
    # invalidate one
    removed = cache_invalidate(url, intent_a)
    assert removed is True
    assert cache_get(url, intent_a) is None
    # other intent untouched
    assert cache_get(url, intent_b) is not None
    # second invalidate of same entry returns False (already gone)
    assert cache_invalidate(url, intent_a) is False
    # cleanup the other test entry
    cache_invalidate(url, intent_b)
