# Contributing to vibatchium

Thanks for hacking on vibatchium. Most of this file is the usual (run the
tests, keep `ruff` clean), but the important part is the **deliberate
non-goals** — decisions that look like missing features or technical debt but
are load-bearing. Please read them before "fixing" one.

## Setup

```bash
pip install -e ".[all]"        # dev install (Apache-2.0 extras)
patchright install chrome
pytest tests/                  # full suite (needs Chrome + Xvfb headless)
ruff check vibatchium/ tests/
```

Run the suite under an **isolated** `HOME` + `XDG_RUNTIME_DIR` if you have a
real daemon running locally, so the test daemon can't disturb it.

## Deliberate non-goals (do NOT "fix" these)

These are strategy, distilled from a competitive scan of the field. vibatchium's
moat is **stateful, login-walled, self-hosted stealth** — every change should
defend or extend that, not chase a commodity the giants already own.

1. **Do NOT migrate to WebDriver BiDi**, and do not add a BiDi backend. The
   whole stealth posture rests on Patchright's CDP-path-specific source patches
   (notably the `Runtime.enable` suppression). BiDi forfeits those for *zero*
   stealth gain. The testing world is going to BiDi; serious automation is
   staying on CDP. We stay on CDP.

2. **Do NOT add JS-injection stealth shims** (puppeteer-extra-style) or
   re-enable the CDP `Runtime` / `Console` domains by default. Patchright keeps
   them off on purpose — enabling them re-arms the `Runtime.enable` leak that is
   the #1 modern bot signal. Console capture (`console_start`) is opt-in and
   detaches its CDP session afterward for exactly this reason; keep it that way.

3. **Do NOT build CAPTCHA-solving as a core feature.** AI CAPTCHA solving is a
   decaying treadmill. The durable answer is *avoid-or-authenticate*: the
   credential vault + IMAP/TOTP + attach-mode + human-takeover. An optional,
   cap-gated, spend-capped solver *plugin* is the most a fallback ever warrants.

4. **Do NOT fake behavioral biometrics.** Synthetic input rides CDP `Input.*`
   with a fixed coordinate signature (`pageX==screenX`, no `CoalescedEvents`);
   `humanize` improves trajectory/timing but cannot change that. We do not ship
   no-op "stealth mouse" controls (see the 0.6.10 `--stealth-mouse` removal).
   The honest answer for behavioral walls is attach-mode against a real headful
   Chrome — document the limit, don't paper over it.

5. **Do NOT chase commodity races.** No proxy-IP-volume business, no generic
   anonymous unblocking, no QA/locator-generation, no no-code workflow builder,
   and don't try to become a general autonomous agent. Integrate with proxy
   networks and agent frameworks; be the stealth+auth substrate they call.

6. **Do NOT silently default-flip stealth-relevant posture.** Headless-by-
   default, `channel="chrome"` (real consumer Chrome, not Chrome-for-Testing),
   the de-Headless'd UA, and `--no-sandbox` removal are intentional. Changing
   any of them is a stealth decision, not a refactor — call it out in the PR and
   the CHANGELOG, and make sure the stealth gate still passes.

If you think one of these is wrong, open an issue and make the case — they're
decisions, not dogma. But they are decisions, so don't quietly reverse one.

## Tests + the stealth gate

`tests/test_wave7_stealth_gate.py` (behavioral posture: `navigator.webdriver`,
`chrome.runtime` shape, de-Headless'd UA, `--no-sandbox`, file perms) and
`tests/test_stealth_drift_gate.py` (the Patchright version tripwire) are the
stealth gate; `publish.yml` runs both before any release build. If you bump
Patchright, the version tripwire fails until you re-run the posture suite against
the new version and add its `(major, minor)` to the vetted set — that friction is
intentional.

Note: a bespoke JS `Runtime.enable` getter-trap probe was prototyped and removed
because it could not be positively verified to fire in CI (it risked passing
vacuously). Don't re-add a behavioral stealth probe without a **positive control**
that proves it goes red when the leak is present — a green gate that can't detect
its own target is worse than no gate.
