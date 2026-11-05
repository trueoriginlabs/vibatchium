"""What made agents stall or report wrong answers, found by running clean
agents and driving the MCP server by hand (2026-11-05).

The unit tests pin the parsing and timing rules. The live tests drive a real
daemon and Chrome against a local server that reproduces each failure.
"""
from __future__ import annotations

import asyncio
import http.server
import shutil
import socketserver
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from vibatchium import caps as caps_mod
from vibatchium import mcp_server as mcp
from vibatchium.daemon import settle
from vibatchium.daemon.action_errors import explain_action_timeout
from vibatchium.daemon.elements import _looks_like_plain_text
from vibatchium.daemon.observe import SnapEntry, heuristic_plan, infer_verb

from .test_integration_daemon import IsolatedDaemon


# ─── settle: the wait after a navigation ─────────────────────────────────
class _Page:
    """Reports the text length the scripted timeline has reached."""

    def __init__(self, timeline, *, hang=False):
        self.timeline = timeline          # [(seconds, chars), ...]
        self.t0 = time.monotonic()
        self.hang = hang

    async def evaluate(self, _js):
        if self.hang:
            await asyncio.sleep(60)
        elapsed = time.monotonic() - self.t0
        return [c for t, c in self.timeline if t <= elapsed][-1]


@pytest.fixture
def fast_quiet(monkeypatch):
    monkeypatch.setattr(settle, "QUIET_S_LONG", 0.08)
    monkeypatch.setattr(settle, "QUIET_S_SHORT", 0.3)
    monkeypatch.setattr(settle, "POLL_S", 0.02)


def _settle(page, **kw):
    return asyncio.run(settle.wait_settled(page, **kw))


def test_settle_waits_out_a_short_plateau(fast_quiet):
    # Officeworks: a level stretch of text, then the results arrive.
    out = _settle(_Page([(0, 2275), (0.2, 7988)]), cap_ms=2000)
    assert out == {"settled": True, "chars": 7988}


def test_settle_does_not_stop_at_the_old_100_char_gate(fast_quiet):
    out = _settle(_Page([(0, 124), (0.1, 1012), (0.2, 5070)]), cap_ms=2000)
    assert out["chars"] == 5070


def test_settle_long_page_returns_on_the_short_window(fast_quiet):
    t0 = time.monotonic()
    assert _settle(_Page([(0, 56_000)]), cap_ms=2000)["settled"]
    assert time.monotonic() - t0 < 0.25


def test_settle_ignores_small_changes(fast_quiet):
    # A ticking clock must not hold the page to the cap.
    timeline = [(i * 0.01, 5000 + i % 2 * 5) for i in range(300)]
    assert _settle(_Page(timeline), cap_ms=2000)["settled"]


def test_settle_reports_a_page_that_never_stops(fast_quiet):
    timeline = [(i * 0.01, 1000 + i * 100) for i in range(300)]
    assert not _settle(_Page(timeline), cap_ms=300)["settled"]


def test_settle_reports_an_empty_page_at_the_cap(fast_quiet):
    assert _settle(_Page([(0, 0)]), cap_ms=200) == {"settled": False, "chars": 0}


def test_settle_survives_a_starved_page(fast_quiet):
    t0 = time.monotonic()
    assert not _settle(_Page([(0, 0)], hang=True), cap_ms=200)["settled"]
    assert time.monotonic() - t0 < 1


def test_settle_waits_for_a_young_request(fast_quiet):
    class Watch:
        def __init__(self):
            self.t0 = time.monotonic()

        def busy(self):
            return time.monotonic() - self.t0 < 0.5

    t0 = time.monotonic()
    assert _settle(_Page([(0, 5000)]), cap_ms=2000, watch=Watch())["settled"]
    assert time.monotonic() - t0 >= 0.5


def test_request_watch_forgets_old_requests(monkeypatch):
    class P:
        def on(self, *_):
            pass

        def remove_listener(self, *_):
            pass

    class R:
        resource_type = "xhr"

    watch = settle.RequestWatch(P())
    request = R()
    watch._started(request)
    assert watch.busy()
    monkeypatch.setattr(settle, "YOUNG_REQUEST_S", 0)
    assert not watch.busy()   # a long poll can't hold every page to the cap


# ─── one-line action errors ──────────────────────────────────────────────
def _log(action: str, *lines: str) -> Exception:
    body = "\n".join(lines)
    return Exception(f"Locator.{action}: Timeout 30000ms exceeded.\nCall log:\n{body}\n")


