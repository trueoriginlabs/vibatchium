"""Capability buckets — the single source of truth for verb grouping.

Buckets let an MCP client (or REST surface) opt into a subset of the verb
surface, and let a running Goal pin its owned session to a restricted set of
verbs (per-goal caps enforcement). This module is dependency-free (no ``mcp``,
no daemon import) so it can be imported from the dispatcher, the MCP server, and
the REST shim alike.

A verb can belong to multiple buckets; it's permitted if *any* selected bucket
contains it. Dotted plugin verbs (``x.search``) are gated by the ``plugins``
bucket. ``status`` is always permitted.
"""
from __future__ import annotations

CAP_BUCKETS: dict[str, set[str]] = {
    "core":     {"start", "attach", "stop", "status", "set_log_verbs",
                 "explore"},
    "session":  {"session_new", "session_list", "session_use", "session_switch",
                 "session_close", "session_close_all", "session_delete",
                 "profile_list", "profile_new", "profile_use", "profile_delete",
                 "clean"},
    "nav":      {"go", "back", "forward", "reload", "url", "title",
                 "wait_url", "wait_load", "wait_fn",
                 "wait_selector", "wait_ref",
                 "verify_url"},
    "content":  {"text", "html", "eval", "attr", "value", "content", "count", "find"},
    "input":    {"click", "fill", "type", "hover", "press", "keys",
                 "check", "uncheck", "scroll", "is_state", "mouse", "upload",
                 "dblclick", "focus", "select",
                 "humanize_on", "humanize_off", "humanize_status"},
    "element":  {"map", "map_compact", "diff_map", "highlight"},
    "pages":    {"pages", "page_new", "page_switch", "frames", "frame",
                 "page_close"},
    "storage":  {"storage_export", "storage_restore", "cookies",
                 "checkpoint_save", "checkpoint_load", "checkpoint_list",
                 "checkpoint_delete"},
    "network":  {"network_start", "network_stop", "network_dump",
                 "route_add", "route_list", "route_clear", "wait_response",
                 "har_start", "har_stop",
                 "proxy_set", "proxy_clear", "proxy_info"},
    "dialogs":  {"dialog_policy", "download_arm", "download_list", "download_save"},
    "overrides": {"geolocation", "media", "viewport"},
    "vision":   {"screenshot", "screenshot_annotate", "pdf",
                 "vision_click", "vision_find", "vision_type",
                 "vision_stats", "vision_clear_cache",
                 "vision_budget"},
    "devtools": {"record_start", "record_stop",
                 "eval_handle", "handle_eval", "handle_list",
                 "handle_dispose", "handle_dispose_all"},
    "agent":    {"observe", "act", "dismiss_banners"},
    "stealth":  {"fingerprint"},
    "liveview": {"liveview_start", "liveview_stop", "liveview_url"},
    "secrets":  {"secret_init", "secret_set", "secret_list", "secret_delete",
                 "secret_totp", "wait_email_code"},
    "safety":   {"safety_set", "safety_status", "safety_scan", "safety_scan_html"},
    "skills":   {"skill_list", "skill_show", "skill_write", "skill_rm",
                 "skill_import"},
    "goals":    {"goal_new", "goal_list", "goal_show", "goal_events",
                 "goal_next", "goal_step", "goal_ask", "goal_answer",
                 "goal_done", "goal_fail", "goal_pause", "goal_resume",
                 "goal_cancel", "goal_spawn", "goal_tree", "goal_artifacts"},
    # Plugin verbs are discovered dynamically (not in the static TOOLS list).
    # The bucket holds no static names; it's a switch — when caps are active,
    # dotted plugin verbs are permitted only if `plugins` is in the cap set.
    "plugins":  set(),
}

# Permitted regardless of the cap filter — necessities for an agent that needs
# to know what to do when nothing else matches.
ALWAYS_EXPOSED: set[str] = {"status"}


class CapsError(ValueError):
    pass


def resolve_caps(caps_spec: str | None) -> set[str] | None:
    """Parse a ``a,b,c`` cap spec into a bucket set; None means 'no filter'
    (everything permitted). ``all`` is an explicit no-filter. Raises CapsError
    on an unknown bucket name."""
    if not caps_spec:
        return None
    parts = {p.strip().lower() for p in caps_spec.split(",") if p.strip()}
    if not parts or "all" in parts:
        return None
    bad = parts - set(CAP_BUCKETS.keys())
    if bad:
        raise CapsError(
            f"unknown capability bucket(s): {sorted(bad)}. "
            f"Available: {sorted(CAP_BUCKETS.keys())}"
        )
    return parts


def verb_in_caps(verb: str, caps: set[str] | None) -> bool:
    """True if ``verb`` is permitted under ``caps``. None = no filter (all)."""
    if caps is None:
        return True
    if verb in ALWAYS_EXPOSED:
        return True
    if "." in verb:                 # dotted plugin verb
        return "plugins" in caps
    allowed = set(ALWAYS_EXPOSED)
    for bucket in caps:
        allowed |= CAP_BUCKETS.get(bucket, set())
    return verb in allowed
