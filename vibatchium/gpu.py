"""Per-session headless GPU WebGL mode (0.13.0).

Headless Chrome's WebGL ``UNMASKED_RENDERER`` defaults to SwiftShader (a software
renderer) — a classic no-GPU/automation tell, and half of the "SwiftShader +
screen==viewport" combo that exists on zero consumer devices. On a host with a DRM
render node, plain-headless Chrome can be steered to the REAL GPU via a single ANGLE
flag pair — no Xvfb, no headed window. Empirically verified on this box (2026-07-05):
baseline ``ANGLE (... SwiftShader driver)`` -> ``ANGLE (Intel, Mesa Intel(R) UHD
Graphics 620 (KBL GT2), OpenGL ES 3.2)``.

Opt-in, per-session, persisted (mirrors proxy.py / geo.py). Default OFF. Enabled ONLY
by an explicit per-session write — `vb gpu set --on` or `vb start --gpu` (both write
gpu.json). There is deliberately NO daemon-wide env default: a global auto-on would
flip every fresh session to the SAME real GPU, and on this box that turns two bot
accounts from an identical-SwiftShader (huge anonymity set) into an identical-Intel
string (a rarer, tighter same-machine join key) — the opposite of de-correlation. So
opt-in is always a deliberate per-session act (no implicit path ⇒ no grandfather clamp,
no re-derivation to stabilize).

De-twinning (per-session render node): on a box with >1 real GPU, different accounts
can be pinned to different render nodes so they report DIFFERENT real renderers — so
two same-box accounts don't share one GPU string (a same-machine join key). Verified
2026-07-05 on this box (Intel UHD 620 + NVIDIA MX150): setting
``__EGL_VENDOR_LIBRARY_FILENAMES`` to the glvnd EGL vendor for a GPU routes the SAME
gl-egl backend to that card — 10_nvidia.json => ``ANGLE (NVIDIA Corporation, NVIDIA
GeForce MX150/PCIe/SSE2, OpenGL ES 3.2)``, 50_mesa.json => the Intel string, both on
OpenGL ES 3.2 (coherent — only the GPU differs). Pin via `vb gpu set --node
nvidia|intel`. Scales only to the number of real GPUs (2 here = 2 de-twinnable
accounts); beyond that the real lever is per-account IP + behavior, not GPU strings.

Honest scope: this is forward fingerprint hardening — it de-correlates the fleet from
the *global* headless-Chrome SwiftShader cluster (a real, monotonic per-session win),
and with per-node pinning it de-twins same-box accounts too. NOT a cure for an
already-tripped account-level throttle — see SPEC/IMPL-headless-gpu-webgl.md.

Resolution: per-session gpu.json {on, node} → host-capability gated. A GPU request on
a host with no accessible render node degrades to SwiftShader + WARN; a node pin with
no matching EGL vendor degrades to the default GPU + WARN. Never a hard fail.
"""
from __future__ import annotations

import glob
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("vibatchium.gpu")

# ── flag set (empirically minimized 2026-07-05 against UNMASKED_RENDERER) ────
# --use-angle=gl-egl is THE load-bearing flag: it routes ANGLE through the EGL/GL
#   path to the DRM render node (only-this + dropping the SwiftShader default =>
#   real Intel; dropping the default alone, with no angle backend => SwiftShader).
# --use-gl=angle selects ANGLE as the GL implementation — the canonical pairing with
#   --use-angle (kept for robustness/self-documentation; gl-egl won even without it,
#   via desktop GL, but the pair is the verified-stable combo).
# --ignore-gpu-blocklist is cross-host insurance: this box's UHD 620 isn't
#   blocklisted, but a blocklisted GPU on another host would silently fall back to
#   SwiftShader without it. Cheap, JS-invisible, clear rationale — not cargo-cult.
GPU_ANGLE_ARGS: list[str] = [
    "--use-gl=angle",
    "--use-angle=gl-egl",
    "--ignore-gpu-blocklist",
]
# Playwright/patchright headless DEFAULTS to drop. --use-angle=swiftshader-webgl is
# the conflicting software-ANGLE default; --disable-gpu forces software when present.
# (Strictly, --use-angle=gl-egl wins by argv last-order even if these stay — verified
# — but removing the conflicting defaults is the explicit, argv-order-independent
# choice.) The exploratory set also carried --disable-software-rasterizer /
# --disable-gpu-compositing; the probe showed NO effect on the renderer, so they are
# dropped as cargo-cult. ignore_default_args silently ignores any entry that isn't an
# actual default, so this list is safe regardless of Playwright's default set.
GPU_IGNORE_DEFAULTS: list[str] = [
    "--use-angle=swiftshader-webgl",
    "--disable-gpu",
]

