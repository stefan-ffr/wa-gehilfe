# wa-gehilfe

**A self-hosted WhatsApp Web client with an AI drafting assistant.**
It reads your chats, drafts a reply to the last message — and never sends
anything until *you* press send.

[![CI](https://github.com/stefan-ffr/wa-gehilfe/actions/workflows/ci.yml/badge.svg)](https://github.com/stefan-ffr/wa-gehilfe/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

*[Deutsche Fassung →](README.de.md)*

---

## ⚠️ Read this before you install

- **This is an unofficial WhatsApp client.** It drives a real WhatsApp Web
  session via [whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js).
  Numbers get banned for that. It also occupies one of your four linked-device
  slots. Not affiliated with WhatsApp/Meta.
- **Chat content is sent to an AI provider.** Drafting a reply, analysing an
  image or building the knowledge base sends the relevant excerpt to Anthropic
  or OpenRouter — whichever you configure. There is no way around that; it is
  what the assistant *is*. Decide whether that is acceptable for your chats
  before you run it.
- **Voice transcription stays local.** That part runs on your machine via
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and never leaves it.
- **Nothing is sent to a chat automatically.** Every suggestion is only placed
  in the input field. Sending is always a deliberate click.

## What it does

| | |
|---|---|
| ✍️ **Reply drafts** | Suggests an answer to the last message, in the tone *you* use in that chat. Learns from what you actually sent instead. |
| 🎙️ **Voice messages** | Transcribes them on demand — locally, no cloud. |
| 🖼️ **Images** | Shown inline, and described for the AI so a picture is not a blind spot. |
| 🧠 **Knowledge base** | Builds searchable notes per contact from the history. Editable, and you can add your own. |
| 📅 **Appointments** | Detects agreed dates, creates a WhatsApp event or a `.ics` file. |
| 🔎 **Search** | Across chats *and* messages — including contacts too old to appear in the list. |
| 📱 **PWA** | Installs to the home screen, built mobile-first. |
| 🔔 **Notifications** | Optional webhook on incoming messages ([ntfy](https://github.com/binwiederhier/ntfy)-compatible). |
| 🌍 **Two languages** | Complete German and English interface (`SPRACHE=de\|en`). |

## How it works

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

Two containers. The **koppler** holds the WhatsApp session; it is reachable
only from the **dienst**, protected by a token. The dienst serves the web UI
and talks to the AI provider.

## Requirements

- Docker with Compose
- A phone with WhatsApp (to scan the QR code once)
- An API key for [Anthropic](https://console.anthropic.com/) or
  [OpenRouter](https://openrouter.ai/)
- ~2 GB RAM; more if you use a larger transcription model

## Quick start

```bash
git clone https://github.com/stefan-ffr/wa-gehilfe.git
cd wa-gehilfe
cp .env.beispiel .env
```

Set at least these three in `.env` (generate tokens with `openssl rand -base64 32`):

```ini
MODELL_API_SCHLUESSEL=sk-...     # your AI provider key
ZUGANG_TOKEN=...                 # to sign in to the web UI
KOPPLER_TOKEN=...                # between the two containers
```

```bash
docker compose up -d --build
```

Open <http://127.0.0.1:8099>, sign in with `ZUGANG_TOKEN`, then scan the
WhatsApp QR code under **More → Connect WhatsApp**.

To reach it from outside, put your own reverse proxy in front, or use the
optional Cloudflare tunnel in `docker-compose.yml`. **Do not expose port 8099
directly.**

## Configuration

Everything is environment variables — see [`.env.beispiel`](.env.beispiel) for
the full list with comments. The ones that matter:

| Variable | Default | What it does |
|---|---|---|
| `MODELL_API_SCHLUESSEL` | — | **Required.** API key for the AI provider. |
| `ZUGANG_TOKEN` | — | **Required.** Sign-in token for the web UI (min. 16 chars). |
| `KOPPLER_TOKEN` | — | **Required.** Shared secret between dienst and koppler. |
| `SPRACHE` | `de` | UI language, `de` or `en`. |
| `EIGENER_NAME` | `Ich` | Whose name the AI writes in. |
| `MODELL_ANBIETER` / `MODELL` | `claude` / `claude-sonnet-5` | Which provider and model. |
| `WHISPER_MODELL` | `base` | Local transcription: `tiny`, `base`, `small`. |
| `BENACHRICHTIGUNG_URL` | — | Webhook for incoming messages (see below). |
| `VERWALTUNG_TOKEN` | — | Optional separate secret for `/verwaltung/*`. Absent = the interface does not exist. |

> Variable names are German, like the rest of the code. That is deliberate and
> not going to change — but every one of them is documented here and in
> `.env.beispiel`.

## Notifications

The koppler can POST to any URL when a message arrives
(`BENACHRICHTIGUNG_URL`, [ntfy](https://github.com/binwiederhier/ntfy)-compatible).
Recommended: a self-hosted **ntfy** plus the ntfy app — cross-platform, open
source, no app signing of your own.

By default only *"New message from X"* goes out — **no content, no groups**.
Both are opt-in, because a preview leaves your machine.

**Roadmap.** An Android wrapper (GitHub Releases) that bundles the ntfy
receiver, so it is one app instead of two — via a foreground service or
UnifiedPush, without Google FCM. A dedicated iOS app is *not* planned:
iOS delivers background push only through APNs, which needs a paid Apple
account. A sideloaded app would only receive notifications while open, which
defeats the purpose. Installed PWA + the ntfy app is the practical route there.

## Security model

- The web UI requires `ZUGANG_TOKEN`. Five failed attempts lock that address
  out for five minutes.
- The koppler is **not** published to the host — only the dienst reaches it,
  and only with the token.
- Cloudflare Access is supported but never *required*: the service accepts a
  valid Access assertion and skips its own prompt, but works without it. A
  forged header gets you nowhere — the JWT is verified against Cloudflare's
  public keys.
- `/verwaltung/*` uses a **separate** secret. An operations tool can check
  health without reading a single message.
- Everything persists in SQLite under `./daten`. Back that up; it contains
  chat excerpts.

## Development

```bash
python -m pip install -r requirements-test.txt
SPRACHE=de pytest tests -q
SPRACHE=en pytest tests -q   # second run: language is fixed at import time
```

CI runs both languages, checks the koppler and extension syntax, and only then
builds images. A `v*` tag additionally triggers a release with versioned
images and notes generated from the commits.

Issues and pull requests are welcome. The code and its comments are German;
you do not need to be to contribute — the tests will tell you if something
broke.

## License

[AGPL-3.0](LICENSE). If you run wa-gehilfe as a network service, you must make
your modified source available to its users.
