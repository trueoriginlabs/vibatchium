"""`vb setup` — wire vibatchium into agent CLIs (Codex, Claude Code, Cursor).

Detects which agent tools are installed and registers vibatchium as an MCP server
+ writes a small global instructions block pointing at the vibatchium binary.
Idempotent: re-running won't duplicate config.

Prior art: agentic-qa skill distributes via cloning a repo into agent skills
dirs. Vibatchium takes a CLI-driven approach so users run one command instead of
cloning per agent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ─── shared content ─────────────────────────────────────────────────────

# Idempotent block written into ~/.codex/AGENTS.md and ~/.claude/CLAUDE.md.
# Re-running setup replaces between markers without duplicating.
_BLOCK_BEGIN = "<!-- vibatchium-setup-begin -->"
_BLOCK_END = "<!-- vibatchium-setup-end -->"

_DOC_BLOCK_TEMPLATE = """{begin}
## vibatchium — agentic browser on $PATH

`vb` is installed at `{binary}` (also on $PATH as `vb`). When the user
mentions browse / scrape / research / login on a website, shell out:

```bash
vb explore <url>        # one-call: text + screenshot, auto-closes
vb research \\
  --target <url> \\
  --intent "..." --intent "..."
                              # parallel fan-out, writes per-intent markdown
vb verify_url --url <url>
                              # ~50ms DNS pre-check (skip dead URLs)
```

Use WebSearch / WebFetch for Google / news / plain HTML. Use vibatchium for
walled (Cloudflare, Datadome), SPAs, multi-step interactive flows, login.

DO NOT `pip install vibatchium` (Debian/Ubuntu blocks it via PEP 668) — already
installed. DO NOT call `python -m vibatchium.cli` — binary is on $PATH.

