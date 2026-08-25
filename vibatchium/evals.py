"""Wave 6.2c — vb evals benchmark suite.

Runs the `fingerprint` scorer across a matrix of (target × backend × humanize)
and emits a markdown/JSON table. Used to:
  - replace the README's "70-90%" guesses with measured numbers
  - prove regressions in CI (--min-score gates pass-rate)
  - compare backends on the same targets

Cells in the matrix:
  - target: sannysoft | creepjs | brotector (built-ins) or custom URL
  - backend: patchright (default) | nodriver
  - humanize: on | off

For each cell, we spawn an isolated session (`vibatchium_evals_<uuid>`),
configure backend + humanize, navigate to the target, scrape the score,
then tear down the session. Default: all three built-in targets × patchright
× humanize off (humanize_modes=(False,)) to keep wall-clock short (~30s).

Output formats:
  - markdown (default): printable table for the README
  - json: machine-readable for CI / dashboards
  - --update-readme: patches `<!-- vibatchium-evals -->...<!-- /vibatchium-evals -->`
    region in README.md (idempotent)

CI gate: `--min-score N` exits non-zero if any cell scored below N.
"""
from __future__ import annotations

import json as _json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any
from .gpu import renderer_is_real as _renderer_is_real

log = logging.getLogger("vibatchium.evals")


BUILTIN_TARGETS = ("sannysoft", "creepjs", "brotector")
BUILTIN_BACKENDS = ("patchright",)  # nodriver is opt-in; user passes explicitly


def run_eval_matrix(client_call, *, targets=BUILTIN_TARGETS,
                     backends=BUILTIN_BACKENDS,
                     humanize_modes=(False,),
                     settle_ms: int = 5000,
                     gpu: bool | None = None) -> list[dict]:
    """Iterate the matrix sequentially. Returns list of cell results.

    Sequential (not parallel) because each cell spawns a fresh Chrome and
    we want clean per-cell measurements without resource contention.

    ``gpu`` (None = leave the profile default) forces the WebGL renderer on or
    off per cell. It exists because the software-renderer caveat this module
    prints was previously unactionable: we told the reader to "re-run with an
    accessible DRM render node" while giving them no switch to do it with, so
    every published number was a SwiftShader floor by construction.
    """
    rows = []
    for backend in backends:
        for humanize in humanize_modes:
            for target in targets:
                log.info("eval: target=%s backend=%s humanize=%s gpu=%s",
                         target, backend, humanize, gpu)
                cell = _run_one_cell_sync(
                    client_call, target, backend, humanize, settle_ms, gpu
                )
                rows.append(cell)
    return rows


