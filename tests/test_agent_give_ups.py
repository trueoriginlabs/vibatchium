"""What made agents stall or report wrong answers, found by running clean
agents and driving the MCP server by hand (2026-11-05).

The unit tests pin the parsing and timing rules. The live tests drive a real
daemon and Chrome against a local server that reproduces each failure.
"""
from __future__ import annotations

import asyncio
import http.server
import os
import re
import shutil
import signal
import socketserver
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
    """A page whose in-page wait answers from a script of outcomes."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def wait_for_function(self, _js, *, arg, polling, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _settle(page, **kw):
    return asyncio.run(settle.wait_settled(page, **kw))


def test_settle_is_one_call_into_the_page():
    # Each call from the driver gives the page a user activation.
    page = _Page(True)
    assert _settle(page) is True
    assert page.calls == 1


def test_settle_retries_through_a_navigation():
    page = _Page(Exception("Execution context was destroyed"), True)
    assert _settle(page, cap_ms=2000) is True


def test_settle_gives_up_at_the_cap():
    assert _settle(_Page(Exception("Timeout")), cap_ms=300) is False


def test_settle_goes_round_again_while_a_request_is_young():
    class Watch:
        def __init__(self):
            self.answers = [True, False]

        def busy(self):
            return self.answers.pop(0)

    page = _Page(True)
    assert _settle(page, cap_ms=2000, watch=Watch()) is True
    assert page.calls == 2


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
# Level for a second, then the results: what Officeworks' search page does.
PLATEAU = (b"<!doctype html><title>Plateau</title><p>" + b"Site header text. " * 12 +
           b"</p><main id=m></main><script>setTimeout(()=>{m.textContent='Result row. '"
           b".repeat(300)},1000)</script>")
TICKER = (b"<!doctype html><title>Ticker</title><p>" + b"Long article text. " * 300 +
          b"</p><span id=t></span><script>setInterval(()=>{t.textContent=Date.now()},100)"
          b"</script>")
GROWING = (b"<!doctype html><title>Growing</title><p>" + b"Text. " * 30 +
           b"</p><script>setInterval(()=>{document.body.append('More text here. '.repeat(20))"
           b"},150)</script>")
OVERLAY = PAGE + b"<div style='position:fixed;inset:0;z-index:9'>drawer</div>"


OUTER = (b"<!doctype html><body><p>outer words</p><a href='/from-outer'>o</a>"
         b"<iframe name='inner' src='/inner'></iframe></body>")
INNER = (b"<!doctype html><body><h1 id='h'>inner words</h1><a href='/from-inner'>i</a>"
         b"<form><input name='q'></form></body>")
FRAMESET = (b"<!doctype html><html><frameset cols='50%,50%'>"
            b"<frame src='/left'><frame src='/right'></frameset></html>")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        status, body = 200, PAGE
        if path == "/outer":
            body = OUTER
        elif path == "/inner":
            body = INNER
        elif path == "/frameset":
            body = FRAMESET
        elif path == "/left":
            body = b"<!doctype html><body>left words</body>"
        elif path == "/right":
            body = b"<!doctype html><body>right words</body>"
        elif path == "/spa":
            body = SPA
        elif path == "/dropdown":
            body = DROPDOWN
        elif path == "/plateau":
            body = PLATEAU
        elif path == "/ticker":
            body = TICKER
        elif path == "/growing":
            body = GROWING
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


@pytest.fixture
def short_tmp():
    """pytest's macOS temp paths overflow the 104-byte AF_UNIX socket limit."""
    with tempfile.TemporaryDirectory(prefix="gu", dir="/tmp", ignore_cleanup_errors=True) as d:
        yield Path(d)


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    if not shutil.which("google-chrome") and not shutil.which("google-chrome-stable"):
        pytest.skip("needs channel Chrome")
    run = IsolatedDaemon(tmp_path_factory.mktemp("giveups"),
                         {"VIBATCHIUM_MAX_SESSIONS": "16", "VIBATCHIUM_MAX_EPHEMERAL": "2"}).start()
    yield run
    run.stop()


def _ok(resp):
    assert resp["ok"], resp
    return resp["result"]


def test_explore_reads_the_rendered_page(daemon, site):
    out = _ok(daemon.call("explore", {"url": f"{site}/spa", "screenshot": "never"}))
    assert "Product 1" in out["text"]
    assert "still_rendering" not in out


def test_go_waits_out_a_level_stretch(daemon, site):
    _ok(daemon.call("go", {"url": f"{site}/plateau"}, session="p1"))
    assert "Result row" in _ok(daemon.call("text", session="p1"))["text"]


def test_go_is_not_held_up_by_a_ticking_clock(daemon, site):
    t0 = time.monotonic()
    out = _ok(daemon.call("go", {"url": f"{site}/ticker"}, session="p1"))
    assert "still_rendering" not in out and time.monotonic() - t0 < 4


