"""Phase-1 smoke test: prove the Patchwright + real-Chrome + persistent-context
config actually passes a Cloudflare wall (HackerOne is our canary).

Run: .venv/bin/python tests/smoke_cloudflare.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from patchright.sync_api import sync_playwright

TARGET = "https://hackerone.com/anthropic"
CLOUDFLARE_MARKERS = (
    "performing security verification",
    "checking your browser",
    "verifying you are human",
    "ray id",
)
SUCCESS_MARKERS = (
    "anthropic",
    "bug bounty",
    "policy",
    "hacktivity",
    "scope",
)


def smoke() -> int:
    profile_dir = Path(tempfile.gettempdir()) / "patchium-smoke-profile"
    profile_dir.mkdir(exist_ok=True)
    print(f"[+] profile dir: {profile_dir}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print(f"[+] navigating to {TARGET}")
        page.goto(TARGET, wait_until="domcontentloaded", timeout=60_000)

        # Cloudflare challenges typically resolve in 3-8s on a clean fingerprint
        time.sleep(8)
        body = (page.inner_text("body") or "").lower()
        title = page.title()

        print(f"[+] final URL: {page.url}")
        print(f"[+] title: {title}")
        print(f"[+] body bytes: {len(body)}")

        cf_hit = any(m in body for m in CLOUDFLARE_MARKERS)
        success_hit = any(m in body for m in SUCCESS_MARKERS)

        if cf_hit and not success_hit:
            print("[!] FAIL: Cloudflare wall still showing.")
            page.screenshot(path="/tmp/patchium-smoke-fail.png")
            print("[!] screenshot: /tmp/patchium-smoke-fail.png")
            ctx.close()
            return 1

        if success_hit:
            print("[+] PASS: Cloudflare cleared, HackerOne policy page reached.")
            page.screenshot(path="/tmp/patchium-smoke-pass.png")
            print("[+] screenshot: /tmp/patchium-smoke-pass.png")
            ctx.close()
            return 0

        print("[?] ambiguous: neither CF markers nor success markers found.")
        page.screenshot(path="/tmp/patchium-smoke-ambiguous.png")
        print("[?] screenshot: /tmp/patchium-smoke-ambiguous.png")
        print(f"[?] first 500 chars of body: {body[:500]}")
        ctx.close()
        return 2


if __name__ == "__main__":
    sys.exit(smoke())
