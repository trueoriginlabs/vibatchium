"""0.9.0 — stealth drift tripwire.

The landscape lesson was "CI-gate the stealth so a silent Patchright change
can't re-arm the Runtime.enable leak without failing the build." The behavioral
posture itself (navigator.webdriver, chrome.runtime shape, de-Headless'd UA,
--no-sandbox, file perms) is already pinned by `test_wave7_stealth_gate.py`, and
`publish.yml` now runs that suite as a release gate.

This file adds the missing piece: a **version drift tripwire**. Patchright's
Runtime.enable patch is the whole product's stealth foundation, and the dep
floats `>=1.59,<2.0` — `uv lock --upgrade` or a fresh `pip install` can bump it
with zero symptom. Pinning a vetted (major, minor) set here means any bump trips
this test ON PURPOSE, forcing a human to re-run the full stealth suite against
the new Patchright and add it to the allowlist — so a regressed posture can't
ship silently.

(A bespoke JS Runtime.enable getter-trap probe was prototyped and removed: it
could not be positively verified to fire in CI — i.e. it risked passing
vacuously — and a green gate that can't detect the thing it guards is worse than
no gate. See CONTRIBUTING.md.)
"""
from __future__ import annotations

import pytest

# (major, minor) Patchright releases whose stealth posture has been verified
# against test_wave7_stealth_gate.py. Bump deliberately after re-running that
# suite — NOT implicitly via a lockfile refresh.
#
# (1, 61) added 2026-07-20 against patchright 1.61.2, vetted the way this
# docstring demands rather than by inspection:
#   - test_wave7_stealth_gate.py (the posture suite): 16 passed
#   - full suite in a throwaway venv on 1.61.2: 1014 passed, 1 skipped
#     (this gate deselected — it fails on an unvetted minor by construction)
#   - CONTROL, same venv downgraded to 1.60.1: identical results
# The control matters: an earlier vetting attempt showed ~79 failures that
# looked like engine regressions and were entirely a broken harness
# (pytest-asyncio absent). Swapping the version back reproduced the failures
# exactly, which is what proved 1.61.2 innocent.
_VETTED_PATCHRIGHT = {(1, 59), (1, 60), (1, 61)}


def test_patchright_version_is_vetted():
    """Fail when the installed Patchright minor isn't vetted, so a silent bump
    forces a re-vet of the stealth suite instead of shipping unverified."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        ver = version("patchright")
    except PackageNotFoundError:
        pytest.skip("patchright not installed")
    parts = ver.split(".")
    try:
        mm = (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        pytest.fail(f"unparseable patchright version {ver!r}")
    assert mm in _VETTED_PATCHRIGHT, (
        f"patchright {ver} (minor {mm}) is NOT vetted for stealth. Re-run "
        f"`pytest tests/test_wave7_stealth_gate.py` against it, then add {mm} to "
        f"_VETTED_PATCHRIGHT here. (Floats >=1.59,<2.0 — a bump is expected to "
        f"trip this gate.)"
    )
