"""Wave 6.3a — credential vault + TOTP.

Encrypted on-disk vault for per-site credentials with native RFC 6238 TOTP.
Unlocks every auth-gated flow that breaks at 2FA today.

### Storage

A single encrypted blob at `~/.config/patchium/secrets.enc`:
  - file mode: 0600
  - format: `[24-byte nonce][ciphertext]` (PyNaCl SecretBox = XSalsa20-Poly1305)
  - key: 32 bytes, sourced from one of (priority order):
      1. `PATCHIUM_SECRETS_KEY` env var (base64) — CI / headless servers
      2. OS keyring (gnome-keyring / macOS Keychain / Windows Cred Mgr)

### Schema

```json
{
  "version": 1,
  "sites": {
    "github.com": {
      "username": "alice",
      "password": "hunter2",
      "totp-seed": "JBSWY3DPEHPK3PXP",
      "email-poll": "imap://user:pass@imap.gmail.com:993?regex=\\d{6}"
    }
  }
}
```

### Hard security requirements

- Vault content NEVER appears in logs / observe-cache / HAR captures.
- `secret list` returns MASKED values (`<set>` instead of the value).
- Logs only mention site names + key names, never values.
- The CI grep-for-leakage test in `test_wave6_vault.py` enforces this.

### TOTP

RFC 6238 HMAC-SHA1, 30-second windows, 6 digits. Pure stdlib (`hmac`, `hashlib`,
`base64`, `struct`) — no external dep.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("patchium.secrets")


VAULT_PATH = Path.home() / ".config" / "patchium" / "secrets.enc"
KEY_SERVICE = "patchium"
KEY_ACCOUNT = "secrets-key"
ENV_KEY = "PATCHIUM_SECRETS_KEY"

# Vault key is 32 bytes (SecretBox key size). Stored as base64 in env/keyring.
KEY_BYTES = 32


# ─── key management ────────────────────────────────────────────────────


class VaultLocked(RuntimeError):
    """Vault key not available — caller must set PATCHIUM_SECRETS_KEY,
    initialize keyring (`patchium secret init`), or set the key explicitly."""


def _key_from_env() -> bytes | None:
    raw = os.environ.get(ENV_KEY)
    if not raw:
        return None
    try:
        key = base64.b64decode(raw)
    except binascii.Error:
        return None
    if len(key) != KEY_BYTES:
        return None
    return key


def _key_from_keyring() -> bytes | None:
    try:
        import keyring
    except ImportError:
        return None
    try:
        raw = keyring.get_password(KEY_SERVICE, KEY_ACCOUNT)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        key = base64.b64decode(raw)
    except binascii.Error:
        return None
    return key if len(key) == KEY_BYTES else None


def get_vault_key() -> bytes:
    """Resolve vault key. Raises VaultLocked if none available."""
    key = _key_from_env() or _key_from_keyring()
    if key is None:
        raise VaultLocked(
            "vault key not available. Either set PATCHIUM_SECRETS_KEY "
            "(base64-32-bytes) or run `patchium secret init` to provision "
            "the OS keyring."
        )
    return key


def init_vault_key(prefer: str = "keyring") -> dict:
    """Generate a fresh 32-byte vault key and store it. Returns metadata
    about where the key was stored (caller may want to print the env value
    for CI/headless setups).

    `prefer`: 'keyring' (default) | 'env' (just print, don't store)
    """
    from nacl.utils import random as _nacl_random
    key = _nacl_random(KEY_BYTES)
    encoded = base64.b64encode(key).decode()
    out = {"key_b64": encoded, "stored_in": None}
    if prefer == "keyring":
        try:
            import keyring
            keyring.set_password(KEY_SERVICE, KEY_ACCOUNT, encoded)
            out["stored_in"] = "keyring"
        except Exception as exc:  # noqa: BLE001
            log.warning("keyring store failed (%s); user must set env", exc)
    return out


# ─── vault encryption ──────────────────────────────────────────────────


def _empty_vault() -> dict:
    return {"version": 1, "sites": {}}


def _encrypt(plaintext: bytes, key: bytes) -> bytes:
    from nacl.secret import SecretBox
    box = SecretBox(key)
    return box.encrypt(plaintext)  # nonce prepended to ciphertext


def _decrypt(blob: bytes, key: bytes) -> bytes:
    from nacl.secret import SecretBox
    box = SecretBox(key)
    return box.decrypt(blob)


def load_vault(key: bytes | None = None) -> dict:
    """Decrypt and parse the vault. Returns empty vault if file doesn't exist."""
    if not VAULT_PATH.exists():
        return _empty_vault()
    if key is None:
        key = get_vault_key()
    blob = VAULT_PATH.read_bytes()
    plaintext = _decrypt(blob, key)
    return json.loads(plaintext.decode())


def save_vault(vault: dict, key: bytes | None = None) -> None:
    """Encrypt and write the vault to disk with 0600 perms."""
    if key is None:
        key = get_vault_key()
    plaintext = json.dumps(vault).encode()
    blob = _encrypt(plaintext, key)
    VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    VAULT_PATH.write_bytes(blob)
    os.chmod(VAULT_PATH, 0o600)


# ─── vault CRUD ────────────────────────────────────────────────────────


def set_secret(site: str, key: str, value: str) -> None:
    vault = load_vault()
    sites = vault.setdefault("sites", {})
    site_dict = sites.setdefault(site, {})
    site_dict[key] = value
    save_vault(vault)
    log.info("secret set site=%s key=%s", site, key)


def get_secret(site: str, key: str) -> str | None:
    """Return the secret value, or None if not set. Use sparingly — prefer
    resolving via `fill --use-secret` so the value is never returned over RPC."""
    vault = load_vault()
    return vault.get("sites", {}).get(site, {}).get(key)


def list_secrets(site: str | None = None) -> dict:
    """List secrets in MASKED form. Never returns actual values."""
    vault = load_vault()
    sites = vault.get("sites", {})
    if site:
        site_dict = sites.get(site, {})
        return {site: {k: "<set>" for k in site_dict}}
    return {s: {k: "<set>" for k in site_dict} for s, site_dict in sites.items()}


def delete_secret(site: str, key: str | None = None) -> bool:
    """Delete a single key (key given) or the whole site entry (key=None)."""
    vault = load_vault()
    sites = vault.get("sites", {})
    if site not in sites:
        return False
    if key is None:
        del sites[site]
        save_vault(vault)
        return True
    if key not in sites[site]:
        return False
    del sites[site][key]
    if not sites[site]:
        del sites[site]
    save_vault(vault)
    return True


# ─── TOTP (RFC 6238) ───────────────────────────────────────────────────


def _b32_decode(seed: str) -> bytes:
    """Decode a base32 TOTP seed, tolerant of whitespace/spaces."""
    cleaned = seed.upper().replace(" ", "").replace("-", "")
    # Pad to multiple of 8
    while len(cleaned) % 8:
        cleaned += "="
    return base64.b32decode(cleaned)


def totp(seed: str, *, at: float | None = None, digits: int = 6,
         step: int = 30) -> str:
    """RFC 6238 HMAC-SHA1 TOTP. Returns a zero-padded string of `digits` digits.

    `at`: Unix timestamp (defaults to now). Useful for deterministic tests.
    """
    key = _b32_decode(seed)
    when = int(at if at is not None else time.time()) // step
    msg = struct.pack(">Q", when)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    truncated = (
        (h[offset] & 0x7F) << 24
        | (h[offset + 1] & 0xFF) << 16
        | (h[offset + 2] & 0xFF) << 8
        | (h[offset + 3] & 0xFF)
    )
    code = truncated % (10 ** digits)
    return str(code).zfill(digits)


# ─── resolution for `fill --use-secret` ────────────────────────────────


# ─── Wave 6.3b: email-code polling (IMAP) ──────────────────────────────


class EmailPollConfig:
    """Parsed email-poll URL for IMAP-based code retrieval."""
    def __init__(self, server: str, port: int, username: str, password: str,
                 regex: str, from_filter: str | None, use_ssl: bool,
                 mailbox: str = "INBOX") -> None:
        self.server = server
        self.port = port
        self.username = username
        self.password = password
        self.regex = regex
        self.from_filter = from_filter
        self.use_ssl = use_ssl
        self.mailbox = mailbox


def parse_email_poll_url(url: str) -> EmailPollConfig:
    """Parse `imap[s]://user:pass@server:port?regex=...&from=...&mailbox=...`.

    Uses `unquote` (not `unquote_plus`) on params so `+` in regexes stays
    literal — common since regex `\\d+` would otherwise become `\\d ` after
    form-style decoding.
    """
    from urllib.parse import urlparse, unquote
    p = urlparse(url)
    if p.scheme not in ("imap", "imaps"):
        raise ValueError(
            f"email-poll URL scheme must be imap or imaps, got {p.scheme!r}"
        )
    if not (p.username and p.password and p.hostname):
        raise ValueError("email-poll URL must include user:pass@host")
    # Hand-parse query string with `unquote` (preserves `+`).
    params: dict[str, str] = {}
    if p.query:
        for pair in p.query.split("&"):
            if "=" in pair:
                k, _, v = pair.partition("=")
                params[unquote(k)] = unquote(v)
            else:
                params[unquote(pair)] = ""
    regex = params.get("regex")
    if not regex:
        raise ValueError("email-poll URL must include ?regex=PATTERN")
    return EmailPollConfig(
        server=p.hostname,
        port=p.port or (993 if p.scheme == "imaps" else 143),
        username=p.username,
        password=p.password,
        regex=regex,
        from_filter=params.get("from"),
        use_ssl=(p.scheme == "imaps"),
        mailbox=params.get("mailbox", "INBOX"),
    )


def _extract_email_body(msg) -> str:
    """Pull out a usable text body from an email.message.EmailMessage."""
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    parts.append(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"))
                except Exception:  # noqa: BLE001
                    pass
        if parts:
            return "\n".join(parts)
        # Fall back to text/html
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    pass
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return str(msg.get_payload())


def wait_for_email_code(cfg: EmailPollConfig, *, timeout: int = 60,
                         max_age_s: int = 300, mark_read: bool = False,
                         poll_interval_s: float = 5.0,
                         _imap_class=None) -> str | None:
    """Poll IMAP for a message matching `cfg.from_filter` whose body matches
    `cfg.regex`. Returns regex group(1) (or group(0) if no groups). None if
    timeout elapsed.

    `_imap_class` is for testing — pass a mock IMAP4 class.
    """
    import email
    import email.utils
    import imaplib
    import re

    pattern = re.compile(cfg.regex)
    deadline = time.time() + timeout
    imap_cls = _imap_class or (imaplib.IMAP4_SSL if cfg.use_ssl else imaplib.IMAP4)

    while time.time() < deadline:
        try:
            conn = imap_cls(cfg.server, cfg.port)
            try:
                conn.login(cfg.username, cfg.password)
                conn.select(cfg.mailbox)
                criteria_parts = ["UNSEEN"]
                if cfg.from_filter:
                    criteria_parts.append(f'FROM "{cfg.from_filter}"')
                typ, data = conn.search(None, *criteria_parts)
                if typ == "OK" and data and data[0]:
                    # Newest first
                    uids = data[0].split()[::-1]
                    for uid in uids:
                        typ, msg_data = conn.fetch(uid, "(RFC822)")
                        if typ != "OK" or not msg_data or not msg_data[0]:
                            continue
                        try:
                            msg = email.message_from_bytes(msg_data[0][1])
                        except Exception:  # noqa: BLE001
                            continue
                        # Check max age. parsedate_to_datetime returns a NAIVE
                        # datetime for `-0000` ("no zone info" per RFC 5322).
                        # .timestamp() on naive treats as LOCAL — wrong. Force
                        # UTC interpretation when the header is naive.
                        date_str = msg.get("Date")
                        if date_str:
                            try:
                                import datetime as _dt
                                parsed_dt = email.utils.parsedate_to_datetime(date_str)
                                if parsed_dt.tzinfo is None:
                                    parsed_dt = parsed_dt.replace(tzinfo=_dt.timezone.utc)
                                ts = parsed_dt.timestamp()
                                if time.time() - ts > max_age_s:
                                    continue
                            except Exception:  # noqa: BLE001
                                pass
                        body = _extract_email_body(msg)
                        m = pattern.search(body)
                        if m:
                            if mark_read:
                                try:
                                    conn.store(uid, "+FLAGS", "\\Seen")
                                except Exception:  # noqa: BLE001
                                    pass
                            return m.group(1) if m.groups() else m.group(0)
            finally:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            log.debug("imap poll failed: %s", exc)
        # Sleep before retry, but not past deadline
        sleep_for = min(poll_interval_s, max(0, deadline - time.time()))
        if sleep_for <= 0:
            break
        time.sleep(sleep_for)
    return None


def resolve_secret_reference(ref: str) -> str:
    """Resolve a 'site:key' reference (e.g. 'github.com:totp') to a value.

    Special case: 'site:totp' generates a TOTP from the stored 'totp-seed'.
    """
    if ":" not in ref:
        raise ValueError(f"invalid secret reference {ref!r}; expected site:key")
    site, key = ref.split(":", 1)
    if key == "totp":
        seed = get_secret(site, "totp-seed")
        if not seed:
            raise KeyError(f"no totp-seed set for site {site!r}")
        return totp(seed)
    val = get_secret(site, key)
    if val is None:
        raise KeyError(f"no secret {key!r} for site {site!r}")
    return val
