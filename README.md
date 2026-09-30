# email-backup.py

A small, dependency-free script that downloads **all** e-mails from **all**
folders of an IMAP mailbox as `.eml` files and additionally collects **all**
attachments into a single separate folder.

It is meant as a simple, complete backup: pull everything down now, sort it
later if you ever need to.

---

## What it does

- Connects to your mailbox over IMAP (encrypted, port 993 by default).
- Logs in with a normal password, or with **OAuth2 / XOAUTH2** where passwords
  are no longer accepted (Microsoft 365 / Exchange Online). The browser sign-in
  happens once; after that a stored refresh token is reused.
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

If you are not sure which server to name, or a password is being rejected, run
the diagnostic first — it downloads nothing and needs no password:

```bash
python3 email-backup.py --check
```

---

## Finding your IMAP server and password

You need your provider's **IMAP server name**. A few common ones:

| Provider                 | IMAP server            | Notes                                                            |
|--------------------------|------------------------|-----------------------------------------------------------------|
| Gmail                    | `imap.gmail.com`       | Requires an **app password** if 2FA is on; enable IMAP in Gmail settings. |
| Outlook.com / Microsoft 365 | `outlook.office365.com`| Passwords are **no longer accepted**; the script switches to OAuth2 by itself. See [Microsoft 365 / Exchange Online](#microsoft-365--exchange-online). |
| University / company     | ask your IT, or copy   | Usually the same server your existing mail client already uses. |

If you already use Thunderbird, Outlook, or Apple Mail, the IMAP server name is
in that program's account settings.

**App passwords:** If your account uses two-factor authentication, your normal
password will usually be rejected over IMAP. Create a dedicated app password in
your account's security settings and use that instead. This applies to Gmail
and to many smaller providers — but **not** to Microsoft work or university
accounts, which have no app passwords at all and need OAuth2 instead.

---

## Microsoft 365 / Exchange Online

If your mailbox is in the Microsoft cloud, a password over IMAP is rejected no
matter what you type:

```
Login failed: b'Basic authentication is disabled.'
```

Microsoft permanently switched off IMAP password ("basic") login for Exchange
Online. Neither you, nor your administrator, nor Microsoft Support can turn it
back on, and **app passwords do not exist for work or university accounts** —
so creating one is not the fix. The only supported method is OAuth2, which this
script speaks.

### Which server am I actually on?

```bash
python3 email-backup.py --check
```

This asks Microsoft whether your address belongs to a Microsoft 365 tenant,
then connects to the likely servers for your domain and reports what each one
offers:

```
-- Is 'you@example.edu' a Microsoft 365 account? --
   YES - the domain is a Microsoft tenant (Managed).
   Tenant/domain: example.edu   <- usable as IMAP_OAUTH_TENANT

-- outlook.office365.com:993 --
   XOAUTH2 supported, password login disabled
   => Microsoft host: run with IMAP_AUTH=oauth2 (the default here).

-- owa.example.edu:993 --
   no XOAUTH2, password login offered
   => Normal password login should work (IMAP_AUTH=basic).
```

Many universities run **both**: a Microsoft 365 tenant *and* their own Exchange
server. If your institution's own server still accepts a password, that is the
simplest route — point `IMAP_HOST` at it and nothing else changes. Otherwise
use OAuth2.

### Signing in with OAuth2

```bash
IMAP_HOST=outlook.office365.com \
IMAP_USER=you@example.edu \
python3 email-backup.py
```

For `outlook.office365.com` the script picks OAuth2 on its own. Your browser
opens on Microsoft's sign-in page; sign in as usual (2FA included) and approve
read access to your mail. The reply is caught on `127.0.0.1`, the tab says it
is done, and the backup starts.

```
Opening your browser to sign in ...
If nothing happens, open this address yourself:

https://login.microsoftonline.com/...

Waiting for the sign-in to complete ...
```

There is a second flow, `IMAP_OAUTH_FLOW=devicecode`, which instead shows a
short code to type in at <https://microsoft.com/devicelogin> from any device.
It is useful over SSH on a machine with no browser — but many organizations
block it (see [AADSTS53003](#the-browser-says-you-dont-have-access-to-this-aadsts53003)),
which is why the local browser flow is the default.

The refresh token is stored in `~/.config/email-backup/tokens.json` with
owner-only permissions (`-rw-------`), so later runs need no browser at all. It
is deliberately **not** kept inside the backup folder, which tends to get
copied onto external drives. Delete that file to sign out.

### If the app itself is not approved (AADSTS65001 / AADSTS700016)

The script defaults to Mozilla Thunderbird's public application ID
(`9e5f94bc-e8a4-4e73-b8be-63364c29d753`). Many organizations already permit it,
which is why it is the default — but yours may not, and then the sign-in ends
with `AADSTS65001` (consent required) or `AADSTS700016` (app unknown). Two ways
out:

**1. Register your own application**, if your tenant lets ordinary users do so:

1. [Microsoft Entra admin center](https://entra.microsoft.com) →
   **App registrations** → **New registration**. Any name will do; under
   *Supported account types* choose accounts in your own organization only.
2. **Authentication** → **Add a platform** → **Mobile and desktop
   applications**, and set **Allow public client flows** to **Yes**. Without
   this the device-code sign-in fails with `AADSTS7000218`.
3. **API permissions** → **Add a permission** → **APIs my organization uses** →
   search *Office 365 Exchange Online* → **Delegated permissions** → tick
   **IMAP.AccessAsUser.All**.
4. Copy the **Application (client) ID** from the Overview page:

```bash
IMAP_OAUTH_CLIENT_ID=<your-client-id> \
IMAP_OAUTH_TENANT=example.edu \
IMAP_HOST=outlook.office365.com IMAP_USER=you@example.edu \
python3 email-backup.py
```

### The browser says "You don't have access to this" (AADSTS53003)

Your credentials were fine — a **Conditional Access** policy rejected the
sign-in afterwards. The two common reasons:

1. **Device code flow is blocked.** Entra has a policy condition specifically
   for this, and blocking it is a common hardening step. The giveaway is
   `Device state: Unregistered` in the error details, since that flow cannot
   convey any device identity. The script defaults to the local browser flow
   for exactly this reason — if you had switched to
   `IMAP_OAUTH_FLOW=devicecode`, switch back.
2. **A managed or compliant device is required.** If the local browser flow is
   also refused, this is the likely cause and there is nothing the script can
   do about it. Either use your organization's own IMAP server if `--check`
   shows one that still accepts a password, or ask your IT department to allow
   IMAP for your account.

### Asking your IT department

Some of this only an administrator can answer or change. Something along these
lines, with the error code and Request Id from the page you were shown:

> I would like to back up my own mailbox over IMAP using a script that
> authenticates with OAuth2 (SASL XOAUTH2), since basic authentication is
> disabled. My sign-in is rejected with error code `<code>` (Request Id
> `<id>`). Could you tell me:
>
> - whether a Conditional Access policy blocks IMAP, the device code flow, or
>   sign-ins from unmanaged devices for my account;
> - whether IMAP is enabled on my mailbox at all;
> - whether my mailbox is hosted in Exchange Online or on our own Exchange
>   server;
> - and whether I may either register a public-client app in Entra ID with the
>   delegated permission `IMAP.AccessAsUser.All`, or have tenant admin consent
>   granted for client ID `9e5f94bc-e8a4-4e73-b8be-63364c29d753` (Mozilla
>   Thunderbird) with `IMAP.AccessAsUser.All` and `offline_access`.

IMAP can also be switched off per mailbox, independently of everything above.

### Shared mailboxes

Sign in as yourself and name the shared address separately:

```bash
IMAP_USER=you@example.edu \
IMAP_MAILBOX_USER=team@example.edu \
python3 email-backup.py
```

You need Full Access to that mailbox.

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
| `IMAP_PORT`   | IMAP port (`143` switches to STARTTLS) | `993`    |
| `IMAP_OUTDIR` | Output directory                 | `email-backup` |
| `IMAP_AUTH`   | `auto`, `basic` or `oauth2`      | `auto`         |
| `IMAP_MAILBOX_USER` | Mailbox to open, if not your own (shared mailboxes) | = `IMAP_USER` |
| `IMAP_OAUTH_CLIENT_ID` | Entra application (client) ID | Thunderbird's |
| `IMAP_OAUTH_TENANT` | Entra tenant: your e-mail domain or a tenant ID | `common` |
| `IMAP_OAUTH_SCOPE` | OAuth scopes to request       | IMAP + `offline_access` |
| `IMAP_OAUTH_FLOW` | `authcode` (local browser) or `devicecode` | `authcode` |
| `IMAP_TOKEN_CACHE` | Where the refresh token is kept | `~/.config/email-backup/tokens.json` |
| `IMAP_CHECK`  | `1` runs the diagnostic (same as `--check`) | (off) |

`auto` means: use OAuth2 when the server is a Microsoft host that offers
XOAUTH2, and a normal password login everywhere else.

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
  else. With OAuth2 the script never sees your password at all — you type it
  on Microsoft's own sign-in page, and the script only receives a token.
- **The OAuth token is a credential.** It is written to
  `~/.config/email-backup/tokens.json` (mode `-rw-------`, outside the backup
  folder) and stays valid for weeks. Delete that file to sign out.
- **Re-running is safe:** because downloads are resumable and the server is
  never modified, you can run the script as often as you like.

---

## Troubleshooting

**Start here:** `python3 email-backup.py --check` tells you which server your
mailbox is on and which login methods it accepts. Most of the entries below
follow directly from its output.

**`Basic authentication is disabled.`** – Your mailbox is in Microsoft 365 and
no password will ever work. Do not create an app password; work and university
accounts do not have them. See
[Microsoft 365 / Exchange Online](#microsoft-365--exchange-online).

**"Login failed" on a non-Microsoft server** – Usually two-factor
authentication. Create an app password in your account's security settings and
use that instead of your normal password. For Gmail, also make sure IMAP is
enabled in the Gmail settings.

**`AADSTS65001` / `AADSTS700016` during the OAuth sign-in** – Your organization
has not approved the application the script uses. Register your own app or ask
your IT department; both routes are spelled out
[above](#if-the-app-itself-is-not-approved-aadsts65001--aadsts700016).

**`AADSTS53003` / "You don't have access to this"** – A Conditional Access
policy blocked the sign-in. See
[the section above](#the-browser-says-you-dont-have-access-to-this-aadsts53003).

**`AADSTS50059`** – Microsoft cannot tell which organization to sign you in to.
Set `IMAP_OAUTH_TENANT` to your e-mail domain, e.g.
`IMAP_OAUTH_TENANT=example.edu`.

**The browser sign-in is asked for on every run** – The token cache could not be
written. Check that `~/.config/email-backup/` is writable, or point
`IMAP_TOKEN_CACHE` somewhere else.

**"Connection failed"** – Check the server name and that you are online. Some
providers use port `143` with STARTTLS instead of `993`. Set `IMAP_PORT=143`
and the script negotiates STARTTLS on that port; the connection stays
encrypted either way.

**It seems slow** – Large mailboxes simply take a while, because every message
is fetched individually. The progress bar shows how far along it is. You can
stop with `Ctrl+C` and resume later.

**Some messages were skipped** – Check `errors.log` in the output folder for the
list. Re-running will retry anything that is not yet saved.

---

## License
`email-backup.py` is released under GPLv3 license.
