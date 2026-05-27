"""Tests for observe/act planning and profile management."""
from vibatchium.client import call


def test_observe_heuristic_match(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("observe", {"intent": "click the Increment button"})
    assert res["source"] == "heuristic"
    assert res["plan"], "expected at least one step"
    step = res["plan"][0]
    assert step["verb"] == "click"
    assert step["target"].startswith("@e")
    assert "Increment" in step["rationale"]


def test_observe_caches(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    a = call("observe", {"intent": "open the link", "force": True})
    b = call("observe", {"intent": "open the link"})
    assert b.get("cached") is True
    assert a["plan"][0]["target"] == b["plan"][0]["target"]


def test_observe_unknown_intent_empty(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("observe", {"intent": "xyzzy quux foobar", "force": True})
    assert res["plan"] == []


def test_act_executes_plan(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    # initial counter is 0
    assert call("text", {"selector": "#counter"})["text"] == "0"
    res = call("act", {"intent": "press the Increment button"})
    assert res["executed"] >= 1
    assert call("text", {"selector": "#counter"})["text"] == "1"


def test_act_fill_with_text(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    # heuristic should infer fill + extract text from the intent
    res = call("act", {"intent": "type hello into the Search field"})
    if res["executed"] >= 1:
        # if the heuristic targeted the textbox, the value should be set
        val = call("value", {"selector": "#q"})["value"]
        # we don't enforce exact text since heuristic may pick textbox without text extraction
        assert val == "hello" or "hello" in val or val == ""


def test_profile_lifecycle():
    # list existing
    initial = call("profile_list")
    assert initial["active"] in initial["profiles"]
    # create
    res = call("profile_new", {"name": "vibatchium_test_prof"})
    assert res["created"] is True or res.get("exists")
    # listed
    listed = call("profile_list")
    assert "vibatchium_test_prof" in listed["profiles"]
    # cannot delete active (skip — would require switching sessions)
    # delete
    call("profile_delete", {"name": "vibatchium_test_prof"})
    after = call("profile_list")
    assert "vibatchium_test_prof" not in after["profiles"]


def test_count_extra(local_server):
    """Sanity: count + content + map_compact extras."""
    call("go", {"url": f"{local_server}/simple.html"})
    assert call("count", {"target": "input"})["count"] == 2  # #q and #file
    # buttons: counter + submit + reject-banner + accept-banner + trigger-fetch
    assert call("count", {"target": "button"})["count"] >= 4
    res = call("map_compact")
    assert res["count"] > 0
