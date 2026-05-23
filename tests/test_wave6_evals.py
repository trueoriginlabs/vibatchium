"""Wave 6.2c — evals benchmark tests.

Verifies:
- render_markdown produces a well-formed table
- render_markdown lists errors in a separate section
- render_json is parseable
- min_score returns the lowest among scored cells
- update_readme patches the marked region idempotently
- update_readme leaves no markers = no-op
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path


from patchium.evals import (
    render_markdown, render_json, min_score, update_readme,
)


# ─── markdown rendering ─────────────────────────────────────────────────


def test_render_markdown_basic():
    rows = [
        {"target": "sannysoft", "backend": "patchright", "humanize": False,
         "score": 100, "error": None, "elapsed_s": 5.2},
        {"target": "creepjs", "backend": "patchright", "humanize": False,
         "score": 78, "error": None, "elapsed_s": 8.1},
    ]
    md = render_markdown(rows)
    assert "| Target | Backend | Humanize | Score | Status | Time |" in md
    assert "sannysoft" in md and "100" in md
    assert "creepjs" in md and "78" in md
    assert "OK" in md


def test_render_markdown_includes_humanize_column():
    rows = [{"target": "x", "backend": "y", "humanize": True,
             "score": 50, "error": None, "elapsed_s": 1.0}]
    md = render_markdown(rows)
    assert "| on |" in md  # humanize=True renders as 'on'


def test_render_markdown_errors_listed_separately():
    rows = [
        {"target": "sannysoft", "backend": "patchright", "humanize": False,
         "score": 100, "error": None, "elapsed_s": 5.2},
        {"target": "creepjs", "backend": "patchright", "humanize": False,
         "score": None, "error": "TimeoutError: nav", "elapsed_s": 60.0},
    ]
    md = render_markdown(rows)
    assert "**Errors:**" in md
    assert "TimeoutError" in md
    assert "creepjs" in md


def test_render_markdown_empty():
    md = render_markdown([])
    assert "no eval cells" in md.lower()


# ─── json rendering ─────────────────────────────────────────────────────


def test_render_json_parseable():
    rows = [{"target": "x", "backend": "y", "humanize": False,
             "score": 50, "error": None, "elapsed_s": 1.0}]
    out = render_json(rows)
    parsed = json.loads(out)
    assert parsed["rows"] == rows
    assert "generated_at" in parsed


# ─── min_score gate ─────────────────────────────────────────────────────


def test_min_score_picks_lowest():
    rows = [
        {"score": 100, "error": None},
        {"score": 75, "error": None},
        {"score": 88, "error": None},
    ]
    assert min_score(rows) == 75


def test_min_score_ignores_errored_cells():
    rows = [
        {"score": 100, "error": None},
        {"score": None, "error": "TimeoutError"},
        {"score": 50, "error": None},
    ]
    assert min_score(rows) == 50


def test_min_score_none_when_all_errored():
    rows = [
        {"score": None, "error": "x"},
        {"score": None, "error": "y"},
    ]
    assert min_score(rows) is None


# ─── update_readme idempotent patcher ──────────────────────────────────


def test_update_readme_patches_marked_region():
    with tempfile.TemporaryDirectory() as td:
        readme = Path(td) / "README.md"
        readme.write_text(
            "# Title\n\nIntro.\n\n"
            "## Stealth\n\n"
            "<!-- patchium-evals -->\n"
            "old table\n"
            "<!-- /patchium-evals -->\n\n"
            "## Other\n"
        )
        changed = update_readme(readme, "| new | table |\n|---|---|\n| a | b |")
        assert changed is True
        content = readme.read_text()
        assert "old table" not in content
        assert "new | table" in content
        # Markers still there
        assert "<!-- patchium-evals -->" in content
        assert "<!-- /patchium-evals -->" in content


def test_update_readme_idempotent():
    with tempfile.TemporaryDirectory() as td:
        readme = Path(td) / "README.md"
        readme.write_text(
            "<!-- patchium-evals -->\nold\n<!-- /patchium-evals -->\n"
        )
        update_readme(readme, "fresh content")
        # Second call with same content → no change
        changed2 = update_readme(readme, "fresh content")
        assert changed2 is False


def test_update_readme_no_markers_returns_false():
    with tempfile.TemporaryDirectory() as td:
        readme = Path(td) / "README.md"
        readme.write_text("# README\n\nno markers here\n")
        changed = update_readme(readme, "anything")
        assert changed is False


def test_update_readme_missing_file_returns_false():
    changed = update_readme(Path("/nonexistent/README.md"), "x")
    assert changed is False
