"""`vb fleet-check` — measure cross-session fingerprint TWINNING.

vibatchium's pitch is N persistent, logged-in identities on ONE box. Every one of
them shares the machine: same CPU count, same RAM bucket, same GPU (unless pinned
with `vb gpu set --node`), same fonts, same Chrome build, same headless screen. A
detector that sees two "unrelated" accounts on unrelated sites reporting the same
hardware + rendering + browser internals doesn't need an IP to link them — that is
exactly how DataDome said it fingerprinted Meta's Muse fleet (2026-09-28: "the same
sandboxed image" behind sessions that should have been independent).

This module answers "how twinned are MY sessions?" with numbers instead of a guess:

1. ``PROBE_JS`` runs in each session's page and returns a fingerprint VECTOR —
   one entry per *surface* (UA + client hints, screen, window, canvas, WebGL,
   audio, fonts, WebGPU, voices, permissions, Intl, math, storage quota, ...)
   plus a CreepJS-style main-thread-vs-worker lie check. Heavy values (canvas
   pixels, WebGL readPixels, audio samples) are hashed in-page so the vector
   stays small; small values are returned verbatim so a report can SHOW what is
   twinned, not just that something is.
2. ``compare_vectors`` groups sessions by equal value per surface and labels
   each surface ``identical`` / ``partial`` / ``distinct``.
3. ``twin_score`` is the mean pairwise-equality rate over the measured surfaces
   (100 = every pair of sessions identical on every surface; 0 = no two sessions
   share any surface). It's reported twice: over ALL surfaces, and over the
   *actionable* ones only (excluding surfaces that must stay shared because they
   are the real Chrome binary talking — faking those is a lie, see SURFACES).

The per-surface ``verdict`` letter is the assessment, not a measurement:

  a  already variable per session (geo/proxy/gpu --node / persona)
  b  cheap AND coherent to vary at launch — what `vb persona` does
  c  only coherent at ENGINE level — faking from JS is a detectable lie, so we
     deliberately do NOT (and say why)
  d  intentionally shared — it IS the real Chrome build / host; must match

Read-only and side-effect-free on the sessions it probes except for one
navigation (to a local blank page served by the caller, or about:blank).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any
from collections.abc import Callable, Iterable

log = logging.getLogger("vibatchium.fleet")


# ─── surface catalogue (assessment lives next to the measurement) ────────────
# name -> (verdict, why/lever). Order is the report order.
SURFACES: dict[str, tuple[str, str]] = {
    "user_agent": ("d", "the real Chrome build; must match TLS/H2 + feature set"),
    "ua_client_hints": ("d", "high-entropy hints are the same binary + kernel"),
    "platform": ("d", "Linux x86_64 — the real OS"),
    "hardware_concurrency": (
        "c", "CDP override can't change worker scheduling throughput; 8 is a top "
             "bucket anyway"),
    "device_memory": ("c", "bucketed to 8 by Chrome (>=8 GB) — a huge anonymity set"),
    "js_heap_limit": ("c", "derived from physical RAM inside V8"),
    "screen": ("b", "headless screen via --screen-info (persona)"),
    "window": ("b", "window size via --window-size (persona)"),
    "timezone": ("a", "vb geo set / proxy-inferred geo"),
    "locale": ("c", "a CDP locale override misses workers; en-US is the global "
                    "majority anyway"),
    "intl": ("c", "follows timezone/locale — varies when those do"),
    "canvas_2d": ("a", "GPU-rasterized: follows the GPU node (measured). JS noise "
                       "on top would be detectable by re-reading the canvas"),
    "webgl_renderer": ("a", "vb gpu set --on --node intel|nvidia"),
    "webgl_params": ("a", "follows the GPU/driver actually used"),
    "webgl_pixels": ("a", "follows the GPU/driver actually used"),
    "audio": ("c", "OfflineAudioContext output is the CPU's float math; noise "
                   "is detectable by re-rendering"),
    "fonts": ("c", "claimed-but-unrenderable fonts are a lie; real variation "
                   "needs a per-profile fontconfig with no realistic distribution"),
    "webgpu": ("d", "headless Chrome on this Linux build exposes no adapter, GPU "
                    "or not — shared with every such install"),
    "media_devices": ("d", "headless exposes none; faking devices needs streams"),
    "speech_voices": ("d", "the host's speech-dispatcher voices"),
    "permissions": ("d", "default permission states of the build"),
    "plugins": ("d", "Chrome's fixed built-in PDF plugin list"),
    "media_queries": ("d", "build + headless defaults (color-gamut, pointer, ...)"),
    "math": ("d", "V8/libm float results — identical for the same build+CPU arch"),
    "navigator_misc": ("d", "build defaults (webdriver, pdfViewer, touch points)"),
    "storage_quota": ("d", "a fixed 10 GiB per-origin cap on this build (measured "
                           "on tmpfs and on a 209 GB disk alike) — not box-specific"),
    "lies": ("-", "main-thread vs worker mismatches — should be EMPTY"),
}

# Surfaces that must stay shared because they ARE the real binary/host — the
# 'actionable' twin score excludes them (varying them would be the lie).
INTENTIONALLY_SHARED = frozenset(k for k, (v, _) in SURFACES.items() if v == "d")


# ─── the in-page probe ───────────────────────────────────────────────────────
# One async function, evaluated with page.evaluate (Patchright isolated world —
# navigator/screen/canvas/WebGL/audio read the same values there). Every
# sub-probe is individually try/caught so one missing API can't sink the vector;
# a failure is recorded as {"err": ...} which still compares (and twins) cleanly.
PROBE_JS = r"""async () => {
const out = {};
const h = (str) => {           // cyrb53 — fast, good-enough 53-bit string hash
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761); h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(16);
};
const tryit = async (k, fn) => {
  try { out[k] = await fn(); } catch (e) { out[k] = {err: String(e && e.message || e).slice(0, 120)}; }
};
const withTimeout = (p, ms, dflt) => Promise.race([p, new Promise(r => setTimeout(() => r(dflt), ms))]);
const N = navigator;