def test_error_names_the_element_in_the_way():
    reason = explain_action_timeout(_log(
        "click",
        '  - waiting for locator("aria-ref=e187")',
        '    - locator resolved to <button class="addToCart">Add</button>',
        "  - attempting click action",
        '    - <div class="a">first</div> from <div>…</div> subtree intercepts pointer events',
        '    - <div class="drawer">…</div> from <div>…</div> subtree intercepts pointer events',
        "  - retrying click action", ""))
    assert reason.startswith("click timed out after 30000ms: ")
    assert '<div class="drawer">…</div> is covering' in reason
    assert "\n" not in reason


def test_error_sends_a_hidden_radio_to_its_label():
    reason = explain_action_timeout(_log(
        "click",
        '    - locator resolved to <input type="radio" class="sr-only" value="delivery"/>',
        "    - <span>Delivery</span> intercepts pointer events"))
    assert "hidden input" in reason and "label" in reason


def test_error_says_disabled_and_not_visible_and_no_match():
    disabled = explain_action_timeout(_log(
        "click", "    - locator resolved to <button disabled>Go</button>",
        "    - element is not enabled"))
    hidden = explain_action_timeout(_log(
        "fill", "    - locator resolved to <input/>", "    - element is not visible"))
    missing = explain_action_timeout(_log("click", '  - waiting for locator("#nope")'))
    assert "is disabled" in disabled
    assert "not visible" in hidden
    assert 'nothing on the page matched locator("#nope")' in missing


def test_error_reads_a_page_wide_block_as_an_open_menu():
    reason = explain_action_timeout(_log(
        "click", "    - locator resolved to <button>Confirm</button>",
        '    - <html lang="en">…</html> intercepts pointer events'))
    assert "open menu or dialog" in reason


def test_error_points_a_hidden_select_at_the_visible_control():
    reason = explain_action_timeout(_log(
        "select_option", '    - locator resolved to <select aria-hidden="true">…</select>',
        "  - attempting select option action"))
    assert "custom dropdown" in reason


def test_other_errors_keep_their_own_text():
    assert explain_action_timeout(Exception("Page.goto: Timeout 30000ms exceeded.")) is None
    assert explain_action_timeout(ValueError("no such option")) is None


# ─── act's planner ───────────────────────────────────────────────────────
ENTRIES = [
    SnapEntry(ref="@e1", role="textbox", name="Email", raw=""),
    SnapEntry(ref="@e2", role="combobox", name="State", raw=""),
    SnapEntry(ref="@e3", role="option", name="New South Wales", raw=""),
    SnapEntry(ref="@e4", role="checkbox", name="Accept terms", raw=""),
    SnapEntry(ref="@e5", role="button", name="Checkout", raw=""),
]


@pytest.mark.parametrize("intent,value", [
    ("fill the email field with a@b.co", "a@b.co"),
    ("fill email with 'a b'", "a b"),
    ("type a@b.co into the email field", "a@b.co"),
    ("enter a@b.co in email", "a@b.co"),
])
def test_fill_value_is_only_the_value(intent, value):
    step = heuristic_plan(intent, ENTRIES[:1])[0]
    assert (step["verb"], step["text"]) == ("fill", value)


def test_fill_with_no_value_has_none():
    assert "text" not in heuristic_plan("fill the email field", ENTRIES[:1])[0]


def test_uncheck_is_not_check_and_checkout_is_not_check():
    assert infer_verb("uncheck the terms", "checkbox") == "uncheck"
    assert infer_verb("tick the terms", "checkbox") == "check"
    assert infer_verb("go to checkout", "button") == "click"


def test_select_targets_the_dropdown_with_the_option_as_value():
    step = heuristic_plan("select New South Wales in state", ENTRIES)[0]
    assert (step["verb"], step["target"], step["text"]) == ("select", "@e2", "New South Wales")


def test_select_named_by_dropdown():
    step = heuristic_plan("choose Victoria from state", ENTRIES[1:2])[0]
    assert (step["verb"], step["text"]) == ("select", "Victoria")


# ─── target grammar ──────────────────────────────────────────────────────
@pytest.mark.parametrize("target", [
    'button:has-text("Pay now")',
    'button:text-is("View Cart"), a:text-is("View Cart")',
    "li:nth-child(2) a",
])
def test_pseudo_selectors_are_css(target):
    assert not _looks_like_plain_text(target)


@pytest.mark.parametrize("target", ["Note: pay now", "Add to cart", "Total (incl. GST)"])
def test_visible_text_stays_text(target):
    assert _looks_like_plain_text(target)


# ─── the MCP surface ─────────────────────────────────────────────────────
def test_mcp_trims_whole_page_results():
    result = {"text": "x" * 50_000}
    mcp._trim_result("text", result, None)
    assert len(result["text"]) == 40_000
    assert result["truncated"] and result["total_chars"] == 50_000