def test_go_says_when_a_page_never_stops(daemon, site):
    out = _ok(daemon.call("go", {"url": f"{site}/growing", "render_timeout_ms": 1200},
                          session="p1"))
    assert out["still_rendering"] is True


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


def test_explore_refuses_when_the_wait_runs_out(short_tmp, site):
    run = IsolatedDaemon(short_tmp, {"VIBATCHIUM_MAX_EPHEMERAL": "1",
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


def test_idle_agent_sessions_close_and_cli_sessions_stay(short_tmp, site):
    from vibatchium import fspolicy
    run = IsolatedDaemon(short_tmp, {"VIBATCHIUM_AGENT_SESSION_IDLE_S": "3"}).start()
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


def test_a_used_agent_session_stays_open(short_tmp, site):
    from vibatchium import fspolicy
    run = IsolatedDaemon(short_tmp, {"VIBATCHIUM_AGENT_SESSION_IDLE_S": "4"}).start()
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
    assert all(c.args[1]["path"].endswith("/tmp/a.png") for c in call.call_args_list)




def _drivers(run):
    pid = (run.run / "vibatchium" / "daemon.pid").read_text().strip()
    out = subprocess.run(["pgrep", "-P", pid, "-f", "patchright/driver"],
                         capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def test_daemon_survives_losing_its_driver(short_tmp, site):
    """A dead driver used to fail every launch until the daemon was restarted."""
    run = IsolatedDaemon(short_tmp).start()
    try:
        _ok(run.call("start", {"headless": True}, session="kept"))
        _ok(run.call("go", {"url": site}, session="kept"))
        assert len(_drivers(run)) == 1                  # not one per racing request
        os.kill(_drivers(run)[0], signal.SIGKILL)
        time.sleep(1)

        assert _ok(run.call("explore", {"url": site}))["status"] == 200
        assert len(_drivers(run)) == 1
        _ok(run.call("go", {"url": site}, session="kept"))
        assert _ok(run.call("text", {}, session="kept"))["text"]
    finally:
        run.stop()


def test_reads_follow_the_active_frame(daemon, site):
    """They read the top page, so an agent got the wrong text with no warning."""
    _ok(daemon.call("go", {"url": f"{site}/outer"}, session="fr"))
    _ok(daemon.call("frame", {"name": "inner"}, session="fr"))

    def read(cmd, **args):
        return _ok(daemon.call(cmd, args, session="fr"))

    assert read("text")["text"].strip().startswith("inner words")
    assert "outer" not in read("text", settle=True)["text"]
    assert "inner words" in read("html")["html"]
    md = read("extract")
    assert "inner words" in md["markdown"] and "outer" not in md["markdown"]
    assert md["url"].endswith("/inner")
    assert "inner words" in read("extract", mode="main")["markdown"]
    assert [x["url"].rsplit("/", 1)[1] for x in read("extract", mode="links")["links"]] == ["from-inner"]
    assert read("extract_fields", fields={"h": "#h"})["fields"]["h"] == "inner words"
    assert read("detect_forms")["count"] == 1
    assert read("wait_selector", selector="#h", timeout_ms=2000)["matched"] == "#h"
    assert "inner words" in read("text", target="#h")["text"]
    read("fill", target="[name=q]", text="typed")
    assert read("value", target="[name=q]")["value"] == "typed"
    ref = re.search(r"textbox.*?(@f\d+e\d+)", read("map")["text"]).group(1)
    read("fill", target=ref, text="by ref")
    assert read("value", target="[name=q]")["value"] == "by ref"

    _ok(daemon.call("frame", {}, session="fr"))
    assert "outer words" in read("text")["text"]

    # A top-page map lists iframe elements too, and those refs must work.
    ref = re.search(r"textbox.*?(@f\d+e\d+)", read("map")["text"]).group(1)
    read("fill", target=ref, text="from the top page")
    _ok(daemon.call("frame", {"name": "inner"}, session="fr"))
    assert read("value", target="[name=q]")["value"] == "from the top page"


def test_a_frameset_page_reads_as_its_frames(daemon, site):
    """It has no <body>, so a read waited 30s and failed."""
    started = time.monotonic()
    out = _ok(daemon.call("go", {"url": f"{site}/frameset"}, session="fs"))
    assert "still_rendering" not in out
    text = _ok(daemon.call("text", {}, session="fs"))["text"]
    assert "left words" in text and "right words" in text
    md = _ok(daemon.call("extract", {}, session="fs"))["markdown"]
    assert "left words" in md and "right words" in md
    assert time.monotonic() - started < 10
    assert "right words" in _ok(daemon.call("explore", {"url": f"{site}/frameset"}))["text"]
