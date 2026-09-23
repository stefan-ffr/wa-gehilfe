# wa-gehilfe

**Your WhatsApp in a browser, with an assistant that writes the first draft.**

[![CI](https://github.com/stefan-ffr/wa-gehilfe/actions/workflows/ci.yml/badge.svg)](https://github.com/stefan-ffr/wa-gehilfe/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

*[Deutsche Fassung →](README.de.md)* · [Jump to the technical reference](#technical-reference)

---

## What this is, in plain words

You run this on a computer of your own. It links to your WhatsApp the same way
WhatsApp Web does — you scan a QR code once with your phone.

After that you get a web page that looks a bit like WhatsApp: your chats, your
messages, voice messages you can play. The difference is that an assistant
reads along. When someone writes to you, it puts a suggested answer into the
text box.

**It never sends that answer.** It types it, you read it, and then you either
change it, delete it, or press send yourself. The assistant is a first draft,
not a voice speaking for you.

It can also do a few things WhatsApp itself will not:

- **Turn voice messages into text** so you can read them in a meeting.
- **Remember things about people.** "Works shifts", "never call before 10" —
  gathered from your history, and you can correct or add to it.
- **Spot appointments.** If you agree on Friday at 6, it offers to put that in
  your calendar.
- **Search properly** — across all chats, including people you have not
  written to in a year.
- **Live on your phone's home screen** like a normal app.

## Before you decide: three honest things

**1. An AI company reads the messages it works on.**
To suggest an answer, the text of that conversation is sent to Anthropic or
OpenRouter — whichever you choose. That is not a side effect, it is how the
assistant works. If there are chats you would not want a company to process,
this tool is not right for them.
*(One exception: turning voice messages into text happens on your own computer
and is never sent anywhere.)*

**2. WhatsApp does not permit this kind of client.**
Accounts do get banned for using unofficial clients. Nobody can tell you the
odds. It also uses up one of your four "linked devices" slots.

**3. You need to be able to follow a terminal, or know someone who can.**
There is no installer and no app store. Setup is a handful of commands you
copy and paste. Once it runs, it keeps running — but the first half hour is
technical, and there is no way around that.

If any of those three is a no, stop here. That is a fine answer.

## What you need

| | |
|---|---|
| **A computer that stays on** | A small home server, a NAS that runs Docker, or a cheap virtual server. Your laptop works for trying it out, but the assistant sleeps when the laptop does. |
| **Docker** | The program that runs the two parts of this. Free. |
| **Your phone** | To scan the QR code once. |
| **An AI account** | At [Anthropic](https://console.anthropic.com/) or [OpenRouter](https://openrouter.ai/). You pay per use — for normal private messaging that is typically a few euros a month, not hundreds. |
| **About 2 GB of memory** | On that computer. |

## Setting it up

**1. Get the files**

```bash
git clone https://github.com/stefan-ffr/wa-gehilfe.git
cd wa-gehilfe
cp .env.beispiel .env
```

**2. Fill in three values**

Open `.env` in any text editor. You need three entries:

```ini
MODELL_API_SCHLUESSEL=sk-...   # the key from your AI account
ZUGANG_TOKEN=...               # your own password for this page
KOPPLER_TOKEN=...              # an internal password, you never type it
```

For the two passwords, do **not** invent something short. Let the computer
make them:

```bash
openssl rand -base64 32     # run it twice, paste one into each
```

`ZUGANG_TOKEN` is what you will type to log in. Keep it somewhere safe —
a password manager, not a sticky note.

**3. Start it**

```bash
docker compose up -d --build
```

The first run takes a few minutes: it is downloading and building. Later
starts are seconds.

**4. Open it and log in**

Go to <http://127.0.0.1:8099> in a browser on that same computer. You will see
a login box. Paste your `ZUGANG_TOKEN`.

**5. Connect WhatsApp**

Go to **More → Connect WhatsApp**. A QR code appears. On your phone open
WhatsApp → **Settings → Linked devices → Link a device**, and scan it.

After a few seconds your chats appear. That is it.

### Reaching it from your phone

Step 4 only works on the computer it runs on. To use it from your phone you
need to make it reachable — a reverse proxy, a VPN into your home network, or
the optional Cloudflare tunnel described in `docker-compose.yml`.

**Do not simply open port 8099 to the internet.** Your messages would be one
guessed password away from a stranger. If that sentence is not familiar
territory, use a VPN (for example [Tailscale](https://tailscale.com/)) — it is
the least dangerous option.

## Questions people ask

**Does it answer on its own while I sleep?**
No. It can *prepare* drafts in the background, but nothing is ever sent
without you pressing send.

**Can the other person tell?**
Not from the message itself. It goes out as a normal message from your number.
What is written is whatever you approved.

**What does it cost?**
Only the AI usage. There is no licence fee — the software is free and open
source. Cost depends on how much you write; for private use it is usually a
few euros a month.

**Where are my messages stored?**
In a file on your own computer (`./daten`). Not in a cloud belonging to this
project — there is no such cloud. Back that folder up; it contains chat
excerpts.

**What if I want to stop?**
In WhatsApp on your phone, under Linked devices, remove the entry. The
connection is dead immediately. `docker compose down` stops the software;
deleting the folder removes everything.

---

# Technical reference

## Architecture

```
   your phone                     your server                     AI provider
  ┌──────────┐   linked device   ┌──────────────┐   excerpts    ┌───────────┐
  │ WhatsApp │◄─────────────────►│   koppler    │               │  Claude / │
  └──────────┘                   │  (session)   │               │ OpenRouter│
                                 └──────┬───────┘               └─────▲─────┘
                                  token │ internal only               │
                                 ┌──────▼───────┐                     │
   you ───── browser / PWA ─────►│    dienst    │─────────────────────┘
                                 │ web UI + AI  │
                                 └──────────────┘
```

Two containers. The **koppler** holds the WhatsApp session via
[whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js); it is never
published to the host and only the **dienst** reaches it, with a token. The
dienst serves the UI and talks to the AI provider. Transcription runs locally
through [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

## Configuration

All environment variables; the full commented list is in
[`.env.beispiel`](.env.beispiel).

| Variable | Default | What it does |
|---|---|---|
| `MODELL_API_SCHLUESSEL` | — | **Required.** API key for the AI provider. |
| `ZUGANG_TOKEN` | — | **Required.** Sign-in token for the web UI (min. 16 chars). |
| `KOPPLER_TOKEN` | — | **Required.** Shared secret between dienst and koppler. |
| `SPRACHE` | `de` | UI language, `de` or `en`. |
| `EIGENER_NAME` | `Ich` | Whose name the AI writes in. |
| `PROFIL` | `standard` | Data partition. Changing it hides everything stored under the old value. |
| `MODELL_ANBIETER` / `MODELL` | `claude` / `claude-sonnet-5` | Provider and model. |
| `WHISPER_MODELL` | `base` | Local transcription: `tiny`, `base`, `small`. |
| `KALENDER_DOMAIN` | `wa-gehilfe.local` | Identifier in `.ics` files. No network access. |
| `BENACHRICHTIGUNG_URL` | — | Webhook for incoming messages. |
| `VERWALTUNG_TOKEN` | — | Optional separate secret for `/verwaltung/*`. Absent = the interface does not exist. |

Variable names are German, like the rest of the code. That is deliberate — but
every one of them is documented here and in `.env.beispiel`.

## Notifications

The koppler can POST to any URL when a message arrives
(`BENACHRICHTIGUNG_URL`, [ntfy](https://github.com/binwiederhier/ntfy)-compatible).
Recommended: a self-hosted ntfy plus the ntfy app — cross-platform, open
source, no app signing of your own.

By default only *"New message from X"* goes out: **no content, no groups**.
Both are opt-in, because a preview leaves your machine.

**Roadmap.** An Android wrapper (GitHub Releases) bundling the ntfy receiver,
so it is one app instead of two — foreground service or UnifiedPush, without
Google FCM. A dedicated iOS app is *not* planned: iOS delivers background push
only through APNs, which requires a paid Apple account; a sideloaded app would
only notify while open, which defeats the purpose.

## Security model

- The web UI requires `ZUGANG_TOKEN`. Five failed attempts lock that address
  out for five minutes.
- The koppler is not published to the host — only the dienst reaches it, and
  only with the token.
- Cloudflare Access is supported but never *required*: a valid Access
  assertion skips the service's own prompt; it works fine without. Forged
  headers get nowhere — the JWT is verified against Cloudflare's public keys.
- `/verwaltung/*` uses a **separate** secret, so an operations tool can check
  health without reading a single message.
- Everything persists in SQLite under `./daten`.

## Development

```bash
python -m pip install -r requirements-test.txt
SPRACHE=de pytest tests -q
SPRACHE=en pytest tests -q   # second run: language is fixed at import time
```

CI runs both languages, checks koppler and extension syntax, and only then
builds images. A `v*` tag triggers a release with versioned images and notes
generated from the commits.

Issues and pull requests welcome. Code and comments are German; you do not
need to be — the tests will tell you if something broke.

## License

[AGPL-3.0](LICENSE). If you run wa-gehilfe as a network service, you must make
your modified source available to its users.
