#!/usr/bin/env python3
"""
email_backup.py - Full backup of an IMAP mailbox.

Downloads ALL e-mails from ALL folders as .eml files (lossless, including
embedded attachments) and additionally collects ALL attachments in a
separate folder.

- Uses only the Python standard library (no installation required).
- Credentials stay local on your machine.
- Resumable: re-running skips messages that were already downloaded.

Usage:
    python3 email_backup.py

Optionally preset via environment variables:
    IMAP_HOST, IMAP_PORT (default 993), IMAP_USER, IMAP_PASS, IMAP_OUTDIR

Copyright (c) 2026 Mark O. Mints <mark@mints.de>
"""

import os
import re
import sys
import time
import base64
import getpass
import imaplib
import email
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


def parse_folder(line) -> str:
    """Extract the (raw) folder name from a LIST response line."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "surrogateescape")
    m = _LIST_RE.match(line)
    if not m:
        return ""
    name = m.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name


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
# Main flow
# ----------------------------------------------------------------------
def main() -> int:
    global HOST, PORT, USER, PASS

    print("=== IMAP Mailbox Backup ===\n")
    HOST = HOST or input("IMAP server (e.g. imap.gmail.com): ").strip()
    USER = USER or input("Username / e-mail address: ").strip()
    if not PASS:
        PASS = getpass.getpass("Password (input stays hidden): ")

    if not (HOST and USER and PASS):
        print("Aborting: server, user and password are required.")
        return 1

    eml_root = os.path.join(OUTDIR, "mails")
    att_dir = os.path.join(OUTDIR, "attachments")
    os.makedirs(eml_root, exist_ok=True)
    os.makedirs(att_dir, exist_ok=True)

    print(f"\nConnecting to {HOST}:{PORT} ...")
    try:
        imap = imaplib.IMAP4_SSL(HOST, PORT)
    except Exception as e:
        print(f"Connection failed: {e}")
        return 1

    try:
        imap.login(USER, PASS)
    except imaplib.IMAP4.error as e:
        print(f"\nLogin failed: {e}")
        print("Common cause: with Gmail/Outlook and 2FA you need an app "
              "password, not your normal password. You may also have to "
              "enable IMAP in your account settings first.")
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
        raw_name = parse_folder(raw_line)
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

        parts = re.split(r"[/.]", display)
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
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted. Already-downloaded mails are kept; "
              "re-running continues from there.")
        sys.exit(130)
