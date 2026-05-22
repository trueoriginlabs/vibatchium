"""Lifecycle + navigation tests."""
from patchium.client import call


def test_status_running():
    s = call("status")
    assert s["running"] is True


def test_go_and_back(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    assert call("url")["url"].endswith("/simple.html")
    assert "Patchium Test Page" in call("title")["title"]
    call("go", {"url": f"{local_server}/second.html"})
    assert call("url")["url"].endswith("/second.html")
    call("back")
    assert call("url")["url"].endswith("/simple.html")
    call("forward")
    assert call("url")["url"].endswith("/second.html")


def test_text_extracts_body(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    body = call("text")["text"]
    assert "Hello, Patchium" in body
    assert "This is a fixture page" in body


def test_eval_isolated_context(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("eval", {"expr": "2 + 2"})
    assert res["value"] == 4
    res = call("eval", {"expr": "document.title"})
    assert res["value"] == "Patchium Test Page"