await tryit('user_agent', () => N.userAgent);
await tryit('ua_client_hints', async () => {
  const d = N.userAgentData;
  if (!d) return null;
  const hi = await d.getHighEntropyValues(['architecture', 'bitness', 'model',
    'platformVersion', 'fullVersionList', 'uaFullVersion', 'wow64', 'formFactors']);
  return {brands: d.brands.map(b => b.brand + '/' + b.version), mobile: d.mobile,
          platform: d.platform, architecture: hi.architecture, bitness: hi.bitness,
          model: hi.model, platformVersion: hi.platformVersion,
          fullVersionList: (hi.fullVersionList || []).map(b => b.brand + '/' + b.version),
          wow64: hi.wow64, formFactors: hi.formFactors};
});
await tryit('platform', () => N.platform);
await tryit('hardware_concurrency', () => N.hardwareConcurrency);
await tryit('device_memory', () => N.deviceMemory ?? null);
await tryit('js_heap_limit', () => (performance.memory || {}).jsHeapSizeLimit ?? null);
await tryit('screen', () => ({w: screen.width, h: screen.height, aw: screen.availWidth,
  ah: screen.availHeight, al: screen.availLeft, at: screen.availTop,
  cd: screen.colorDepth, pd: screen.pixelDepth, dpr: window.devicePixelRatio,
  ext: screen.isExtended ?? null, orient: (screen.orientation || {}).type || null}));
await tryit('window', () => ({ow: outerWidth, oh: outerHeight, iw: innerWidth,
  ih: innerHeight, sx: screenX, sy: screenY}));
