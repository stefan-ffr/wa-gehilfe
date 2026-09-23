# wa-gehilfe

*Deutsch unten · [English below](#english)*

Selbst gehosteter WhatsApp-Web-Client mit KI-Assistenz: liest deine Chats,
schlägt auf die letzte Nachricht eine Antwort vor (die **du** prüfst und
absendest), transkribiert Sprachnachrichten auf Anforderung, wertet Bilder
aus, baut aus Verläufen eine durchsuchbare Wissensdatenbank und erkennt
vereinbarte Termine. Läuft als PWA.

> **Grundsatz:** Der Dienst sendet **nichts** von selbst. Jeder Vorschlag wird
> nur ins Eingabefeld getippt; abgeschickt wird ausschließlich auf deinen
> Knopfdruck.

## Bestandteile

| Container | Aufgabe |
|---|---|
| **dienst** | Weboberfläche (FastAPI) + KI-Anbindung (Claude / OpenRouter) |
| **koppler** | hält die WhatsApp-Web-Sitzung über [whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js) |

Der Koppler ist nur intern erreichbar; nur der Dienst spricht mit ihm,
geschützt durch einen Token.

## Schnellstart

```bash
cp .env.beispiel .env      # und Werte setzen (mind. MODELL_API_SCHLUESSEL,
                           # ZUGANG_TOKEN, KOPPLER_TOKEN)
docker compose up -d --build
```

Dann `http://127.0.0.1:8099` öffnen, mit `ZUGANG_TOKEN` anmelden, unter
**Mehr** den WhatsApp-QR-Code scannen. Von außen erreichbar machst du es über
einen eigenen Reverse-Proxy oder den optionalen Cloudflare-Tunnel (siehe
`docker-compose.yml`).

Token erzeugen: `openssl rand -base64 32`

## Konfiguration

Alles über Umgebungsvariablen, siehe [`.env.beispiel`](.env.beispiel). Die
wichtigsten:

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `SPRACHE` | `de` | Oberflächensprache (`de`/`en`) |
| `EIGENER_NAME` | `Ich` | in wessen Namen die KI schreibt |
| `MODELL_ANBIETER` / `MODELL` | `claude` / `claude-sonnet-5` | KI-Backend |
| `MODELL_API_SCHLUESSEL` | — | **Pflicht** |
| `ZUGANG_TOKEN` | — | **Pflicht**, Anmeldung der Oberfläche |
| `KOPPLER_TOKEN` | — | **Pflicht**, Dienst ↔ Koppler |
| `WHISPER_MODELL` | `base` | Transkription (`tiny`/`base`/`small`) |

## Lizenz

[AGPL-3.0](LICENSE). Wer wa-gehilfe als Netzwerkdienst betreibt, muss den
Quellcode seiner Änderungen zugänglich machen.

## Haftungsausschluss

Kein offizielles WhatsApp-Produkt und nicht mit WhatsApp/Meta verbunden.
Automatisierung von WhatsApp Web kann deren Nutzungsbedingungen berühren;
Betrieb auf eigene Verantwortung.

---

## English

Self-hosted WhatsApp Web client with an AI assistant: it reads your chats,
drafts a reply to the last message (which **you** review and send),
transcribes voice messages on demand, analyses images, turns your history
into a searchable knowledge base, and detects agreed appointments. Installs
as a PWA.

> **Principle:** the service sends **nothing** on its own. Every suggestion is
> only typed into the input field; it goes out only when you press send.

### Parts

| Container | Role |
|---|---|
| **dienst** | web UI (FastAPI) + AI backend (Claude / OpenRouter) |
| **koppler** | holds the WhatsApp Web session via whatsapp-web.js |

The koppler is reachable internally only; the token protects it.

### Quick start

```bash
cp .env.beispiel .env      # set at least MODELL_API_SCHLUESSEL,
                           # ZUGANG_TOKEN, KOPPLER_TOKEN
docker compose up -d --build
```

Open `http://127.0.0.1:8099`, sign in with `ZUGANG_TOKEN`, then scan the
WhatsApp QR code under **More**. Expose it through your own reverse proxy or
the optional Cloudflare tunnel in `docker-compose.yml`.

Config is entirely via environment variables — see [`.env.beispiel`](.env.beispiel).
Set `SPRACHE=en` for the English UI.

### License

[AGPL-3.0](LICENSE). Running wa-gehilfe as a network service obliges you to
make your modified source available.

### Disclaimer

Not an official WhatsApp product and not affiliated with WhatsApp/Meta.
Automating WhatsApp Web may conflict with their terms; use at your own risk.
