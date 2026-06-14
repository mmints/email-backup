# email-backup.py

A small, dependency-free script that downloads **all** e-mails from **all**
folders of an IMAP mailbox as `.eml` files and additionally collects **all**
attachments into a single separate folder.

It is meant as a simple, complete backup: pull everything down now, sort it
later if you ever need to.

---

## What it does

- Connects to your mailbox over IMAP (encrypted, port 993 by default).
- Walks through every folder and downloads every message as a `.eml` file
  (lossless full copy, including the attachments embedded inside).
- Mirrors your server-side folder structure on disk.
- Names each file `Date__Sender__Subject__UID.eml` so files sort
  chronologically and are easy to recognize.
- Sets each file's timestamp to the **real** date the mail was sent/received,
  not the moment you ran the backup. This applies to both the `.eml` files and
  the extracted attachments.
- Copies every attachment into one flat `attachments/` folder.
- Shows a single continuous progress bar while running.
- Is **resumable**: if it is interrupted, running it again continues where it
  left off and skips what is already downloaded.
- Only ever **reads** from the server. Nothing on your mailbox is changed,
  moved, or deleted.

---

## Requirements / installation

**Nothing to install.** The script uses only the Python standard library.

You only need Python 3.7 or newer, which is already present on most Linux and
macOS systems. To check:

```bash
python3 --version
```

If that prints something like `Python 3.10.12`, you are ready. On Windows, use
`python` instead of `python3` in the commands below.

---

## Quick start

1. Put `email-backup.py` in any folder.
2. Open a terminal in that folder.
3. Run:

```bash
python3 email-backup.py
```

The script will ask for three things:

```
IMAP server (e.g. imap.gmail.com): imap.example.com
Username / e-mail address:         you@example.com
Password (input stays hidden):
```

The password is typed invisibly (nothing appears as you type), is never shown,
and is never written to disk. When it finishes, you will have an
`email-backup/` folder next to the script.

---

## Finding your IMAP server and password

You need your provider's **IMAP server name**. A few common ones:

| Provider                 | IMAP server            | Notes                                                            |
|--------------------------|------------------------|-----------------------------------------------------------------|
| Gmail                    | `imap.gmail.com`       | Requires an **app password** if 2FA is on; enable IMAP in Gmail settings. |
| Outlook.com / Office 365 | `outlook.office365.com`| Many organizations block basic IMAP login and require OAuth (see Troubleshooting). |
| University / company     | ask your IT, or copy   | Usually the same server your existing mail client already uses. |

If you already use Thunderbird, Outlook, or Apple Mail, the IMAP server name is
in that program's account settings.

**App passwords:** If your account uses two-factor authentication, your normal
password will usually be rejected over IMAP. Create a dedicated app password in
your account's security settings and use that instead.

---

## Usage examples

### 1. Standard interactive run

```bash
python3 email-backup.py
```

Prompts for server, user, and password; writes to `email-backup/`.

### 2. Preset everything via environment variables (no prompts)

Useful for scripting or re-running without retyping. On Linux/macOS:

```bash
export IMAP_HOST=imap.gmail.com
export IMAP_USER=you@example.com
export IMAP_PASS='your-app-password'
python3 email-backup.py
```

Any variable you do **not** set will simply be asked for interactively. For
example, you can preset the host and user but still be prompted for the
password (which keeps it out of your shell history):

```bash
IMAP_HOST=imap.gmail.com IMAP_USER=you@example.com python3 email-backup.py
```

### 3. Choose a different output folder

```bash
IMAP_OUTDIR=~/backups/mail-2026 python3 email-backup.py
```

### 4. Non-standard port

```bash
IMAP_PORT=143 python3 email-backup.py
```

### 5. Resume after an interruption

Just run the same command again. Press `Ctrl+C` any time to stop; nothing is
lost, and the next run picks up where it stopped:

```bash
python3 email-backup.py
```

### Available environment variables

| Variable      | Meaning                          | Default        |
|---------------|----------------------------------|----------------|
| `IMAP_HOST`   | IMAP server name                 | (prompted)     |
| `IMAP_USER`   | Username / e-mail address        | (prompted)     |
| `IMAP_PASS`   | Password or app password         | (prompted)     |
| `IMAP_PORT`   | IMAP port                        | `993`          |
| `IMAP_OUTDIR` | Output directory                 | `email-backup` |

---

## Output directory structure

After a run, you get a single output folder (default `email-backup/`) with two
main parts plus an optional error log:

