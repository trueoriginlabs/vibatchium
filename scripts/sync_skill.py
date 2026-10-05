#!/usr/bin/env python3
"""Regenerate the committed agent-skill artifacts from the package.

Two files are derived, never hand-edited:

* ``skills/vibatchium/SKILL.md`` — what `npx skills add trueoriginlabs/vibatchium`
  (skills.sh) and the Claude Code plugin install. Rendered from the template in
  vibatchium/setup_cmd.py, the same one `vb setup` writes to ~/.claude/skills/.
* the ``version`` in ``.claude-plugin/plugin.json`` — pinned to
  ``vibatchium.__version__`` so plugin users move on releases, not on every
  commit to master.

Edit the template (or bump the version), then run:

    python scripts/sync_skill.py           # rewrite what drifted
    python scripts/sync_skill.py --check   # exit 1 if anything drifted (CI)

tests/test_skill_file_sync.py enforces the same thing in the test suite.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from vibatchium import __version__  # noqa: E402
from vibatchium.setup_cmd import REPO_SKILL_PATH, portable_skill_md  # noqa: E402

PLUGIN_JSON = Path(".claude-plugin") / "plugin.json"


def render_plugin_json(current: str) -> str:
    """``current`` with only its ``version`` set to the package version."""
    data = json.loads(current)
    data["version"] = __version__
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def expected() -> dict[Path, str]:
    """Every derived file -> the exact text it should contain."""
    plugin = REPO / PLUGIN_JSON
    return {
        REPO_SKILL_PATH: portable_skill_md(),
        PLUGIN_JSON: render_plugin_json(plugin.read_text()),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="don't write; exit 1 if a committed file is stale")
    args = ap.parse_args(argv)

    stale = 0
    for rel, want in expected().items():
        target = REPO / rel
        have = target.read_text() if target.exists() else None
        if have == want:
            print(f"{rel}: {'in sync' if args.check else 'unchanged'}")
            continue
        if args.check:
            stale += 1
            print(f"{rel}: STALE — run `python scripts/sync_skill.py`",
                  file=sys.stderr)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(want)
        print(f"{rel}: {'updated' if have is not None else 'created'}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
