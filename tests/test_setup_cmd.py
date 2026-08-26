"""Tests for the `vibatchium setup` command — agent CLI registration.

Coverage:
- Detection of installed agents (codex, claude, cursor)
- Idempotent doc-block writer (create / update / unchanged)
- Dry-run never writes
- Cursor JSON config write
- run_setup orchestration with monkeypatched detection
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest


from vibatchium import setup_cmd


# ─── detection ──────────────────────────────────────────────────────────


def test_detect_codex_via_path(monkeypatch):
    monkeypatch.setattr(shutil, "which",
                        lambda n: "/fake/codex" if n == "codex" else None)
    info = setup_cmd.detect_codex()
    assert info.detected is True
    assert "/fake/codex" in info.reason


def test_detect_codex_via_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda n: None)
    cfg = tmp_path / ".codex"
    cfg.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    info = setup_cmd.detect_codex()
    assert info.detected is True
    assert "config dir" in info.reason


def test_detect_codex_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda n: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    info = setup_cmd.detect_codex()
    assert info.detected is False


def test_detect_claude_via_json(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda n: None)
    (tmp_path / ".claude.json").write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    info = setup_cmd.detect_claude()
    assert info.detected is True


# ─── ensure_md_block ────────────────────────────────────────────────────


def _block(content="hello"):
    return f"{setup_cmd._BLOCK_BEGIN}\n{content}\n{setup_cmd._BLOCK_END}\n"


def test_ensure_md_block_creates_new(tmp_path):
    p = tmp_path / "AGENTS.md"
    result = setup_cmd.ensure_md_block(p, _block("v1"))
    assert result == "created"
    assert _block("v1") in p.read_text()


def test_ensure_md_block_updates_existing(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("# preamble\n\n" + _block("v1"))
    result = setup_cmd.ensure_md_block(p, _block("v2"))
    assert result == "updated"
    text = p.read_text()
    assert "v2" in text
    assert "v1" not in text
    assert "# preamble" in text  # preamble preserved


def test_ensure_md_block_appends_to_existing_without_block(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("# existing user content\n")
    result = setup_cmd.ensure_md_block(p, _block("new"))
    assert result == "updated"  # appended
    text = p.read_text()
    assert "# existing user content" in text
    assert "new" in text


def test_ensure_md_block_unchanged_when_identical(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text(_block("same"))
    result = setup_cmd.ensure_md_block(p, _block("same"))
    assert result == "unchanged"


def test_ensure_md_block_dry_run_never_writes(tmp_path):
    p = tmp_path / "AGENTS.md"
    # Create case
    result = setup_cmd.ensure_md_block(p, _block("v1"), dry_run=True)
    assert result == "would-created"
    assert not p.exists()
    # Update case
    p.write_text(_block("v1"))
    result = setup_cmd.ensure_md_block(p, _block("v2"), dry_run=True)
    assert result == "would-updated"
    assert "v1" in p.read_text()  # unchanged on disk


def test_ensure_md_block_idempotent_on_repeat(tmp_path):
    p = tmp_path / "AGENTS.md"
    block = _block("body")
    setup_cmd.ensure_md_block(p, block)
    setup_cmd.ensure_md_block(p, block)
    setup_cmd.ensure_md_block(p, block)
    text = p.read_text()
    # Only one block, no duplication
    assert text.count(setup_cmd._BLOCK_BEGIN) == 1
    assert text.count(setup_cmd._BLOCK_END) == 1


# ─── cursor JSON writer ────────────────────────────────────────────────


def test_setup_cursor_writes_fresh_mcp_json(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = setup_cmd.setup_cursor("/fake/vibatchium", dry_run=False, write_docs=False)
    assert result.mcp == "registered"
    cfg = tmp_path / ".cursor" / "mcp.json"
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["vibatchium"]["command"] == "/fake/vibatchium"
    # Registers the lean curated tool surface, not the full ~145.
    assert data["mcpServers"]["vibatchium"]["args"] == [
        "mcp", "--caps", setup_cmd.LEAN_CAPS]


def test_setup_cursor_preserves_existing_servers(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    setup_cmd.setup_cursor("/fake/vibatchium", dry_run=False, write_docs=False)
    data = json.loads(cfg.read_text())
    assert "other" in data["mcpServers"]
    assert "vibatchium" in data["mcpServers"]


def test_setup_cursor_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    setup_cmd.setup_cursor("/fake/vibatchium", dry_run=False, write_docs=False)
    second = setup_cmd.setup_cursor("/fake/vibatchium", dry_run=False, write_docs=False)
    assert second.mcp == "already"


def test_setup_cursor_refuses_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text("not valid json {")
    result = setup_cmd.setup_cursor("/fake/vibatchium", dry_run=False, write_docs=False)
    assert result.mcp == "failed"
    # Original content preserved
    assert "not valid json" in cfg.read_text()


# ─── run_setup orchestration ───────────────────────────────────────────


def test_run_setup_dry_run_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Force all agents undetected so we don't run real subprocess
    monkeypatch.setattr(setup_cmd, "_DETECTORS", {
        "codex": lambda: setup_cmd.AgentInfo("codex", False),
        "claude": lambda: setup_cmd.AgentInfo("claude", False),
        "cursor": lambda: setup_cmd.AgentInfo("cursor", False),
    })
    out = setup_cmd.run_setup(dry_run=True)
    assert out["dry_run"] is True
    assert out["results"] == []  # nothing detected → nothing to do
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".cursor").exists()


def test_run_setup_respects_agents_filter(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    out = setup_cmd.run_setup(agents=["cursor"], dry_run=False,
                             write_docs=False)
    assert [r["agent"] for r in out["results"]] == ["cursor"]


def test_run_setup_unknown_agent_reports_clearly(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    out = setup_cmd.run_setup(agents=["notarealagent"], dry_run=False)
    assert out["results"][0]["notes"][0].startswith("unknown agent")


def test_doc_block_contains_canonical_commands():
    block = setup_cmd._doc_block("/x/vb")
    assert "vb explore" in block
    assert "vb research" in block
    assert "vb verify_url" in block
    assert "PEP 668" in block
    assert "/x/vb" in block


def test_resolve_vibatchium_binary_prefers_path(monkeypatch):
    monkeypatch.setattr(shutil, "which",
                        lambda n: "/usr/local/bin/vb" if n == "vb" else None)
    assert setup_cmd.resolve_vibatchium_binary() == "/usr/local/bin/vb"


# ─── on-system discoverability: lean caps + auto-discoverable skill ─────────


def test_setup_lean_caps_are_valid_buckets():
    """LEAN_CAPS must resolve cleanly — a typo'd bucket would make the
    `vb mcp --caps=…` registration command fail and silently break setup."""
    from vibatchium.caps import CAP_BUCKETS, resolve_caps
    resolved = resolve_caps(setup_cmd.LEAN_CAPS)  # raises CapsError on a bad name
    assert resolved == {"core", "nav", "content", "input", "element",
                        "agent", "vision", "session", "pages"}
    assert resolved <= set(CAP_BUCKETS)            # every bucket really exists
    # It is a genuine subset — not accidentally the whole surface.
    assert resolved < set(CAP_BUCKETS)


def test_claude_skill_frontmatter_and_triggers(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    res = setup_cmd.write_claude_skill("/opt/vb")
    assert res == "created"
    skill = tmp_path / ".claude" / "skills" / "vibatchium" / "SKILL.md"
    text = skill.read_text()
    # YAML frontmatter the host matches on to auto-invoke.
    assert text.startswith("---\nname: vibatchium\ndescription: ")
    assert "Cloudflare" in text and "SPA" in text and "log into" in text
    # The 80%-case verbs and the "already installed" guardrail.
    assert "vb explore" in text and "vb research" in text and "vb observe" in text
    assert "/opt/vb" in text
    assert "python -m vibatchium" in text  # the do-NOT trap


def test_claude_skill_idempotent_then_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert setup_cmd.write_claude_skill("/opt/vb") == "created"
    assert setup_cmd.write_claude_skill("/opt/vb") == "unchanged"
    assert setup_cmd.write_claude_skill("/usr/bin/vb") == "updated"  # binary changed


def test_claude_skill_dry_run_never_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert setup_cmd.write_claude_skill("/opt/vb", dry_run=True) == "would-created"
    assert not (tmp_path / ".claude" / "skills" / "vibatchium" / "SKILL.md").exists()


def test_setup_claude_installs_skill_even_without_claude_cli(tmp_path, monkeypatch):
    """setup_claude writes the skill as part of the docs pass; it must not
    depend on the `claude` binary being present (skill is just a file)."""
    monkeypatch.setattr(shutil, "which", lambda n: None)  # no claude CLI
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    res = setup_cmd.setup_claude("/opt/vb", dry_run=False, write_docs=True)
    assert res.skill == "created"
    assert (tmp_path / ".claude" / "skills" / "vibatchium" / "SKILL.md").exists()


def test_setup_cursor_never_writes_global_mdc_rule(tmp_path, monkeypatch):
    """Cursor ignores ~/.cursor/rules/*.mdc (global rules are plain-text in
    Settings; .mdc is project-scoped only) — so setup must NOT write one, and
    must say so instead of pretending it installed an auto-applied rule."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    res = setup_cmd.setup_cursor("/opt/vb", dry_run=False, write_docs=True)
    assert res.skill == "skipped"
    assert not (tmp_path / ".cursor" / "rules").exists()
    assert any("project" in n.lower() and "rule" in n.lower() for n in res.notes)


# ─── registration argv (the gap that let the missing-`--` bug ship) ─────────


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capture_mcp_add(monkeypatch):
    """Mock subprocess.run so `mcp get` reports 'not registered' and `mcp add`
    succeeds, recording every argv. Returns the shared calls list."""
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "get" in argv:                 # _mcp_already_registered probe
            return _FakeCompleted(returncode=1)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(setup_cmd.subprocess, "run", fake_run)
    return calls


def test_setup_claude_registration_argv_has_separator_and_caps(tmp_path, monkeypatch):
    """`claude mcp add` parses `--caps` as its OWN option unless a `--`
    separator precedes the command — without it, registration fails. Pin the
    exact argv so a regression in the separator/caps is caught."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which",
                        lambda n: "/fake/claude" if n == "claude" else None)
    calls = _capture_mcp_add(monkeypatch)
    res = setup_cmd.setup_claude("/opt/vb", dry_run=False, write_docs=False)
    assert res.mcp == "registered"
    add = next(c for c in calls if "add" in c)
    assert add == ["/fake/claude", "mcp", "add", "--scope", "user",
                   "vibatchium", "--", "/opt/vb", "mcp",
                   "--caps", setup_cmd.LEAN_CAPS]
    assert add[add.index("/opt/vb") - 1] == "--"   # `--` immediately before cmd


def test_setup_codex_registration_argv_has_separator_and_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which",
                        lambda n: "/fake/codex" if n == "codex" else None)
    calls = _capture_mcp_add(monkeypatch)
    res = setup_cmd.setup_codex("/opt/vb", dry_run=False, write_docs=False)
    assert res.mcp == "registered"
    add = next(c for c in calls if "add" in c)
    assert add == ["/fake/codex", "mcp", "add", "vibatchium", "--",
                   "/opt/vb", "mcp", "--caps", setup_cmd.LEAN_CAPS]
    assert add[add.index("/opt/vb") - 1] == "--"


# ─── 0.19.1: --force / --caps re-registration ───────────────────────────

def _fake_run_recorder(calls):
    def _run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    return _run


def test_registered_caps_parses_caps_from_mcp_get(monkeypatch):
    class R:
        returncode = 0
        stdout = ("vibatchium:\n  Type: stdio\n  Command: /x/vb\n"
                  "  Args: mcp --caps core,nav,content\n")
    monkeypatch.setattr(setup_cmd.subprocess, "run", lambda *a, **k: R())
    assert setup_cmd._registered_caps("claude", "vibatchium") == "core,nav,content"


def test_registered_caps_none_when_absent(monkeypatch):
    class R:
        returncode = 1
        stdout = 'No MCP server named "vibatchium".'
    monkeypatch.setattr(setup_cmd.subprocess, "run", lambda *a, **k: R())
    assert setup_cmd._registered_caps("claude", "vibatchium") is None


def test_setup_claude_leaves_existing_registration_alone(monkeypatch, tmp_path):
    """A plain re-run must NOT clobber a hand-widened --caps."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/claude")
    monkeypatch.setattr(setup_cmd, "_mcp_already_registered", lambda *a: True)
    monkeypatch.setattr(setup_cmd, "_registered_caps", lambda *a: "all")
    calls: list = []
    monkeypatch.setattr(setup_cmd.subprocess, "run", _fake_run_recorder(calls))
    res = setup_cmd.setup_claude("/x/vb", write_docs=False)
    assert res.mcp == "already"
    assert res.caps == "all"
    assert not any("add" in c for c in calls), "must not re-register without --force"
    # ...but it must SAY the caps drifted, naming the fix.
    assert any("--caps all" in n and "vb setup --force" in n for n in res.notes)


def test_setup_claude_force_removes_then_adds_with_caps(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/claude")
    monkeypatch.setattr(setup_cmd, "_mcp_already_registered", lambda *a: True)
    monkeypatch.setattr(setup_cmd, "_registered_caps", lambda *a: setup_cmd.LEAN_CAPS)
    calls: list = []
    monkeypatch.setattr(setup_cmd.subprocess, "run", _fake_run_recorder(calls))
    res = setup_cmd.setup_claude("/x/vb", write_docs=False,
                                 force=True, caps="lean,search")
    assert res.mcp == "re-registered"
    assert calls[0][1:3] == ["mcp", "remove"]
    assert calls[1][1:3] == ["mcp", "add"]
    assert calls[1][-2:] == ["--caps", "lean,search"]


def test_setup_claude_force_dry_run_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/claude")
    monkeypatch.setattr(setup_cmd, "_mcp_already_registered", lambda *a: True)
    monkeypatch.setattr(setup_cmd, "_registered_caps", lambda *a: "lean")
    calls: list = []
    monkeypatch.setattr(setup_cmd.subprocess, "run", _fake_run_recorder(calls))
    res = setup_cmd.setup_claude("/x/vb", write_docs=False, force=True, dry_run=True)
    assert res.mcp == "would-re-register"
    assert calls == []


def test_setup_cursor_force_rewrites_caps(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"vibatchium": {
        "command": "/x/vb", "args": ["mcp", "--caps", "lean"]}}}))
    res = setup_cmd.setup_cursor("/x/vb", force=True, caps="lean,search")
    assert res.mcp == "re-registered"
    args = json.loads(cfg.read_text())["mcpServers"]["vibatchium"]["args"]
    assert args[-2:] == ["--caps", "lean,search"]


def test_setup_cursor_without_force_reports_drift(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"vibatchium": {
        "command": "/x/vb", "args": ["mcp", "--caps", "lean"]}}}))
    res = setup_cmd.setup_cursor("/x/vb", caps="lean,search")
    assert res.mcp == "already"
    assert any("vb setup --force" in n for n in res.notes)
    # unchanged on disk
    args = json.loads(cfg.read_text())["mcpServers"]["vibatchium"]["args"]
    assert args[-2:] == ["--caps", "lean"]


def test_run_setup_rejects_bad_caps_before_writing_anything(monkeypatch, tmp_path):
    """An invalid --caps must fail HERE, not at the next agent session."""
    from vibatchium.caps import CapsError
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(setup_cmd, "resolve_vibatchium_binary",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("must not get this far")))
    with pytest.raises(CapsError):
        setup_cmd.run_setup(["claude"], caps="lean,serach")


def test_skill_advertises_search(monkeypatch):
    """0.19.0 shipped `vb search` and no installed agent was told."""
    md = setup_cmd._skill_md("/x/vb")
    assert "vb search" in md
    assert "--engine" in md and "attempts" in md
    # The description is the auto-invoke trigger — search must be in it.
    assert "search" in setup_cmd._SKILL_DESCRIPTION.lower()
    # And it must say why the search tool may be missing from MCP.
    assert "vb setup --force --caps lean,search" in md


# ─── the skill must describe the SHIPPED surface, not an imagined one ────

def test_skill_teaches_the_wall_escalation_ladder():
    md = setup_cmd._skill_md("/x/vb")
    assert "vb show" in md, "the human-solves-it path is the first thing to try"
    assert "--headed" in md and "--backend nodriver" in md
    # Headedness/backend are fixed at launch, so every rung needs a close first.
    assert "session close" in md


def test_skill_names_only_extras_that_actually_exist():
    """`[vision]` was written here once; the lane is `vision`, the extra is
    `[llm]`. A skill naming a nonexistent extra sends the agent to a failing
    install command."""
    import tomllib
    real = set(tomllib.loads(Path("pyproject.toml").read_text())
               ["project"]["optional-dependencies"])
    md = setup_cmd._skill_md("/x/vb")
    named = set(re.findall(r"`\[([a-z_]+)\]`", md))
    assert named, "expected the skill to name some extras"
    assert named <= real, f"skill names extras that don't exist: {named - real}"


def test_skill_documents_the_fetch_lane_and_its_no_js_limit():
    md = setup_cmd._skill_md("/x/vb")
    assert "vb fetch" in md
    assert "curl_cffi" in md
    # The limit is the whole reason to pick explore instead — it must be stated.
    assert "No JavaScript runs" in md


def test_skill_warns_extras_belong_to_the_daemon_venv():
    """The venv maze: a caller venv with curl_cffi still fails if the daemon
    was spawned from one without it."""
    md = setup_cmd._skill_md("/x/vb")
    assert "daemon's venv" in md
    assert "vb install" in md, "must point at the self-diagnosis command"
    assert "vb shutdown" in md, "installing without a bounce doesn't take effect"


def test_skill_says_explore_cannot_use_nodriver():
    """explore's ephemeral auto-start passes no backend — silently patchright.
    Guarded here because an agent that assumes otherwise debugs the wrong thing."""
    md = setup_cmd._skill_md("/x/vb")
    assert "only on `vb start`" in md


# ─── the measured anti-patterns the skill exists to prevent ─────────────

def test_skill_teaches_wait_over_blind_sleep():
    """Mined transcripts: blind `sleep` outnumbered `vb wait` 15:1, and removing
    sleeps was almost the entire 34s->7s win on a measured flow."""
    md = setup_cmd._skill_md("/x/vb")
    assert "wait selector" in md
    assert "Never `sleep`" in md


def test_skill_redirects_eval_to_cheaper_verbs():
    """~76% of eval bodies re-implemented a verb in JS. The table names the
    replacement for each of the five common shapes."""
    md = setup_cmd._skill_md("/x/vb")
    for verb in ("vb text", "vb count", "vb click", "vb attr", "vb scroll"):
        assert verb in md, f"{verb} missing from the eval-replacement guidance"
    assert "busy page" in md, "eval-on-a-busy-page trap must be stated"


def test_skill_prefers_act_and_says_no_api_key_needed():
    """act went unused partly because agents assumed it cost inference."""
    md = setup_cmd._skill_md("/x/vb")
    assert "act" in md and "heuristic" in md
    assert "no API key" in md


def test_skill_documents_selector_grammar_and_the_wait_exception():
    """@text:/@label:/@role: resolve for click/fill/count but NOT for
    `wait selector`, which errors 'Unsupported token'. Verified against
    handlers._wait_selector, which bypasses elements.resolve_target."""
    md = setup_cmd._skill_md("/x/vb")
    for form in ("@text:", "@label:", "@role:button"):
        assert form in md
    assert "wait selector` does NOT accept" in md
    assert "text=" in md, "must give the Playwright form that does work"


def test_skill_covers_typing_not_just_clicking():
    md = setup_cmd._skill_md("/x/vb")
    for verb in ("vb --session work fill", "vb --session work type",
                 "vb --session work select", "vb --session work press"):
        assert verb in md
    # fill-vs-type is the distinction agents get wrong.
    assert "real keystrokes" in md


def test_skill_teaches_session_hygiene():
    """233 of 283 session names ever used were never explicitly closed; each
    live session holds ~1.4GB of resident Chrome."""
    md = setup_cmd._skill_md("/x/vb")
    assert "session close" in md
    assert "--ephemeral" in md


def test_skill_examples_survive_str_format():
    """_SKILL_BODY goes through .format(binary=...), so every literal brace in
    an example (JSON, xargs -I{}) must be doubled. This has broken twice."""
    md = setup_cmd._skill_md("/x/vb")
    assert '{"title":"h1"' in md, "JSON example lost its braces"
    assert "xargs -I{}" in md
    assert "{{" not in md and "}}" not in md, "un-rendered doubled braces leaked"