await tryit('timezone', () => ({tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  off: new Date(2026, 0, 1).getTimezoneOffset(), offJul: new Date(2026, 6, 1).getTimezoneOffset()}));
await tryit('locale', () => ({lang: N.language, langs: [...N.languages]}));
await tryit('intl', () => {
  const dt = Intl.DateTimeFormat().resolvedOptions();
  const nf = Intl.NumberFormat().resolvedOptions();
  return {locale: dt.locale, calendar: dt.calendar, numberingSystem: dt.numberingSystem,
    hourCycle: dt.hourCycle ?? null, nfLocale: nf.locale,
    collator: Intl.Collator().resolvedOptions().locale,
    sample: new Date(Date.UTC(2020, 0, 2, 3, 4, 5)).toLocaleString(),
    num: (1234567.891).toLocaleString()};
});
await tryit('canvas_2d', () => {
  const c = document.createElement('canvas'); c.width = 300; c.height = 70;
  const x = c.getContext('2d');
  x.textBaseline = 'top'; x.font = '16px Arial';
  x.fillStyle = '#f60'; x.fillRect(120, 5, 70, 22);
  x.fillStyle = '#069'; x.fillText('Cwm fjordbank glyphs vext quiz \u{1F603}\u{1F984}', 4, 8);
  x.fillStyle = 'rgba(102, 204, 0, 0.7)'; x.font = '18px serif';
  x.fillText('Sphinx of black quartz ☃', 6, 36);
  x.beginPath(); x.arc(250, 40, 20, 0, Math.PI * 2, true); x.closePath();
  x.fillStyle = 'rgba(255,0,255,0.5)'; x.fill();
  return h(c.toDataURL());
});
const glinfo = (() => {
  try {
    const c = document.createElement('canvas'); c.width = 64; c.height = 64;
    const gl = c.getContext('webgl', {preserveDrawingBuffer: true}) || c.getContext('experimental-webgl');
    if (!gl) return {err: 'no_gl_context'};
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    const renderer = {vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
                      renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null};
    const P = ['MAX_TEXTURE_SIZE', 'MAX_VIEWPORT_DIMS', 'MAX_RENDERBUFFER_SIZE',
      'MAX_VERTEX_ATTRIBS', 'MAX_VERTEX_UNIFORM_VECTORS', 'MAX_FRAGMENT_UNIFORM_VECTORS',
      'MAX_VARYING_VECTORS', 'MAX_COMBINED_TEXTURE_IMAGE_UNITS', 'MAX_TEXTURE_IMAGE_UNITS',
      'MAX_VERTEX_TEXTURE_IMAGE_UNITS', 'MAX_CUBE_MAP_TEXTURE_SIZE',
      'ALIASED_LINE_WIDTH_RANGE', 'ALIASED_POINT_SIZE_RANGE', 'SHADING_LANGUAGE_VERSION',
      'VERSION', 'VENDOR', 'RENDERER', 'SUBPIXEL_BITS', 'DEPTH_BITS', 'STENCIL_BITS'];
    const params = {};
    for (const p of P) { const v = gl.getParameter(gl[p]); params[p] = (v && v.length !== undefined && typeof v !== 'string') ? Array.from(v) : v; }
    const prec = [];
    for (const st of ['VERTEX_SHADER', 'FRAGMENT_SHADER'])
      for (const pt of ['HIGH_FLOAT', 'MEDIUM_FLOAT', 'HIGH_INT']) {
        const f = gl.getShaderPrecisionFormat(gl[st], gl[pt]);
        prec.push([f.rangeMin, f.rangeMax, f.precision].join(','));
      }
    const exts = (gl.getSupportedExtensions() || []).slice().sort();
    // readPixels of a smooth-shaded triangle — rasterization + float precision
    const vs = 'attribute vec2 p;varying vec2 v;void main(){v=p;gl_Position=vec4(p,0.,1.);}';
    const fs = 'precision highp float;varying vec2 v;void main(){gl_FragColor=vec4(sin(v.x*7.3)*.5+.5,cos(v.y*5.1)*.5+.5,fract(v.x*v.y*13.7),1.);}';
    const sh = (t, s) => { const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o); return o; };
    const pr = gl.createProgram();
    gl.attachShader(pr, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(pr); gl.useProgram(pr);
    const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-0.9, -0.8, 0.85, -0.6, 0.1, 0.95]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(pr, 'p'); gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    gl.clearColor(0.1, 0.2, 0.3, 1); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    const px = new Uint8Array(64 * 64 * 4); gl.readPixels(0, 0, 64, 64, gl.RGBA, gl.UNSIGNED_BYTE, px);
    let s = ''; for (let i = 0; i < px.length; i += 1) s += String.fromCharCode(px[i]);
    return {renderer, params_hash: h(JSON.stringify(params) + '|' + prec.join(';')),
            ext_hash: h(exts.join(',')), n_ext: exts.length,
            max_texture: params.MAX_TEXTURE_SIZE, pixels: h(s)};
  } catch (e) { return {err: String(e && e.message || e).slice(0, 120)}; }
})();
out.webgl_renderer = glinfo.err ? glinfo : glinfo.renderer;
out.webgl_params = glinfo.err ? glinfo : {params: glinfo.params_hash, exts: glinfo.ext_hash,
  n_ext: glinfo.n_ext, max_texture: glinfo.max_texture};