# Shared WebGL renderer probe — used by tests, the `gpu_info` handler, and the repro.
# Returns {vendor, renderer} or {err}. The experimental-webgl fallback + explicit
# no-context/no-ext branches guard the "GPU flag succeeded but WebGL is absent" mode
# (which would be a LOUDER tell than SwiftShader), so callers can distinguish it.
WEBGL_PROBE = """() => { try {
  const gl = document.createElement('canvas').getContext('webgl')
          || document.createElement('canvas').getContext('experimental-webgl');
  if (!gl) return {err: 'no_gl_context'};
  const e = gl.getExtension('WEBGL_debug_renderer_info');
  if (!e) return {err: 'no_debug_ext'};
  return {vendor: gl.getParameter(e.UNMASKED_VENDOR_WEBGL),
          renderer: gl.getParameter(e.UNMASKED_RENDERER_WEBGL)};
} catch (err) { return {err: String(err)}; } }"""

# Renderer substrings that mean "software" (the tell we're killing).
_SOFTWARE_MARKERS = ("swiftshader", "llvmpipe", "software")


def render_nodes() -> list[str]:
    """DRM render nodes present on the host (e.g. /dev/dri/renderD128)."""
    return sorted(glob.glob("/dev/dri/renderD*"))


def gpu_available() -> bool:
    """True iff at least one render node exists AND is readable+writable by us.
    The feature is host-capability-gated: no accessible render node => a GPU request
    must degrade to SwiftShader (never a hard fail)."""
    return any(os.access(n, os.R_OK | os.W_OK) for n in render_nodes())


def renderer_is_real(renderer: str | None) -> bool:
    """Classify an UNMASKED_RENDERER string as a real GPU (not software/absent).

    Real = present AND not a known software marker. We test against the *software*
    set (the finite, known thing we're killing) rather than a vendor allowlist — an
    allowlist would false-negative an unknown-but-real GPU."""
    r = (renderer or "").lower()
    return bool(r) and not any(s in r for s in _SOFTWARE_MARKERS)


# ── de-twinning: per-session render-node pin via the glvnd EGL vendor ─────────
# The verified lever (2026-07-05): __EGL_VENDOR_LIBRARY_FILENAMES=<glvnd egl vendor
# json> routes ANGLE gl-egl to that vendor's GPU while KEEPING the gl-egl backend, so
# a pinned NVIDIA session and a default Intel session report the same OpenGL-ES-3.2
# ANGLE format with only the GPU differing (a coherent de-twin). A node name maps to a
# substring matched against the egl_vendor json filenames (nvidia -> 10_nvidia.json,
# intel/mesa -> 50_mesa.json). "default"/None = whatever ANGLE picks (Intel here).
EGL_VENDOR_DIR = Path("/usr/share/glvnd/egl_vendor.d")
_NODE_VENDOR_SUBSTR = {"nvidia": "nvidia", "intel": "mesa", "mesa": "mesa"}
VALID_GPU_NODES = frozenset({"nvidia", "intel", "mesa", "default"})


def egl_vendor_for_node(node: str | None) -> str | None:
    """Path to the glvnd EGL vendor JSON that routes ANGLE to `node`'s GPU, or None for
    the host default (unpinned). Set at launch via __EGL_VENDOR_LIBRARY_FILENAMES."""
    if not node:
        return None
    substr = _NODE_VENDOR_SUBSTR.get(node.strip().lower())
    if not substr or not EGL_VENDOR_DIR.is_dir():
        return None
    for p in sorted(EGL_VENDOR_DIR.glob("*.json")):
        if substr in p.name.lower():
            return str(p)
    return None


