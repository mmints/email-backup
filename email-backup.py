#!/usr/bin/env python3
"""
email_backup.py - Full backup of an IMAP mailbox.

Downloads ALL e-mails from ALL folders as .eml files (lossless, including
embedded attachments) and additionally collects ALL attachments in a
separate folder.

- Uses only the Python standard library (no installation required).
- Credentials stay local on your machine.
- Resumable: re-running skips messages that were already downloaded.
- Supports both classic password login and OAuth2 / SASL XOAUTH2, which is
  the only method Microsoft 365 / Exchange Online still accepts.

Usage:
    python3 email_backup.py
    python3 email_backup.py --check     # diagnose server / auth method only

Optionally preset via environment variables:
    IMAP_HOST, IMAP_PORT (default 993), IMAP_USER, IMAP_PASS, IMAP_OUTDIR,
    IMAP_AUTH (auto|basic|oauth2), IMAP_MAILBOX_USER,
    IMAP_OAUTH_CLIENT_ID, IMAP_OAUTH_TENANT, IMAP_OAUTH_SCOPE,
    IMAP_OAUTH_FLOW (authcode|devicecode), IMAP_TOKEN_CACHE

Copyright (c) 2026 Mark O. Mints <mark@mints.de>
"""

import os
import re
import ssl
import sys
import json
import time
import base64
import socket
import hashlib
import getpass
import imaplib
import email
import http.server
import webbrowser
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

# imaplib limits lines to ~1 MB by default; long header lines (e.g. many
# recipients) can trip over that. Raise the limit generously.
imaplib._MAXLINE = 10_000_000


# ----------------------------------------------------------------------
# Configuration (can also be set via environment variables)
# ----------------------------------------------------------------------
HOST = os.environ.get("IMAP_HOST")          # e.g. "imap.gmail.com"
PORT = int(os.environ.get("IMAP_PORT", "993"))
USER = os.environ.get("IMAP_USER")
PASS = os.environ.get("IMAP_PASS")
OUTDIR = os.environ.get("IMAP_OUTDIR", "email-backup")

# How to authenticate: "auto" picks XOAUTH2 for Microsoft hosts that offer it,
# and a normal password login everywhere else.
AUTH_MODE = (os.environ.get("IMAP_AUTH") or "auto").strip().lower()

# The mailbox to open. Normally identical to USER, but for a shared mailbox you
# sign in as yourself and name the shared address here.
MAILBOX_USER = os.environ.get("IMAP_MAILBOX_USER")

# --- OAuth2 (Microsoft Entra ID) --------------------------------------
OAUTH_AUTHORITY = "https://login.microsoftonline.com"
OAUTH_TENANT = os.environ.get("IMAP_OAUTH_TENANT", "common")
# Default is Mozilla Thunderbird's public application. Many organizations have
# already consented to it, so it often works without involving an admin. If
# yours has not, register your own app and set IMAP_OAUTH_CLIENT_ID (README).
OAUTH_CLIENT_ID = os.environ.get(
    "IMAP_OAUTH_CLIENT_ID", "9e5f94bc-e8a4-4e73-b8be-63364c29d753")
OAUTH_SCOPE = os.environ.get(
    "IMAP_OAUTH_SCOPE",
    "https://outlook.office.com/IMAP.AccessAsUser.All offline_access")

# How the browser sign-in is performed:
#   "authcode"   - open a browser here, catch the reply on 127.0.0.1 (default)
#   "devicecode" - show a code to type in on any device
# Device code flow is frequently blocked by Conditional Access policies
# (AADSTS53003), which is why the local browser flow is the default.
OAUTH_FLOW = (os.environ.get("IMAP_OAUTH_FLOW") or "authcode").strip().lower()

# Refresh tokens are long-lived credentials, so they are kept outside OUTDIR
# (which tends to get copied onto backup drives).
TOKEN_CACHE = os.environ.get("IMAP_TOKEN_CACHE") or os.path.join(
    os.path.expanduser("~"), ".config", "email-backup", "tokens.json")