out.webgl_pixels = glinfo.err ? glinfo : glinfo.pixels;
await tryit('audio', async () => {
  const C = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  if (!C) return null;
  const ctx = new C(1, 5000, 44100);
  const osc = ctx.createOscillator(); osc.type = 'triangle'; osc.frequency.value = 10000;
  const cmp = ctx.createDynamicsCompressor();
  cmp.threshold.value = -50; cmp.knee.value = 40; cmp.ratio.value = 12;
  cmp.attack.value = 0; cmp.release.value = 0.25;
  osc.connect(cmp); cmp.connect(ctx.destination); osc.start(0);
  const buf = await withTimeout(ctx.startRendering(), 3000, null);
  if (!buf) return {err: 'timeout'};
  const d = buf.getChannelData(0); let sum = 0;
  for (let i = 4500; i < 5000; i++) sum += Math.abs(d[i]);
  return {sum: sum.toString(), hash: h(Array.from(d.slice(4000, 5000)).join(','))};
});
await tryit('fonts', () => {
  const FONTS = %FONTS%;
  const c = document.createElement('canvas').getContext('2d');
  const txt = 'mmmmmmmmmmlli1WQ@#&%';
  const bases = ['monospace', 'sans-serif', 'serif'];
  const w = {};
  for (const b of bases) { c.font = '72px ' + b; w[b] = c.measureText(txt).width; }
  const present = [];
  for (const f of FONTS) {
    for (const b of bases) {
      c.font = '72px "' + f + '", ' + b;
      if (c.measureText(txt).width !== w[b]) { present.push(f); break; }
    }
  }
  return {n: present.length, of: FONTS.length, hash: h(present.join('|')), present};
});
await tryit('webgpu', async () => {
  if (!N.gpu) return null;
  const a = await withTimeout(N.gpu.requestAdapter(), 3000, 'timeout');
  if (a === 'timeout') return {err: 'timeout'};
  if (!a) return {adapter: null};
  const info = a.info || {};
  const lim = {}; for (const k of ['maxTextureDimension2D', 'maxBufferSize', 'maxComputeWorkgroupStorageSize', 'maxStorageBufferBindingSize']) lim[k] = a.limits[k];
  return {vendor: info.vendor, architecture: info.architecture, device: info.device,
          description: info.description, features: h([...a.features].sort().join(',')),
          limits: lim};
});
await tryit('media_devices', async () => {
  if (!N.mediaDevices || !N.mediaDevices.enumerateDevices) return null;
  const ds = await N.mediaDevices.enumerateDevices(); const k = {};
  for (const d of ds) k[d.kind] = (k[d.kind] || 0) + 1;
  return k;
});
await tryit('speech_voices', async () => {
  if (!window.speechSynthesis) return null;
  let v = speechSynthesis.getVoices();
  if (!v.length) {
    await withTimeout(new Promise(r => speechSynthesis.addEventListener('voiceschanged', r, {once: true})), 1000, null);
    v = speechSynthesis.getVoices();
  }
  return {n: v.length, hash: h(v.map(x => x.name + '|' + x.lang + '|' + x.localService).join(';'))};
});
await tryit('permissions', async () => {
  const r = {};
  for (const n of ['geolocation', 'notifications', 'camera', 'microphone', 'midi',
                   'clipboard-read', 'clipboard-write', 'persistent-storage',
                   'background-sync', 'accelerometer', 'local-fonts', 'storage-access']) {
    try { r[n] = (await N.permissions.query({name: n})).state; } catch (e) { r[n] = 'err'; }
  }
  return r;
});
await tryit('plugins', () => ({plugins: Array.from(N.plugins || []).map(p => p.name),
  mime: Array.from(N.mimeTypes || []).map(m => m.type)}));
await tryit('media_queries', () => {
  const q = (s) => matchMedia(s).matches;
  return {dark: q('(prefers-color-scheme: dark)'), rm: q('(prefers-reduced-motion: reduce)'),
    contrast: q('(prefers-contrast: more)'), p3: q('(color-gamut: p3)'),
    hdr: q('(dynamic-range: high)'), fine: q('(pointer: fine)'), hover: q('(hover: hover)'),
    anyCoarse: q('(any-pointer: coarse)'), forced: q('(forced-colors: active)'),
    invert: q('(inverted-colors: inverted)')};
});
await tryit('math', () => [Math.tan(-1e300), Math.acosh(1e300), Math.asinh(1), Math.atanh(0.5),
  Math.cbrt(Math.PI), Math.cosh(10), Math.expm1(1), Math.log1p(10), Math.sinh(1),
  Math.tanh(0.5), Math.pow(Math.PI, -100), Math.sin(1e300)].map(String));
await tryit('navigator_misc', () => ({webdriver: N.webdriver, pdf: N.pdfViewerEnabled,
  touch: N.maxTouchPoints, cookies: N.cookieEnabled, dnt: N.doNotTrack, vendor: N.vendor,
  productSub: N.productSub, ect: (N.connection || {}).effectiveType ?? null,
  online: N.onLine, gpc: N.globalPrivacyControl ?? null}));