```
email-backup/
├── mails/
│   ├── INBOX/
│   │   ├── 2026-06-12_14-30-00__Alice Müller__Re_ meeting notes__4711.eml
│   │   └── 2026-06-13_09-05-12__newsletter@shop.com__Your receipt__4712.eml
│   ├── INBOX/
│   │   └── Important/
│   │       └── 2026-05-02_18-22-41__Bob__Contract draft__3980.eml
│   ├── Sent/
│   │   └── 2026-06-10_11-00-00__you@example.com__Project update__5521.eml
│   └── Drafts/
│       └── ...
├── attachments/
│   ├── Contract draft.pdf
│   ├── meeting notes.docx
│   ├── invoice.pdf
│   └── invoice_1.pdf
└── errors.log        (only created if something was skipped)
```

### `mails/`

A faithful copy of your mailbox, with the server-side folder structure
mirrored as subfolders. Nested folders (e.g. a `Important` folder inside
`INBOX`) become nested directories. Each message is one `.eml` file.

`.eml` is a standard format: double-click any file to open it in Thunderbird,
Outlook, Apple Mail, or most other mail clients, with the original formatting
and attachments intact.

### `attachments/`

Every attachment from every e-mail, regardless of which mail it came from,
collected flat in one folder. Original file names are kept. If two attachments
share a name, the later one gets a numeric suffix (for example `invoice.pdf`
and `invoice_1.pdf`) so nothing is overwritten.

### `errors.log`

Created only if one or more messages could not be downloaded. It lists the
affected message UIDs and folders so you can see what was skipped. If every
message downloaded cleanly, this file is not created.

---

## Understanding the file names

Each `.eml` file is named:

```
Date__Sender__Subject__UID.eml
```

Example:

```
2026-06-12_14-30-00__Alice Müller__Re_ meeting notes__4711.eml
```

- **Date** – `YYYY-MM-DD_HH-MM-SS`, taken from the message's `Date` header (when
  it was sent). If that is missing or unreadable, the server's receive time
  (IMAP `INTERNALDATE`) is used instead. This prefix makes files sort in
  chronological order. If no date can be determined at all, it reads
  `date-unknown`.
- **Sender** – the display name if available, otherwise the e-mail address.
- **Subject** – the message subject (`no-subject` if empty).
- **UID** – the mailbox's unique message ID. It is kept at the end for two
  reasons: it guarantees two otherwise-identical names never collide, and the
  resume feature uses it to recognize which messages are already saved.

Characters that are not allowed in file names (such as `/`, `:`, `?`) are
replaced with `_`, and very long senders or subjects are shortened.

---

## Timestamps

The file modification time of every `.eml` and every attachment is set to the
**actual** time the message was sent/received, not the time you ran the backup.
So sorting your backup by "Date modified" in any file manager gives you the
true chronological order of your mail.

The exact instant is stored, and your file manager displays it in your local
time zone. Note: on Linux and macOS only the modification time can be set from
the standard library, not the separate Windows "Created" date. In practice this
is fine, since file managers and search tools sort by modification time anyway.

---

## Safety notes

- **Read-only:** the script opens every folder in read-only mode. It never
  writes to, deletes, or rearranges anything on the server.
- **Credentials stay local:** your password is only used to log in to your own
  mail server from your own machine. It is not stored and not sent anywhere
  else.
- **Re-running is safe:** because downloads are resumable and the server is
  never modified, you can run the script as often as you like.

---

## Troubleshooting

**"Login failed"** – The most common cause is two-factor authentication. Create
an app password in your account's security settings and use that instead of
your normal password. For Gmail, also make sure IMAP is enabled in the Gmail
settings.

**Outlook / Office 365 / university account rejects the password** – Many
organizations disable basic IMAP login and require modern OAuth authentication,
which this script does not perform. Workaround: set the account up once in
Thunderbird (it handles the OAuth browser login for you), let it sync all mail
locally, and your backup then lives in Thunderbird's local profile. The
attachments can be extracted from there separately if needed.

**"Connection failed"** – Check the server name and that you are online. Some
providers use port `143` with STARTTLS instead of `993`; this script connects
over SSL on `993` by default. If your provider only offers `143`, set
`IMAP_PORT=143`.

**It seems slow** – Large mailboxes simply take a while, because every message
is fetched individually. The progress bar shows how far along it is. You can
stop with `Ctrl+C` and resume later.

**Some messages were skipped** – Check `errors.log` in the output folder for the
list. Re-running will retry anything that is not yet saved.

---

## License
`email-backup.py` is released under GPLv3 license.
