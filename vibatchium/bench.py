"""vb bench — stealth-WALL pass-rate harness (0.11.0).

Complements `vb evals` (which scores fingerprint *scoreboards* like sannysoft/
creepjs). This one measures the thing the moat is actually about: does a cold,
stealth navigation CLEAR a bot WALL (Cloudflare / DataDome / PerimeterX)?

Per target: spin a throwaway ephemeral session, cold `go`, read the `walled`
field the daemon already computes, capture an evidence screenshot, tear down.

Two fields, deliberately distinct (the correction that makes the number honest):

  - `expected_waf` — the A-PRIORI label of what defender a target sits behind
    ("cloudflare" / "datadome" / "perimeterx" / None for a control). This is the
    AGGREGATION KEY. It is supplied by the operator, never inferred at runtime.
  - `walled`       — the RUNTIME read from `is_walled(title, status)`:
    None == no wall detected == CLEARED == passed; a defender name == still
    walled == failed. It returns None on a *cleared* wall, so it can't double as
    the bucket key — hence the split.

HONESTY: `is_walled` is TITLE-ONLY (it no-ops on bare 403/429; a body- or
iframe-rendered challenge with an innocuous <title> reads as cleared). So any
published pass-rate from this harness is an OPTIMISTIC UPPER BOUND. The rendered
output says so; do not strip that caveat.

The offline test drives this against four local fixtures to prove the
detection + aggregation plumbing (a CF-title page reads cloudflare, a legit
"Access Denied" reads cleared). The live lane (`vb bench run --live`) is a
manual, rate-limited acknowledgement-gated act — never a CI gate.
"""
from __future__ import annotations

import ipaddress
import json as _json
import logging
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("vibatchium.bench")

# Tier → posture. 'standard' is the default headless lane. 'hardened' goes
# HEADED (drops the headless screen/GPU/scrollbar tells) — NOT nodriver, which
# is an opt-in optional dependency, not a posture.
TIER_HEADLESS = {"standard": True, "hardened": False}

VALID_WAFS = ("cloudflare", "datadome", "perimeterx")