def test_mcp_trim_respects_max_chars_and_leaves_short_text():
    wide = {"html": "x" * 50_000}
    mcp._trim_result("html", wide, 60_000)
    assert "truncated" not in wide
    short = {"text": "x"}
    mcp._trim_result("map_compact", short, None)
    assert short == {"text": "x"}


def test_mcp_trim_ignores_other_verbs():
    result = {"text": "x" * 50_000}
    mcp._trim_result("fetch", result, None)
    assert "truncated" not in result


def test_trimmed_verbs_offer_max_chars():
    schemas = {t[0]: t[2] for t in mcp._filter_tools(None)}
    for verb in ("text", "html", "map", "map_compact"):
        assert "max_chars" in schemas[verb]["properties"], verb


def test_settled_read_is_loaded_up_front():
    assert "text" in mcp._ALWAYS_LOAD
    assert "act" not in mcp._ALWAYS_LOAD


def test_instructions_carry_the_routing_rule():
    text = mcp._build_instructions(caps_mod.resolve_caps("lean"))
    assert "WebSearch first" in text
    assert "text(settle=true)" in text
    assert "session_close" in text



# ─── live: real daemon, real Chrome ──────────────────────────────────────
SPA = (b"<!doctype html><title>Search</title><header>Free shipping over $50. Sign in. "
       b"Cart. Help. Stores. Gift cards. Sale. New arrivals. Brands. Departments.</header>"
       b"<main id=m>Loading</main><script>setTimeout(()=>{m.textContent="
       b"'Product 1 $10.00 '.repeat(40)},900)</script>")
PAGE = (b"<!doctype html><title>Form</title><p>" + b"Body text for the form page. " * 8 +
        b"</p><button id=add>Add to Cart</button><button id=load onclick=\"fetch('/api')"
        b".then(r=>r.text()).then(t=>o.textContent=t)\">Load</button><div id=o></div>")
DROPDOWN = (b"<!doctype html><title>Pick</title><p>" + b"Body text for the form page. " * 8 +
            b"</p><button id=st onclick=\"l.hidden=false\">State</button>"
            b"<ul id=l role=listbox hidden><li role=option onclick=\"out.textContent="
            b"this.textContent;l.hidden=true\">Victoria</li></ul><div id=out></div>")
OVERLAY = PAGE + b"<div style='position:fixed;inset:0;z-index:9'>drawer</div>"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        status, body = 200, PAGE
        if path == "/spa":
            body = SPA
        elif path == "/dropdown":
            body = DROPDOWN
        elif path == "/overlay":
            body = OVERLAY
        elif path == "/api":
            time.sleep(0.3)
            body = b"api-result"
        elif path == "/slow":
            time.sleep(3)
        elif path == "/forbidden":
            status, body = 403, b"<title>denied</title>nope"
        elif path == "/forbidden-empty":
            status, body = 403, b""
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture(scope="module")
def site():
    server = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    if not shutil.which("google-chrome") and not shutil.which("google-chrome-stable"):
        pytest.skip("needs channel Chrome")
    run = IsolatedDaemon(tmp_path_factory.mktemp("giveups"),
                         {"VIBATCHIUM_MAX_EPHEMERAL": "2"}).start()
    yield run
    run.stop()


def _ok(resp):
    assert resp["ok"], resp
    return resp["result"]


def test_explore_reads_the_rendered_page(daemon, site):
    out = _ok(daemon.call("explore", {"url": f"{site}/spa", "screenshot": "never"}))
    assert "Product 1" in out["text"]
    assert "still_rendering" not in out


def test_go_flags_a_refusal(daemon, site):
    out = _ok(daemon.call("go", {"url": f"{site}/forbidden"}, session="g1"))
    assert out["status"] == 403 and out["blocked"] and "refused" in out["hint"]
    explored = _ok(daemon.call("explore", {"url": f"{site}/forbidden", "screenshot": "never"}))
    assert explored["blocked"]


def test_empty_http_error_is_explained(daemon, site):
    resp = daemon.call("go", {"url": f"{site}/forbidden-empty"}, session="g1")
    assert not resp["ok"] and "usually a block" in resp["error"]


def test_parallel_explores_queue_instead_of_failing(daemon, site):
    def one(i):
        return daemon.call("explore", {"url": f"{site}/slow?{i}", "screenshot": "never"},
                           timeout=90)

    with ThreadPoolExecutor(5) as pool:
        results = list(pool.map(one, range(5)))
    assert all(r["ok"] for r in results), results
    assert any(r["result"].get("queued_ms") for r in results)


