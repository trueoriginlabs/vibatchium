"""Wave 6.3a — credential vault + TOTP.

Encrypted on-disk vault for per-site credentials with native RFC 6238 TOTP.
Unlocks every auth-gated flow that breaks at 2FA today.

### Storage

A single encrypted blob at `~/.config/vibatchium/secrets.enc`:
  - file mode: 0600
  - format: `[24-byte nonce][ciphertext]` (PyNaCl SecretBox = XSalsa20-Poly1305)
  - key: 32 bytes, sourced from one of (priority order):
      1. `VIBATCHIUM_SECRETS_KEY` env var (base64) — CI / headless servers
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
      "email-poll": "imap://user:pass@imap.gmail.com:993?regex=\\d{6}",
      "origins": "https://github.com,https://gist.github.com"
    }
  }
}
```

### Hard security requirements

- Vault content NEVER appears in logs / observe-cache / HAR captures.
- `secret list` returns MASKED values (`<set>` instead of the value).
- Logs only mention site names + key names, never values.
- The CI grep-for-leakage test in `test_wave6_vault.py` enforces this.

`origins` is optional: it replaces the default rule for where `fill
--use-secret` may write this entry's values (see "origin binding" below).

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

log = logging.getLogger("vibatchium.secrets")


# `VIBATCHIUM_VAULT_PATH` relocates the vault. This exists so the TEST SUITE
# can stop writing to the real user vault: conftest forces a fixed test
# encryption key, and `save_vault` re-encrypts the WHOLE file under whatever
# key is active — so a suite run against the default path silently re-keys a
# real user's vault and makes every existing entry permanently undecryptable.
# Unique per-test site names (the previous mitigation) do not help, because the
# damage is to the file's key, not to the entries.
VAULT_PATH = Path(
    os.environ.get("VIBATCHIUM_VAULT_PATH")
    or Path.home() / ".config" / "vibatchium" / "secrets.enc"
)
KEY_SERVICE = "vibatchium"
KEY_ACCOUNT = "secrets-key"
ENV_KEY = "VIBATCHIUM_SECRETS_KEY"

# Vault key is 32 bytes (SecretBox key size). Stored as base64 in env/keyring.
KEY_BYTES = 32


# ─── key management ────────────────────────────────────────────────────


class VaultLocked(RuntimeError):
    """Vault key not available — caller must set VIBATCHIUM_SECRETS_KEY,
    initialize keyring (`vb secret init`), or set the key explicitly."""


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
            "vault key not available. Either set VIBATCHIUM_SECRETS_KEY "
            "(base64-32-bytes) or run `vb secret init` to provision "
            "the OS keyring."
        )
    return key


class VaultAlreadyInitialized(RuntimeError):
    """Raised when `init_vault_key` is called against an existing vault
    without force=True. Prevents the silent-data-loss footgun where a
    fresh key would render the existing encrypted vault undecryptable."""


def init_vault_key(prefer: str = "keyring", *, force: bool = False) -> dict:
    """Generate a fresh 32-byte vault key and store it. Returns metadata
    about where the key was stored (caller may want to print the env value
    for CI/headless setups).

    `prefer`: 'keyring' (default) | 'env' (just print, don't store)
    `force`: required when an existing vault file is present. Without it,
        raises VaultAlreadyInitialized — a fresh key would silently render
        the existing ciphertext unrecoverable on next read.
    """
    try:
        from nacl.utils import random as _nacl_random
    except ImportError as exc:
        raise RuntimeError(
            "vault key init requires pynacl. Install with: "
            "`pip install vibatchium[secrets]`"
        ) from exc

    # Existence check: a vault file already encrypted to some prior key
    # MUST NOT be invisibly orphaned by generating a new one.
    if VAULT_PATH.exists() and not force:
        raise VaultAlreadyInitialized(
            f"vault already initialized at {VAULT_PATH} ({VAULT_PATH.stat().st_size} bytes). "
            f"Generating a new key would render existing entries undecryptable. "
            f"Pass force=True (CLI: `--force`) to overwrite, OR archive "
            f"{VAULT_PATH} first if you may need to recover old entries with "
            f"the original key."
        )

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
                                    parsed_dt = parsed_dt.replace(tzinfo=_dt.UTC)
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


def split_secret_reference(ref: str) -> tuple[str, str]:
    """Parse a 'site:key' reference WITHOUT touching the vault. The site is
    everything before the FIRST colon, so a site can never carry a port or a
    scheme — it is a bare host name."""
    if not isinstance(ref, str) or ":" not in ref:
        raise ValueError(f"invalid secret reference {ref!r}; expected site:key")
    site, key = ref.split(":", 1)
    if not site or not key:
        raise ValueError(f"invalid secret reference {ref!r}; expected site:key")
    return site, key


def resolve_secret_reference(ref: str) -> str:
    """Resolve a 'site:key' reference (e.g. 'github.com:totp') to a value.

    Special case: 'site:totp' generates a TOTP from the stored 'totp-seed'.

    Callers that WRITE the value into a page must run `check_secret_origin`
    first (the `fill` handler does) — this function has no idea where the value
    is going.
    """
    site, key = split_secret_reference(ref)
    if key == "totp":
        seed = get_secret(site, "totp-seed")
        if not seed:
            raise KeyError(f"no totp-seed set for site {site!r}")
        return totp(seed)
    val = get_secret(site, key)
    if val is None:
        raise KeyError(f"no secret {key!r} for site {site!r}")
    return val


# ─── origin binding for `fill --use-secret` (0.19.4) ───────────────────
#
# A vault entry is keyed by a site name, and `fill --use-secret site:key` used
# to write the value into whatever element was targeted on whatever page was
# open. A prompt-injected agent could therefore `fill @e3 --use-secret
# github.com:password` on evil.com and read the field back. The value is now
# only written into a document whose origin belongs to the site:
#
#   * default — the frame's host is the site or a subdomain of it ("github.com"
#     allows github.com and *.github.com; never github.com.evil.com or
#     evilgithub.com). A leading "www." on the site is dropped first, so
#     "www.example.com" also allows login.example.com.
#   * explicit — an `origins` key on the entry REPLACES the default:
#       vb secret set github.com origins "https://github.com,https://gist.github.com"
#     Entries: "https://host[:port]" (exact origin), "https://*.host" (any
#     subdomain, not the apex), or a bare "host" (the default host rule).
#   * always — https only, except loopback hosts (localhost, *.localhost,
#     127.0.0.0/8, ::1) which may be plain http, for local dev and tests.
#
# No public-suffix list: a site named after a suffix ("co.uk") would match
# every host under it. Site names are chosen by the operator, not the page, and
# a single-label site ("intranet", "com") matches only itself exactly.

ORIGINS_KEY = "origins"
ALLOW_CROSS_ORIGIN_ENV = "VIBATCHIUM_SECRET_ALLOW_CROSS_ORIGIN"
# Lets value / eval / wait_fn / eval_handle / handle_eval / detect_forms
# values=true run while a vault secret is live in the session, and lets a secret
# be filled into a document caller JS already ran in. Daemon env only.
ALLOW_READBACK_ENV = "VIBATCHIUM_SECRET_ALLOW_READBACK"

# Args that only an operator may set. The MCP server (and a caps-restricted
# REST shim) refuse them, because an injected agent on those surfaces would
# simply pass them. The daemon itself accepts them from the CLI / SDK, which
# already run as the user with direct access to the vault.
OPERATOR_ONLY_ARGS: dict[str, tuple[str, ...]] = {
    "fill": ("allow_cross_origin",),
}

# Verbs refused outright on agent surfaces: every vault MUTATION (any key, not
# just `origins` — `email-poll` repoints the IMAP poller, deleting `origins`
# drops a narrowing policy, `secret_init --force` orphans the vault) and every
# verb that RETURNS a code without an origin check. `secret_list` stays: it is
# masked and read-only.
OPERATOR_ONLY_VERBS: dict[str, str] = {
    "secret_init": "provisions or replaces the vault key",
    "secret_set": "writes a vault entry",
    "secret_delete": "deletes a vault entry",
    "secret_totp": "returns a live TOTP code with no origin check",
    "wait_email_code": "returns a one-time email code with no origin check",
}

_DEFAULT_PORTS = {"https": 443, "http": 80}


class SecretOriginError(PermissionError):
    """The target document's origin is not one the secret's site allows."""


def normalize_host(host: str | None) -> str | None:
    """Lower-case, strip a trailing dot / IPv6 brackets, and IDNA-encode a host
    so `Bücher.DE.` and `xn--bcher-kva.de` compare equal. None if unusable."""
    if not host or not isinstance(host, str):
        return None
    h = host.strip().rstrip(".").strip("[]").lower()
    if not h:
        return None
    if any(c in h for c in "/\\@:?#% ") and not _is_ip(h):
        return None
    if h.isascii():
        return h
    # UTS #46 non-transitional (IDNA2008), which is what browsers resolve:
    # `straße.de` is `xn--strae-oqa.de`, NOT `strasse.de` (Python's built-in
    # "idna" codec is IDNA2003 and maps ß→ss, so a `straße.de` entry would have
    # matched a different registrable domain). Without the `idna` package we
    # refuse the name instead of guessing — use the punycode form.
    try:
        import idna as _idna
    except ImportError:  # pragma: no cover — declared in pyproject
        return None
    try:
        return _idna.encode(h, uts46=True, transitional=False).decode("ascii").lower()
    except (_idna.IDNAError, UnicodeError, ValueError):
        return None


def _is_ip(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_loopback_host(host: str | None) -> bool:
    import ipaddress
    h = normalize_host(host)
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def host_matches_site(host: str | None, site: str | None) -> bool:
    """True when `host` is `site` or a subdomain of it. Label-boundary aware
    (no `evilgithub.com`, no `github.com.evil.com`); IPs and single-label sites
    match exactly only."""
    h, s = normalize_host(host), normalize_host(site)
    if not h or not s:
        return False
    if h == s:
        return True
    if _is_ip(h) or _is_ip(s):
        return False
    if s.startswith("www.") and "." in s[4:]:
        s = s[4:]
        if h == s:
            return True
    if "." not in s:
        return False
    return h.endswith("." + s)


def url_origin(url: str | None) -> tuple[str, str, int] | None:
    """(scheme, normalized host, effective port) for an http(s) URL, else None.
    A `blob:` URL takes its creator's origin; about:/data:/file: are opaque."""
    from urllib.parse import urlsplit
    if not url or not isinstance(url, str):
        return None
    if url.startswith("blob:"):
        url = url[5:]
    try:
        p = urlsplit(url.strip())
        scheme = (p.scheme or "").lower()
        port = p.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS:
        return None
    host = normalize_host(p.hostname)
    if not host:
        return None
    return scheme, host, port or _DEFAULT_PORTS[scheme]


def format_origin(origin: tuple[str, str, int]) -> str:
    scheme, host, port = origin
    shown = f"[{host}]" if ":" in host else host
    if port == _DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{shown}"
    return f"{scheme}://{shown}:{port}"


def parse_origins(value) -> list[str]:
    """Split a stored `origins` value (comma/whitespace-separated string, or a
    list) into entries, validating each. Raises ValueError on a bad entry so a
    typo is caught at `secret set` time, not at fill time."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else \
        str(value).replace(",", " ").split()
    out: list[str] = []
    for raw in items:
        entry = str(raw).strip().rstrip("/")
        if not entry:
            continue
        if "://" in entry:
            scheme, _, rest = entry.partition("://")
            if scheme.lower() not in _DEFAULT_PORTS:
                raise ValueError(f"origins entry {raw!r}: scheme must be https "
                                 f"(or http for a loopback host)")
            if "/" in rest:
                raise ValueError(f"origins entry {raw!r}: an origin has no path")
            wildcard = rest.startswith("*.")
            probe = f"{scheme}://{rest[2:] if wildcard else rest}"
            o = url_origin(probe)
            if o is None:
                raise ValueError(f"origins entry {raw!r} is not a valid origin")
            if o[0] == "http" and not is_loopback_host(o[1]):
                raise ValueError(f"origins entry {raw!r}: plain http is only "
                                 f"allowed for loopback hosts")
        else:
            if normalize_host(entry.removeprefix("*.")) is None:
                raise ValueError(f"origins entry {raw!r} is not a valid host")
        out.append(entry)
    return out


def _entry_allows(entry: str, origin: tuple[str, str, int]) -> bool:
    scheme, host, port = origin
    if "://" not in entry:
        # Bare host: the default host-or-subdomain rule (scheme rule applied by
        # the caller, port ignored — same as a site name).
        if entry.startswith("*."):
            base = normalize_host(entry[2:])
            return bool(base) and host != base and host_matches_site(host, base)
        return host_matches_site(host, entry)
    e_scheme, _, rest = entry.partition("://")
    if e_scheme.lower() != scheme:
        return False
    if rest.startswith("*."):
        o = url_origin(f"{e_scheme}://{rest[2:]}")
        if o is None or o[2] != port or host == o[1] or _is_ip(host):
            return False
        return host.endswith("." + o[1])
    return url_origin(f"{e_scheme}://{rest}") == origin


def check_secret_origin(url: str | None, site: str,
                        origins=None) -> str:
    """Raise SecretOriginError unless a secret for `site` may be written into a
    document at `url`. Returns the serialized origin on success.

    `origins` is the entry's stored `origins` value (string or list); when
    non-empty it REPLACES the default site-name rule. Pure function — no vault
    access — so it is unit-testable and runs before anything is decrypted.
    """
    origin = url_origin(url)
    if origin is None:
        raise SecretOriginError(
            f"refusing to fill a {site!r} secret: the target document has no "
            f"http(s) origin ({url!r})")
    scheme, host, _port = origin
    shown = format_origin(origin)
    if scheme != "https" and not is_loopback_host(host):
        raise SecretOriginError(
            f"refusing to fill a {site!r} secret into {shown}: plain http is "
            f"only allowed for loopback hosts")
    entries = parse_origins(origins)
    if entries:
        if any(_entry_allows(e, origin) for e in entries):
            return shown
        raise SecretOriginError(
            f"refusing to fill a {site!r} secret into {shown}: not in the "
            f"entry's origins ({', '.join(entries)}). Add it with "
            f"`vb secret set {site} origins \"...\"` if this is intended.")
    if host_matches_site(host, site):
        return shown
    raise SecretOriginError(
        f"refusing to fill a {site!r} secret into {shown}: the page is not "
        f"{site} or a subdomain of it. If this site legitimately uses another "
        f"domain, list it with `vb secret set {site} origins \"https://...\"`.")


def get_site_origins(site: str) -> list[str]:
    """The entry's explicit `origins` policy (possibly empty). Decrypts the
    vault to read it but returns ONLY the policy — no secret value is resolved
    here. A missing vault / site yields [] (the default rule then applies, and
    the later resolve reports the missing secret)."""
    if not VAULT_PATH.exists():
        return []
    entry = load_vault().get("sites", {}).get(site) or {}
    try:
        return parse_origins(entry.get(ORIGINS_KEY))
    except ValueError as exc:
        # A malformed stored policy must fail CLOSED, not fall back to the
        # (possibly broader) default rule.
        raise SecretOriginError(
            f"refusing to fill a {site!r} secret: its stored origins policy is "
            f"invalid ({exc})") from exc


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def cross_origin_env_enabled() -> bool:
    return _env_flag(ALLOW_CROSS_ORIGIN_ENV)


def readback_env_enabled() -> bool:
    return _env_flag(ALLOW_READBACK_ENV)


def agent_surface_violation(cmd: str, args: dict | None) -> str | None:
    """For agent-facing surfaces (MCP, caps-restricted REST): return an error
    message if the call is operator-only, else None.

    Operator-only: the fill escape hatch (`allow_cross_origin`), every vault
    mutation, and the verbs that hand back a TOTP / email code without the
    origin check `fill --use-secret` applies. An agent that needs a TOTP fills
    it: `fill <target> --use-secret site:totp`.
    """
    args = args or {}
    why = OPERATOR_ONLY_VERBS.get(cmd)
    if why:
        shell = "vb " + cmd.replace("_", " ", 1) if cmd.startswith("secret_") \
            else "vb " + cmd.replace("_", "-")
        return (f"{cmd!r} {why} and is operator-only — it is not accepted on "
                f"this surface. Run `{shell} ...` from a shell.")
    for name in OPERATOR_ONLY_ARGS.get(cmd, ()):
        if args.get(name):
            return (f"{name!r} is operator-only and is not accepted on this "
                    f"surface — use `vb {cmd} --allow-cross-origin` from a shell "
                    f"or set {ALLOW_CROSS_ORIGIN_ENV}=1 on the daemon")
    return None