def _run_one_cell_sync(client_call, target: str, backend: str,
                        humanize: bool, settle_ms: int,
                        gpu: bool | None = None) -> dict:
    """Run a single (target, backend, humanize) cell. Returns a row dict.
    Sync — the eval client (daemon RPC) is synchronous."""
    sname = f"vibatchium_evals_{uuid.uuid4().hex[:8]}"
    cell: dict[str, Any] = {
        "target": target, "backend": backend, "humanize": humanize,
        "score": None, "error": None, "elapsed_s": None,
        "renderer": None, "software_renderer": None,
    }
    t0 = time.time()
    try:
        client_call("session_new", {"name": sname})
        start_args = {"headless": True}
        if backend != "patchright":
            start_args["backend"] = backend
        if gpu is not None:
            start_args["gpu"] = gpu
        client_call("start", start_args, session=sname)
        # Record the renderer this cell ACTUALLY came up on. Headless Chrome
        # falls back to SwiftShader whenever the GPU path doesn't take, and on
        # a proof-of-browser detector software GL is a POSITIVE tell, not a
        # neutral one — so a score measured on SwiftShader is not the score a
        # real deployment gets. Publishing one without saying so would be the
        # same class of unbacked number this file exists to replace.
        # Observed, never enforced: a host with no render node degrades to
        # software by design, and failing the run there would be worse than
        # reporting it.
        try:
            gi = client_call("gpu_info", session=sname) or {}
            renderer = gi.get("renderer") or (gi.get("webgl") or {}).get("renderer")
            cell["renderer"] = renderer
            cell["software_renderer"] = (
                not _renderer_is_real(renderer) if renderer else None)
        except Exception:  # noqa: BLE001 — never let provenance break the run
            cell["renderer"] = None
            cell["software_renderer"] = None
        if humanize:
            client_call("humanize_on", session=sname)
        res = client_call("fingerprint",
                          {"target": target, "settle_ms": settle_ms},
                          session=sname)
        cell["score"] = res.get("score")
        cell["signals"] = res.get("signals")
        cell["url"] = res.get("url")
    except Exception as exc:  # noqa: BLE001
        cell["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            client_call("session_close", {"name": sname})
        except Exception:  # noqa: BLE001
            pass
        try:
            client_call("session_delete", {"name": sname})
        except Exception:  # noqa: BLE001
            pass
        cell["elapsed_s"] = round(time.time() - t0, 2)
    return cell


# ─── output formatting ─────────────────────────────────────────────────


def render_markdown(rows: list[dict]) -> str:
    """Render a (target, backend, humanize) → score markdown table."""
    if not rows:
        return "_no eval cells ran_\n"
    lines = []
    lines.append("| Target | Backend | Humanize | GPU | Score | Status | Time |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        score = r.get("score")
        score_s = f"{score}" if score is not None else "-"
        status = "ERR" if r.get("error") else ("OK" if score is not None else "?")
        humanize_s = "on" if r["humanize"] else "off"
        time_s = f"{r['elapsed_s']}s"
        sw = r.get("software_renderer")
        gpu_s = "?" if sw is None else ("**software**" if sw else "real")
        lines.append(
            f"| {r['target']} | {r['backend']} | {humanize_s} | {gpu_s} "
            f"| {score_s} | {status} | {time_s} |"
        )
    if any(r.get("software_renderer") for r in rows):
        soft = sorted({r.get("renderer") for r in rows
                       if r.get("software_renderer") and r.get("renderer")})
        lines.append("")
        lines.append(
            "> **Measured on a software renderer.** At least one cell above came "
            "up on software GL"
            + (f" ({', '.join(soft)})" if soft else "")
            + " rather than a real GPU. Detectors that probe for proof-of-browser "
              "treat software GL as a positive automation signal, so these scores "
              "are a floor, not the number a GPU-backed deployment gets. Re-run "
              "with an accessible DRM render node before quoting them."
        )
    if any(r.get("error") for r in rows):
        lines.append("")
        lines.append("**Errors:**")
        for r in rows:
            if r.get("error"):
                lines.append(
                    f"- {r['target']}/{r['backend']}/humanize={r['humanize']}: "
                    f"`{r['error']}`"
                )
    return "\n".join(lines) + "\n"


def render_json(rows: list[dict]) -> str:
    return _json.dumps({"rows": rows, "generated_at": time.time()}, indent=2)


def update_readme(readme_path: Path, markdown_table: str) -> bool:
    """Patch the `<!-- vibatchium-evals -->...<!-- /vibatchium-evals -->` region
    in `readme_path` with the new table. Idempotent — re-running on same
    data produces no diff.

    Returns True if the file changed, False if no markers found or already
    up-to-date.
    """
    if not readme_path.exists():
        return False
    content = readme_path.read_text()
    start = "<!-- vibatchium-evals -->"
    end = "<!-- /vibatchium-evals -->"
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        re.DOTALL,
    )
    if not pattern.search(content):
        return False
    replacement = f"{start}\n{markdown_table.strip()}\n{end}"
    new = pattern.sub(replacement, content)
    if new == content:
        return False
    readme_path.write_text(new)
    return True


def min_score(rows: list[dict]) -> int | None:
    """Lowest score across all non-error rows. None if no scored cells."""
    scores = [r["score"] for r in rows if r.get("score") is not None]
    return min(scores) if scores else None
