# wa-gehilfe

**Selbst gehosteter WhatsApp-Web-Client mit KI-Assistenz.**
Er liest deine Chats, entwirft eine Antwort auf die letzte Nachricht — und
schickt nichts los, bevor *du* auf Senden drückst.

[![CI](https://github.com/stefan-ffr/wa-gehilfe/actions/workflows/ci.yml/badge.svg)](https://github.com/stefan-ffr/wa-gehilfe/actions/workflows/ci.yml)
[![Lizenz: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

*[English version →](README.md)*

---

## ⚠️ Vor dem Installieren lesen

- **Das ist ein inoffizieller WhatsApp-Client.** Er steuert eine echte
  WhatsApp-Web-Sitzung über
  [whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js). Dafür
  werden Nummern gesperrt. Außerdem belegt er einen deiner vier Plätze für
  verknüpfte Geräte. Nicht mit WhatsApp/Meta verbunden.
- **Chat-Inhalte gehen an einen KI-Anbieter.** Ein Antwortentwurf, eine
  Bildbeschreibung oder eine Wissensrunde schickt den betreffenden Ausschnitt
  an Anthropic oder OpenRouter — je nachdem, was du einstellst. Daran führt
  kein Weg vorbei; genau das *ist* der Assistent. Entscheide vorher, ob das
  für deine Chats in Ordnung ist.
- **Die Transkription bleibt lokal.** Sie läuft über
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) auf deiner
  Maschine und verlässt sie nicht.
- **Nichts geht automatisch in einen Chat.** Jeder Vorschlag landet nur im
  Eingabefeld. Senden ist immer ein bewusster Klick.

## Was er kann

| | |
|---|---|
| ✍️ **Antwortentwürfe** | Schlägt eine Antwort auf die letzte Nachricht vor — in dem Ton, den *du* in diesem Chat anschlägst. Lernt daraus, was du stattdessen geschrieben hast. |
| 🎙️ **Sprachnachrichten** | Werden auf Anforderung abgeschrieben — lokal, ohne Cloud. |
| 🖼️ **Bilder** | Werden im Verlauf angezeigt und für die KI beschrieben, damit ein Bild kein blinder Fleck bleibt. |
| 🧠 **Wissensdatenbank** | Baut aus dem Verlauf durchsuchbare Notizen je Kontakt. Streichbar, und du kannst eigene ergänzen. |
| 📅 **Termine** | Erkennt vereinbarte Termine, legt ein WhatsApp-Ereignis oder eine `.ics`-Datei an. |
| 🔎 **Suche** | Über Chats *und* Nachrichten — auch über Kontakte, die für die Liste zu alt sind. |
| 📱 **PWA** | Lässt sich auf den Startbildschirm legen, von Anfang an fürs Telefon gebaut. |
| 🔔 **Benachrichtigungen** | Optionaler Webhook bei eingehenden Nachrichten ([ntfy](https://github.com/binwiederhier/ntfy)-kompatibel). |
| 🌍 **Zwei Sprachen** | Vollständig deutsche und englische Oberfläche (`SPRACHE=de\|en`). |

## Wie es zusammenhängt

```
  dein Telefon                    dein Server                    KI-Anbieter
  ┌──────────┐  verknüpftes Gerät ┌──────────────┐  Ausschnitte ┌───────────┐
  │ WhatsApp │◄──────────────────►│   koppler    │              │  Claude / │
  └──────────┘                    │  (Sitzung)   │              │ OpenRouter│
                                  └──────┬───────┘              └─────▲─────┘
                                   Token │ nur intern                 │
                                  ┌──────▼───────┐                    │
   du ────── Browser / PWA ──────►│    dienst    │────────────────────┘
                                  │ Oberfläche   │
                                  │ + KI         │
                                  └──────────────┘
```

Zwei Container. Der **koppler** hält die WhatsApp-Sitzung; erreichbar ist er
nur vom **dienst**, geschützt durch einen Token. Der dienst liefert die
Oberfläche und spricht mit dem KI-Anbieter.

## Voraussetzungen

- Docker mit Compose
- Ein Telefon mit WhatsApp (einmal QR-Code scannen)
- Ein API-Schlüssel für [Anthropic](https://console.anthropic.com/) oder
  [OpenRouter](https://openrouter.ai/)
- ~2 GB RAM; mehr, wenn du ein größeres Transkriptionsmodell nimmst

## Schnellstart

```bash
git clone https://github.com/stefan-ffr/wa-gehilfe.git
cd wa-gehilfe
cp .env.beispiel .env
```

Mindestens diese drei in `.env` setzen (Token erzeugen mit `openssl rand -base64 32`):

```ini
MODELL_API_SCHLUESSEL=sk-...     # Schlüssel deines KI-Anbieters
ZUGANG_TOKEN=...                 # Anmeldung an der Oberfläche
KOPPLER_TOKEN=...                # zwischen den beiden Containern
```

```bash
docker compose up -d --build
```

<http://127.0.0.1:8099> öffnen, mit `ZUGANG_TOKEN` anmelden, dann unter
**Mehr → WhatsApp verbinden** den QR-Code scannen.

Von außen erreichbar machst du es über einen eigenen Reverse-Proxy oder den
optionalen Cloudflare-Tunnel in `docker-compose.yml`. **Port 8099 gehört nicht
offen ins Netz.**

## Konfiguration

Alles über Umgebungsvariablen — die vollständige, kommentierte Liste steht in
[`.env.beispiel`](.env.beispiel). Die wichtigen:

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `MODELL_API_SCHLUESSEL` | — | **Pflicht.** API-Schlüssel des KI-Anbieters. |
| `ZUGANG_TOKEN` | — | **Pflicht.** Anmeldung an der Oberfläche (mind. 16 Zeichen). |
| `KOPPLER_TOKEN` | — | **Pflicht.** Gemeinsames Geheimnis von dienst und koppler. |
| `SPRACHE` | `de` | Oberflächensprache, `de` oder `en`. |
| `EIGENER_NAME` | `Ich` | In wessen Namen die KI schreibt. |
| `MODELL_ANBIETER` / `MODELL` | `claude` / `claude-sonnet-5` | Anbieter und Modell. |
| `WHISPER_MODELL` | `base` | Lokale Transkription: `tiny`, `base`, `small`. |
| `BENACHRICHTIGUNG_URL` | — | Webhook bei eingehenden Nachrichten (siehe unten). |
| `VERWALTUNG_TOKEN` | — | Optionales getrenntes Geheimnis für `/verwaltung/*`. Fehlt es, gibt es die Schnittstelle nicht. |

## Benachrichtigungen

Der Koppler kann bei einer eingehenden Nachricht ein POST an eine beliebige
URL schicken (`BENACHRICHTIGUNG_URL`,
[ntfy](https://github.com/binwiederhier/ntfy)-kompatibel). Empfohlen: ein
selbst gehostetes **ntfy** plus die ntfy-App — plattformübergreifend,
quelloffen, ohne eigenes App-Signing.

Per Vorgabe geht nur *„Neue Nachricht von X"* hinaus — **kein Inhalt, keine
Gruppen**. Beides ist bewusst opt-in, weil eine Vorschau die eigene Maschine
verlässt.

**Roadmap.** Ein Android-Wrapper (über GitHub Releases), der den ntfy-Empfang
gleich mitbringt — eine App statt zwei, über einen Vordergrunddienst oder
UnifiedPush, ohne Google FCM. Eine eigene iOS-App ist *nicht* geplant: iOS
liefert Hintergrund-Push ausschließlich über APNs, und das braucht ein
kostenpflichtiges Apple-Konto. Eine per Sideloadly geladene App bekäme
Benachrichtigungen nur, solange sie offen ist — also genau dann nicht, wenn
man sie braucht. Dort bleibt: installierte PWA plus ntfy-App.

## Sicherheit

- Die Oberfläche verlangt `ZUGANG_TOKEN`. Fünf Fehlversuche sperren die
  Absenderadresse für fünf Minuten.
- Der Koppler wird **nicht** auf den Host veröffentlicht — nur der dienst
  erreicht ihn, und nur mit dem Token.
- Cloudflare Access wird unterstützt, aber nie *verlangt*: Der Dienst
  akzeptiert einen gültigen Access-Nachweis und spart sich dann die eigene
  Maske, läuft aber auch ohne. Ein gefälschtes Kopffeld hilft nicht — das JWT
  wird gegen Cloudflares öffentliche Schlüssel geprüft.
- `/verwaltung/*` hat ein **getrenntes** Geheimnis. Ein Betriebswerkzeug kann
  nach dem Zustand sehen, ohne eine einzige Nachricht zu lesen.
- Alles liegt in SQLite unter `./daten`. Das gehört ins Backup — und es
  enthält Chat-Ausschnitte.

## Mitmachen

```bash
python -m pip install -r requirements-test.txt
SPRACHE=de pytest tests -q
SPRACHE=en pytest tests -q   # zweiter Lauf: die Sprache steht beim Import fest
```

Die CI fährt beide Sprachen, prüft die Syntax von Koppler und Erweiterung und
baut die Abbilder erst danach. Ein Tag `v*` löst zusätzlich einen Release aus
(versionierte Abbilder plus Notiz aus den Commits).

Issues und Pull Requests sind willkommen.

## Lizenz

[AGPL-3.0](LICENSE). Wer wa-gehilfe als Netzwerkdienst betreibt, muss den
Quellcode seiner Änderungen dessen Nutzern zugänglich machen.
