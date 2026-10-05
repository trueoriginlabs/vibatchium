"""Per-session hardware "persona": de-twin the cheap, coherent surfaces.

N sessions on one box share the box. `vb fleet-check` measured what that means on
the reference laptop (Chrome 153, Intel UHD 620 + NVIDIA MX150, 8 threads): with
default sessions, every headless Chrome reports the SAME 800x600 screen, the SAME
780x580 window at (10,10), and — unless `vb gpu set --node` pins differ — the same
WebGL renderer, canvas and readPixels hashes. Those are exactly the "same sandboxed
image" signals DataDome cited when it fingerprinted Meta's Muse fleet (2026-09-28).
The 800x600 headless screen is also a stand-alone headless tell: no consumer
desktop has reported it in a decade.

A persona changes ONLY what can be changed coherently, at the engine level:

* **screen + work area** — Chrome's headless platform screen is a launch
  switch, ``--screen-info={WxH workAreaTop=.. workAreaBottom=..}``. It is the
  screen the engine itself believes in: ``screen.*``, ``availTop``/``availHeight``,
  CSS ``device-width`` media queries and the window manager clamp all agree.
  Verified on Chrome 153 headless (2026-10-06). Not a JS property patch, so
  there is no getter whose ``toString`` or prototype chain can be inspected.
* **window size + position** — ``--window-size`` / ``--window-position``, chosen
  to fit INSIDE the persona's work area (a window larger than its screen is a
  lie; fleet-check flags it). Maximized windows sit at the work-area origin.
* **GPU render node** — when the session has GPU on (``vb gpu set --on``) but no
  explicit ``--node``, the persona supplies one, balanced across this host's
  available nodes at persona-creation time, so consecutive GPU sessions alternate
  Intel / NVIDIA instead of all landing on the host default. A persona never turns
  the GPU on by itself and never overrides an explicit node pin.

The screen distribution is restricted to sizes a DPR-1 Linux desktop plausibly
drives with an integrated/entry GPU (every entry is <= 2560x1440, well inside the
UHD 620 / MX150 / SwiftShader max viewport of 16384), and DPR stays 1 — a
fractional DPR changes canvas rasterization and screenshot geometry, which is a
capture decision (`vb start --scale`) and not a fingerprint one.

What a persona deliberately does NOT touch (see `vb fleet-check` verdict ``c``):
hardwareConcurrency, deviceMemory, canvas/audio noise, fonts, locale. Each can
only be faked from JS or CDP emulation, and each fake is checkable against
something the engine does for real (worker scheduling throughput, the V8 heap
limit, re-rendering the same canvas twice, glyph metrics, worker
``navigator.languages``). A detectable lie is worse than a twin.

Opt-in, per-session, persisted in ``persona.json`` next to gpu.json/display.json
(for a caller-chosen ``start --profile <dir>`` outside vibatchium's profiles dir,
in the operator-only pins store instead — the same trust rule as browser.json:
a persona.json planted inside such a dir is ignored), generated ONCE from a random seed and never re-derived — the same identity must
come back with the same screen on every launch and every self-heal relaunch (a
logged-in account whose monitor changes size every restart is its own tell).
Default sessions launch byte-identically to before: no persona.json, no args.
Headless patchright only; headed/attach sessions already have a real screen and
window, and the nodriver backend spawns Chrome itself.
"""
from __future__ import annotations

import json
import logging
import os
import random
import secrets
from pathlib import Path
from typing import Any

log = logging.getLogger("vibatchium.persona")

PERSONA_VERSION = 1

# (width, height, weight). Linux desktop / laptop panels at DPR 1, roughly
# following public desktop-resolution share with the HiDPI-only and Windows-
# scaled sizes (1536x864, 1280x720@1.5, 4K@1) left out: on a Linux UA they
# would be rarer than the twin they replace.
SCREENS: tuple[tuple[int, int, int], ...] = (
    (1920, 1080, 44),
    (2560, 1440, 12),
    (1366, 768, 11),
    (1600, 900, 7),
    (1920, 1200, 7),
    (1680, 1050, 5),
    (1440, 900, 5),
    (1280, 1024, 4),
    (1280, 800, 3),
    (2560, 1080, 2),
)

# Desktop shell geometry -> (name, workAreaTop, workAreaBottom, hidden_top, weight).
# GNOME's top bar (32 px at DPR 1), a KDE/Cinnamon-style bottom panel (44 px),
# XFCE's default top panel (28 px). "wayland" is GNOME-on-Wayland, the most common
# modern Linux case, and it is different in two ways Chrome makes visible: the
# compositor gives Chrome NO work-area hint (avail == screen) yet the top bar
# still takes 32 px from a maximized window (hidden_top), and Wayland never
# exposes global window coordinates, so screenX/screenY are always 0.
SHELLS: tuple[tuple[str, int, int, int, int], ...] = (
    ("wayland", 0, 0, 32, 40),
    ("gnome-x11", 32, 0, 0, 30),
    ("kde", 0, 44, 0, 20),
    ("xfce", 28, 0, 0, 10),
)