# Hosts for which "auto" prefers OAuth2 over a password.
MICROSOFT_HOSTS = (
    "outlook.office365.com",
    "outlook.office.com",
    "outlook.com",
    "outlook.live.com",
    "office365.com",
)


# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def _modified_b64decode(s: str) -> str:
    """Decode the Base64 part of an IMAP modified-UTF-7 sequence."""
    b = s.replace(",", "/").encode("ascii")
    b += b"=" * ((4 - len(b) % 4) % 4)
    return base64.b64decode(b).decode("utf-16-be")


def imap_utf7_decode(s) -> str:
    """IMAP modified UTF-7 -> readable Unicode string (for folder names)."""
    if isinstance(s, bytes):
        s = s.decode("ascii", "replace")
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "&":
            j = s.find("-", i)
            if j == -1:
                out.append(s[i:])
                break
            out.append("&" if j == i + 1 else _modified_b64decode(s[i + 1:j]))
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def decode_mime_words(s) -> str:
    """Decode MIME-encoded headers (e.g. =?UTF-8?...?= in file names)."""
    if s is None:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return str(s)


_INVALID = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def sanitize(name: str, fallback: str = "unnamed", maxlen: int = 150) -> str:
    """Turn arbitrary text into a safe file/folder name."""
    name = _INVALID.sub("_", (name or "").strip())
    name = name.strip(" .")            # no leading/trailing dots or spaces
    if len(name) > maxlen:
        root, ext = os.path.splitext(name)
        name = root[: maxlen - len(ext)] + ext
    return name or fallback


def unique_path(directory: str, filename: str) -> str:
    """Return a not-yet-taken path (appends _1, _2 ... on collision)."""
    dest = os.path.join(directory, filename)
    if not os.path.exists(dest):
        return dest
    root, ext = os.path.splitext(filename)
    k = 1
    while True:
        cand = os.path.join(directory, f"{root}_{k}{ext}")
        if not os.path.exists(cand):
            return cand
        k += 1


# UID at the end of an .eml file name (..._<uid>.eml) -> used for resuming
_UID_IN_NAME = re.compile(r"__(\d+)\.eml$")


def sender_name(msg) -> str:
    """Return a readable, filename-safe sender."""
    name, addr = parseaddr(msg.get("From", ""))
    chosen = decode_mime_words(name).strip() or addr or "unknown"
    return sanitize(chosen, fallback="unknown", maxlen=40)


_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\) (?P<sep>"[^"]*"|NIL) (?P<name>.*)$')


def parse_folder(line) -> tuple:
    """Extract (raw folder name, hierarchy delimiter) from a LIST response.

    The delimiter is whatever the server reports ("/" on Exchange, "." on some
    Courier/Dovecot setups). Splitting on it - rather than on a fixed set of
    characters - keeps folder names that merely contain a dot or slash intact.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", "surrogateescape")
    m = _LIST_RE.match(line)
    if not m:
        return "", ""
    name = m.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    sep = m.group("sep")
    if sep == "NIL":
        sep = ""
    else:
        sep = sep[1:-1].replace("\\\\", "\\")
    return name, sep


def message_datetime(msg, meta) -> tuple:
    """Determine the real message timestamp.

    Prefers the 'Date' header (time sent); otherwise falls back to the
    IMAP INTERNALDATE (time received at the server).
    Returns: (epoch_or_None, "YYYY-MM-DD_HH-MM-SS"_or_fallback)
    """
    dt = None
    raw_date = msg.get("Date")
    if raw_date:
        try:
            dt = parsedate_to_datetime(raw_date)
        except Exception:
            dt = None
    if dt is None and meta:
        try:
            t = imaplib.Internaldate2tuple(meta)
            if t:
                dt = datetime.fromtimestamp(time.mktime(t))
        except Exception:
            dt = None
    if dt is None:
        return None, "date-unknown"
    try:
        epoch = dt.timestamp()
    except Exception:
        epoch = None
    return epoch, dt.strftime("%Y-%m-%d_%H-%M-%S")


def set_file_time(path: str, epoch) -> None:
    """Set the file's access/modification time to the message timestamp."""
    if epoch is None:
        return
    try:
        os.utime(path, (epoch, epoch))
    except Exception:
        pass