def is_localhost(url: str) -> bool:
    """True if a target is genuine loopback (127.0.0.0/8 / localhost / ::1).
    Non-local targets require the explicit --live acknowledgement so the harness
    never hammers a real commercial WAF by accident.

    Uses a real loopback test, NOT a `127.` string prefix — the prefix let a
    registered host like `127.0.0.1.evil.com` masquerade as local and slip past
    the gate. `ipaddress` parses only true IPs; a hostname raises → non-local
    (errs toward requiring --live, the safe direction)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_bench(client_call: Callable[..., Any], targets: list[dict], *,
              tier: str = "standard", settle_ms: int = 4000,
              evidence_dir: str | Path | None = None) -> list[dict]:
    """Run each target sequentially (fresh Chrome per target = clean cold
    measurement, no cross-target contention). Returns a list of record dicts."""
    rows = []
    ev_dir = Path(evidence_dir) if evidence_dir else None
    for target in targets:
        log.info("bench: name=%s url=%s expected_waf=%s tier=%s",
                 target.get("name"), target.get("url"),
                 target.get("expected_waf"), tier)
        rows.append(_run_one_target(client_call, target, tier, settle_ms, ev_dir))
    return rows


def _run_one_target(client_call: Callable[..., Any], target: dict, tier: str,
                    settle_ms: int, evidence_dir: Path | None) -> dict:
    name = target.get("name") or target.get("url", "?")
    url = target["url"]
    expected_waf = target.get("expected_waf")
    headless = TIER_HEADLESS.get(tier, True)
    sname = f"vibatchium_bench_{uuid.uuid4().hex[:8]}"

    rec: dict[str, Any] = {
        "target": name, "url": url, "expected_waf": expected_waf,
        "stealth_tier": tier, "walled": None, "passed": None,
        "status": None, "title": None, "elapsed_ms": None,
        "evidence_path": None, "error": None,
    }
    t0 = time.time()
    try:
        # Throwaway ephemeral session, started DIRECTLY (no session_new prewarm).
        client_call("start", {"ephemeral": True, "headless": headless},
                    session=sname)
        go_args: dict[str, Any] = {"url": url}
        if settle_ms:
            go_args["render_timeout_ms"] = settle_ms
        res = client_call("go", go_args, session=sname)
        walled = res.get("walled")  # None == cleared == passed
        rec["walled"] = walled
        rec["passed"] = walled is None
        rec["status"] = res.get("status")
        rec["title"] = res.get("title")
        if evidence_dir is not None:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            ev_path = evidence_dir / f"{_safe_name(name)}.png"
            try:
                client_call("screenshot",
                            {"path": str(ev_path), "full_page": True},
                            session=sname)
                rec["evidence_path"] = str(ev_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("evidence screenshot failed for %s: %s", name, exc)
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        # Ephemeral close rmtree's the profile; the delete is belt-and-suspenders.
        try:
            client_call("session_close", {"name": sname})
        except Exception:  # noqa: BLE001
            pass
        try:
            client_call("session_delete", {"name": sname})
        except Exception:  # noqa: BLE001
            pass
        rec["elapsed_ms"] = int((time.time() - t0) * 1000)
    return rec


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name))[:64] or "target"


# ─── aggregation (PURE — unit-tested) ────────────────────────────────────────


def cold_pass_rate_by_waf(rows: list[dict]) -> dict[str, dict]:
    """Aggregate pass-rate keyed on `expected_waf` (the a-priori label), NOT on
    runtime `walled` (which is None on a cleared wall and so can't bucket).

    A control target (expected_waf=None) buckets under 'control'. `pass_rate` is
    over the TESTED rows (errors excluded from the denominator but counted).
    """
    buckets: dict[str, dict] = {}
    for r in rows:
        key = r.get("expected_waf") or "control"
        b = buckets.setdefault(
            key, {"total": 0, "tested": 0, "passed": 0, "errors": 0,
                  "pass_rate": None})
        b["total"] += 1
        if r.get("error"):
            b["errors"] += 1
            continue
        b["tested"] += 1
        if r.get("passed"):
            b["passed"] += 1
    for b in buckets.values():
        b["pass_rate"] = (round(b["passed"] / b["tested"], 3)
                          if b["tested"] else None)
    return buckets


def min_pass_rate(rows: list[dict]) -> float | None:
    """Lowest pass-rate across the WAF buckets (the CI/manual gate metric). The
    'control' bucket is EXCLUDED — it measures a non-WAF baseline, not evasion,
    so a walled control shouldn't drag the WAF gate down. None if no WAF bucket
    had a tested row."""
    rates = [b["pass_rate"] for key, b in cold_pass_rate_by_waf(rows).items()
             if key != "control" and b["pass_rate"] is not None]
    return min(rates) if rates else None


# ─── rendering ───────────────────────────────────────────────────────────────


_UPPER_BOUND_NOTE = (
    "_Pass-rate is an **optimistic upper bound**: wall detection is title-only "
    "(`is_walled`), so a body/iframe-rendered challenge with an innocuous title "
    "reads as cleared. Treat these numbers as a ceiling, not a guarantee._"
)


def render_markdown(rows: list[dict]) -> str:
    if not rows:
        return "_no bench targets ran_\n"
    lines = ["| Target | Expected WAF | Walled | Pass | Status | Time |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        walled = r.get("walled") or "-"
        if r.get("error"):
            passed_s = "ERR"
        elif r.get("passed") is True:
            passed_s = "✅"
        elif r.get("passed") is False:
            passed_s = "❌"
        else:
            passed_s = "?"
        status = r.get("status")
        status_s = str(status) if status is not None else "-"
        time_s = f"{r['elapsed_ms']}ms" if r.get("elapsed_ms") is not None else "-"
        lines.append(
            f"| {r['target']} | {r.get('expected_waf') or 'control'} "
            f"| {walled} | {passed_s} | {status_s} | {time_s} |")
    lines.append("")
    lines.append("**Cold pass-rate by WAF:**")
    lines.append("")
    lines.append("| Defender | Passed/Tested | Rate |")
    lines.append("|---|---|---|")
    for key, b in sorted(cold_pass_rate_by_waf(rows).items()):
        rate = "-" if b["pass_rate"] is None else f"{int(b['pass_rate'] * 100)}%"
        lines.append(f"| {key} | {b['passed']}/{b['tested']} | {rate} |")
    lines.append("")
    lines.append(_UPPER_BOUND_NOTE)
    errs = [r for r in rows if r.get("error")]
    if errs:
        lines.append("")
        lines.append("**Errors:**")
        for r in errs:
            lines.append(f"- {r['target']}: `{r['error']}`")
    return "\n".join(lines) + "\n"


def render_json(rows: list[dict]) -> str:
    return _json.dumps({
        "rows": rows,
        "by_waf": cold_pass_rate_by_waf(rows),
        "min_pass_rate": min_pass_rate(rows),
        "upper_bound": True,
        "generated_at": time.time(),
    }, indent=2)


def update_readme(readme_path: Path, markdown_table: str) -> bool:
    """Patch the `<!-- vibatchium-bench -->...<!-- /vibatchium-bench -->` region
    in `readme_path`. Idempotent. Returns True if the file changed."""
    readme_path = Path(readme_path)
    if not readme_path.exists():
        return False
    content = readme_path.read_text()
    start = "<!-- vibatchium-bench -->"
    end = "<!-- /vibatchium-bench -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(content):
        return False
    replacement = f"{start}\n{markdown_table.strip()}\n{end}"
    new = pattern.sub(replacement, content)
    if new == content:
        return False
    readme_path.write_text(new)
    return True


# ─── offline target set (the local fixtures used by the release gate) ─────────


def offline_targets(base_url: str) -> list[dict]:
    """The four local fixtures the offline gate runs. These prove DETECTION +
    aggregation (there's no real WAF to evade locally): a CF/DataDome/PerimeterX
    title page must read as that defender, and a legit "Access Denied" must read
    as cleared (no false-positive)."""
    base = base_url.rstrip("/")
    return [
        {"name": "cloudflare", "url": f"{base}/walled.html",
         "expected_waf": "cloudflare"},
        {"name": "datadome", "url": f"{base}/wall_datadome.html",
         "expected_waf": "datadome"},
        {"name": "perimeterx", "url": f"{base}/wall_perimeterx.html",
         "expected_waf": "perimeterx"},
        {"name": "control", "url": f"{base}/wall_control.html",
         "expected_waf": None},
    ]
