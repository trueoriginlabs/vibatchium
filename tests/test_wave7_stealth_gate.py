"""Wave 7.5d — stealth posture gate.

The existing sannysoft battery and the Cloudflare cold-clear test exercise
JavaScript-runtime signals (`navigator.webdriver`, plugins, WebGL vendor,
the `chrome` object). They do NOT exercise:

  1. Chrome process flags visible via `ps` (e.g. `--no-sandbox` triggers
     a visible yellow infobar AND is a layer-7 detector signal — but is
     invisible to a JS-runtime probe).
  2. File permissions of patchium-written artifacts (cookies, HAR, network
     dumps, vision/observe caches, checkpoints). Inheriting umask 0664 from
     the user's profile leaks session state to every other system user.
  3. The full `chrome.runtime` object shape (Patchright sets `window.chrome`
     but leaves `chrome.runtime` undefined — a strong fingerprint signal).

These gates catch the categories the prior tests missed. If this file
turns red, a regression has restored a previously-fixed leak.
"""
from __future__ import annotations

import os
import stat
import subprocess

import pytest

from patchium.client import call, DaemonError
from patchium.daemon.paths import (
    CACHE_DIR, CONFIG_DIR, DEFAULT_PROFILE_DIR, PROFILES_DIR,
)


# ─── 1. Chrome process flags ─────────────────────────────────────────────


