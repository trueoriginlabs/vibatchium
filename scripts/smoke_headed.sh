#!/usr/bin/env bash
# Real integration smoke for the headed-window honesty changes (0.13.1 / 0.13.2).
#
# Drives the ACTUAL daemon + real Chrome, not unit mocks. Runs a fully ISOLATED
# daemon (short XDG_RUNTIME_DIR under /run/user/<uid>, isolated HOME, NO DISPLAY)
# so the live bots' default-socket daemon is never touched. Verifies:
#   1. GUARD    — cold `start --headed` on a display-less daemon -> clean refusal
#                 pointing at `vb show` (NOT a raw Playwright/X-server crash).
#   2. CONTROL  — `start --headless` still launches (guard doesn't over-block).
#   3. M1 note  — `start --headed` on the already-running headless session ->
#                 headed_ignored + a note pointing at `vb show <profile>`.
#   4. DISCOVER — `vb show` is listed in --help and self-documents as a real window.
# Also asserts the live default socket is untouched throughout.
#
# Usage:  bash scripts/smoke_headed.sh
#         VB=/path/to/vb bash scripts/smoke_headed.sh
set -uo pipefail

VB="${VB:-$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/vb}"
UID_="$(id -u)"
RT="/run/user/${UID_}/vbsmoke-headed"           # SHORT (AF_UNIX 108-char limit)
HM="$(mktemp -d "${TMPDIR:-/tmp}/vbsmoke-home.XXXXXX")"
DEF="/run/user/${UID_}/vibatchium/daemon.pid"
PASS=0; FAIL=0

run() {  # isolated vb: own socket + home, NO display, real browser cache
  env -u DISPLAY -u WAYLAND_DISPLAY -u XDG_SESSION_TYPE \
    HOME="$HM" XDG_RUNTIME_DIR="$RT" \
    PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}" \
    VIBATCHIUM_LOG_FILE="$RT/daemon.log" \
    "$VB" "$@"
}
cleanup() { run --session smoke_hl stop >/dev/null 2>&1 || true
            run shutdown >/dev/null 2>&1 || true; rm -rf "$RT" "$HM"; }
trap cleanup EXIT

ck()  { if grep -qiF -- "$2" <<<"$3"; then echo "  PASS: $1"; PASS=$((PASS+1));
        else echo "  FAIL: $1  (missing: $2)"; sed 's/^/      | /' <<<"$3" | head -8; FAIL=$((FAIL+1)); fi; }
nck() { if grep -qiF -- "$2" <<<"$3"; then echo "  FAIL: $1  (found forbidden: $2)"; FAIL=$((FAIL+1));
        else echo "  PASS: $1"; PASS=$((PASS+1)); fi; }
ok()  { if eval "$2"; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1"; FAIL=$((FAIL+1)); fi; }

before="$(cat "$DEF" 2>/dev/null || echo none)"
rm -rf "$RT"; mkdir -p "$RT"; chmod 700 "$RT"
echo "vb = $VB"; echo "isolated runtime = $RT ; home = $HM ; DISPLAY unset"; echo

echo "== 1. GUARD: cold 'start --headed' on a no-DISPLAY daemon =="
out="$(run --session smoke_g start --headed 2>&1)"; rc=$?
ck  "clean 'cannot launch headed' message"  "cannot launch headed" "$out"
ck  "points the caller at vb show"          "vb show smoke_g"      "$out"
# The clean guard message quotes 'Missing X server or $DISPLAY' on purpose, so
# discriminate on Playwright's actual crash-dump signatures instead.
nck "no Playwright 'Call log:' crash dump"  "Call log:"              "$out"
nck "no Playwright 'launched a headed browser' box" "launched a headed browser" "$out"
nck "no python Traceback"                   "Traceback"            "$out"
ok  "non-zero exit on refusal"              "[ $rc -ne 0 ]"

echo "== 2. CONTROL: 'start --headless' still launches =="
out="$(run --json --session smoke_hl start --headless 2>&1)"
ok  "headless session launched"  "grep -qE '\"started\"|already_started' <<<\"\$out\""

echo "== 3. M1: 'start --headed' on the already-running headless session =="
out="$(run --json --session smoke_hl start --headed 2>&1)"
ck  "already_started"                 '"already_started": true' "$out"
ck  "headed_ignored flag set"         '"headed_ignored": true'  "$out"
ck  "note points at vb show <profile>" "vb show smoke_hl"       "$out"

echo "== 4. DISCOVERABILITY: vb show =="
ok  "vb --help lists 'show'"  "run --help 2>&1 | grep -qE '^[[:space:]]+show\b'"
ck  "vb show --help self-documents"  "REAL, VISIBLE"  "$(run show --help 2>&1)"

echo "== 5. ISOLATION: live default socket untouched =="
after="$(cat "$DEF" 2>/dev/null || echo none)"
ok  "default daemon pid unchanged ($before -> $after)"  "[ \"$before\" = \"$after\" ]"

echo; echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
