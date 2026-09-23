# wa-gehilfe

**Dein WhatsApp im Browser, mit einem Assistenten, der den ersten Entwurf schreibt.**

[![CI](https://github.com/stefan-ffr/wa-gehilfe/actions/workflows/ci.yml/badge.svg)](https://github.com/stefan-ffr/wa-gehilfe/actions/workflows/ci.yml)
[![Lizenz: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

*[English version →](README.md)* · [Zur technischen Referenz](#technische-referenz)

---

## Was das ist, in einfachen Worten

Du betreibst das auf einem eigenen Rechner. Es verbindet sich mit deinem
WhatsApp genau so wie WhatsApp Web — du scannst einmal einen QR-Code mit dem
Telefon.

Danach hast du eine Webseite, die ein bisschen aussieht wie WhatsApp: deine
Chats, deine Nachrichten, Sprachnachrichten zum Abspielen. Der Unterschied
ist, dass ein Assistent mitliest. Schreibt dir jemand, legt er dir eine
mögliche Antwort ins Textfeld.

**Abgeschickt wird sie nie.** Sie wird nur hineingetippt. Du liest sie, änderst
sie, löschst sie — oder drückst selbst auf Senden. Der Assistent ist ein
erster Entwurf, keine Stimme, die für dich spricht.

Ein paar Dinge kann er, die WhatsApp selbst nicht anbietet:

- **Sprachnachrichten in Text verwandeln**, damit du sie in einer Besprechung
  lesen kannst.
- **Sich Dinge über Menschen merken.** „Arbeitet Schicht", „nie vor 10 Uhr
  anrufen" — aus dem Verlauf zusammengetragen, und du kannst es korrigieren
  oder ergänzen.
- **Termine erkennen.** Verabredet ihr euch auf Freitag um 18 Uhr, bietet er
  an, das in den Kalender zu legen.
- **Richtig suchen** — über alle Chats, auch über Leute, denen du seit einem
  Jahr nicht geschrieben hast.
- **Auf dem Startbildschirm liegen** wie eine normale App.

## Bevor du dich entscheidest: drei ehrliche Punkte

**1. Ein KI-Unternehmen liest die Nachrichten mit, an denen es arbeitet.**
Um eine Antwort vorzuschlagen, geht der Text des betreffenden Gesprächs an
Anthropic oder OpenRouter — je nachdem, was du einstellst. Das ist kein
Nebeneffekt, sondern die Funktionsweise. Gibt es Chats, die kein Unternehmen
verarbeiten soll, ist dieses Werkzeug für die nicht das richtige.
*(Eine Ausnahme: Sprachnachrichten in Text zu verwandeln passiert auf deinem
eigenen Rechner und wird nirgendwohin geschickt.)*

**2. WhatsApp erlaubt solche Clients nicht.**
Konten werden dafür gesperrt. Wie wahrscheinlich das ist, kann dir niemand
sagen. Außerdem belegt es einen deiner vier Plätze für „verknüpfte Geräte".

**3. Du musst einer Anleitung im Terminal folgen können — oder jemanden
kennen, der das kann.**
Es gibt kein Installationsprogramm und keinen App Store. Die Einrichtung sind
eine Handvoll Befehle zum Kopieren. Läuft es einmal, läuft es weiter — aber
die erste halbe Stunde ist technisch, und daran führt nichts vorbei.

Ist einer der drei Punkte ein Nein, hör hier auf. Das ist eine gute Antwort.

## Was du brauchst

| | |
|---|---|
| **Einen Rechner, der anbleibt** | Ein kleiner Heimserver, ein NAS mit Docker oder ein günstiger virtueller Server. Zum Ausprobieren tut es der Laptop — nur schläft der Assistent mit ihm ein. |
| **Docker** | Das Programm, das die zwei Teile ausführt. Kostenlos. |
| **Dein Telefon** | Einmal für den QR-Code. |
| **Ein KI-Konto** | Bei [Anthropic](https://console.anthropic.com/) oder [OpenRouter](https://openrouter.ai/). Bezahlt wird nach Nutzung — für normales privates Schreiben sind das meist ein paar Euro im Monat, keine Hunderte. |
| **Rund 2 GB Arbeitsspeicher** | Auf diesem Rechner. |

## Einrichten

**1. Dateien holen**

```bash
git clone https://github.com/stefan-ffr/wa-gehilfe.git
cd wa-gehilfe
cp .env.beispiel .env
```

**2. Drei Werte eintragen**

Öffne `.env` in einem beliebigen Texteditor. Drei Einträge brauchst du:

```ini
MODELL_API_SCHLUESSEL=sk-...   # der Schlüssel aus deinem KI-Konto
ZUGANG_TOKEN=...               # dein eigenes Passwort für diese Seite
KOPPLER_TOKEN=...              # ein internes Passwort, das du nie tippst
```

Für die beiden Passwörter denk dir nichts Kurzes aus. Lass sie den Rechner
erzeugen:

```bash
openssl rand -base64 32     # zweimal ausführen, je eins einsetzen
```

`ZUGANG_TOKEN` ist das, was du zum Anmelden eintippst. Bewahr es gut auf —
in einem Passwortspeicher, nicht auf einem Zettel.

**3. Starten**

```bash
docker compose up -d --build
```

Der erste Start dauert ein paar Minuten, da wird geladen und gebaut. Spätere
Starts sind eine Sache von Sekunden.

**4. Öffnen und anmelden**

Ruf <http://127.0.0.1:8099> in einem Browser **auf demselben Rechner** auf. Du
siehst ein Anmeldefeld. Dort das `ZUGANG_TOKEN` einsetzen.

**5. WhatsApp verbinden**

Geh auf **Mehr → WhatsApp verbinden**. Es erscheint ein QR-Code. Am Telefon in
WhatsApp auf **Einstellungen → Verknüpfte Geräte → Gerät verknüpfen** und den
Code scannen.

Nach ein paar Sekunden sind deine Chats da. Fertig.

### Vom Telefon aus erreichen

Schritt 4 funktioniert nur auf dem Rechner, auf dem es läuft. Damit du es vom
Telefon aus nutzen kannst, muss es erreichbar werden — über einen
Reverse-Proxy, ein VPN ins Heimnetz oder den optionalen Cloudflare-Tunnel, der
in `docker-compose.yml` beschrieben ist.

**Gib Port 8099 nicht einfach ins Internet frei.** Deine Nachrichten wären dann
ein geratenes Passwort von einem Fremden entfernt. Wenn dieser Satz kein
vertrautes Gelände ist, nimm ein VPN (zum Beispiel
[Tailscale](https://tailscale.com/)) — das ist die ungefährlichste Variante.

## Häufige Fragen

**Antwortet es nachts von allein?**
Nein. Es kann Entwürfe im Hintergrund *vorbereiten*, aber abgeschickt wird nie
etwas ohne deinen Druck auf Senden.

**Merkt es die Gegenseite?**
An der Nachricht selbst nicht. Sie geht als ganz normale Nachricht von deiner
Nummer hinaus. Und was drinsteht, hast du freigegeben.

**Was kostet das?**
Nur die KI-Nutzung. Es gibt keine Lizenzgebühr — die Software ist frei und
quelloffen. Die Kosten hängen davon ab, wie viel du schreibst; privat sind es
meist ein paar Euro im Monat.

**Wo liegen meine Nachrichten?**
In einer Datei auf deinem eigenen Rechner (`./daten`). Nicht in einer Cloud
dieses Projekts — die gibt es nicht. Sichere diesen Ordner; er enthält
Chat-Ausschnitte.

**Und wenn ich aufhören will?**
In WhatsApp am Telefon unter „Verknüpfte Geräte" den Eintrag entfernen. Damit
ist die Verbindung sofort tot. `docker compose down` hält die Software an, und
den Ordner zu löschen entfernt alles.

---

# Technische Referenz

## Aufbau

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

Zwei Container. Der **koppler** hält die WhatsApp-Sitzung über
[whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js); er wird nie
auf den Host veröffentlicht, nur der **dienst** erreicht ihn, mit Token. Der
dienst liefert die Oberfläche und spricht mit dem KI-Anbieter. Die
Transkription läuft lokal über
[faster-whisper](https://github.com/SYSTRAN/faster-whisper).

## Konfiguration

Alles über Umgebungsvariablen; die vollständige, kommentierte Liste steht in
[`.env.beispiel`](.env.beispiel).

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `MODELL_API_SCHLUESSEL` | — | **Pflicht.** API-Schlüssel des KI-Anbieters. |
| `ZUGANG_TOKEN` | — | **Pflicht.** Anmeldung an der Oberfläche (mind. 16 Zeichen). |
| `KOPPLER_TOKEN` | — | **Pflicht.** Gemeinsames Geheimnis von dienst und koppler. |
| `SPRACHE` | `de` | Oberflächensprache, `de` oder `en`. |
| `EIGENER_NAME` | `Ich` | In wessen Namen die KI schreibt. |
| `PROFIL` | `standard` | Datenpartition. Änderst du sie, ist alles unter dem alten Wert unsichtbar. |
| `MODELL_ANBIETER` / `MODELL` | `claude` / `claude-sonnet-5` | Anbieter und Modell. |
| `WHISPER_MODELL` | `base` | Lokale Transkription: `tiny`, `base`, `small`. |
| `KALENDER_DOMAIN` | `wa-gehilfe.local` | Bezeichner in `.ics`-Dateien. Kein Netzzugriff. |
| `BENACHRICHTIGUNG_URL` | — | Webhook bei eingehenden Nachrichten. |
| `VERWALTUNG_TOKEN` | — | Optionales getrenntes Geheimnis für `/verwaltung/*`. Fehlt es, gibt es die Schnittstelle nicht. |

## Benachrichtigungen

Der Koppler kann bei einer eingehenden Nachricht ein POST an eine beliebige
URL schicken (`BENACHRICHTIGUNG_URL`,
[ntfy](https://github.com/binwiederhier/ntfy)-kompatibel). Empfohlen: ein
selbst gehostetes ntfy plus die ntfy-App — plattformübergreifend, quelloffen,
ohne eigenes App-Signing.

Per Vorgabe geht nur *„Neue Nachricht von X"* hinaus: **kein Inhalt, keine
Gruppen**. Beides ist bewusst opt-in, weil eine Vorschau die eigene Maschine
verlässt.

**Roadmap.** Ein Android-Wrapper (über GitHub Releases), der den ntfy-Empfang
mitbringt — eine App statt zwei, über einen Vordergrunddienst oder
UnifiedPush, ohne Google FCM. Eine eigene iOS-App ist *nicht* geplant: iOS
liefert Hintergrund-Push ausschließlich über APNs, was ein kostenpflichtiges
Apple-Konto braucht; eine sideloadete App würde nur benachrichtigen, solange
sie offen ist.

## Sicherheit

- Die Oberfläche verlangt `ZUGANG_TOKEN`. Fünf Fehlversuche sperren die
  Absenderadresse für fünf Minuten.
- Der Koppler wird nicht auf den Host veröffentlicht — nur der dienst erreicht
  ihn, und nur mit dem Token.
- Cloudflare Access wird unterstützt, aber nie *verlangt*: Ein gültiger
  Nachweis spart die eigene Maske, ohne läuft es genauso. Gefälschte Kopffelder
  helfen nicht — das JWT wird gegen Cloudflares öffentliche Schlüssel geprüft.
- `/verwaltung/*` hat ein **getrenntes** Geheimnis, damit ein Betriebswerkzeug
  nach dem Zustand sehen kann, ohne eine einzige Nachricht zu lesen.
- Alles liegt in SQLite unter `./daten`.

## Mitmachen

```bash
python -m pip install -r requirements-test.txt
SPRACHE=de pytest tests -q
SPRACHE=en pytest tests -q   # zweiter Lauf: die Sprache steht beim Import fest
```

Die CI fährt beide Sprachen, prüft die Syntax von Koppler und Erweiterung und
baut die Abbilder erst danach. Ein Tag `v*` löst zusätzlich einen Release aus.

Issues und Pull Requests sind willkommen.

## Lizenz

[AGPL-3.0](LICENSE). Wer wa-gehilfe als Netzwerkdienst betreibt, muss den
Quellcode seiner Änderungen dessen Nutzern zugänglich machen.