# A windowed (non-maximized) browser never goes below this — real users don't
# browse in a 700 px-wide window on a desktop panel, and responsive sites would
# serve a different layout.
MIN_WINDOW = (1024, 680)
P_MAXIMIZED = 0.55


FILE_NAME = "persona.json"


def session_persona_path(profile_dir: Path) -> Path:
    """Where this profile's persona lives: in the profile dir when vibatchium
    manages it, else in the operator-only pins store (see browser_binary.py for
    why a caller-chosen dir can't carry its own launch config)."""
    from .daemon.paths import profile_config_dir
    return profile_config_dir(profile_dir) / FILE_NAME


def _pick(rng: random.Random, table, weight_idx: int = -1):
    return rng.choices(table, weights=[row[weight_idx] for row in table], k=1)[0]


def generate_persona(seed: str, *, gpu_nodes: list[str] | None = None,
                     node_usage: dict[str, int] | None = None) -> dict:
    """Deterministic persona for ``seed`` (pure — unit-tested).

    ``gpu_nodes`` are the de-twinnable nodes on this host; ``node_usage`` counts
    how many existing profiles already use each. The least-used node wins (ties
    broken by the seed), so N GPU personas created in a row alternate nodes.
    """
    rng = random.Random(f"vibatchium-persona:{seed}")
    sw, sh, _ = _pick(rng, SCREENS)
    shell, top, bottom, hidden_top, _ = _pick(rng, SHELLS)
    # The area a window can actually occupy: the reported work area, minus any
    # panel the shell doesn't report (wayland's top bar).
    aw, ah = sw, sh - top - bottom - hidden_top
    if rng.random() < P_MAXIMIZED:
        ww, wh, wx, wy = aw, ah, 0, top
    else:
        mw, mh = min(MIN_WINDOW[0], aw), min(MIN_WINDOW[1], ah)
        ww = max(mw, int(aw * rng.uniform(0.62, 0.95))) & ~1
        wh = max(mh, int(ah * rng.uniform(0.72, 0.97))) & ~1
        wx = rng.randint(0, max(0, aw - ww))
        wy = top + rng.randint(0, max(0, ah - wh))
    if shell == "wayland":
        wx = wy = 0  # Wayland: no global window coordinates, ever
    node = None
    if gpu_nodes:
        usage = node_usage or {}
        low = min(usage.get(n, 0) for n in gpu_nodes)
        tied = sorted(n for n in gpu_nodes if usage.get(n, 0) == low)
        node = rng.choice(tied)
    return {"v": PERSONA_VERSION, "seed": seed,
            "screen": [sw, sh], "shell": shell,
            "work_area": {"top": top, "bottom": bottom},
            "window": [ww, wh], "position": [wx, wy],
            "gpu_node": node}


def validate_persona(p: Any) -> dict | None:
    """Return the persona if it is self-consistent, else None.

    The coherence rules fleet-check also enforces at runtime: the window must
    fit inside the work area, the work area inside the screen, all sizes
    positive and inside a sane bound. A corrupt or hand-edited persona.json
    that breaks them degrades to NO persona (the default posture), never to a
    launch with a window larger than its screen.
    """
    try:
        if not isinstance(p, dict) or p.get("v") != PERSONA_VERSION:
            return None
        sw, sh = (int(x) for x in p["screen"])
        ww, wh = (int(x) for x in p["window"])
        wx, wy = (int(x) for x in p["position"])
        top = int(p["work_area"]["top"])
        bottom = int(p["work_area"]["bottom"])
        if not (640 <= sw <= 7680 and 480 <= sh <= 4320):
            return None
        if top < 0 or bottom < 0 or top + bottom >= sh // 2:
            return None
        if ww <= 0 or wh <= 0 or wx < 0 or wy < top:
            return None
        if wx + ww > sw or wy + wh > sh - bottom:
            return None
        node = p.get("gpu_node")
        if node is not None and not isinstance(node, str):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return p


def persona_launch_args(p: dict) -> list[str]:
    """Chrome switches for a (validated) persona. Headless-only: ``--screen-info``
    defines the headless platform screen; headed Chrome uses the real one."""
    sw, sh = p["screen"]
    wa = p["work_area"]
    info = f"{{{sw}x{sh}"
    if wa.get("top"):
        info += f" workAreaTop={int(wa['top'])}"
    if wa.get("bottom"):
        info += f" workAreaBottom={int(wa['bottom'])}"
    info += "}"
    ww, wh = p["window"]
    wx, wy = p["position"]
    return [f"--screen-info={info}", f"--window-size={ww},{wh}",
            f"--window-position={wx},{wy}"]


