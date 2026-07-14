"""In-page JS payloads for the structured-extract / dump-mode verbs (0.14.0).

These run through Patchright's ISOLATED execution context (``page.evaluate`` /
``locator.evaluate`` — the same stealth default as the ``eval`` verb), so page JS
never sees them and NO DOM is mutated (unlike obscura's ``data-*-ref`` stamping,
which is a fingerprint/diff tell). Each core references ``root`` and ``arg`` and
is wrapped into a *page* form (``root = document``) and an *element* form
(``root`` = a resolved locator) so the same logic serves a whole-page call and a
``target``-scoped subtree call.

The field executor is deliberately a DUMB interpreter: the field-spec grammar is
parsed in Python (:func:`vibatchium.extract.parse_field_specs`, pure and
unit-tested) into a normalized instruction list, and NO caller-supplied selector
is ever interpolated into JS *source* — it arrives as a serialized ``arg``. That
closes the string-built-``eval`` injection surface a ``format!()``-style blob
leaves open, while the grammar stays compatible so agent knowledge transfers.
"""
from __future__ import annotations


def _page_el(core: str) -> tuple[str, str]:
    """Wrap a JS ``core`` (referencing ``root`` and ``arg``) into (page_fn, el_fn)."""
    page_fn = "(arg) => { const root = document; " + core + " }"
    el_fn = "(el, arg) => { const root = el; " + core + " }"
    return page_fn, el_fn


# ── structured field extraction ────────────────────────────────────────────
_FIELDS_CORE = r"""
  const specs = arg.specs, maxChars = arg.maxChars, nodeCap = arg.nodeCap;
  const norm = (s) => (s == null ? '' : String(s)).replace(/\s+/g, ' ').trim();
  const clip = (s) => (maxChars && s.length > maxChars) ? s.slice(0, maxChars) : s;
  // modes: 'text' (innerText/textContent), 'html' (innerHTML), else an attribute
  // NAME read via getAttribute. Deliberately never reads a live input value
  // property — keeps the verb retry-safe and can't leak typed credentials.
  const getVal = (el, mode) => {
    let v;
    if (mode === 'text') v = norm(el.innerText != null ? el.innerText : el.textContent);
    else if (mode === 'html') v = el.innerHTML;
    else v = el.getAttribute(mode);
    return v == null ? null : clip(String(v));
  };
  const fields = {}, matched = {}, misses = [], errors = [];
  for (const sp of specs) {
    try {
      if (sp.array) {
        const nodes = Array.from(root.querySelectorAll(sp.selector)).slice(0, nodeCap);
        const vals = [];
        for (const n of nodes) { const v = getVal(n, sp.mode); if (v != null) vals.push(v); }
        fields[sp.name] = vals; matched[sp.name] = vals.length;
        if (vals.length === 0) misses.push(sp.name);
      } else {
        const n = root.querySelector(sp.selector);
        if (n) { fields[sp.name] = getVal(n, sp.mode); matched[sp.name] = 1; }
        else { fields[sp.name] = null; matched[sp.name] = 0; misses.push(sp.name); }
      }
    } catch (e) { fields[sp.name] = null; matched[sp.name] = 0; errors.push(sp.name); }
  }
  return { fields, matched, misses, errors };
"""
FIELDS_PAGE, FIELDS_EL = _page_el(_FIELDS_CORE)


# ── dump mode: links (browser-resolved absolute URLs over the live DOM) ─────
_LINKS_CORE = r"""
  const maxLinks = arg && arg.maxLinks ? arg.maxLinks : 500;
  const seen = new Set(); const out = [];
  for (const a of root.querySelectorAll('a[href]')) {
    const rawv = (a.getAttribute('href') || '').trim();
    if (!rawv || rawv === '#' || rawv.toLowerCase().startsWith('javascript:')) continue;
    const url = a.href;                        // absolute, post-hydration
    if (!url || seen.has(url)) continue;
    seen.add(url);
    const text = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200);
    out.push({ url, text });
    if (out.length >= maxLinks) break;
  }
  return { links: out, count: out.length };
"""
LINKS_PAGE, LINKS_EL = _page_el(_LINKS_CORE)


# ── dump mode: assets (sub-resources; data: URIs dropped per our no-base64 rule)
_ASSETS_CORE = r"""
  const maxAssets = arg && arg.maxAssets ? arg.maxAssets : 500;
  const seen = new Set(); const out = [];
  const push = (url, type, rel) => {
    if (out.length >= maxAssets) return;       // cap early (mirrors links break)
    if (!url || url.startsWith('data:') || seen.has(url)) return;
    seen.add(url);
    const o = { url: url, type: type }; if (rel) o.rel = rel;
    out.push(o);
  };
  for (const el of root.querySelectorAll('img')) push(el.currentSrc || el.src, 'image');
  for (const el of root.querySelectorAll('source')) push(el.src, 'source');
  for (const el of root.querySelectorAll('script[src]')) push(el.src, 'script');
  for (const el of root.querySelectorAll('link[href]')) push(el.href, 'link', el.rel || null);
  for (const el of root.querySelectorAll('video')) { push(el.src, 'video'); push(el.poster, 'image'); }
  for (const el of root.querySelectorAll('audio')) push(el.src, 'audio');
  for (const el of root.querySelectorAll('iframe[src]')) push(el.src, 'iframe');
  const capped = out.slice(0, maxAssets);
  return { assets: capped, count: capped.length };
"""
ASSETS_PAGE, ASSETS_EL = _page_el(_ASSETS_CORE)


# ── dump mode: main-content (Readability-lite text-density scorer) ──────────
# Picks the block whose (text length − 3×link-text length) is highest — favours
# prose over nav/link farms. Always whole-page: "the article" is document-level.
# obscura's `text` dump has no density scorer, so this is a genuine improvement.
MAIN_PAGE = r"""
(arg) => {
  const body = document.body;
  const bodyLen = body ? (body.innerText || body.textContent || '').replace(/\s+/g, ' ').trim().length : 0;
  let best = null, bestScore = -1;
  for (const el of document.querySelectorAll('article, main, [role=main], section, div')) {
    const tl = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().length;
    if (tl < 200) continue;
    let linkLen = 0;
    for (const a of el.querySelectorAll('a')) linkLen += (a.innerText || a.textContent || '').length;
    const score = tl - 3 * linkLen;
    if (score > bestScore) { bestScore = score; best = el; }
  }
  if (!best) return { html: null, ratio: 0, bodyLen: bodyLen };
  const chosenLen = (best.innerText || best.textContent || '').replace(/\s+/g, ' ').trim().length;
  return { html: best.innerHTML, ratio: bodyLen ? chosenLen / bodyLen : 0, bodyLen: bodyLen };
}
"""
