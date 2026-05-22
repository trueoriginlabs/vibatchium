"""Observe → act: Stagehand-style intent → action planning.

Two backends, fallback chain:
1. **Heuristic** (default, no API key) — keyword overlap between the user's
   intent and the @eN snapshot entries. Cheap, deterministic, works offline.
2. **LLM** (when ANTHROPIC_API_KEY is set OR --llm forced) — sends the snapshot
   + intent to Claude and parses a structured action plan back.

Both produce the same envelope:
    {
        "intent": "...",
        "url": "...",
        "plan": [{"verb": "click|fill|...", "target": "@eN",
                  "text": "<for fill>", "rationale": "...", "confidence": 0.x}],
        "source": "heuristic" | "llm",
    }

The result is cached to disk so re-running the same (url, intent) skips
inference. `act` reads from that cache and executes via the existing daemon
verbs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import elements
from .paths import CACHE_DIR


CACHE_PATH = CACHE_DIR / "observe-cache.json"
CACHE_TTL_S = 7 * 24 * 3600  # one week


# ─── snapshot parsing ─────────────────────────────────────────────────────


_REF_LINE = re.compile(
    r'^\s*-\s+(?P<role>\S+)'
    r'(?:\s+"(?P<name>[^"]+)")?'
    r'(?:\s+\[(?P<attrs>[^\]@]+)\])?'
    r'(?:[^@]*@(?P<ref>e\d+))'
)


@dataclass
class SnapEntry:
    ref: str
    role: str
    name: str = ""
    raw: str = ""

    @property
    def tokens(self) -> set[str]:
        toks = set()
        for w in re.findall(r"\w+", self.name.lower()):
            toks.add(w)
        toks.add(self.role.lower())
        return toks


def parse_snapshot(yaml_text: str) -> list[SnapEntry]:
    """Pull SnapEntry rows from a Vibium-flavored aria_snapshot YAML."""
    out = []
    for line in yaml_text.splitlines():
        m = _REF_LINE.search(line)
        if not m:
            continue
        out.append(SnapEntry(
            ref="@" + m["ref"],
            role=m["role"] or "",
            name=m["name"] or "",
            raw=line.strip(),
        ))
    return out


# ─── verb inference ───────────────────────────────────────────────────────


_INTENT_VERB_HINTS = [
    ("click", {"click", "press", "tap", "submit", "open", "go", "select",
               "choose", "pick", "follow"}),
    ("fill", {"type", "fill", "enter", "input", "write"}),
    ("hover", {"hover", "move over"}),
    ("check", {"check", "tick", "enable"}),
    ("uncheck", {"uncheck", "untick", "disable"}),
]


def infer_verb(intent: str, role: str | None = None) -> str:
    intent_l = intent.lower()
    for verb, hints in _INTENT_VERB_HINTS:
        if any(h in intent_l for h in hints):
            # role guards: don't `fill` a button, don't `click` a textbox
            if verb == "fill" and role in {"button", "link"}:
                continue
            if verb == "click" and role == "textbox":
                continue
            return verb
    if role == "textbox":
        return "fill"
    return "click"


# ─── heuristic backend ────────────────────────────────────────────────────


_STOPWORDS = {"the", "a", "an", "to", "into", "on", "for", "with", "by", "of",
              "and", "or", "in", "at", "from", "i", "you", "we", "me", "my",
              "your", "it", "this", "that", "is", "was", "are", "were"}


def _intent_tokens(intent: str) -> set[str]:
    return {w for w in re.findall(r"\w+", intent.lower())
            if w not in _STOPWORDS and len(w) > 1}


def heuristic_plan(intent: str, entries: list[SnapEntry]) -> list[dict]:
    """Score each entry by token overlap with intent; return the best as a plan step.

    If the intent has fill-style verbs, the plan will be a two-step click+fill
    where appropriate. Otherwise a single-action click.
    """
    intent_toks = _intent_tokens(intent)
    if not intent_toks or not entries:
        return []

    scored = []
    for e in entries:
        overlap = intent_toks & e.tokens
        score = len(overlap) / max(1, len(intent_toks))
        if score > 0:
            scored.append((score, e, overlap))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], len(x[1].name)))
    score, best, overlap = scored[0]

    verb = infer_verb(intent, role=best.role)
    step = {
        "verb": verb,
        "target": best.ref,
        "rationale": f"name {best.name!r} overlaps with intent on: {sorted(overlap)}",
        "confidence": round(min(0.85, score), 2),  # heuristic caps at 0.85
    }
    if verb == "fill":
        # extract "type X" / "fill X with Y" tail as the value
        m = re.search(r"(?:type|fill|enter|input|write)\s+(?:in\s+|into\s+)?[\"']?([^\"']+?)[\"']?\s*$",
                      intent, re.I)
        if m:
            step["text"] = m.group(1).strip()
    return [step]


# ─── llm backend ──────────────────────────────────────────────────────────


_LLM_SYSTEM = """\
You are an agent action planner for a browser. The user states an intent and
the page's current accessibility-tree snapshot. You return a JSON object:

  {"plan": [{"verb": "click|fill|hover|press|check|uncheck",
             "target": "@eN",
             "text": "<for fill>",     // optional, only with fill
             "rationale": "<one short sentence>",
             "confidence": 0.0..1.0}]}