def draw_progress(done: int, total: int, label: str = "", width: int = 38) -> None:
    """Draw a single-line progress bar (updates in place via \\r)."""
    if total <= 0:
        return
    frac = min(done / total, 1.0)
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    label = (label[:22]).ljust(22)
    sys.stdout.write(f"\r[{bar}] {int(frac*100):3d}%  {done}/{total}  {label}")
    sys.stdout.flush()


def save_attachments(msg, att_dir: str, epoch) -> int:
    """Save all named parts (attachments / named inline images)."""
    count = 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        raw_name = part.get_filename()
        if not raw_name:
            continue
        filename = sanitize(decode_mime_words(raw_name), fallback="attachment")
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        dest = unique_path(att_dir, filename)
        with open(dest, "wb") as f:
            f.write(payload)
        set_file_time(dest, epoch)     # real message date as timestamp
        count += 1
    return count


# ----------------------------------------------------------------------
# OAuth2 / Microsoft Entra ID (device code flow, standard library only)
# ----------------------------------------------------------------------
class OAuthError(Exception):
    """Something went wrong while obtaining an access token."""


def _oauth_post(url: str, fields: dict) -> dict:
    """POST a form to an Entra endpoint and return the parsed JSON.

    Entra reports its errors with a 4xx status *and* a useful JSON body, so the
    body is parsed in that case too instead of being thrown away.
    """
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except Exception:
            return {"error": f"http_{e.code}", "error_description": body[:500]}
    except Exception as e:
        return {"error": "network", "error_description": str(e)}


def _oauth_error_text(res: dict, client_id: str, tenant: str) -> str:
    """Turn an Entra error response into something actionable."""
    code = res.get("error", "unknown_error")
    desc = (res.get("error_description") or "").strip()
    # Entra puts the real reason in the first line of error_description.
    first = desc.splitlines()[0] if desc else ""
    msg = f"{code}: {first}" if first else code

    hints = []
    if "AADSTS65001" in desc or code == "consent_required":
        hints.append(
            f"Your organization has not consented to client ID {client_id}. "
            "Either register your own app (see README) or ask your IT "
            "department to grant admin consent for it.")
    if "AADSTS700016" in desc:
        hints.append(
            f"Client ID {client_id} is unknown in tenant '{tenant}'. Register "
            "your own app and set IMAP_OAUTH_CLIENT_ID.")
    if "AADSTS50059" in desc or "AADSTS900023" in desc:
        hints.append(
            "Microsoft could not tell which organization to sign you in to. "
            "Set IMAP_OAUTH_TENANT to your e-mail domain, e.g. "
            "IMAP_OAUTH_TENANT=example.edu.")
    if "AADSTS7000218" in desc:
        hints.append(
            "The app registration is not marked as a public client. Enable "
            "'Allow public client flows' in its Authentication settings.")
    if "AADSTS53003" in desc or "AADSTS50005" in desc:
        hints.append(
            "A Conditional Access policy blocked the sign-in itself (the "
            "password was fine).")
        if OAUTH_FLOW == "devicecode":
            hints.append(
                "Device code flow is the flow admins most often block. Try "
                "the local browser flow instead: IMAP_OAUTH_FLOW=authcode.")
        else:
            hints.append(
                "Since this was already the local browser flow, the policy "
                "most likely demands a managed/compliant device. Ask your IT "
                "department, or use your organization's own IMAP server if "
                "it still accepts a password ('--check' lists it).")
    if "AADSTS50076" in desc or "AADSTS50079" in desc:
        hints.append(
            "Multi-factor authentication is required for this sign-in. "
            "Complete the MFA prompt in the browser and try again.")
    if hints:
        msg += "\n  " + "\n  ".join(hints)
    return msg