await tryit('storage_quota', async () => {
  if (!N.storage || !N.storage.estimate) return null;
  const e = await N.storage.estimate(); return e.quota;
});
// CreepJS-style lie check: a value faked on the main thread but not in a worker
// (or vice versa) is a louder tell than the twin it was meant to hide.
await tryit('lies', async () => {
  const src = `onmessage = async () => { let hi = null; try { hi = navigator.userAgentData ? await navigator.userAgentData.getHighEntropyValues(['platformVersion']) : null; } catch (e) {}
    postMessage({hc: navigator.hardwareConcurrency, ua: navigator.userAgent,
    platform: navigator.platform, langs: [...navigator.languages], dm: navigator.deviceMemory ?? null,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    uadp: navigator.userAgentData ? navigator.userAgentData.platform : null,
    pv: hi ? hi.platformVersion : null}); }`;
  const url = URL.createObjectURL(new Blob([src], {type: 'application/javascript'}));
  const wk = new Worker(url);
  const res = await withTimeout(new Promise(r => { wk.onmessage = (e) => r(e.data); wk.postMessage(1); }), 3000, null);
  wk.terminate(); URL.revokeObjectURL(url);
  if (!res) return {err: 'worker_timeout'};
  let pv = null;
  try { pv = N.userAgentData ? (await N.userAgentData.getHighEntropyValues(['platformVersion'])).platformVersion : null; } catch (e) {}
  const main = {hc: N.hardwareConcurrency, ua: N.userAgent, platform: N.platform,
    langs: [...N.languages], dm: N.deviceMemory ?? null,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    uadp: N.userAgentData ? N.userAgentData.platform : null, pv};
  const mism = [];
  for (const k of Object.keys(main)) if (JSON.stringify(main[k]) !== JSON.stringify(res[k])) mism.push(k);
  // screen must contain the window; a window bigger than its screen is a lie
  if (outerWidth > screen.width || outerHeight > screen.height) mism.push('window>screen');
  if (screen.availWidth > screen.width || screen.availHeight > screen.height) mism.push('avail>screen');
  return mism;
});
out._secure = window.isSecureContext;
out._url = location.href;
return out;
}"""

# Font list for the availability probe: Linux defaults (what this box would really
# have), cross-platform web fonts, and Windows/macOS faces whose PRESENCE on a
# Linux-UA session would itself be a lie. Measured by glyph width vs. 3 generic
# fallbacks, so only fonts that actually RENDER count — the honest version of the
# check (document.fonts.check() answers true for fonts that merely don't need
# loading).
FONT_LIST: tuple[str, ...] = (
    "Arial", "Helvetica", "Times New Roman", "Courier New", "Verdana", "Georgia",
    "Tahoma", "Trebuchet MS", "Comic Sans MS", "Impact", "Segoe UI", "Calibri",
    "Cambria", "Consolas", "Candara", "Lucida Console", "MS Gothic", "SimSun",
    "Meiryo", "Malgun Gothic", "Arial Unicode MS", "Menlo", "Monaco",
    "Helvetica Neue", "Lucida Grande", "SF Pro Text", "Apple Color Emoji",
    "Liberation Sans", "Liberation Serif", "Liberation Mono", "DejaVu Sans",
    "DejaVu Serif", "DejaVu Sans Mono", "Ubuntu", "Ubuntu Mono", "Ubuntu Condensed",
    "Noto Sans", "Noto Serif", "Noto Sans Mono", "Noto Color Emoji",
    "Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans Arabic", "Noto Sans Hebrew",
    "Cantarell", "Droid Sans", "FreeSans", "FreeSerif", "FreeMono", "Nimbus Sans",
    "Nimbus Roman", "Nimbus Mono PS", "URW Bookman", "URW Gothic", "C059", "P052",
    "Z003", "Roboto", "Open Sans", "Lato", "Source Code Pro", "Fira Code",
    "Fira Sans", "JetBrains Mono", "Inter", "Hack", "Bitstream Vera Sans",
    "Lohit Devanagari", "Kacst Book", "Waree", "Loma", "Padauk", "Purisa",
    "Sawasdee", "Tlwg Typo", "Umpush", "Norasi", "Garuda", "Kinnari",
    "Gubbi", "Samanata", "Pothana2000", "Khmer OS", "Mitra Mono", "Rachana",
)

PROBE_JS = PROBE_JS.replace("%FONTS%", json.dumps(list(FONT_LIST)))


# ─── pure diff / scoring (no browser — unit-tested) ─────────────────────────


def surface_digest(value: Any) -> str:
    """Stable short digest of one surface's value (canonical JSON)."""
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _surfaces_of(vectors: dict[str, dict]) -> list[str]:
    seen: list[str] = [s for s in SURFACES]
    for vec in vectors.values():
        for k in vec:
            if not k.startswith("_") and k not in seen:
                seen.append(k)
    return seen


