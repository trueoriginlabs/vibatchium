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
from dataclasses import dataclass

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
    # Self-heal metadata: stash the role+name so `act` can rebuild a durable
    # selector that survives snapshot invalidation (Wave 5.3).
    if best.role:
        step["_role"] = best.role
    if best.name:
        step["_name"] = best.name
    durable = build_durable_selector(best.role, best.name)
    if durable:
        step["_durable"] = durable
    if verb == "fill":
        # extract "type X" / "fill X with Y" tail as the value. Two-pass:
        # 1. "fill X with Y" / "fill X into Y" → Y
        # 2. tail-only: "type|fill|enter|input|write <Y>"
        m = re.search(r"(?:fill|type|enter|input|write)\s+\S+\s+with\s+[\"']?([^\"']+?)[\"']?\s*$",
                      intent, re.I)
        if not m:
            m = re.search(r"(?:type|fill|enter|input|write)\s+[\"']?([^\"']+?)[\"']?\s+(?:in|into)\s+",
                          intent, re.I)
        if not m:
            m = re.search(r"(?:type|fill|enter|input|write)\s+(?:in\s+|into\s+)?[\"']?([^\"']+?)[\"']?\s*$",
                          intent, re.I)
        if m:
            step["text"] = m.group(1).strip()
    return [step]


# ─── self-healing selector derivation (Wave 5.3) ─────────────────────────


def build_durable_selector(role: str | None, name: str | None) -> str | None:
    """Build a Playwright selector that survives across snapshots.

    `@eN` refs are snapshot-specific — they're invalid after navigation or
    significant DOM mutation. For cache hits we want a selector that resolves
    against the LIVE page, not a frozen snapshot.

    Preference order:
      1. `role=R[name="N"]`  — most resilient; survives DOM reorder
      2. `text="N"`          — fallback for unnamed-role elements with text
      3. None                — caller falls back to re-observe + @eN
    """
    if not name:
        return None
    safe_name = name.replace('"', '\\"')
    if role:
        return f'role={role}[name="{safe_name}"]'
    return f'text="{safe_name}"'


def cache_invalidate(url: str, intent: str) -> bool:
    """Remove a single (url, intent) entry from the on-disk cache.

    Called by `act` when a cached durable selector fails to resolve — the
    page has likely changed, so we drop the stale plan and re-observe.
    Returns True if an entry was removed.
    """
    data = cache_load()
    key = _cache_key(url, intent)
    if key in data:
        data.pop(key)
        cache_save(data)
        return True
    return False


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


# Params that identify a campaign/referrer, never the page. Leaving them in
# the key meant a single `?utm_source=` busted every entry — the same page
# arrived from an email and from a search and cached twice, so the plan was
# re-derived (an LLM call) for a page we had already solved.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform", "utm_creative_format", "utm_marketing_tactic",
    "gclid", "gclsrc", "dclid", "wbraid", "gbraid",   # Google
    "fbclid",                                          # Meta
    "msclkid",                                         # Microsoft
    "twclid", "ttclid", "igshid", "li_fat_id",         # X / TikTok / IG / LinkedIn
    "mc_cid", "mc_eid",                                # Mailchimp
    "_hsenc", "_hsmi", "hsCtaTracking",                # HubSpot
    "yclid", "_openstat",                              # Yandex
    "ref", "ref_src", "referrer", "source",
})


def _normalize_url(url: str) -> str:
    """Canonical form of a URL for cache keying.

    Drops tracking params and sorts what remains, so links to the same page
    that differ only in campaign tagging or param ORDER share one entry.
    The fragment goes too — it never reaches the server and does not change
    which elements a plan targets.

    Deliberately conservative: everything that is not a known tracking key is
    KEPT, because `?id=42` or `?page=3` genuinely selects a different page and
    collapsing those would serve a stale plan for the wrong content.
    """
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(url)
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in _TRACKING_PARAMS]
        kept.sort()
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(kept), ""))
    except Exception:  # noqa: BLE001
        return url


def _cache_key(url: str, intent: str) -> str:
    return hashlib.sha256(
        f"{_normalize_url(url)}\n{intent}".encode()).hexdigest()[:16]


def cache_load() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def cache_save(data: dict) -> None:
    # Wave 7.5d: observe cache holds (url, intent) → plan tuples. Intents
    # can be sensitive ("log in as admin", "find the SSN field"). 0600.
    from .paths import secure_write as _sw
    _sw(CACHE_PATH, json.dumps(data, indent=2))


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


async def observe(page, intent: str, *, use_llm: bool = False,
                  force_refresh: bool = False, daemon=None) -> dict:
    """Return a cached or freshly-computed plan for `intent`.

    Side-effect: when `daemon` is supplied, writes the freshly-taken AX snapshot
    to `daemon._snapshot` so that subsequent `act`-style verb dispatch (`click @eN`)
    can resolve refs without a separate `map` call.

    Wave 5.3 (self-heal): every returned plan step gets enriched with
    `_role`, `_name`, and `_durable` metadata derived from the current
    snapshot. `act` uses `_durable` to replay cached plans without trusting
    the snapshot-specific @eN ref, and falls back to re-observe if the
    durable selector fails (the page changed).
    """
    snap = await elements.take_snapshot(page)
    yaml_text = snap.text(indent=True)
    url = page.url

    if daemon is not None:
        daemon._prev_snapshot = daemon._snapshot
        daemon._snapshot = snap

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

    # Enrich every plan step with durable-selector metadata for self-heal.
    # Heuristic_plan already does this; LLM plans don't, so look up role+name
    # for each step's @eN against the current snapshot.
    snap_by_ref = {e.ref: e for e in parse_snapshot(yaml_text)}
    for step in plan:
        if "_durable" in step:
            continue  # heuristic_plan already enriched
        tgt = step.get("target", "")
        ref_key = tgt if tgt.startswith("@") else ("@" + tgt)
        entry = snap_by_ref.get(ref_key)
        if entry:
            step.setdefault("_role", entry.role)
            step.setdefault("_name", entry.name)
            d = build_durable_selector(entry.role, entry.name)
            if d:
                step["_durable"] = d

    result = {
        "intent": intent,
        "url": url,
        "plan": plan,
        "source": source,
    }
    cache_put(url, intent, result)
    return {**result, "cached": False}
