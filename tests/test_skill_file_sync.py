"""The committed agent skill + Claude Code plugin must match the package.

skills/vibatchium/SKILL.md is what `npx skills add trueoriginlabs/vibatchium`
(skills.sh) and the Claude Code plugin install; .claude-plugin/ is the plugin
marketplace. Both are derived from vibatchium (setup_cmd's skill template and
__version__) by scripts/sync_skill.py. These tests fail on drift, and pin the
frontmatter to the Agent Skills spec (agentskills.io/specification) so a
template edit can't silently produce a skill the registries reject.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from vibatchium import __version__, setup_cmd

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / setup_cmd.REPO_SKILL_PATH
PLUGIN = REPO / ".claude-plugin" / "plugin.json"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
RESYNC = "stale — run `python scripts/sync_skill.py` and commit the result"


def _frontmatter(md: str) -> tuple[str, str]:
    assert md.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    fm, sep, body = md[4:].partition("\n---\n")
    assert sep, "frontmatter is never closed"
    return fm, body


# ─── drift ───────────────────────────────────────────────────────────────

def test_committed_skill_matches_template():
    assert SKILL.read_text() == setup_cmd.portable_skill_md(), f"{SKILL}: {RESYNC}"


def test_plugin_version_tracks_package_version():
    """A pinned plugin `version` is what moves users on `claude plugin update`;
    left behind, every release after it is invisible to plugin users."""
    assert json.loads(PLUGIN.read_text())["version"] == __version__, \
        f"{PLUGIN}: {RESYNC}"


def test_sync_script_check_passes():
    script = REPO / "scripts" / "sync_skill.py"
    if not script.exists():  # sdist without scripts/
        pytest.skip("scripts/sync_skill.py not shipped here")
    r = subprocess.run([sys.executable, str(script), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ─── the two renderings ──────────────────────────────────────────────────

def test_portable_skill_is_machine_independent():
    """The committed copy lands on machines without vb — no local path, no
    "already installed", and it must say how to get the CLI."""
    md = setup_cmd.portable_skill_md()
    assert "installed at" not in md
    assert "Already installed" not in md
    assert "/home/" not in md and "/usr/" not in md
    assert "pipx install 'vibatchium[all]'" in md
    assert "vb --version" in md


def test_installed_skill_still_names_the_binary():
    """`vb setup` output keeps its pre-portable shape."""
    md = setup_cmd._skill_md("/x/vb")
    assert "`vb` is installed at `/x/vb` (also on `$PATH` as `vb`)." in md
    assert "Already installed; do **not** `pip install`" in md
    assert "pipx install" not in md


def test_renderings_share_one_body():
    """Only the intro and the install note differ — everything an agent learns
    from the skill is identical in both."""
    a = setup_cmd._skill_md("/x/vb").splitlines()
    b = setup_cmd.portable_skill_md().splitlines()
    common = set(a) & set(b)
    assert len(common) >= 0.95 * len(set(a))


def test_skill_does_not_repeat_the_wait_until_commit_myth():
    """AGENTS.md corrected this: `go` already waits for domcontentloaded (60s)
    and the killed-client lock is released normally."""
    md = setup_cmd.portable_skill_md()
    assert "--wait-until commit --timeout" not in md
    assert "does **not** release the daemon-side lock" not in md
    assert "domcontentloaded" in md


# ─── Agent Skills spec (agentskills.io/specification) ────────────────────

def test_frontmatter_name_follows_spec():
    fm, _ = _frontmatter(SKILL.read_text())
    m = re.search(r"^name: (.+)$", fm, re.M)
    assert m
    name = m.group(1).strip()
    assert 1 <= len(name) <= 64
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), \
        "lowercase alnum + single hyphens, no leading/trailing hyphen"
    assert name == SKILL.parent.name, "name must match the parent directory"


def test_frontmatter_description_follows_spec():
    fm, _ = _frontmatter(SKILL.read_text())
    m = re.search(r"^description: (.+)$", fm, re.M)
    assert m
    desc = m.group(1).strip()
    assert 1 <= len(desc) <= 1024
    assert desc == setup_cmd._SKILL_DESCRIPTION
    # Claude Code rejects XML-ish tags in a skill description.
    assert "<" not in desc and ">" not in desc
    # It is the auto-invoke trigger: what it does AND when to use it.
    assert "Use whenever" in desc


def test_frontmatter_is_valid_yaml():
    """The description is a plain YAML scalar; one stray ': ' or ' #' would
    truncate it or break the parse in every registry."""
    yaml = pytest.importorskip("yaml")
    fm, _ = _frontmatter(SKILL.read_text())
    data = yaml.safe_load(fm)
    assert data == {"name": setup_cmd.SKILL_NAME,
                    "description": setup_cmd._SKILL_DESCRIPTION}


def test_skill_body_within_recommended_size():
    """Spec: keep SKILL.md under 500 lines (it loads whole on activation)."""
    assert len(SKILL.read_text().splitlines()) < 500


def test_only_one_skill_md_in_skills_dir():
    """skills.sh walks skills/ three levels deep; a stray SKILL.md (or one
    under vibatchium/skills/, the unrelated per-host notes feature) would show
    up as a second installable skill."""
    found = sorted(p.relative_to(REPO) for p in (REPO / "skills").rglob("SKILL.md"))
    assert found == [setup_cmd.REPO_SKILL_PATH]
    assert not list((REPO / "vibatchium").rglob("SKILL.md"))


# ─── Claude Code plugin marketplace ──────────────────────────────────────

def test_marketplace_lists_the_repo_root_plugin():
    mk = json.loads(MARKETPLACE.read_text())
    assert mk["name"] and mk["owner"]["name"]
    [entry] = mk["plugins"]
    assert entry["name"] == json.loads(PLUGIN.read_text())["name"], \
        "entry name and plugin.json name must match or installs by name fail"
    # The repo root IS the plugin, so its default skills/ dir is the same file
    # skills.sh installs — one copy, no duplication.
    assert entry["source"] == "."
    # Component fields on the entry would override/append to plugin.json.
    for key in ("skills", "commands", "agents", "hooks", "mcpServers"):
        assert key not in entry


def test_plugin_bundles_vb_mcp_stdio_server():
    plugin = json.loads(PLUGIN.read_text())
    assert plugin["name"] == "vibatchium"
    assert plugin["mcpServers"] == {"vibatchium": {"command": "vb", "args": ["mcp"]}}
    # Must stay a parseable URL or the plugin fails to load.
    assert plugin["homepage"].startswith("https://")
    # No root .mcp.json: it would also register as a PROJECT server for anyone
    # who opens this repo in Claude Code.
    assert not (REPO / ".mcp.json").exists()


def test_plugin_root_has_no_stray_default_components():
    """With source ".", every default plugin dir at the repo root loads. Only
    skills/ is meant to."""
    for d in ("commands", "agents", "hooks", "output-styles", "workflows",
              "themes", "monitors", "bin"):
        assert not (REPO / d).exists(), f"{d}/ at the repo root would load into the plugin"
    assert not (REPO / "settings.json").exists()
