"""Host-keyed Markdown note store under ``CONFIG_DIR/skills/<host>/*.md``.

Layout mirrors browser-use's ``domain-skills/<host>/<task>.md`` so their
directory is import-compatible::

    ~/.config/vibatchium/skills/
      github.com/
        scraping.md
      amazon.com/
        product-search.md

All writes go through ``secure_write`` (0600) / ``secure_mkdir`` (0700). Host
and filename are validated against the shared path-traversal regex so a note
can't escape the skills root.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from ..daemon.paths import (
    CONFIG_DIR, secure_mkdir, secure_write, validate_name,
)

SKILLS_DIR = CONFIG_DIR / "skills"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _skills_dir() -> Path:
    return SKILLS_DIR


def validate_host(host: str) -> str:
    """Validate + normalize a host key (lowercased). Reuses the path-traversal
    regex (``[A-Za-z0-9][A-Za-z0-9._-]*``) which accepts dotted hostnames like
    ``github.com`` while rejecting ``..``, leading dots, and separators."""
    if not isinstance(host, str) or not host:
        raise ValueError("host must be a non-empty string")
    host = host.strip().lower()
    return validate_name(host, kind="host")


def slugify(title: str) -> str:
    """Turn a note title into a safe filename stem (no extension)."""
    slug = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-")
    slug = slug[:48] or "note"
    return slug


def _note_path(host: str, filename: str) -> Path:
    host = validate_host(host)
    stem = filename[:-3] if filename.endswith(".md") else filename
    validate_name(stem, kind="skill filename")
    return _skills_dir() / host / f"{stem}.md"


def list_hosts() -> list[str]:
    base = _skills_dir()
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def list_notes(host: str) -> list[str]:
    host = validate_host(host)
    hdir = _skills_dir() / host
    if not hdir.is_dir():
        return []
    return sorted(p.name for p in hdir.glob("*.md") if p.is_file())


def read_note(host: str, filename: str) -> str:
    path = _note_path(host, filename)
    if not path.is_file():
        raise FileNotFoundError(f"no skill note {host}/{filename}")
    return path.read_text(encoding="utf-8", errors="replace")


def write_note(host: str, body: str, *, title: str | None = None,
               filename: str | None = None, dated: bool = True) -> Path:
    """Write a note. If ``filename`` is omitted it's derived from ``title``
    (slugified). When ``dated`` and the body has no front matter, a verified
    line is prepended (browser-use convention)."""
    host = validate_host(host)
    if not filename:
        if not title:
            raise ValueError("write_note needs either filename or title")
        filename = slugify(title) + ".md"
    path = _note_path(host, filename)
    secure_mkdir(path.parent)
    content = body
    if title and not body.lstrip().startswith("#"):
        header = f"# {title}\n"
        if dated:
            header += f"\n_verified: {time.strftime('%Y-%m-%d')}_\n"
        content = header + "\n" + body
    if not content.endswith("\n"):
        content += "\n"
    secure_write(path, content)
    return path


def remove_note(host: str, filename: str) -> bool:
    path = _note_path(host, filename)
    if not path.is_file():
        return False
    path.unlink()
    # Drop the host dir if now empty.
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return True