# ── per-session storage (mirrors gpu.py / display.py) ───────────────────────


def _node_usage(profiles_root: Path | None = None, *,
                exclude: Path | None = None,
                pins_root: Path | None = None) -> dict[str, int]:
    """How many OTHER vibatchium profiles already render on each GPU node — an
    explicit gpu.json pin, else a persona-supplied node for a GPU-on profile.

    Scans only vibatchium's own stores: the managed profiles dir and the pins
    store (whose entries name the caller-chosen dir they configure). Never the
    parent of a caller-chosen dir — for ``start --profile /tmp/x`` that was all
    of /tmp.
    """
    from .daemon import paths as _paths
    from .gpu import load_session_gpu
    if profiles_root is None:
        profiles_root = _paths.PROFILES_DIR
    if pins_root is None:
        pins_root = _paths.PINS_DIR
    skip = os.path.realpath(exclude) if exclude is not None else None
    dirs: list[Path] = []
    if profiles_root.is_dir():
        dirs += [d for d in profiles_root.iterdir() if d.is_dir()]
    if pins_root.is_dir():
        for e in pins_root.iterdir():
            try:
                ref = (e / "profile").read_text().strip()
            except OSError:
                continue
            if ref and os.path.isabs(ref) and os.path.isdir(ref):
                dirs.append(Path(ref))
    usage: dict[str, int] = {}
    for d in dirs:
        if skip is not None and os.path.realpath(d) == skip:
            continue
        g = load_session_gpu(d) or {}
        node = g.get("node")
        if not node:
            node = (load_session_persona(d) or {}).get("gpu_node") if g.get("on") else None
        if node:
            node = "intel" if node == "mesa" else node
            usage[node] = usage.get(node, 0) + 1
    return usage


def save_session_persona(profile_dir: Path, p: dict | None) -> None:
    """Persist a persona (``None`` removes it). 0600 like every profile file."""
    path = session_persona_path(profile_dir)
    if p is None:
        if path.exists():
            path.unlink()
        return
    if validate_persona(p) is None:
        raise ValueError("refusing to persist an incoherent persona")
    from .daemon.paths import ensure_profile_config_dir
    ensure_profile_config_dir(profile_dir)
    path.write_text(json.dumps(p))
    os.chmod(path, 0o600)


def load_session_persona(profile_dir: Path) -> dict | None:
    path = session_persona_path(profile_dir)
    if not path.exists():
        return None
    try:
        return validate_persona(json.loads(path.read_text()))
    except Exception:  # noqa: BLE001 — corrupt file degrades to no persona
        return None


def ensure_session_persona(profile_dir: Path, *, reroll: bool = False) -> dict:
    """Load the profile's persona, creating it once if absent (or ``reroll``).

    Creation is the ONLY place a seed is drawn; after that the persona is a
    pure read, so every launch and self-heal relaunch gets the same identity.
    """
    if not reroll:
        cur = load_session_persona(profile_dir)
        if cur is not None:
            return cur
    from .gpu import available_gpu_nodes
    nodes = available_gpu_nodes()
    usage = _node_usage(exclude=profile_dir) if nodes else {}
    p = generate_persona(secrets.token_hex(8), gpu_nodes=nodes, node_usage=usage)
    save_session_persona(profile_dir, p)
    return p


def resolve_persona(profile_dir: Path, *, name: str = "?") -> dict | None:
    """Effective persona for a launch: the persisted persona.json or None. Pure
    read (the registry calls it on every launch AND relaunch)."""
    from .browser_binary import warn_untrusted_in_profile
    warn_untrusted_in_profile(profile_dir, FILE_NAME, "a persona", name=name)
    p = load_session_persona(profile_dir)
    if p is not None:
        log.info("session %s: persona screen=%sx%s window=%sx%s@%s,%s",
                 name, *p["screen"], *p["window"], *p["position"])
    elif session_persona_path(profile_dir).exists():
        log.warning("session %s: persona.json is corrupt or incoherent — "
                    "launching with the default posture", name)
    return p


def resolve_persona_gpu_node(profile_dir: Path, *, name: str = "?") -> str | None:
    """The persona-supplied GPU node, if any and if it is pinnable on this host.
    Only consulted when GPU is on and gpu.json pins no node of its own."""
    from .gpu import egl_vendor_for_node
    node = (load_session_persona(profile_dir) or {}).get("gpu_node")
    if not node:
        return None
    if egl_vendor_for_node(node) is None:
        log.warning("session %s: persona gpu node %r is not available on this "
                    "host — using the default GPU", name, node)
        return None
    return node
