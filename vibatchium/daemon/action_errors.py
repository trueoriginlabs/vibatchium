"""One line saying why a click, fill or similar timed out.

Playwright's message is a retry log: 20 to 30 lines, the reason somewhere in
the middle, a blank line last. Agents read the tail (`| tail -1`) or skim, so
in a real run one lost eight 30-second clicks to an open drawer without seeing
why. The full log still goes to the daemon log.
"""
from __future__ import annotations

import re

_ACTION_TIMEOUT_RE = re.compile(
    r"^(?:Locator|Page|Frame)\.(click|dblclick|hover|tap|fill|type|press|check|"
    r"uncheck|set_checked|select_option|focus|set_input_files): "
    r"Timeout (\d+)ms exceeded")
_BLOCKER_RE = re.compile(
    r"(<[^\n]*?>)(?: from <[^\n]*?> subtree)? intercepts pointer events")
_RESOLVED_RE = re.compile(r"locator resolved to (<[^\n]*)")
_WAITING_RE = re.compile(r"waiting for (.+)")
_MAX_HTML = 120


def _short(html: str) -> str:
    return html if len(html) <= _MAX_HTML else html[:_MAX_HTML] + "…"


def explain_action_timeout(exc: BaseException) -> str | None:
    """A one-line reason for an action timeout, or None for anything else."""
    text = str(exc)
    head = _ACTION_TIMEOUT_RE.match(text)
    if head is None:
        return None
    action, ms = head.group(1), head.group(2)
    prefix = f"{action} timed out after {ms}ms: "
    resolved = _RESOLVED_RE.search(text)
    if resolved is None:
        waiting = _WAITING_RE.search(text)
        what = waiting.group(1).strip() if waiting else "the target"
        return (prefix + f"nothing on the page matched {what}. Read the page "
                f"again (map_compact) for current refs and names.")
    target = resolved.group(1)
    blockers = _BLOCKER_RE.findall(text)
    if blockers and re.match(r"<(?:html|body)\b", blockers[-1]):
        return (prefix + "an open menu or dialog is making the rest of the page "
                "ignore clicks. Finish choosing in it, or close it with `keys` "
                "Escape, then retry.")
    if blockers:
        if re.match(r'<input\b[^>]*type="(?:radio|checkbox)"', target):
            return (prefix + f"{_short(target)} is a hidden input, and "
                    f"{_short(blockers[-1])} sits over it. Click its visible "
                    f"label instead.")
        return (prefix + f"{_short(blockers[-1])} is covering {_short(target)}. "
                f"Close or finish that first (often a drawer, popup or banner; "
                f"`keys` Escape can do it), then retry.")
    if "not enabled" in text or "not editable" in text:
        return (prefix + f"{_short(target)} is disabled. Something on the page "
                f"has to enable it first, usually a required field or checkbox.")
    if "not visible" in text:
        return (prefix + f"{_short(target)} is on the page but not visible. It "
                f"may sit in a closed menu or tab; open that first.")
    if action == "select_option":
        return (prefix + f"{_short(target)} is hidden, probably behind a custom "
                f"dropdown. Run select on the visible control instead, with the "
                f"option's label.")
    if "not stable" in text:
        return prefix + f"{_short(target)} kept moving (an animation or a reflow)."
    return prefix + f"{_short(target)} never became ready to act on."