def compare_vectors(vectors: dict[str, dict]) -> list[dict]:
    """Group sessions by equal value, per surface.

    ``vectors`` maps session name -> probe vector. Returns one row per surface:
    ``{surface, verdict, lever, status, groups, pair_equal, n, sample}`` where
    ``groups`` is a list of session-name lists sharing one value (largest
    first), ``pair_equal`` the fraction of session PAIRS with equal values, and
    ``status`` one of identical / partial / distinct / single / missing.
    ``lies`` is special-cased: equal-and-empty is the goal, not a twin, so it is
    reported but never counted toward the twin score.
    """
    names = sorted(vectors)
    rows: list[dict] = []
    for surf in _surfaces_of(vectors):
        present = [n for n in names if surf in vectors[n]]
        verdict, lever = SURFACES.get(surf, ("?", ""))
        row: dict[str, Any] = {"surface": surf, "verdict": verdict, "lever": lever,
                               "n": len(present)}
        if not present:
            row.update(status="missing", groups=[], pair_equal=None, sample=None)
            rows.append(row)
            continue
        by_digest: dict[str, list[str]] = {}
        for n in present:
            by_digest.setdefault(surface_digest(vectors[n][surf]), []).append(n)
        groups = sorted(by_digest.values(), key=lambda g: (-len(g), g))
        row["groups"] = groups
        row["sample"] = {g[0]: vectors[g[0]][surf] for g in groups}
        k = len(present)
        if k < 2:
            row["status"], row["pair_equal"] = "single", None
        else:
            pairs = k * (k - 1) / 2
            eq = sum(len(g) * (len(g) - 1) / 2 for g in groups)
            row["pair_equal"] = round(eq / pairs, 4)
            row["status"] = ("identical" if len(groups) == 1
                             else "distinct" if len(groups) == k else "partial")
        rows.append(row)
    return rows


def twin_score(rows: list[dict], *, actionable_only: bool = False) -> float | None:
    """Mean pairwise-equality over measured surfaces, 0-100.

    100 = every pair of sessions matches on every surface (a perfect fleet of
    twins); 0 = no pair shares any surface. ``actionable_only`` drops the
    intentionally-shared (verdict ``d``) surfaces — the number a persona can
    actually move. ``lies`` never counts (it isn't a fingerprint surface).
    """
    vals = [r["pair_equal"] for r in rows
            if r.get("pair_equal") is not None and r["surface"] != "lies"
            and not (actionable_only and r["surface"] in INTENTIONALLY_SHARED)]
    if not vals:
        return None
    return round(100.0 * sum(vals) / len(vals), 1)


def lies_by_session(vectors: dict[str, dict]) -> dict[str, list]:
    """Per-session list of main-vs-worker / screen-containment mismatches."""
    out = {}
    for n, vec in vectors.items():
        v = vec.get("lies")
        out[n] = v if isinstance(v, list) else ([] if v is None else [v])
    return out


def summarize(vectors: dict[str, dict], meta: dict[str, dict] | None = None) -> dict:
    rows = compare_vectors(vectors)
    counts = {"identical": 0, "partial": 0, "distinct": 0}
    for r in rows:
        if r["surface"] != "lies" and r["status"] in counts:
            counts[r["status"]] += 1
    return {"sessions": sorted(vectors), "meta": meta or {}, "rows": rows,
            "counts": counts, "twin_score": twin_score(rows),
            "twin_score_actionable": twin_score(rows, actionable_only=True),
            "lies": lies_by_session(vectors)}


# ─── rendering ───────────────────────────────────────────────────────────────


def _short(v: Any, width: int = 46) -> str:
    if isinstance(v, dict) and set(v) >= {"n", "hash"}:
        s = f"{v['n']} ({v['hash'][:8]})"
    elif isinstance(v, (dict, list)):
        s = json.dumps(v, separators=(",", ":"), default=str)
    else:
        s = str(v)
    s = s.replace("|", "/")
    return s if len(s) <= width else s[: width - 1] + "…"


