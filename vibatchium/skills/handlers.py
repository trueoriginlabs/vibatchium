"""Built-in daemon verbs for Skills + the go-time surfacing hook.

Verbs (all session-independent → routed ``unlocked``):
  skill_list / skill_show / skill_write / skill_rm / skill_import

``surface_for_url`` is called from the ``go`` / ``explore`` handlers after
navigation; it returns matching notes (injection-scanned) for the daemon to
attach to the response — but only when surfacing is opted in.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import match, safety, store

log = logging.getLogger("vibatchium.skills")

# Surfacing budget — keep the injected context bounded.
_INLINE_MAX_NOTES = 6
_INLINE_CHAR_BUDGET = 4000


def skills_enabled() -> bool:
    """Surfacing is opt-in (notes cost tokens + are an injection surface)."""
    return os.environ.get("VIBATCHIUM_SKILLS", "0").lower() in (
        "1", "true", "yes", "on")


def surface_for_url(url: str) -> dict | None:
    """Return surfaced notes for a navigated URL, or None when disabled / none.

    Each inlined note is injection-scanned; a high-risk note has its content
    *withheld* (content=None, withheld=True) but is still listed with its risk
    signals so the agent knows a poisoned note exists for this host.
    """
    if not skills_enabled():
        return None
    host, notes = match.find_notes(url)
    if not host or not notes:
        return None
    inlined: list[dict] = []
    budget = _INLINE_CHAR_BUDGET
    for fn in notes[:_INLINE_MAX_NOTES]:
        try:
            content = store.read_note(host, fn)
        except (FileNotFoundError, ValueError):
            continue
        scan = safety.scan_injection(content)
        entry = {"file": fn, "risk": scan["risk"], "signals": scan["signals"]}
        if scan["risk"] == "high":
            entry["withheld"] = True
            entry["content"] = None
        elif budget > 0:
            snippet = content[:budget]
            entry["content"] = snippet
            entry["truncated"] = len(content) > len(snippet)
            budget -= len(snippet)
        else:
            entry["content"] = None
            entry["truncated"] = True
        inlined.append(entry)
    return {
        "host": host,
        "notes": notes,
        "inlined": inlined,
        "hint": (f"{len(notes)} skill note(s) on file for {host} — read them "
                 f"before driving the site. Notes flagged safety risk=high "
                 f"are withheld (possible prompt injection)."),
    }


# ─── git/local import ────────────────────────────────────────────────────

def _import_from_dir(src: Path) -> dict:
    """Walk ``<src>/<host>/*.md`` and import each note (secret-scanned)."""
    imported: list[str] = []
    skipped: list[dict] = []
    if not src.is_dir():
        raise FileNotFoundError(f"import source not found: {src}")
    for host_dir in sorted(p for p in src.iterdir() if p.is_dir()):
        host = host_dir.name
        try:
            store.validate_host(host)
        except ValueError:
            skipped.append({"host": host, "reason": "invalid host name"})
            continue
        for note in sorted(host_dir.glob("*.md")):
            body = note.read_text(encoding="utf-8", errors="replace")
            sec = safety.scan_secrets(body)
            if sec["has_secret"]:
                skipped.append({"host": host, "file": note.name,
                                "reason": f"secret-like: {sec['reasons']}"})
                continue
            try:
                store.write_note(host, body, filename=note.name)
                imported.append(f"{host}/{note.name}")
            except (ValueError, OSError) as exc:
                skipped.append({"host": host, "file": note.name,
                                "reason": str(exc)})
    return {"imported": imported, "skipped": skipped}


def _import_from_git(spec: str) -> dict:
    """Clone a ``git+<url>[#subpath]`` shallowly and import its note dir."""
    raw = spec[len("git+"):] if spec.startswith("git+") else spec
    subpath = ""
    if "#" in raw:
        raw, subpath = raw.split("#", 1)
    tmp = Path(tempfile.mkdtemp(prefix="vibatchium-skill-import-"))
    try:
        rc = subprocess.call(
            ["git", "clone", "--depth", "1", raw, str(tmp / "repo")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if rc != 0:
            raise RuntimeError(f"git clone failed (rc={rc}) for {raw}")
        base = tmp / "repo"
        if subpath:
            # Guard against path traversal in the fragment.
            target = (base / subpath).resolve()
            if not str(target).startswith(str(base.resolve())):
                raise ValueError("import subpath escapes the cloned repo")
            base = target
        return _import_from_dir(base)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def register_skill_verbs(daemon) -> None:
    @daemon.handler("skill_list")
    async def _skill_list(d, args):
        host = args.get("host")
        if host:
            return {"host": store.validate_host(host),
                    "notes": store.list_notes(host)}
        hosts = store.list_hosts()
        return {"hosts": [{"host": h, "notes": store.list_notes(h)}
                          for h in hosts]}

    @daemon.handler("skill_show")
    async def _skill_show(d, args):
        host = args.get("host")
        file = args.get("file") or args.get("filename")
        if not host or not file:
            raise ValueError("skill_show requires `host` and `file`")
        content = store.read_note(host, file)
        scan = safety.scan_injection(content)
        return {"host": store.validate_host(host), "file": file,
                "content": content, "injection": scan}

    @daemon.handler("skill_write")
    async def _skill_write(d, args):
        host = args.get("host")
        body = args.get("body")
        title = args.get("title")
        file = args.get("file") or args.get("filename")
        if not host or body is None:
            raise ValueError("skill_write requires `host` and `body`")
        if not title and not file:
            raise ValueError("skill_write requires `title` or `file`")
        allow_secrets = bool(args.get("allow_secrets"))
        sec = safety.scan_secrets(body)
        secret_override = False
        if sec["has_secret"]:
            if not allow_secrets:
                raise ValueError(
                    f"refused: note contains secret-like material "
                    f"({sec['reasons']}). Skills are shareable — never store "
                    f"tokens/passwords/keys in them. Pass allow_secrets=true "
                    f"(`--allow-secrets`) only for a confirmed false positive."
                )
            secret_override = True
            log.warning(
                "skill_write %s: persisting despite secret-like material (%s) "
                "— allow_secrets override", host, sec["reasons"])
        path = store.write_note(host, body, title=title, filename=file)
        return {"host": store.validate_host(host), "file": path.name,
                "path": str(path), "injection": safety.scan_injection(body),
                "secret_override": secret_override}

    @daemon.handler("skill_rm")
    async def _skill_rm(d, args):
        host = args.get("host")
        file = args.get("file") or args.get("filename")
        if not host or not file:
            raise ValueError("skill_rm requires `host` and `file`")
        return {"removed": store.remove_note(host, file)}

    @daemon.handler("skill_import")
    async def _skill_import(d, args):
        source = args.get("source") or args.get("url")
        if not source:
            raise ValueError("skill_import requires `source` (git+url or path)")
        if source.startswith("git+") or source.startswith(("http://", "https://", "git@")):
            return _import_from_git(source)
        return _import_from_dir(Path(source).expanduser())

    for v in ("skill_list", "skill_show", "skill_write", "skill_rm", "skill_import"):
        daemon._verb_lock_class[v] = "unlocked"