Deep docs in the vibatchium repo: `AGENTS.md`, `docs/OPERATIONS.md`,
`docs/CAPABILITIES.md`. Run `vb --help` for the full surface.
{end}
"""


def _doc_block(binary: str) -> str:
    return _DOC_BLOCK_TEMPLATE.format(begin=_BLOCK_BEGIN, end=_BLOCK_END,
                                     binary=binary)


# ─── detection ──────────────────────────────────────────────────────────

@dataclass
class AgentInfo:
    name: str
    detected: bool
    reason: str = ""


def detect_codex() -> AgentInfo:
    binary = shutil.which("codex")
    if binary:
        return AgentInfo("codex", True, f"binary at {binary}")
    cfg = Path.home() / ".codex"
    if cfg.is_dir():
        return AgentInfo("codex", True, f"config dir at {cfg}")
    return AgentInfo("codex", False, "no `codex` on PATH and no ~/.codex")


def detect_claude() -> AgentInfo:
    binary = shutil.which("claude")
    if binary:
        return AgentInfo("claude", True, f"binary at {binary}")
    if (Path.home() / ".claude.json").exists():
        return AgentInfo("claude", True, "~/.claude.json present")
    return AgentInfo("claude", False, "no `claude` on PATH and no ~/.claude.json")


def detect_cursor() -> AgentInfo:
    binary = shutil.which("cursor")
    if binary:
        return AgentInfo("cursor", True, f"binary at {binary}")
    if (Path.home() / ".cursor").is_dir():
        return AgentInfo("cursor", True, "~/.cursor present")
    return AgentInfo("cursor", False, "no `cursor` on PATH and no ~/.cursor")


# ─── utilities ──────────────────────────────────────────────────────────

def resolve_vibatchium_binary() -> str:
    """Best-effort path to the `vb` binary the user will run.

    Prefers `which vb` (PATH-installed), falls back to sys.executable-based
    path so the setup still works when run via `python -m vibatchium.cli`.
    """
    p = shutil.which("vb")
    if p:
        return p
    # Running as `python -m vibatchium.cli`: derive from sys.executable
    parent = Path(sys.executable).parent
    candidate = parent / "vb"
    if candidate.exists():
        return str(candidate)
    # Last resort: bare name (PATH lookup at exec time)
    return "vb"


def ensure_md_block(path: Path, block: str, dry_run: bool = False) -> str:
    """Write `block` (already wrapped in markers) into `path`. Idempotent:
    if a block with the same markers exists, replace it. Otherwise append.

    Returns: "created" | "updated" | "unchanged" (or "would-X" in dry-run).
    """
    def _label(action: str) -> str:
        return f"would-{action}" if dry_run else action
    if path.exists():
        existing = path.read_text()
        if _BLOCK_BEGIN in existing and _BLOCK_END in existing:
            # Replace existing block
            before, _, rest = existing.partition(_BLOCK_BEGIN)
            _, _, after = rest.partition(_BLOCK_END)
            new = before.rstrip() + ("\n\n" if before.strip() else "") + block + after.lstrip()
            if new == existing:
                return "unchanged"
            if not dry_run:
                path.write_text(new)
            return _label("updated")
        # Append (preserve existing content)
        new = existing.rstrip() + "\n\n" + block
        if not dry_run:
            path.write_text(new)
        return _label("updated")
    # Create fresh
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block)
    return _label("created")


def _mcp_already_registered(cli: str, name: str) -> bool:
    try:
        r = subprocess.run([cli, "mcp", "get", name],
                          capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ─── per-agent setup ────────────────────────────────────────────────────

@dataclass
class SetupResult:
    agent: str
    mcp: str = "skipped"      # "registered" | "already" | "skipped" | "failed"
    docs: str = "skipped"     # "created" | "updated" | "unchanged" | "skipped" | "failed"
    notes: list[str] = field(default_factory=list)


def setup_codex(binary: str, dry_run: bool = False,
                write_docs: bool = True) -> SetupResult:
    res = SetupResult("codex")
    cli = shutil.which("codex")
    if cli:
        if _mcp_already_registered("codex", "vibatchium"):
            res.mcp = "already"
        elif dry_run:
            res.mcp = "would-register"
        else:
            try:
                subprocess.run([cli, "mcp", "add", "vibatchium", "--", binary, "mcp"],
                              capture_output=True, check=True, text=True, timeout=20)
                res.mcp = "registered"
            except subprocess.CalledProcessError as e:
                res.mcp = "failed"
                res.notes.append(f"codex mcp add failed: {e.stderr.strip()[:200]}")
    else:
        res.notes.append("`codex` not on PATH — skipping MCP registration")
    if write_docs:
        try:
            res.docs = ensure_md_block(Path.home() / ".codex" / "AGENTS.md",
                                       _doc_block(binary), dry_run=dry_run)
        except OSError as e:
            res.docs = "failed"
            res.notes.append(f"AGENTS.md write failed: {e}")
    return res


def setup_claude(binary: str, dry_run: bool = False,
                 write_docs: bool = True) -> SetupResult:
    res = SetupResult("claude")
    cli = shutil.which("claude")
    if cli:
        if _mcp_already_registered("claude", "vibatchium"):
            res.mcp = "already"
        elif dry_run:
            res.mcp = "would-register"
        else:
            try:
                subprocess.run([cli, "mcp", "add", "--scope", "user",
                              "vibatchium", binary, "mcp"],
                              capture_output=True, check=True, text=True, timeout=20)
                res.mcp = "registered"
            except subprocess.CalledProcessError as e:
                res.mcp = "failed"
                res.notes.append(f"claude mcp add failed: {e.stderr.strip()[:200]}")
    else:
        res.notes.append("`claude` not on PATH — skipping MCP registration")
    if write_docs:
        try:
            res.docs = ensure_md_block(Path.home() / ".claude" / "CLAUDE.md",
                                       _doc_block(binary), dry_run=dry_run)
        except OSError as e:
            res.docs = "failed"
            res.notes.append(f"CLAUDE.md write failed: {e}")
    return res


def setup_cursor(binary: str, dry_run: bool = False,
                 write_docs: bool = True) -> SetupResult:
    """Cursor has no `mcp add` CLI — write ~/.cursor/mcp.json directly."""
    res = SetupResult("cursor")
    cfg = Path.home() / ".cursor" / "mcp.json"
    existing: dict = {}
    if cfg.exists():
        try:
            existing = json.loads(cfg.read_text() or "{}")
        except json.JSONDecodeError:
            res.mcp = "failed"
            res.notes.append("~/.cursor/mcp.json is not valid JSON; refusing to overwrite")
            return res
    servers = existing.setdefault("mcpServers", {})
    if servers.get("vibatchium", {}).get("command") == binary:
        res.mcp = "already"
    else:
        if dry_run:
            res.mcp = "would-register"
        else:
            servers["vibatchium"] = {"command": binary, "args": ["mcp"]}
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(json.dumps(existing, indent=2))
            res.mcp = "registered"
    # Cursor has no widely-supported global instructions file — note it
    if write_docs:
        res.notes.append("Cursor has no user-scope AGENTS.md convention; project rules only")
    return res


# ─── orchestrator ───────────────────────────────────────────────────────

_SETUPPERS = {"codex": setup_codex, "claude": setup_claude, "cursor": setup_cursor}
_DETECTORS = {"codex": detect_codex, "claude": detect_claude, "cursor": detect_cursor}


def run_setup(agents: list[str] | None = None, dry_run: bool = False,
              write_docs: bool = True) -> dict:
    """Top-level entry. `agents=None` → auto-detect all."""
    binary = resolve_vibatchium_binary()
    detected = {n: _DETECTORS[n]() for n in _SETUPPERS}
    if agents is None:
        agents = [n for n, info in detected.items() if info.detected]
    results = []
    for name in agents:
        if name not in _SETUPPERS:
            results.append(SetupResult(name, notes=[f"unknown agent: {name}"]))
            continue
        results.append(_SETUPPERS[name](binary, dry_run=dry_run, write_docs=write_docs))
    return {
        "binary": binary,
        "dry_run": dry_run,
        "detected": {n: {"detected": info.detected, "reason": info.reason}
                    for n, info in detected.items()},
        "results": [{"agent": r.agent, "mcp": r.mcp, "docs": r.docs, "notes": r.notes}
                   for r in results],
    }
