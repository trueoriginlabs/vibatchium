"""Phase 3 — integration tests through a REAL daemon subprocess.

Every prior plugin/skill/goal test is in-process (`Daemon().dispatch(...)`).
These spawn an actual `python -m vibatchium.daemon.server` with an **isolated
HOME + XDG_RUNTIME_DIR** (so the user's ~/.config/vibatchium is never touched),
talk to it over its Unix socket, and tear it down by PID. This covers the wiring
most likely to break in real use: socket dispatch, daemon-process plugin
discovery, MCP list/call, and restart durability.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path


# Browsers are installed under the *real* home; an isolated HOME would hide them
# from patchright. Pin the path so a Chrome-using daemon still finds them.
_REAL_BROWSERS = os.environ.get(
    "PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))


class IsolatedDaemon:
    """A real daemon subprocess rooted at a temp HOME + XDG_RUNTIME_DIR."""

    def __init__(self, tmp_path: Path, env_extra: dict | None = None):
        self.home = tmp_path / "home"
        self.run = tmp_path / "run"
        self.home.mkdir(exist_ok=True)
        self.run.mkdir(exist_ok=True)
        self.config = self.home / ".config" / "vibatchium"
        self.sock = self.run / "vibatchium" / "daemon.sock"
        self.errfile = tmp_path / "daemon.err"
        env = dict(os.environ)
        env.update({
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.run),
            "VIBATCHIUM_WARM": "off",
            "VIBATCHIUM_DEFAULT_HEADLESS": "1",
            "PLAYWRIGHT_BROWSERS_PATH": _REAL_BROWSERS,
        })
        env.update(env_extra or {})
        self.env = env
        self.proc: subprocess.Popen | None = None

    # ─── filesystem setup helpers (call before start) ───────────────────────

    def write_plugin(self, name: str, body: str) -> None:
        pdir = self.config / "plugins" / name
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "__init__.py").write_text(textwrap.dedent(body))

    def write_skill(self, host: str, filename: str, body: str) -> None:
        hdir = self.config / "skills" / host
        hdir.mkdir(parents=True, exist_ok=True)
        (hdir / filename).write_text(body)

    # ─── lifecycle ───────────────────────────────────────────────────────────

    def start(self, wait: float = 20.0) -> IsolatedDaemon:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "vibatchium.daemon.server"],
            env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=self.errfile.open("wb"),
            start_new_session=True, close_fds=True,
        )
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.sock.exists():
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(1.0)
                    s.connect(str(self.sock))
                    s.close()
                    return self
                except OSError:
                    pass
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"daemon exited rc={self.proc.returncode}\n"
                    f"{self._stderr()}")
            time.sleep(0.1)
        raise RuntimeError(f"daemon socket never came up\n{self._stderr()}")

    def call(self, cmd: str, args: dict | None = None, *,
             session: str | None = None, timeout: float = 60.0) -> dict:
        payload = dict(args or {})
        if session:
            payload["_session"] = session
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(self.sock))
        try:
            req = json.dumps({"id": "1", "cmd": cmd, "args": payload}) + "\n"
            s.sendall(req.encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        return json.loads(buf.decode())

    def _stderr(self) -> str:
        try:
            return self.errfile.read_text()[-2000:]
        except OSError:
            return "(no stderr)"

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.call("shutdown", timeout=5)
            try:
                self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    self.proc.wait(timeout=3)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


_ECHO_PLUGIN = '''
    async def _echo(daemon, args):
        return {"echoed": args.get("msg", ""), "n": args.get("n", 0)}

    def register(daemon):
        daemon.add_verb(
            name="demo.echo", handler=_echo,
            inputs_schema={"msg": "string", "n": "integer"},
            description="Echo a message back.", lock="unlocked")
'''


# ─── 3.1 plugin verb e2e over the socket ───────────────────────────────────

def test_plugin_verb_over_socket(tmp_path):
    d = IsolatedDaemon(tmp_path)
    d.write_plugin("demo", _ECHO_PLUGIN)
    with d:
        listed = d.call("plugin_list")
        assert listed["ok"], listed
        plugins = {p["name"]: p for p in listed["result"]["plugins"]}
        assert "demo" in plugins
        assert plugins["demo"]["source"] == "local_dir"
        assert "demo.echo" in plugins["demo"]["verbs"]

        r = d.call("demo.echo", {"msg": "hi", "n": 7})
        assert r["ok"], r
        assert r["result"] == {"echoed": "hi", "n": 7}


# ─── 3.4 goals durability across a real daemon restart ──────────────────────

def test_goal_survives_daemon_restart(tmp_path):
    d = IsolatedDaemon(tmp_path)
    with d:
        g = d.call("goal_new", {"description": "survive", "session": "s",
                                "budget": "steps=10"})
        assert g["ok"], g
        gid = g["result"]["id"]
        nxt = d.call("goal_next")
        assert nxt["ok"] and nxt["result"]["goal"]["id"] == gid
        assert nxt["result"]["goal"]["status"] == "running"
    # daemon is now down (context manager called shutdown). Respawn on the SAME
    # home/run so it reads the same goals.db.
    d2 = IsolatedDaemon(tmp_path)
    with d2:
        show = d2.call("goal_show", {"goal_id": gid})
        assert show["ok"], show
        # running → paused on restart (crash-resume)
        assert show["result"]["status"] == "paused"
        # and it's runnable again
        nxt2 = d2.call("goal_next")
        assert nxt2["ok"] and nxt2["result"]["goal"]["id"] == gid
        assert nxt2["result"]["goal"]["status"] == "running"


# ─── 3.3 MCP dynamic exposure of plugin verbs ───────────────────────────────

async def test_mcp_exposes_and_forwards_plugin_verb(tmp_path, monkeypatch):
    from vibatchium import client, mcp_server

    d = IsolatedDaemon(tmp_path)
    d.write_plugin("demo", _ECHO_PLUGIN)
    with d:
        # Point the MCP server's daemon client at THIS isolated daemon.
        monkeypatch.setattr(client, "SOCK_PATH", d.sock)
        monkeypatch.setattr(mcp_server, "daemon_is_running", lambda: True)

        # No caps filter → plugin verb is exposed and forwards.
        monkeypatch.setattr(mcp_server, "_ACTIVE_CAPS", None)
        tools = await mcp_server.list_tools()
        assert "demo.echo" in {t.name for t in tools}

        res = await mcp_server.call_tool("demo.echo", {"msg": "via-mcp", "n": 1})
        assert isinstance(res, list)
        payload = json.loads(res[0].text)
        assert payload == {"echoed": "via-mcp", "n": 1}

        # Caps active WITHOUT `plugins` → verb absent + call refused.
        monkeypatch.setattr(mcp_server, "_ACTIVE_CAPS", {"core"})
        tools2 = await mcp_server.list_tools()
        assert "demo.echo" not in {t.name for t in tools2}
        refused = await mcp_server.call_tool("demo.echo", {"msg": "x"})
        assert getattr(refused, "isError", False) is True


# ─── 3.2 skill surfacing on a real navigation (needs Chrome) ────────────────

def test_skill_surfacing_on_real_go(tmp_path_factory, local_server):
    """With VIBATCHIUM_SKILLS=1, a real `go` attaches a `skills` key naming the
    host's notes. (The disabled-by-default path is covered without Chrome in
    test_skills.py::test_surfacing_off_by_default — kept out of here to avoid a
    second Chrome spawn that can destabilize the shared session under memory
    pressure.)"""
    host = "127.0.0.1"
    on_dir = tmp_path_factory.mktemp("skills-on")
    d_on = IsolatedDaemon(on_dir, env_extra={"VIBATCHIUM_SKILLS": "1"})
    d_on.write_skill(host, "tips.md", "# tips\n\nUse the search box.\n")
    with d_on:
        r = d_on.call("go", {"url": local_server + "/"}, timeout=90)
        assert r["ok"], (r.get("error"), d_on._stderr())
        sk = r["result"].get("skills")
        assert sk is not None, "expected a skills key with VIBATCHIUM_SKILLS=1"
        assert sk["host"] == host
        assert "tips.md" in sk["notes"]


# ─── 4.1 goal checkpoint round-trip on pause/resume (needs Chrome) ──────────

def test_goal_checkpoint_roundtrip(tmp_path_factory, local_server):
    d = IsolatedDaemon(tmp_path_factory.mktemp("ckpt"))
    sess = "g"
    with d:
        # Bring up a live session on the goal's session name.
        r = d.call("go", {"url": local_server + "/"}, session=sess, timeout=90)
        assert r["ok"], (r.get("error"), d._stderr())
        url_before = r["result"]["url"]

        g = d.call("goal_new", {"description": "ck", "session": sess,
                                "budget": "steps=10"})
        gid = g["result"]["id"]
        assert d.call("goal_next")["ok"]
        step = d.call("goal_step", {"goal_id": gid, "observation": {"t": "x"}})
        assert step["ok"], step

        show = d.call("goal_show", {"goal_id": gid})["result"]
        kinds = [e["kind"] for e in show["events"]]
        # a real checkpoint was taken at the step boundary
        assert "checkpoint_saved" in kinds, kinds
        assert show["checkpoint_id"], "checkpoint_id not set on the record"

        # pause releases the session; resume restores the snapshot
        assert d.call("goal_pause", {"goal_id": gid})["ok"]
        res = d.call("goal_resume", {"goal_id": gid})
        assert res["ok"] and res["result"]["goal"]["status"] == "running"
        url_after = d.call("url", {}, session=sess)["result"]["url"]
        assert url_after == url_before
