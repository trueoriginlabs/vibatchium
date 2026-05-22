"""Element model tests — map / click / fill / type / refs."""
from patchium.client import call


def test_map_returns_refs(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    m = call("map")
    assert m["count"] > 0
    assert "@e" in m["text"]


def test_map_compact(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    m = call("map_compact")
    # button "Increment" and button "Submit" should show up
    assert "button" in m["text"]
    assert "Increment" in m["text"] or "Submit" in m["text"]
    # compact format is one-per-line, no nested YAML
    lines = [ln for ln in m["text"].splitlines() if ln.strip()]
    assert all(ln.lstrip().startswith("@e") for ln in lines)


def test_click_increment_button(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    # initial counter is 0
    assert call("text", {"selector": "#counter"})["text"] == "0"
    # click via CSS selector
    call("click", {"target": "#counter-btn"})
    assert call("text", {"selector": "#counter"})["text"] == "1"
    # click via @eN via the snapshot
    # find the Increment ref from the snapshot (note: refs come AFTER the name
    # in playwright's aria_snapshot output)
    snap_text = call("map")["text"]
    import re
    m = re.search(r'"Increment"[^\n]*@(e\d+)', snap_text)
    assert m, f"Increment button not found in snapshot: {snap_text[:300]}"
    ref = "@" + m.group(1)
    call("click", {"target": ref})
    assert call("text", {"selector": "#counter"})["text"] == "2"


def test_fill_and_submit(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    call("fill", {"target": "#q", "text": "hello world"})
    assert call("value", {"selector": "#q"})["value"] == "hello world"
    call("click", {"target": "#submit"})
    # wait for the form's onsubmit to run
    call("sleep", {"ms": 200})
    assert call("text", {"selector": "#result"})["text"] == "hello world"


def test_find_text(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("find", {"kind": "text", "query": "Hello, Patchium"})
    assert res["count"] >= 1
    assert "Hello, Patchium" in res["first_text"]


def test_find_label(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("find", {"kind": "label", "query": "Search:"})
    assert res["count"] >= 1


def test_find_placeholder(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("find", {"kind": "placeholder", "query": "Type something"})
    assert res["count"] == 1


def test_count(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    # exactly one button with id #counter-btn
    assert call("count", {"target": "#counter-btn"})["count"] == 1
    # multiple buttons total
    assert call("count", {"target": "button"})["count"] == 2