def render_markdown(summary: dict, *, title: str = "fleet-check") -> str:
    names = summary["sessions"]
    lines = [f"### {title} — {len(names)} sessions: {', '.join(names)}", ""]
    meta = summary.get("meta") or {}
    if meta:
        for n in names:
            m = meta.get(n)
            if m:
                lines.append(f"- `{n}`: " + ", ".join(f"{k}={v}" for k, v in m.items()))
        lines.append("")
    c = summary["counts"]
    lines += [
        f"**twin score {summary['twin_score']}** (all surfaces) · "
        f"**{summary['twin_score_actionable']}** (actionable — excl. intentionally "
        f"shared) · identical {c['identical']} / partial {c['partial']} / "
        f"distinct {c['distinct']}", "",
        "| surface | status | groups | verdict | value(s) |",
        "|-|-|-|-|-|",
    ]
    for r in summary["rows"]:
        if r["surface"] == "lies":
            continue
        groups = " ".join("{" + ",".join(g) + "}" for g in r.get("groups") or [])
        sample = r.get("sample") or {}
        vals = " ⟂ ".join(_short(v, 40 if len(sample) > 1 else 70)
                           for v in sample.values())
        lines.append(f"| {r['surface']} | {r['status']} | {groups if r['status'] == 'partial' else ''} "
                     f"| {r['verdict']} | {vals} |")
    lies = {n: v for n, v in summary["lies"].items() if v}
    lines += ["", "lies (main vs worker / window⊄screen): "
              + (json.dumps(lies) if lies else "none")]
    lines += ["", "_verdict: a=already variable · b=persona (cheap+coherent) · "
              "c=engine-level only, not faked (a JS fake is a detectable lie) · "
              "d=intentionally shared (it is the real binary/host)_"]
    return "\n".join(lines)


def render_comparison(before: dict, after: dict) -> str:
    """Before/after delta table: one row per surface whose status moved."""
    b = {r["surface"]: r for r in before["rows"]}
    a = {r["surface"]: r for r in after["rows"]}
    lines = ["### before → after", "",
             f"twin score {before['twin_score']} → **{after['twin_score']}** · "
             f"actionable {before['twin_score_actionable']} → "
             f"**{after['twin_score_actionable']}**", "",
             "| surface | before | after |", "|-|-|-|"]
    for s in a:
        if s == "lies" or s not in b:
            continue
        if (b[s]["status"], b[s].get("pair_equal")) != (a[s]["status"], a[s].get("pair_equal")):
            lines.append(f"| {s} | {b[s]['status']} ({b[s].get('pair_equal')}) | "
                         f"{a[s]['status']} ({a[s].get('pair_equal')}) |")
    return "\n".join(lines)


# ─── live runner ─────────────────────────────────────────────────────────────

BLANK_HTML = (b"<!doctype html><html><head><meta charset=utf-8><title>fleet-check"
              b"</title></head><body><p>fleet-check probe</p></body></html>")


class _BlankServer:
    """A loopback HTTP server serving one blank page. A real http://127.0.0.1
    origin is a SECURE context (unlike some opaque-origin about:blank states), so
    the SecureContext-gated APIs — client hints, WebGPU, storage.estimate,
    mediaDevices — answer the way they do on a real site."""

    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(BLANK_HTML)))
                self.end_headers()
                self.wfile.write(BLANK_HTML)

            def log_message(self, *a):  # silence
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def probe_session(client_call: Callable[..., Any], session: str, url: str,
                  *, timeout_ms: int = 30_000) -> dict:
    """Navigate one session to ``url`` and return its fingerprint vector."""
    client_call("go", {"url": url}, session=session)
    res = client_call("eval", {"expr": PROBE_JS, "timeout_ms": timeout_ms},
                      session=session)
    return (res or {}).get("value") or {}


def parse_variant(spec: str) -> dict:
    """Parse a ``--variant`` spec: comma-separated ``key[=value]`` tokens.

    ``gpu`` (GPU on, host default node) · ``gpu=intel|nvidia`` (pinned node) ·
    ``geo=<CC>`` (country timezone) · ``tz=<IANA>`` · ``persona`` (persona on) ·
    ``default`` (nothing). Raises ValueError on an unknown key."""
    out: dict[str, Any] = {}
    for tok in (t.strip() for t in (spec or "").split(",")):
        if not tok or tok == "default":
            continue
        k, _, v = tok.partition("=")
        k = k.strip().lower()
        v = v.strip()
        if k == "gpu":
            out["gpu"] = v.lower() or True
        elif k == "geo":
            if not v:
                raise ValueError("geo=<country code> needs a value")
            out["geo"] = v.upper()
        elif k == "tz":
            if not v:
                raise ValueError("tz=<IANA zone> needs a value")
            out["tz"] = v
        elif k == "persona":
            out["persona"] = True
        else:
            raise ValueError(f"unknown variant key {k!r} (gpu, geo, tz, persona)")
    return out