def test_explore_refuses_when_the_wait_runs_out(tmp_path, site):
    run = IsolatedDaemon(tmp_path, {"VIBATCHIUM_MAX_EPHEMERAL": "1",
                                    "VIBATCHIUM_EXPLORE_QUEUE_S": "0"}).start()
    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(
                lambda i: run.call("explore", {"url": f"{site}/slow?{i}",
                                               "screenshot": "never"}), range(2)))
    finally:
        run.stop()
    assert sorted(r["ok"] for r in results) == [False, True]
    assert "VIBATCHIUM_MAX_EPHEMERAL" in next(r for r in results if not r["ok"])["error"]


def test_click_failure_is_one_line(daemon, site):
    _ok(daemon.call("go", {"url": f"{site}/overlay"}, session="c1"))
    resp = daemon.call("click", {"target": "#add", "timeout_ms": 1200}, session="c1")
    assert not resp["ok"]
    assert "<div>drawer</div> is covering" in resp["error"]
    assert "\n" not in resp["error"]


def test_select_opens_a_custom_dropdown(daemon, site):
    _ok(daemon.call("go", {"url": f"{site}/dropdown"}, session="s1"))
    out = _ok(daemon.call("select", {"target": "#st", "label": "Victoria"}, session="s1"))
    assert out == {"selected": ["Victoria"], "custom_dropdown": True}
    assert _ok(daemon.call("text", {"target": "#out"}, session="s1"))["text"] == "Victoria"


def test_wait_selector_takes_the_click_grammar(daemon, site):
    _ok(daemon.call("go", {"url": site}, session="w1"))
    for target in ("@text:Add to Cart", "Add to Cart", "text=Add to Cart", "#add"):
        _ok(daemon.call("wait_selector", {"selector": target, "timeout_ms": 2000}, session="w1"))


def test_wait_response_sees_the_click_it_was_started_for(daemon, site):
    _ok(daemon.call("go", {"url": site}, session="r1"))
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(daemon.call, "wait_response",
                              {"pattern": "/api", "timeout_ms": 8000, "body": True},
                              session="r1")
        time.sleep(0.5)
        _ok(daemon.call("click", {"target": "#load"}, session="r1"))
        out = _ok(waiting.result(timeout=12))
    assert out["status"] == 200 and out["url"].endswith("/api")


def test_text_settle_waits_for_the_click_result(daemon, site):
    _ok(daemon.call("go", {"url": site}, session="t1"))
    _ok(daemon.call("click", {"target": "#load"}, session="t1"))
    out = _ok(daemon.call("text", {"settle": True}, session="t1"))
    assert "api-result" in out["text"] and out["url"].startswith(site)


def test_idle_agent_sessions_close_and_cli_sessions_stay(tmp_path, site):
    from vibatchium import fspolicy
    run = IsolatedDaemon(tmp_path, {"VIBATCHIUM_AGENT_SESSION_IDLE_S": "3"}).start()
    scope = {fspolicy.SCOPE_ARG: fspolicy.agent_scope()}
    try:
        _ok(run.call("go", {"url": site, **scope}, session="agent1"))
        _ok(run.call("go", {"url": site, **scope}, session="agent2"))
        _ok(run.call("session_lease", {"name": "agent2", "ttl_s": 60}, session="agent2"))
        _ok(run.call("go", {"url": site}, session="bot1"))
        time.sleep(7)
        running = _ok(run.call("status"))["running_sessions"]
    finally:
        run.stop()
    assert set(running) == {"agent2", "bot1"}


def test_a_used_agent_session_stays_open(tmp_path, site):
    from vibatchium import fspolicy
    run = IsolatedDaemon(tmp_path, {"VIBATCHIUM_AGENT_SESSION_IDLE_S": "4"}).start()
    try:
        _ok(run.call("go", {"url": site, fspolicy.SCOPE_ARG: fspolicy.agent_scope()},
                     session="busy1"))
        for _ in range(4):
            time.sleep(2)
            _ok(run.call("title", session="busy1"))
        assert "busy1" in _ok(run.call("status"))["running_sessions"]
    finally:
        run.stop()


def test_cli_press_and_screenshot_take_the_obvious_forms():
    from click.testing import CliRunner
    from unittest import mock
    from vibatchium import cli

    with mock.patch.object(cli, "call", return_value={"pressed": "Escape"}) as call:
        result = CliRunner().invoke(cli.cli, ["press", "Escape"])
    assert result.exit_code == 0, result.output
    assert call.call_args.args == ("keys", {"keys": "Escape"})

    with mock.patch.object(cli, "call", return_value={"path": "/tmp/a.png"}) as call:
        positional = CliRunner().invoke(cli.cli, ["screenshot", "/tmp/a.png"])
        flag = CliRunner().invoke(cli.cli, ["screenshot", "--path", "/tmp/a.png"])
    assert positional.exit_code == 0 and flag.exit_code == 0, positional.output + flag.output
    assert all(c.args[1]["path"] == "/tmp/a.png" for c in call.call_args_list)