Rules:
- Reference `@eN` exactly as it appears in the snapshot.
- Single-step plans are preferred; only return multiple steps when truly required.
- If no element fits, return {"plan": []}.
- Output ONLY the JSON object, nothing else.
"""


async def llm_plan(intent: str, url: str, yaml_text: str, *, model: str = "claude-haiku-4-5-20251001") -> list[dict] | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    client = anthropic.Anthropic(api_key=api_key)
    user_msg = (
        f"URL: {url}\n\n"
        f"Snapshot:\n{yaml_text}\n\n"
        f"Intent: {intent}\n\n"
        f"Return only the JSON action plan."
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text  # type: ignore[attr-defined]
        # tolerate ```json ... ``` fenced output
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
        data = json.loads(text)
        return data.get("plan", [])
    except Exception:  # noqa: BLE001
        return None


# ─── cache ────────────────────────────────────────────────────────────────


def _cache_key(url: str, intent: str) -> str:
    return hashlib.sha256(f"{url}\n{intent}".encode()).hexdigest()[:16]


def cache_load() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def cache_save(data: dict) -> None:
    CACHE_PATH.write_text(json.dumps(data, indent=2))


def cache_get(url: str, intent: str) -> dict | None:
    data = cache_load()
    key = _cache_key(url, intent)
    entry = data.get(key)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > CACHE_TTL_S:
        return None
    return entry


def cache_put(url: str, intent: str, result: dict) -> None:
    data = cache_load()
    key = _cache_key(url, intent)
    data[key] = {**result, "ts": time.time()}
    cache_save(data)


# ─── orchestrator ─────────────────────────────────────────────────────────


async def observe(page, intent: str, *, use_llm: bool = False, force_refresh: bool = False) -> dict:
    """Return a cached or freshly-computed plan for `intent` against page's snapshot."""
    snap = await elements.take_snapshot(page)
    yaml_text = snap.text(indent=True)
    url = page.url

    if not force_refresh:
        cached = cache_get(url, intent)
        if cached:
            return {**cached, "cached": True}

    plan: list[dict] = []
    source = "heuristic"
    if use_llm:
        llm = await llm_plan(intent, url, yaml_text)
        if llm:
            plan = llm
            source = "llm"
    if not plan:
        entries = parse_snapshot(yaml_text)
        plan = heuristic_plan(intent, entries)

    result = {
        "intent": intent,
        "url": url,
        "plan": plan,
        "source": source,
    }
    cache_put(url, intent, result)
    return {**result, "cached": False}