def _setup_spawned(client_call, name: str, variant: dict, persona: bool) -> dict:
    """Create + configure + start one throwaway ephemeral session."""
    client_call("session_new", {"name": name})
    meta: dict[str, Any] = {}
    gpu = variant.get("gpu")
    if gpu:
        args = {"on": True}
        if isinstance(gpu, str):
            args["node"] = gpu
        client_call("gpu_set", args, session=name)
        meta["gpu"] = gpu if isinstance(gpu, str) else "on"
    if variant.get("geo") or variant.get("tz"):
        gargs = {}
        if variant.get("geo"):
            gargs["country"] = variant["geo"]
        if variant.get("tz"):
            gargs["timezone_id"] = variant["tz"]
        r = client_call("geo_set", gargs, session=name) or {}
        meta["tz"] = r.get("timezone_id")
    if persona or variant.get("persona"):
        r = client_call("persona_set", {"on": True}, session=name) or {}
        p = r.get("persona") or {}
        meta["persona"] = f"{p.get('screen')}/{p.get('window')}"
        if p.get("gpu_node") and gpu is True:
            meta["gpu"] = f"auto:{p.get('gpu_node')}"
    r = client_call("start", {"ephemeral": True, "headless": True}, session=name) or {}
    if r.get("gpu"):
        meta.setdefault("gpu", "on")
    return meta


def run_fleet_check(client_call: Callable[..., Any], *,
                    sessions: Iterable[str] = (),
                    variants: Iterable[dict] = (),
                    persona: bool = False,
                    url: str | None = None,
                    tag: str | None = None) -> dict:
    """Probe existing ``sessions`` plus one throwaway ephemeral session per
    entry in ``variants`` (see parse_variant), in parallel; spawned sessions are
    closed + deleted afterwards even on error. Returns a ``summarize`` dict.

    ``sessions`` must already be RUNNING — a name that isn't is refused
    (ValueError) before anything is spawned or navigated, rather than silently
    auto-started by the probe's `go`. Each one is navigated back to the URL it
    was on once probing is done (a failure to restore lands in ``errors``
    under ``"<name> (restore)"``).

    ``persona`` turns the persona posture on for every SPAWNED session (the
    "after" pass); existing sessions are probed as they are.
    """
    import uuid
    sessions = list(sessions)
    tag = tag or uuid.uuid4().hex[:6]
    spawned: list[str] = []
    meta: dict[str, dict] = {s: {"existing": True} for s in sessions}
    vectors: dict[str, dict] = {}
    errors: dict[str, str] = {}
    origin: dict[str, str | None] = {}
    if sessions:
        rows = (client_call("session_list", {}) or {}).get("sessions") or []
        running = {r.get("name"): r for r in rows if r.get("running")}
        missing = [s for s in sessions if s not in running]
        if missing:
            raise ValueError(
                f"not running: {', '.join(missing)} — fleet-check --sessions "
                f"probes live sessions as they are and won't start one (start "
                f"it first, or use --spawn / --variant for throwaway sessions)")
        origin = {s: running[s].get("url") for s in sessions}
    try:
        for i, var in enumerate(variants):
            name = f"fleet-{tag}-{i}"
            spawned.append(name)
            meta[name] = _setup_spawned(client_call, name, var, persona)
        targets = list(sessions) + spawned

        def run(srv_url: str) -> None:
            lock = threading.Lock()

            def one(n: str) -> None:
                try:
                    v = probe_session(client_call, n, srv_url)
                    with lock:
                        vectors[n] = v
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors[n] = f"{type(exc).__name__}: {exc}"[:200]

            ts = [threading.Thread(target=one, args=(n,)) for n in targets]
            for t in ts:
                t.start()
            for t in ts:
                t.join()

        if url:
            run(url)
        else:
            with _BlankServer() as srv:
                run(srv.url)
    finally:
        for n in sessions:
            back = origin.get(n)
            if not back or back == "about:blank":
                back = "about:blank"
            try:
                client_call("go", {"url": back}, session=n)
            except Exception as exc:  # noqa: BLE001
                errors[f"{n} (restore)"] = f"{type(exc).__name__}: {exc}"[:200]
        for n in spawned:
            for verb in ("session_close", "session_delete"):
                try:
                    client_call(verb, {"name": n})
                except Exception:  # noqa: BLE001
                    pass
    out = summarize(vectors, meta)
    out["errors"] = errors
    out["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return out
