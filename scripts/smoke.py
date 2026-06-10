#!/usr/bin/env python
"""Live end-to-end smoke harness for vibatchium.

Spins up a *real*, isolated daemon (its own ``XDG_RUNTIME_DIR`` → own socket,
pre-warm off for determinism) and drives real headless Chrome through a set of
comprehensive flows the way an agent would chain them — catching integration
regressions the unit suite can miss. Prints a PASS/FAIL report and exits
non-zero if any scenario fails (so it can gate CI).

    .venv/bin/python scripts/smoke.py

Requires `patchright install chrome`. The REST scenario is skipped unless the
`rest` extra (fastapi + uvicorn) is installed.

Scenarios:
  1. multi-session per-session geo isolation (no timezone bleed across sessions)
  2. geo lifecycle: set → info → clear → restart → info
  3. worker coherence on a real origin (tz reaches workers; navigator.language
     stays coherent main==worker — the locale override we deliberately avoid)
  4. humanize comprehensive flow (humanized type + click land correctly)
  5. goal domain-allowlist enforcement (off-allowlist nav refused)
  6. REST shim: is_state name→cmd translation + caps gating
  7. clean housekeeping (dry-run safe) + MCP exposure
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── isolated daemon: a private runtime dir → private socket, so this never
#    touches a developer's real daemon. Must be set before importing the client
#    (the socket path is derived from XDG_RUNTIME_DIR at import time). ──
_RUNTIME_DIR = tempfile.mkdtemp(prefix="vb-smoke-")
os.environ["XDG_RUNTIME_DIR"] = _RUNTIME_DIR
os.environ["VIBATCHIUM_WARM"] = "off"
os.environ["VIBATCHIUM_MAX_SESSIONS"] = "16"

from vibatchium.client import call, daemon_is_running, spawn_daemon  # noqa: E402

PAGE = b"""<!doctype html><html><head><title>smoke</title></head><body>
<button id="btn" onclick="window.__n=(window.__n||0)+1;document.getElementById('c').textContent=window.__n">go</button>
<span id="c">0</span>
<input id="q"><div id="r"></div>
<a id="lnk" href="https://off-allowlist.example/landing">leave</a>
<script>document.getElementById('q').addEventListener('input',e=>document.getElementById('r').textContent=e.target.value)</script>
</body></html>"""


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


_results = []


def scenario(name, fn):
    try:
        detail = fn() or "ok"
        _results.append((name, True, detail))
        print(f"  PASS  {name} — {detail}")
    except Exception as exc:  # noqa: BLE001
        _results.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"  FAIL  {name} — {type(exc).__name__}: {exc}")
        traceback.print_exc()


def _val(res):
    return res.get("value", res) if isinstance(res, dict) else res


def _cleanup(*names):
    for nm in names:
        for cmd, a in (("session_close", {"name": nm}), ("session_delete", {"name": nm})):
            try:
                call(cmd, a)
            except Exception:  # noqa: BLE001
                pass


def main():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    if daemon_is_running():
        try:
            call("shutdown")
        except Exception:
            pass
    spawn_daemon(wait=10)
    print(f"daemon up (XDG={_RUNTIME_DIR}), fixtures at {base}\n")

    try:
        # ── 1. Multi-session per-session geo isolation ──────────────────
        def s1():
            try:
                for nm, cc in (("jp_sess", "jp"), ("us_sess", "us")):
                    call("session_new", {"name": nm})
                    call("geo_set", {"country": cc}, session=nm)
                    call("start", {"headless": True}, session=nm)
                # cross-check: each session reports ITS OWN timezone (no bleed)
                jp = _val(call("eval", {"expr": "Intl.DateTimeFormat().resolvedOptions().timeZone"}, session="jp_sess"))
                us = _val(call("eval", {"expr": "Intl.DateTimeFormat().resolvedOptions().timeZone"}, session="us_sess"))
                assert jp == "Asia/Tokyo", f"jp_sess tz={jp!r}"
                assert us == "America/New_York", f"us_sess tz={us!r}"
                # and geo_info live-probe agrees
                assert call("geo_info", session="jp_sess")["browser_timezone"] == "Asia/Tokyo"
                return f"jp_sess={jp}, us_sess={us} (isolated)"
            finally:
                _cleanup("jp_sess", "us_sess")
        scenario("multi-session per-session geo isolation", s1)

        # ── 2. geo lifecycle: set → info → clear → restart → info ────────
        def s2():
            try:
                call("session_new", {"name": "geo_life"})
                call("geo_set", {"timezone_id": "Europe/Berlin"}, session="geo_life")
                call("start", {"headless": True}, session="geo_life")
                assert call("geo_info", session="geo_life")["browser_timezone"] == "Europe/Berlin"
                call("geo_clear", session="geo_life")
                call("session_close", {"name": "geo_life"})
                call("start", {"headless": True}, session="geo_life")
                tz = _val(call("eval", {"expr": "Intl.DateTimeFormat().resolvedOptions().timeZone"}, session="geo_life"))
                # after clear+restart, tz is the host default (NOT Berlin)
                assert tz != "Europe/Berlin", f"geo not cleared: tz still {tz!r}"
                assert call("geo_info", session="geo_life")["configured"] is False
                return f"set=Berlin applied; cleared→host tz={tz}"
            finally:
                _cleanup("geo_life")
        scenario("geo lifecycle set/info/clear/restart", s2)

        # ── 3. worker coherence on a REAL origin ────────────────────────
        def s3():
            try:
                call("session_new", {"name": "worker_sess"})
                call("geo_set", {"country": "jp"}, session="worker_sess")
                call("start", {"headless": True}, session="worker_sess")
                call("go", {"url": base + "/"}, session="worker_sess")
                probe = """() => new Promise((res)=>{
                  const main={tz:Intl.DateTimeFormat().resolvedOptions().timeZone,lang:navigator.language};
                  const code="self.onmessage=()=>postMessage({tz:Intl.DateTimeFormat().resolvedOptions().timeZone,lang:navigator.language})";
                  const w=new Worker(URL.createObjectURL(new Blob([code],{type:'application/javascript'})));
                  const t=setTimeout(()=>res({main,worker:'TIMEOUT'}),4000);
                  w.onmessage=e=>{clearTimeout(t);res({main,worker:e.data});};w.postMessage(0);})"""
                r = _val(call("eval", {"expr": probe}, session="worker_sess"))
                assert r["worker"] != "TIMEOUT", "worker timed out"
                assert r["main"]["tz"] == "Asia/Tokyo" and r["worker"]["tz"] == "Asia/Tokyo", \
                    f"tz mismatch main vs worker: {r}"
                assert r["main"]["lang"] == r["worker"]["lang"], \
                    f"navigator.language differs main vs worker: {r}"
                return f"main/worker tz=Asia/Tokyo, lang coherent ({r['main']['lang']})"
            finally:
                _cleanup("worker_sess")
        scenario("tz reaches worker + language coherent (real origin)", s3)

        # ── 4. humanize comprehensive flow (type + click, verified) ─────
        def s4():
            try:
                call("session_new", {"name": "hz"})
                call("start", {"headless": True}, session="hz")
                call("go", {"url": base + "/"}, session="hz")
                call("humanize_on", session="hz")
                t = call("type", {"target": "#q", "text": "smoke test"}, session="hz")
                assert t.get("humanized") is True, f"type not humanized: {t}"
                qv = call("value", {"selector": "#q"}, session="hz")["value"]
                assert qv == "smoke test", f"#q value={qv!r}"
                rv = call("text", {"selector": "#r"}, session="hz")["text"]
                assert rv == "smoke test", f"#r text={rv!r}"
                c = call("click", {"target": "#btn"}, session="hz")
                assert c["humanized"] is True, f"click not humanized: {c}"
                cv = call("text", {"selector": "#c"}, session="hz")["text"]
                assert cv == "1", f"#c counter={cv!r}"
                return "humanized type+click landed correctly"
            finally:
                _cleanup("hz")
        scenario("humanize comprehensive flow", s4)

        # ── 5. goal domain-allowlist blocks off-allowlist navigation ────
        def s5():
            try:
                call("session_new", {"name": "goal_sess"})
                call("start", {"headless": True}, session="goal_sess")
                call("go", {"url": base + "/"}, session="goal_sess")
                host = base.split("//")[1].split(":")[0]
                g = call("goal_new", {"description": "stay home",
                                      "allow_domains": host}, session="goal_sess")
                gid = g.get("id") or g.get("goal_id") or g.get("goal", {}).get("id")
                # on-allowlist nav OK
                call("go", {"url": base + "/"}, session="goal_sess")
                # off-allowlist nav must be refused at the nav layer
                blocked = False
                try:
                    call("go", {"url": "https://off-allowlist.example/x"}, session="goal_sess")
                except Exception as exc:  # noqa: BLE001
                    blocked = "allowlist" in str(exc).lower() or "blocked" in str(exc).lower()
                assert blocked, "off-allowlist navigation was NOT blocked"
                return f"goal {gid}: off-allowlist nav refused; on-allowlist ok"
            finally:
                _cleanup("goal_sess")
        scenario("goal domain-allowlist enforcement", s5)

        # ── 6. REST shim: health, tools, is_state translation, caps 403 ─
        def s6():
            try:
                import uvicorn  # noqa: F401
                from fastapi.testclient import TestClient
            except Exception as exc:  # noqa: BLE001
                return f"SKIP (rest extra not installed: {type(exc).__name__})"
            from vibatchium.rest import build_app
            try:
                call("session_new", {"name": "rest_sess"})
                call("start", {"headless": True}, session="rest_sess")
                call("go", {"url": base + "/"}, session="rest_sess")
                # caps-restricted app: core+nav+input → is_state (input bucket)
                # allowed; a secrets verb denied (not in the granted buckets).
                app = build_app(caps="core,nav,input", require_auth=False)
                client = TestClient(app)
                assert client.get("/v1/health").json()["status"] == "ok"
                names = [t["name"] for t in client.get("/v1/tools").json()["tools"]]
                assert "is_state" in names, "is_state not advertised"
                # is_state (tool name) must translate to daemon cmd 'is'.
                r = client.post("/v1/is_state?session=rest_sess",
                                json={"target": "#btn", "state": "visible"})
                assert r.status_code == 200, f"/v1/is_state failed: {r.status_code} {r.text[:200]}"
                assert r.json()["result"].get("value") is True, f"is_state result: {r.json()}"
                # caps gate: a secrets verb is NOT in core,nav,input → 403
                denied = client.post("/v1/secret_list", json={})
                assert denied.status_code == 403, f"caps gate let secret_list through: {denied.status_code}"
                return "health+tools ok; /v1/is_state→200 (name→cmd translation); caps 403 ok"
            finally:
                _cleanup("rest_sess")
        scenario("REST shim: is_state translation + caps gating", s6)

        # ── 7. clean housekeeping: dry-run safe + exposed via MCP ────────
        def s7():
            from vibatchium.mcp_server import TOOLS
            assert "clean" in [t[0] for t in TOOLS], "clean not in MCP TOOLS"
            rep = call("clean", {})  # dry-run default
            assert rep.get("dry_run") is True, f"clean not dry-run by default: {rep}"
            cats = rep.get("categories", {})
            assert {"profiles", "locks", "cache", "logs"} <= set(cats), \
                f"clean categories incomplete: {list(cats)}"
            return f"clean dry-run ok (total_bytes={rep.get('total_bytes')}); in MCP TOOLS"
        scenario("clean housekeeping (dry-run, safe) + MCP exposure", s7)

    finally:
        try:
            call("shutdown")
        except Exception:
            pass
        srv.shutdown()
        shutil.rmtree(_RUNTIME_DIR, ignore_errors=True)

    npass = sum(1 for _, ok, _ in _results if ok)
    nfail = len(_results) - npass
    print(f"\n=== SMOKE: {npass} passed, {nfail} failed ===")
    for nm, ok, d in _results:
        if not ok:
            print(f"  FAILED: {nm} — {d}")
    sys.exit(1 if nfail else 0)


if __name__ == "__main__":
    main()