def _load_token_cache(path: str) -> dict:
    """Read the token cache; any problem simply means 'no cached tokens'."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_token_cache(path: str, cache: dict) -> None:
    """Write the token cache with owner-only permissions."""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        os.chmod(path, 0o600)
    except Exception as e:
        print(f"  (Warning: could not save the token cache: {e})")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches the single redirect Microsoft sends back to 127.0.0.1."""

    def do_GET(self):
        params = dict(urllib.parse.parse_qsl(
            urllib.parse.urlparse(self.path).query))
        if "code" not in params and "error" not in params:
            self.send_error(404)          # favicon.ico and friends
            return
        self.server.oauth_result = params
        ok = "code" in params
        title = "Sign-in complete" if ok else "Sign-in failed"
        body = ("You can close this tab and return to the terminal."
                if ok else
                "Microsoft refused the sign-in: "
                + (params.get("error_description") or params.get("error", "")))
        page = (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                "<body style='font:16px system-ui;margin:4rem auto;max-width:34rem'>"
                f"<h2>{title}</h2><p>{body}</p>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):
        pass                              # keep the progress output clean


def _pkce_pair() -> tuple:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def auth_code_login(tenant: str, client_id: str, scope: str,
                    login_hint: str = "", wait: int = 300) -> dict:
    """OAuth2 authorization code flow with PKCE, via a local browser.

    Unlike the device code flow this happens on *this* machine, so Entra sees
    the real browser and device. Conditional Access policies that block device
    code flow generally allow this one.
    """
    verifier, challenge = _pkce_pair()
    state = base64.urlsafe_b64encode(os.urandom(15)).decode().rstrip("=")

    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    except OSError as e:
        raise OAuthError(
            f"Could not listen on 127.0.0.1 for the browser reply: {e}\n"
            "  Use IMAP_OAUTH_FLOW=devicecode instead.")
    server.oauth_result = None
    server.timeout = 1
    redirect_uri = f"http://localhost:{server.server_address[1]}/"

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    url = (f"{OAUTH_AUTHORITY}/{tenant}/oauth2/v2.0/authorize?"
           + urllib.parse.urlencode(params))

    print("\nOpening your browser to sign in ...")
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        print("Could not open a browser automatically.")
    print("If nothing happens, open this address yourself:\n\n" + url + "\n")
    print("Waiting for the sign-in to complete ...")

    deadline = time.time() + wait
    try:
        while server.oauth_result is None and time.time() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        raise
    finally:
        server.server_close()

    result = server.oauth_result
    if result is None:
        raise OAuthError(
            "Timed out waiting for the browser sign-in.\n"
            "  If the browser showed 'You don't have access to this', a "
            "Conditional Access\n"
            "  policy blocked it - see the README section on AADSTS53003.")
    if "error" in result:
        raise OAuthError(_oauth_error_text(result, client_id, tenant))
    if result.get("state") != state:
        raise OAuthError("The browser reply did not match this request "
                         "(state mismatch). Aborting for safety.")

    print("Sign-in complete.")
    res = _oauth_post(f"{OAUTH_AUTHORITY}/{tenant}/oauth2/v2.0/token", {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "scope": scope,
    })
    if "access_token" not in res:
        raise OAuthError(_oauth_error_text(res, client_id, tenant))
    return res


def device_code_login(tenant: str, client_id: str, scope: str) -> dict:
    """Run the OAuth2 device authorization grant flow."""
    base = f"{OAUTH_AUTHORITY}/{tenant}/oauth2/v2.0"
    res = _oauth_post(f"{base}/devicecode",
                      {"client_id": client_id, "scope": scope})
    if "device_code" not in res:
        raise OAuthError(_oauth_error_text(res, client_id, tenant))

    print("\n" + "=" * 70)
    print(res.get("message") or
          f"Open {res.get('verification_uri')} and enter the code "
          f"{res.get('user_code')}")
    print("=" * 70)
    print("Waiting for you to complete the sign-in in your browser ...")

    interval = int(res.get("interval") or 5)
    deadline = time.time() + int(res.get("expires_in") or 900)
    device_code = res["device_code"]

    while time.time() < deadline:
        time.sleep(interval)
        tok = _oauth_post(f"{base}/token", {
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        })
        err = tok.get("error")
        if not err:
            print("Sign-in complete.")
            return tok
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise OAuthError(_oauth_error_text(tok, client_id, tenant))

    raise OAuthError("The device code expired before the sign-in completed.")


def get_access_token(account: str, tenant: str, client_id: str,
                     scope: str, cache_path: str) -> str:
    """Return a usable access token: from cache, by refresh, or by sign-in."""
    key = f"{client_id}|{tenant}|{account}"
    cache = _load_token_cache(cache_path)
    entry = cache.get(key) or {}

    token = entry.get("access_token")
    if token and float(entry.get("expires_at") or 0) > time.time() + 60:
        return token

    refresh = entry.get("refresh_token")
    if refresh:
        print("Refreshing the stored OAuth token ...")
        res = _oauth_post(f"{OAUTH_AUTHORITY}/{tenant}/oauth2/v2.0/token", {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "scope": scope,
        })
        if "access_token" in res:
            _remember_token(cache, key, res, refresh, cache_path)
            return res["access_token"]
        print(f"  Stored token is no longer valid "
              f"({res.get('error', 'unknown')}); signing in again.")

    if OAUTH_FLOW == "devicecode":
        res = device_code_login(tenant, client_id, scope)
    elif OAUTH_FLOW == "authcode":
        res = auth_code_login(tenant, client_id, scope, login_hint=account)
    else:
        raise OAuthError(f"Unknown IMAP_OAUTH_FLOW value '{OAUTH_FLOW}' "
                         "(expected authcode or devicecode).")
    if "access_token" not in res:
        raise OAuthError(_oauth_error_text(res, client_id, tenant))
    _remember_token(cache, key, res, refresh, cache_path)
    return res["access_token"]


def _remember_token(cache: dict, key: str, res: dict,
                    old_refresh, cache_path: str) -> None:
    """Store the new tokens, keeping the previous refresh token if unchanged."""
    cache[key] = {
        "access_token": res.get("access_token"),
        "refresh_token": res.get("refresh_token") or old_refresh,
        "expires_at": time.time() + float(res.get("expires_in") or 3600) - 30,
    }
    _save_token_cache(cache_path, cache)


def xoauth2_string(user: str, token: str) -> bytes:
    """Build the SASL XOAUTH2 payload (imaplib does the Base64 itself)."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()


def imap_xoauth2_login(imap, user: str, token: str) -> None:
    """AUTHENTICATE XOAUTH2, surfacing the server's real reason on failure."""
    state = {"calls": 0, "challenge": None}

    def responder(challenge):
        state["calls"] += 1
        if state["calls"] == 1:
            return xoauth2_string(user, token)
        # On failure the server sends a Base64 JSON error and expects an empty
        # line back; without it the connection just hangs.
        state["challenge"] = challenge
        return b""

    try:
        imap.authenticate("XOAUTH2", responder)
    except imaplib.IMAP4.error as e:
        detail = ""
        raw = state["challenge"]
        if raw:
            try:
                info = json.loads(raw.decode("utf-8", "replace"))
                detail = " | server said: " + ", ".join(
                    f"{k}={v}" for k, v in info.items())
            except Exception:
                detail = " | server said: " + str(raw)[:200]
        raise imaplib.IMAP4.error(f"{e}{detail}")


# ----------------------------------------------------------------------
# Connecting
# ----------------------------------------------------------------------
def is_microsoft_host(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    return any(h == m or h.endswith("." + m) for m in MICROSOFT_HOSTS)


def connect(host: str, port: int, timeout: int = 30):
    """Open an encrypted IMAP connection (SSL, or STARTTLS on port 143)."""
    cls = imaplib.IMAP4 if port == 143 else imaplib.IMAP4_SSL
    try:
        imap = cls(host, port, timeout=timeout)
    except TypeError:
        # imaplib grew the timeout parameter in Python 3.9.
        imap = cls(host, port)
    if port == 143:
        imap.starttls(ssl.create_default_context())
    return imap


def capability_summary(imap) -> str:
    caps = imap.capabilities
    bits = []
    bits.append("XOAUTH2 supported" if "AUTH=XOAUTH2" in caps
                else "no XOAUTH2")
    bits.append("password login disabled" if "LOGINDISABLED" in caps
                else "password login offered")
    return ", ".join(bits)


# ----------------------------------------------------------------------
# --check: figure out where the mailbox lives and how to log in
# ----------------------------------------------------------------------
def probe_tenant(user: str) -> None:
    """Ask Microsoft whether this address belongs to a Microsoft 365 tenant."""
    print(f"-- Is '{user}' a Microsoft 365 account? --")
    url = (OAUTH_AUTHORITY + "/getuserrealm.srf?login="
           + urllib.parse.quote(user) + "&json=1")
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            info = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"   Could not ask Microsoft: {e}\n")
        return

    ns = info.get("NameSpaceType", "Unknown")
    if ns in ("Managed", "Federated"):
        print(f"   YES - the domain is a Microsoft tenant ({ns}).")
        print(f"   Tenant/domain: {info.get('DomainName') or '(not reported)'}"
              "   <- usable as IMAP_OAUTH_TENANT")
        print("   => Use outlook.office365.com with OAuth2 (IMAP_AUTH=oauth2).")
    else:
        print(f"   NO - Microsoft does not know this domain ({ns}).")
        print("   => Your mailbox is on your own provider's server, not in "
              "the Microsoft cloud.")
        print("      outlook.office365.com will never accept it; use your "
              "provider's IMAP host.")
    print()


def candidate_hosts(user: str, host) -> list:
    """Hosts worth probing: the given one, or guesses from the address."""
    if host:
        return [host]

    hosts = ["outlook.office365.com"]
    domain = user.split("@")[-1].strip().lower() if "@" in (user or "") else ""
    domains = []
    if domain:
        domains.append(domain)
        # A mailbox in a sub-domain (e.g. dept.example.edu) is usually served
        # by the parent domain's host, so try that too.
        parent = domain.split(".", 1)[-1]
        if parent.count(".") >= 1 and parent != domain:
            domains.append(parent)
    for d in domains:
        for prefix in ("imap", "mail", "owa", "exchange"):
            hosts.append(f"{prefix}.{d}")

    # Only keep names that actually exist in DNS, so the probe stays quick.
    alive = []
    for h in hosts:
        if h in alive:
            continue
        try:
            socket.getaddrinfo(h, 993, proto=socket.IPPROTO_TCP)
            alive.append(h)
        except Exception:
            pass
    return alive


def probe_host(host: str, port: int) -> None:
    """Connect to one host and report what it offers."""
    print(f"-- {host}:{port} --")
    try:
        imap = connect(host, port, timeout=15)
    except Exception as e:
        print(f"   Not reachable: {e}\n")
        return
    try:
        welcome = imap.welcome
        if isinstance(welcome, bytes):
            welcome = welcome.decode("utf-8", "replace")
        print(f"   Greeting: {welcome[:120]}")
        print(f"   {capability_summary(imap)}")
        if "AUTH=XOAUTH2" in imap.capabilities and is_microsoft_host(host):
            print("   => Microsoft host: run with IMAP_AUTH=oauth2 "
                  "(the default here).")
        elif "LOGINDISABLED" in imap.capabilities:
            print("   => This server refuses password logins entirely.")
        else:
            print("   => Normal password login should work "
                  "(IMAP_AUTH=basic).")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    print()


def run_check(user: str, host, port: int) -> int:
    """Diagnostics only: no mail is downloaded."""
    print("Checking which server your mailbox is on and how to log in.\n")
    if user:
        probe_tenant(user)

    hosts = candidate_hosts(user, host)
    if not hosts:
        print("No host to probe. Pass one via IMAP_HOST.")
        return 1
    for h in hosts:
        probe_host(h, port)

    print("Pick the host above that recognizes your mailbox and run the "
          "backup with IMAP_HOST set to it.")
    return 0


# ----------------------------------------------------------------------
# Login
# ----------------------------------------------------------------------
def login_oauth2(imap, account: str, mailbox: str) -> None:
    """Obtain a token and authenticate. Raises on failure."""
    print(f"Signing in with OAuth2 (client ID {OAUTH_CLIENT_ID}).")
    token = get_access_token(account, OAUTH_TENANT, OAUTH_CLIENT_ID,
                             OAUTH_SCOPE, TOKEN_CACHE)
    if mailbox != account:
        print(f"Opening mailbox {mailbox} on behalf of {account}.")
    imap_xoauth2_login(imap, mailbox, token)


def basic_login_advice(error_text: str, xoauth_available: bool) -> str:
    """Explain a rejected password in terms of what is actually wrong."""
    low = error_text.lower()
    if "basic authentication is disabled" in low or xoauth_available:
        return (
            "Microsoft permanently switched off password (basic) login for "
            "IMAP.\n"
            "App passwords do not exist for work or university accounts, so "
            "no password\n"
            "will ever work here. Use OAuth2 instead:\n"
            "    IMAP_AUTH=oauth2 python3 email-backup.py\n"
            "See the 'Microsoft 365 / Exchange Online' section of the README.")
    return (
        "If your provider uses two-factor authentication, you may need an "
        "app password\n"
        "instead of your normal one, and IMAP may have to be enabled in your "
        "account\n"
        "settings first. Run 'python3 email-backup.py --check' to see which "
        "login\n"
        "methods this server offers.")


# ----------------------------------------------------------------------
# Main flow
# ----------------------------------------------------------------------
def main(argv) -> int:
    global HOST, PORT, USER, PASS, MAILBOX_USER, AUTH_MODE

    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0

    print("=== IMAP Mailbox Backup ===\n")

    if "--check" in argv or os.environ.get("IMAP_CHECK") == "1":
        USER = USER or input("Username / e-mail address: ").strip()
        print()
        return run_check(USER, HOST, PORT)

    if AUTH_MODE not in ("auto", "basic", "oauth2"):
        print(f"Unknown IMAP_AUTH value '{AUTH_MODE}' "
              "(expected auto, basic or oauth2).")
        return 1

    HOST = HOST or input("IMAP server (e.g. imap.gmail.com): ").strip()
    USER = USER or input("Username / e-mail address: ").strip()
    if not (HOST and USER):
        print("Aborting: server and user are required.")
        return 1
    MAILBOX_USER = MAILBOX_USER or USER

    eml_root = os.path.join(OUTDIR, "mails")
    att_dir = os.path.join(OUTDIR, "attachments")
    os.makedirs(eml_root, exist_ok=True)
    os.makedirs(att_dir, exist_ok=True)

    print(f"\nConnecting to {HOST}:{PORT} ...")
    try:
        imap = connect(HOST, PORT)
    except Exception as e:
        print(f"Connection failed: {e}")
        return 1

    xoauth = "AUTH=XOAUTH2" in imap.capabilities
    mode = AUTH_MODE
    if mode == "auto":
        mode = "oauth2" if (xoauth and is_microsoft_host(HOST)) else "basic"
    print(f"Server: {capability_summary(imap)}. Using {mode} authentication.")

    if mode == "oauth2":
        if not xoauth:
            print(f"\nWarning: {HOST} does not advertise XOAUTH2. "
                  "Trying anyway.")
        try:
            login_oauth2(imap, USER, MAILBOX_USER)
        except OAuthError as e:
            print(f"\nCould not get an access token.\n{e}")
            return 1
        except imaplib.IMAP4.error as e:
            print(f"\nLogin failed: {e}")
            print("The token was issued but the mailbox rejected it. Check "
                  "that IMAP_MAILBOX_USER\nnames the right mailbox and that "
                  "the app has the IMAP.AccessAsUser.All permission.")
            return 1
    else:
        if not PASS:
            PASS = getpass.getpass("Password (input stays hidden): ")
        if not PASS:
            print("Aborting: a password is required for basic authentication.")
            return 1
        try:
            imap.login(USER, PASS)
        except imaplib.IMAP4.error as e:
            print(f"\nLogin failed: {e}")
            print(basic_login_advice(str(e), xoauth))
            if xoauth and sys.stdin.isatty():
                answer = input("\nTry signing in with OAuth2 now? [Y/n] ")
                if answer.strip().lower() in ("", "y", "yes", "j", "ja"):
                    try:
                        imap.logout()
                    except Exception:
                        pass
                    try:
                        imap = connect(HOST, PORT)
                        login_oauth2(imap, USER, MAILBOX_USER)
                    except (OAuthError, imaplib.IMAP4.error, OSError) as e2:
                        print(f"\nOAuth2 sign-in failed: {e2}")
                        return 1
                else:
                    return 1
            else:
                return 1

    print("Login OK. Reading folder list ...")
    typ, folders = imap.list()
    if typ != "OK" or not folders:
        print("Could not read folders.")
        imap.logout()
        return 1

    # --- Pass 1: walk folders, count messages -------------------------
    plan = []          # list of (raw_name, display, local_dir, uids)
    grand_total = 0
    for raw_line in folders:
        raw_name, sep = parse_folder(raw_line)
        if not raw_name:
            continue
        display = imap_utf7_decode(raw_name)

        # Open folder read-only (nothing on the server is modified)
        try:
            typ, _ = imap.select(f'"{raw_name}"', readonly=True)
        except Exception:
            continue
        if typ != "OK":
            continue

        typ, data = imap.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            continue
        uids = data[0].split()

        parts = display.split(sep) if sep else [display]
        local_dir = os.path.join(eml_root, *[sanitize(p) for p in parts if p])
        plan.append((raw_name, display, local_dir, uids))
        grand_total += len(uids)

    print(f"Total messages found: {grand_total}\n")
    if grand_total == 0:
        imap.logout()
        return 0

    # --- Pass 2: download with a continuous progress bar --------------
    total_mails = 0
    total_att = 0
    processed = 0
    errors = []        # collected error messages (would otherwise break the bar)

    for raw_name, display, local_dir, uids in plan:
        os.makedirs(local_dir, exist_ok=True)

        # Derive already-saved UIDs from the file names (for resuming)
        done = set()
        for fn in os.listdir(local_dir):
            m = _UID_IN_NAME.search(fn)
            if m:
                done.add(m.group(1))

        try:
            imap.select(f'"{raw_name}"', readonly=True)
        except Exception as e:
            errors.append(f"Folder '{display}' not selectable: {e}")
            processed += len(uids)
            draw_progress(processed, grand_total, display)
            continue

        for uid in uids:
            processed += 1
            uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)

            if uid_s not in done:
                try:
                    typ, msgdata = imap.uid("fetch", uid, "(INTERNALDATE RFC822)")
                    raw = meta = None
                    if typ == "OK" and msgdata:
                        for part in msgdata:
                            if isinstance(part, tuple):
                                meta, raw = part[0], part[1]
                                break
                    if raw is None:
                        errors.append(f"UID {uid_s} ({display}): not downloadable.")
                    else:
                        msg = email.message_from_bytes(raw)
                        epoch, stamp = message_datetime(msg, meta)
                        sender = sender_name(msg)
                        subject = sanitize(decode_mime_words(msg.get("Subject", "")),
                                           fallback="no-subject", maxlen=80)
                        # File name: Date__Sender__Subject__UID.eml
                        eml_name = f"{stamp}__{sender}__{subject}__{uid_s}.eml"
                        eml_path = os.path.join(local_dir, eml_name)
                        with open(eml_path, "wb") as f:
                            f.write(raw)
                        set_file_time(eml_path, epoch)   # real message date
                        total_mails += 1
                        total_att += save_attachments(msg, att_dir, epoch)
                except Exception as e:
                    errors.append(f"UID {uid_s} ({display}): {e}")

            draw_progress(processed, grand_total, display)

    sys.stdout.write("\n")

    try:
        imap.logout()
    except Exception:
        pass

    # Log errors (if any) instead of printing them into the progress bar
    log_path = None
    if errors:
        log_path = os.path.join(OUTDIR, "errors.log")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(errors))
        except Exception:
            log_path = "(could not be written)"

    print("\n=== Done ===")
    print(f"Mails saved (new this run):     {total_mails}")
    print(f"Attachments saved (new):        {total_att}")
    if errors:
        print(f"Skipped due to errors:          {len(errors)}  "
              f"(details: {log_path})")
    print(f"Mails are in:       {os.path.abspath(eml_root)}")
    print(f"Attachments are in: {os.path.abspath(att_dir)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nAborted. Already-downloaded mails are kept; "
              "re-running continues from there.")
        sys.exit(130)
