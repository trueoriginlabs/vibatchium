"""Per-session timezone coherence (0.6.11).

The host timezone vs the egress IP's geolocation is a *louder* bot tell than any
UA leak: a Chrome whose clock reads `Australia/Sydney` behind a US datacenter
proxy is trivially flagged (compare `Intl.DateTimeFormat().resolvedOptions()
.timeZone` against the IP's country). Proxies make it worse — the whole point of
a proxy is to move the egress IP, but the browser clock stays on the host's zone.

This module persists a per-session `timezone_id` (mirroring `proxy.py`'s
per-session storage) that `launch_session` applies as a protocol-level CDP
Emulation override (`Emulation.setTimezoneOverride`). That rides the CDP protocol
— NOT an injected script — so it survives Patchright's `add_init_script` filter,
AND it propagates to worker threads (verified), so there is no main-vs-worker
inconsistency.

Launch-time + persisted (takes effect on next `start`), exactly like a proxy —
and distinct from the runtime `geolocation` (lat/lng) override. Set it to match
your proxy's country so timezone / IP cohere.

LOCALE NOTE: we deliberately do NOT override `navigator.language`. The only
mechanism (Playwright's per-target `locale` option / `Emulation.setLocaleOverride`)
does not reach worker threads — it would leave a Worker reporting the host
language while the main thread reports the override, a *hard* main-vs-worker
mismatch (the exact class the UA SharedWorker fix eliminated) that is a stronger
tell than the soft "language ≠ IP country" signal it would address. An English
browser physically located abroad (language=en, timezone=local) is a common,
unsuspicious profile; an impossible main≠worker language is not.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Representative IANA timezone per common proxy country. A country may span
# several zones (the US has six); we pick the dominant business zone — a coarse
# match that is still *vastly* more coherent than the host's zone behind a
# foreign IP. For precise control, pass --timezone explicitly (it overrides the
# country lookup).
COUNTRY_TZ: dict[str, str] = {
    "us": "America/New_York",
    "ca": "America/Toronto",
    "gb": "Europe/London",
    "uk": "Europe/London",
    "ie": "Europe/Dublin",
    "de": "Europe/Berlin",
    "fr": "Europe/Paris",
    "es": "Europe/Madrid",
    "it": "Europe/Rome",
    "nl": "Europe/Amsterdam",
    "pl": "Europe/Warsaw",
    "se": "Europe/Stockholm",
    "ch": "Europe/Zurich",
    "ru": "Europe/Moscow",
    "au": "Australia/Sydney",
    "nz": "Pacific/Auckland",
    "jp": "Asia/Tokyo",
    "kr": "Asia/Seoul",
    "cn": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "sg": "Asia/Singapore",
    "in": "Asia/Kolkata",
    "ae": "Asia/Dubai",
    "br": "America/Sao_Paulo",
    "mx": "America/Mexico_City",
    "ar": "America/Argentina/Buenos_Aires",
    "za": "Africa/Johannesburg",
}


class GeoParseError(ValueError):
    """Raised for an invalid timezone or country."""


def _validate_timezone(timezone_id: str) -> None:
    """Reject a timezone Chrome would refuse (`Emulation.setTimezoneOverride`
    throws `Invalid timezone ID`). Validated here so a bad value fails at
    `geo set` time, not silently at the next launch. Falls open if the platform
    has no tz database (don't block a set just because zoneinfo is unavailable).
    """
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover - py<3.9 only
        return
    try:
        ZoneInfo(timezone_id)
    except ZoneInfoNotFoundError as exc:
        raise GeoParseError(f"unknown timezone {timezone_id!r}") from exc
    except (ValueError, OSError):
        raise GeoParseError(f"invalid timezone {timezone_id!r}") from None


def resolve_geo(*, country: str | None = None,
                timezone_id: str | None = None) -> dict:
    """Resolve a {timezone_id} config from a country code and/or an explicit
    timezone. An explicit `timezone_id` wins over the country lookup. Raises
    GeoParseError if nothing usable is produced or the timezone is invalid.
    """
    tz = timezone_id
    if not tz and country:
        cc = country.strip().lower()
        if cc not in COUNTRY_TZ:
            raise GeoParseError(
                f"unknown country {country!r}; known: "
                f"{', '.join(sorted(COUNTRY_TZ))} "
                f"(or pass --timezone explicitly)")
        tz = COUNTRY_TZ[cc]
    if not tz:
        raise GeoParseError("geo set requires --country or --timezone")
    _validate_timezone(tz)
    return {"timezone_id": tz}


# ─── per-session storage (mirrors proxy.py) ────────────────────────────────


def session_geo_path(profile_dir: Path) -> Path:
    return profile_dir / "geo.json"


def save_session_geo(profile_dir: Path, geo: dict | None) -> None:
    """Persist {timezone_id} on the session's profile dir. Takes effect at next
    start. Passing geo=None removes the file."""
    p = session_geo_path(profile_dir)
    if geo is None:
        if p.exists():
            p.unlink()
        return
    p.write_text(json.dumps({"timezone_id": geo.get("timezone_id")}))
    # 0600 for consistency with the rest of the profile dir (no secrets here,
    # but every vibatchium-written file is 0600 — keep the invariant).
    os.chmod(p, 0o600)


def load_session_geo(profile_dir: Path) -> dict | None:
    """Return {timezone_id} for the session, or None if unset."""
    p = session_geo_path(profile_dir)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None
    if raw.get("timezone_id"):
        return {"timezone_id": raw["timezone_id"]}
    return None
