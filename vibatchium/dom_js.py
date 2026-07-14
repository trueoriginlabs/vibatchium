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


# ── form detection (0.15.0 agent-forms) ─────────────────────────────────────
# One isolated-context DOM walk over <form>s (+ a formless-controls group for the
# many SPAs that skip <form>). Per field we emit a READY-TO-USE locator string our
# resolver consumes directly (#id → tag[name=…] → @label:…) so the agent can pipe
# it straight into fill/click — WITHOUT persisting any data-*-ref attribute (obscura
# stamps one; that's a DOM/fingerprint tell on authed pages we deliberately avoid).
#
# Credential hardening (the divergence from obscura, which returns raw values):
# password/hidden and any credential/payment field DETECTED by a type/name/autocomplete
# heuristic is redacted (`sensitive:true`, no value). A free-text field's live typed
# `.value` is withheld by default — we emit only a `filled` boolean — and included
# only under the opt-in `values:true`, and even then never for a heuristic-flagged
# field. Select `options` (with `selected`) and checkbox/radio `checked` are UI state
# (not typed secrets) so they're kept even when the field's name looks sensitive; a
# checkbox/radio's static `value` attribute IS redacted when flagged. The heuristic is
# best-effort, not a guarantee — an unusual secret field name can slip past it.
_FORMS_CORE = r"""
  const maxForms = (arg && arg.maxForms) || 50;
  const maxFields = (arg && arg.maxFields) || 100;
  const maxOptions = (arg && arg.maxOptions) || 100;
  const maxChars = (arg && arg.maxChars) || 200;
  const wantValues = !!(arg && arg.values);
  const norm = (s) => (s == null ? '' : String(s)).replace(/\s+/g, ' ').trim();
  const clip = (s) => { s = norm(s); return s.length > maxChars ? s.slice(0, maxChars) : s; };
  const cssEsc = (window.CSS && CSS.escape) ? (s) => CSS.escape(String(s))
                 : (s) => String(s).replace(/[^\w-]/g, (c) => '\\' + c);
  // a value inside a quoted CSS string: escape " and \, and CSS-hex-escape control chars.
  const cssStr = (s) => String(s).replace(/(["\\])/g, '\\$1')
                                 .replace(/[\n\r\f\t]/g, (c) => '\\' + c.charCodeAt(0).toString(16) + ' ');
  const SENSITIVE = /pass|pwd|passphrase|secret|token|otp|cvv|cvc|card|ccnum|cc-|ssn|\bsin\b|routing|account|iban|swift|\bpin\b|api.?key|api.?token|access.?token|auth.?token|bearer|seed|mnemonic|private.?key|privkey|wallet|security.?(?:code|answer|question)|\bcsc\b|\bcvc2\b|\bcid\b/i;
  const isSensitive = (el) => {
    const type = (el.type || '').toLowerCase();
    if (type === 'password' || type === 'hidden') return true;
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    if (/(?:^|\s)(?:cc-|current-password|new-password|one-time-code)/.test(ac)) return true;
    return SENSITIVE.test((el.getAttribute('name') || '') + ' ' + (el.id || ''));
  };
  // returns { text, source } — source is 'label' for label[for]/wrapping/aria-label/
  // aria-labelledby (all get_by_label resolves), else 'placeholder' or 'title'.
  const labelInfo = (el) => {
    let lab = '', src = 'label';
    if (el.id) { const l = document.querySelector('label[for="' + cssEsc(el.id) + '"]'); if (l) lab = l.textContent; }
    if (!lab && el.closest) { const w = el.closest('label'); if (w) lab = w.textContent; }
    if (!lab) lab = el.getAttribute('aria-label') || '';
    if (!lab) {
      const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
      if (ids.length) lab = ids.map((id) => { const r = document.getElementById(id); return r ? r.textContent : ''; })
        .filter(Boolean).join(' ');
    }
    if (!lab) { lab = el.getAttribute('placeholder') || ''; if (lab) src = 'placeholder'; }
    if (!lab) { lab = el.getAttribute('title') || ''; if (lab) src = 'title'; }
    return { text: clip(lab), source: src };
  };
  const locatorFor = (el, info) => {
    if (el.id) return '#' + cssEsc(el.id);
    const tag = el.tagName.toLowerCase();
    const name = el.getAttribute('name');
    if (name) return tag + '[name="' + cssStr(name) + '"]';
    // a placeholder/title label is NOT a get_by_label target — emit the matching prefix.
    if (info.text) {
      if (info.source === 'placeholder') return '@placeholder:' + info.text;
      if (info.source === 'title') return '@title:' + info.text;
      return '@label:' + info.text;
    }
    return null;
  };
  const NON_FIELD = new Set(['submit', 'button', 'reset', 'image']);
  const FIELD_TAGS = new Set(['input', 'select', 'textarea']);
  const isField = (el) => FIELD_TAGS.has(el.tagName.toLowerCase())
                          && !NON_FIELD.has((el.getAttribute('type') || '').toLowerCase());
  const fieldOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'select' ? 'select'
               : tag === 'textarea' ? 'textarea'
               : (el.getAttribute('type') || 'text').toLowerCase();
    const info = labelInfo(el);
    const sens = isSensitive(el);
    const f = {
      tag: tag, type: type,
      name: el.getAttribute('name') || null,
      id: el.id || null,
      label: info.text || null,
      required: el.required === true || el.getAttribute('aria-required') === 'true',
      disabled: el.disabled === true,
      locator: locatorFor(el, info),
    };
    if (sens) f.sensitive = true;
    if (tag === 'select') {
      const opts = [];
      for (const o of Array.from(el.options).slice(0, maxOptions))
        opts.push({ value: o.value, label: clip(o.textContent), selected: o.selected === true });
      f.options = opts;                                  // UI state — kept even if name looks sensitive
    } else if (type === 'checkbox' || type === 'radio') {
      f.checked = el.checked === true;                   // UI state — always
      if (!sens) f.value = el.getAttribute('value');     // static config value; redact when flagged
    } else {
      f.filled = !!(el.value && String(el.value).length);
      // a free-text typed value crosses the boundary ONLY under the opt-in AND when the
      // field isn't heuristic-flagged sensitive.
      if (wantValues && !sens) f.value = el.value != null ? clip(el.value) : null;
    }
    return f;
  };
  const submitOf = (fm) => {
    const b = fm.querySelector('button[type=submit], input[type=submit], input[type=image], button:not([type])');
    if (!b) return null;
    const lab = clip(b.value || b.innerText || b.textContent || 'Submit');
    // a button is addressed by id/name or its TEXT (get_by_text) — never @label.
    let loc = null;
    if (b.id) loc = '#' + cssEsc(b.id);
    else if (b.getAttribute('name'))
      loc = b.tagName.toLowerCase() + '[name="' + cssStr(b.getAttribute('name')) + '"]';
    else if (lab) loc = '@text:' + lab;
    return { locator: loc, label: lab || null };
  };
  // fm.elements honors the HTML5 form= association (controls OUTSIDE the <form> subtree),
  // which a descendant-only querySelectorAll would silently drop.
  const collect = (fm) => Array.from(fm.elements || [])
    .filter(isField).slice(0, maxFields).map(fieldOf);
  const out = [];
  // include `root` itself when a `target` scoped us straight onto a <form>
  // (querySelectorAll('form') only finds DESCENDANTS, and forms don't nest).
  const formEls = [];
  if (root.matches && root.matches('form')) formEls.push(root);
  for (const fm of root.querySelectorAll('form')) formEls.push(fm);
  formEls.splice(maxForms);
  for (let i = 0; i < formEls.length; i++) {
    const fm = formEls[i];
    out.push({
      index: i,
      id: fm.id || null,
      name: fm.getAttribute('name') || null,
      action: fm.getAttribute('action') != null ? fm.action : null,   // resolved absolute
      method: (fm.getAttribute('method') || 'get').toLowerCase(),
      submit: submitOf(fm),
      fields: collect(fm),
    });
  }
  const loose = Array.from(root.querySelectorAll('input, select, textarea'))
    .filter((el) => !el.form && isField(el)).slice(0, maxFields);
  if (loose.length) out.push({ index: null, formless: true, fields: loose.map(fieldOf) });
  return { forms: out, count: out.length };
"""
FORMS_PAGE, FORMS_EL = _page_el(_FORMS_CORE)


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