def _chrome_argv_for_profile(profile_name: str) -> list[str]:
    """Read /proc/<pid>/cmdline for any Chrome holding our profile dir.

    Returns the full argv as a list. Empty list = no matching process found.
    """
    try:
        out = subprocess.check_output(
            ["ps", "auxww"], text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001
        pytest.skip("ps not available for process-arg probing")
    for line in out.splitlines():
        if "chrome" in line and profile_name in line and "grep" not in line:
            return line.split()
    return []


def test_no_sandbox_flag_is_not_present(local_server):
    """Wave 7.5c regression test: Patchright's default arg list includes
    `--no-sandbox`. We `ignore_default_args=['--no-sandbox']` in
    browser.py:launch_session. If that's regressed, the yellow infobar
    comes back and the fingerprint becomes obviously bot-shaped."""
    # The conftest default session uses /tmp/patchium-test-profile.
    argv = _chrome_argv_for_profile("patchium-test-profile")
    if not argv:
        pytest.skip("no running Chrome found for the test profile")
    no_sandbox = [a for a in argv if a == "--no-sandbox"]
    assert no_sandbox == [], (
        f"--no-sandbox is present in Chrome argv (regression!): "
        f"{[a for a in argv if a.startswith('--')]}"
    )


def test_chrome_object_is_present_with_real_chrome_subproperties(local_server):
    """Wave 7.5d posture: bare Patchright sets `window.chrome` with the
    real-Chrome sub-properties `loadTimes` and `csi`. `chrome.runtime` is
    intentionally NOT shimmed — Patchright filters the CDP method that
    init-scripts use, because the presence of an init script is itself
    a stronger fingerprint signal than the missing runtime object. This
    test pins the trade-off: chrome exists with the real shape, but
    chrome.runtime stays undefined (documented design choice).
    """
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("eval", {"expr": """(() => ({
        chrome: typeof window.chrome,
        chrome_keys: Object.getOwnPropertyNames(window.chrome).sort(),
        runtime: typeof (window.chrome && window.chrome.runtime),
    }))()"""})
    result = res.get("value", res)
    assert result["chrome"] == "object"
    # Patchright populates loadTimes + csi — real Chrome's standard own-props
    assert "loadTimes" in result["chrome_keys"]
    assert "csi" in result["chrome_keys"]
    # Documented trade-off — chrome.runtime stays undefined on patchright
    # backend. If we ever switch to nodriver or remove the CDP scrub, this
    # assertion can flip.
    assert result["runtime"] == "undefined", (
        "chrome.runtime is now defined — either nodriver backend or someone "
        "added an init-script shim. Update the test + the README trade-off "
        "note if this was intentional."
    )


def test_navigator_webdriver_is_falsy(local_server):
    """Defense-in-depth — keep the navigator.webdriver check in the gate
    set even though Patchright handles it, so regressions are caught here
    rather than only via sannysoft."""
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("eval", {"expr": "navigator.webdriver"})
    val = res.get("value", res)
    assert not val, f"navigator.webdriver is truthy: {val!r}"


# ─── 2. File permission audit ────────────────────────────────────────────


def _mode_bits(p) -> int:
    """Return the perm bits (last three octal digits) of a path."""
    return stat.S_IMODE(os.stat(p).st_mode)


def test_config_and_profile_dirs_are_0700():
    """Wave 7.5d: dirs that hold per-session cookies / login state must
    not be readable / traversable by other system users."""
    for d in (CACHE_DIR, CONFIG_DIR, PROFILES_DIR, DEFAULT_PROFILE_DIR):
        mode = _mode_bits(d)
        assert mode == 0o700, (
            f"{d} mode is 0o{mode:03o}, expected 0o700 — "
            "leaks session metadata to other users"
        )


def test_active_session_file_is_0600(local_server):
    """The active-session file holds the current session name. Whether
    that's sensitive is debatable, but inconsistent perms across patchium
    files are exactly how leaks creep in — enforce 0600."""
    from patchium.daemon.paths import ACTIVE_SESSION_PATH, ACTIVE_PROFILE_PATH
    # Trigger a write
    call("session_use", {"name": "default"})
    for p in (ACTIVE_SESSION_PATH, ACTIVE_PROFILE_PATH):
        if p.exists():
            mode = _mode_bits(p)
            assert mode == 0o600, f"{p} mode 0o{mode:03o} != 0o600"


def test_checkpoint_file_is_0600(local_server):
    """Checkpoints contain the full storage_state (cookies, localStorage,
    auth tokens). Must be 0600 or every user on the box has session-
    hijacking material."""
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("checkpoint_save", {"name": "perm_probe"})
    try:
        cp_path = res["path"]
        mode = _mode_bits(cp_path)
        assert mode == 0o600, (
            f"checkpoint {cp_path} mode 0o{mode:03o} != 0o600 "
            "(cookies world-readable!)"
        )
        # And the parent checkpoints/ dir should be 0700.
        cp_dir = os.path.dirname(cp_path)
        dmode = _mode_bits(cp_dir)
        assert dmode == 0o700, (
            f"checkpoint dir {cp_dir} mode 0o{dmode:03o} != 0o700"
        )
    finally:
        try:
            call("checkpoint_delete", {"name": "perm_probe"})
        except DaemonError:
            pass


def test_network_dump_path_is_0600(local_server, tmp_path):
    """`network dump --path` writes request bodies + Authorization headers
    + cookies. Must be 0600."""
    call("network_start", {"max": 100})
    try:
        call("go", {"url": f"{local_server}/simple.html"})
        target = tmp_path / "net.json"
        call("network_dump", {"path": str(target)})
        mode = _mode_bits(target)
        assert mode == 0o600, (
            f"network dump {target} mode 0o{mode:03o} != 0o600"
        )
    finally:
        call("network_stop", {})


def test_har_file_is_0600(local_server, tmp_path):
    """HAR holds every request body + auth header + cookie for the
    capture window. Must be 0600 regardless of the user's umask."""
    target = tmp_path / "out.har"
    # path is supplied at har_start (har_stop just flushes to it)
    call("har_start", {"path": str(target)})
    call("go", {"url": f"{local_server}/simple.html"})
    call("har_stop", {})
    if target.exists():
        mode = _mode_bits(target)
        assert mode == 0o600, f"HAR {target} mode 0o{mode:03o} != 0o600"


def test_vision_cache_is_0600(monkeypatch, tmp_path):
    """Vision cache holds (screenshot hash, intent) → coords entries.
    The intent corpus can describe sensitive workflows. 0600."""
    from patchium import vision
    # Redirect cache to tmp so we don't clobber the user's real cache
    cache_file = tmp_path / "vision-cache.json"
    monkeypatch.setattr(vision, "_cache_path", lambda: cache_file)
    vision.cache_put(b"fake-screenshot-bytes", "click the login button",
                     {"x": 100, "y": 200, "confidence": 0.9})
    mode = _mode_bits(cache_file)
    assert mode == 0o600, (
        f"vision cache {cache_file} mode 0o{mode:03o} != 0o600"
    )


def test_observe_cache_is_0600(monkeypatch, tmp_path):
    """Observe cache stores (url, intent) → plan. Same intent-corpus
    concern as vision. 0600."""
    from patchium.daemon import observe
    cache_file = tmp_path / "observe-cache.json"
    monkeypatch.setattr(observe, "CACHE_PATH", cache_file)
    observe.cache_put("https://example.com", "log in",
                       {"steps": [{"verb": "click", "target": "@e1"}]})
    mode = _mode_bits(cache_file)
    assert mode == 0o600, (
        f"observe cache {cache_file} mode 0o{mode:03o} != 0o600"
    )


# ─── 3. Sanity: secure_write actually does what it claims ────────────────


def test_secure_write_atomic_and_0600(tmp_path):
    """The helper itself: never leaves the file world-readable, even if
    the user's umask is 0002 / 0022."""
    from patchium.daemon.paths import secure_write
    old_umask = os.umask(0o002)
    try:
        target = tmp_path / "nested" / "file.json"  # also creates parent
        secure_write(target, '{"secret":"value"}')
        mode = _mode_bits(target)
        assert mode == 0o600, (
            f"secure_write left file at mode 0o{mode:03o} != 0o600 "
            "(umask leak)"
        )
        # No leftover temp file
        leftovers = [f for f in target.parent.iterdir()
                     if f.name.startswith(target.name + ".tmp")]
        assert not leftovers, f"tempfile leaked: {leftovers}"
    finally:
        os.umask(old_umask)


def test_secure_mkdir_is_0700(tmp_path):
    """secure_mkdir narrows to 0700 even under umask 0002."""
    from patchium.daemon.paths import secure_mkdir
    old_umask = os.umask(0o002)
    try:
        d = secure_mkdir(tmp_path / "deep" / "nested")
        mode = _mode_bits(d)
        assert mode == 0o700, f"secure_mkdir gave 0o{mode:03o} != 0o700"
    finally:
        os.umask(old_umask)


# ─── 4. Wave 7.5e: daemon log + per-verb audit trail ────────────────────


def test_daemon_log_is_0600(local_server):
    """The daemon log holds lifecycle events including `secret set
    site=X key=Y` lines — site names are metadata-sensitive. Must be
    0600 not 0664. (Audit miss: logging.basicConfig inherits umask.)"""
    from patchium.daemon.paths import LOG_PATH
    # conftest has already started the daemon, which runs basicConfig
    # + the chmod-to-0600. The log file should exist at LOG_PATH and
    # be 0600.
    if not LOG_PATH.exists():
        pytest.skip(f"daemon log not yet at {LOG_PATH}")
    mode = _mode_bits(LOG_PATH)
    assert mode == 0o600, (
        f"daemon log {LOG_PATH} mode 0o{mode:03o} != 0o600 "
        "(basicConfig umask leak)"
    )


def test_verb_log_redacts_secret_values():
    """The redactor must strip `value` from secret_set, `text` from fill,
    `url` from proxy_set, `expr` from eval. Direct unit test on the
    redaction helper, so the live audit log can be trusted."""
    from patchium.daemon.server import _redact_for_log
    # secret_set: value redacted
    r = _redact_for_log("secret_set", {"site": "github", "key": "totp-seed",
                                         "value": "JBSWY3DPEHPK3PXP"})
    assert r["value"] == "<redacted>"
    assert r["site"] == "github"
    # fill: text redacted (could be a password via --use-secret resolution)
    r = _redact_for_log("fill", {"target": "@e2", "text": "hunter2"})
    assert r["text"] == "<redacted>"
    assert r["target"] == "@e2"
    # proxy_set: url redacted (contains user:pass@host)
    r = _redact_for_log("proxy_set",
                        {"url": "http://user:hunter2@proxy.example.com:8080"})
    assert r["url"] == "<redacted>"
    # eval: expr redacted (free-form JS, could embed credentials)
    r = _redact_for_log("eval", {"expr": "fetch('/api', {token: 'sk-secret'})"})
    assert r["expr"] == "<redacted>"
    # status: no redaction (no sensitive fields)
    r = _redact_for_log("status", {"foo": "bar"})
    assert r == {"foo": "bar"}