def available_gpu_nodes() -> list[str]:
    """Named GPU nodes that can actually be pinned on THIS host (those with a matching
    glvnd EGL vendor). Surfaced by `gpu info` so an operator knows what's de-twinnable."""
    return [n for n in ("intel", "nvidia") if egl_vendor_for_node(n)]


def gpu_env_for_node(node: str | None) -> dict:
    """Extra process-env for the browser launch to route ANGLE to `node`'s GPU. Empty
    for the default (unpinned) — so a default GPU launch stays env-identical to v1."""
    vendor = egl_vendor_for_node(node)
    return {"__EGL_VENDOR_LIBRARY_FILENAMES": vendor} if vendor else {}


# ── per-session storage (mirrors geo.py) ────────────────────────────────────


def session_gpu_path(profile_dir: Path) -> Path:
    return profile_dir / "gpu.json"


def save_session_gpu(profile_dir: Path, cfg: dict | None) -> None:
    """Persist {"on": bool, "node": str|None} on the session's profile dir. Takes effect
    on next `start` (close the session first if running). cfg=None removes the file.

    Ensures the profile dir exists first, so the CLI/`start`-time persist works on a
    brand-new profile (before `create()` mkdirs it)."""
    p = session_gpu_path(profile_dir)
    if cfg is None:
        if p.exists():
            p.unlink()
        return
    profile_dir.mkdir(parents=True, exist_ok=True)
    node = cfg.get("node")
    p.write_text(json.dumps({"on": bool(cfg.get("on")),
                             "node": node if isinstance(node, str) and node else None}))
    # 0600 for consistency with the rest of the profile dir (no secrets here, but
    # every vibatchium-written file is 0600 — keep the invariant).
    os.chmod(p, 0o600)


def load_session_gpu(profile_dir: Path) -> dict | None:
    """Return {"on": bool, "node": str|None} for the session, or None if unset/corrupt.
    A pre-v2 gpu.json with no `node` reads as node=None (host default) — backward
    compatible.

    A gpu.json holding valid-but-non-dict JSON (e.g. `42`, `"true"`, `[1]`) must
    degrade to None like any other corrupt file — hence the isinstance guard is INSIDE
    the try (a bare `raw.get` on a non-dict would raise AttributeError past the except
    and crash resolve_gpu / launch)."""
    p = session_gpu_path(profile_dir)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        if not isinstance(raw, dict):
            return None
        node = raw.get("node")
        return {"on": bool(raw.get("on")),
                "node": node if isinstance(node, str) and node else None}
    except Exception:  # noqa: BLE001
        return None


def resolve_gpu(profile_dir: Path, *, name: str = "?") -> bool:
    """Effective GPU-on for this profile: the persisted gpu.json, host-capability
    gated. Opt-in is ALWAYS an explicit per-session write (`vb gpu set --on` /
    `vb start --gpu`), so there is no implicit/ambient source to guard against — no
    grandfather clamp, and nothing to re-derive/stabilize. A pure read: the registry
    calls it on every launch AND relaunch, so the self-heal path re-reads gpu.json for
    free (persist-never-re-derive). A GPU request on a host with no accessible render
    node degrades to OFF + WARN (never a hard fail)."""
    cfg = load_session_gpu(profile_dir)
    want = bool(cfg and cfg["on"])
    if want and not gpu_available():
        log.warning(
            "session %s: GPU WebGL requested but no accessible /dev/dri/renderD* — "
            "falling back to SwiftShader", name)
        return False
    return want


def resolve_gpu_node(profile_dir: Path, *, name: str = "?") -> str | None:
    """Effective render-node pin for this profile's GPU launch (de-twinning): the
    persisted `node` IF it names an EGL vendor available on this host, else None (host
    default). "default"/None → None. A node requested but unavailable degrades to the
    default GPU + WARN (never a hard fail). Pure read; the registry re-reads it on
    relaunch so a self-heal keeps the pin. Only meaningful when GPU is on — the caller
    gates that."""
    cfg = load_session_gpu(profile_dir)
    node = (cfg or {}).get("node")
    if not node or node == "default":
        return None
    if egl_vendor_for_node(node) is None:
        log.warning(
            "session %s: GPU node %r has no matching EGL vendor on this host — using "
            "the default GPU (available: %s)", name, node, available_gpu_nodes())
        return None
    return node
