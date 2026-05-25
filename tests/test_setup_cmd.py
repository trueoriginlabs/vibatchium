"""Tests for the `patchium setup` command — agent CLI registration.

Coverage:
- Detection of installed agents (codex, claude, cursor)
- Idempotent doc-block writer (create / update / unchanged)
- Dry-run never writes
- Cursor JSON config write
- run_setup orchestration with monkeypatched detection
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


from patchium import setup_cmd


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
    result = setup_cmd.setup_cursor("/fake/patchium", dry_run=False, write_docs=False)
    assert result.mcp == "registered"
    cfg = tmp_path / ".cursor" / "mcp.json"
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["patchium"]["command"] == "/fake/patchium"
    assert data["mcpServers"]["patchium"]["args"] == ["mcp"]


def test_setup_cursor_preserves_existing_servers(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    setup_cmd.setup_cursor("/fake/patchium", dry_run=False, write_docs=False)
    data = json.loads(cfg.read_text())
    assert "other" in data["mcpServers"]
    assert "patchium" in data["mcpServers"]


def test_setup_cursor_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    setup_cmd.setup_cursor("/fake/patchium", dry_run=False, write_docs=False)
    second = setup_cmd.setup_cursor("/fake/patchium", dry_run=False, write_docs=False)
    assert second.mcp == "already"


def test_setup_cursor_refuses_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text("not valid json {")
    result = setup_cmd.setup_cursor("/fake/patchium", dry_run=False, write_docs=False)
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
    block = setup_cmd._doc_block("/x/patchium")
    assert "patchium explore" in block
    assert "patchium research" in block
    assert "patchium verify_url" in block
    assert "PEP 668" in block
    assert "/x/patchium" in block


def test_resolve_patchium_binary_prefers_path(monkeypatch):
    monkeypatch.setattr(shutil, "which",
                        lambda n: "/usr/local/bin/patchium" if n == "patchium" else None)
    assert setup_cmd.resolve_patchium_binary() == "/usr/local/bin/patchium"
