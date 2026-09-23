"""Entwurfsdienst fuer WhatsApp Web.

Die Browser-Erweiterung liest den offenen Chat aus dem DOM und fragt hier einen
Antwortentwurf an. Der Entwurf wird ins Eingabefeld getippt -- gesendet wird
NICHTS automatisch, das macht der Mensch von Hand.

Warum ein eigener Dienst und nicht alles in der Erweiterung:

  * Der API-Schluessel gehoert nicht in eine Browser-Erweiterung. Dort ist er
    fuer jede Seite und jedes andere Add-on lesbar.
  * Das Gedaechtnis soll den Rechnerwechsel und das Leeren des Browserspeichers
    ueberleben.
  * Spaeter sollen mehrere getrennte Instanzen moeglich sein; die Erweiterung
    traegt dafuer nur eine Profilkennung.

Gelernt wird ohne Fine-Tuning: Jeder Entwurf wird mitsamt seinem Ausgang
festgehalten (uebernommen / geaendert / verworfen). Das wertvollste Signal ist
dabei nicht die Ablehnung, sondern **was stattdessen geschrieben wurde** --
Entwurf und Endfassung im Vergleich. Bei der naechsten Anfrage im selben Chat
gehen die aehnlichsten dieser Faelle als Beispiele in den Prompt.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from typing import Any, Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import zugang
from .i18n import SPRACHE, T

DB_PFAD = os.environ.get("ENTWURF_DB", "/daten/entwuerfe.sqlite3")

# Startwerte aus der Umgebung. Was in der Einrichtung gesetzt wird, gewinnt --
# siehe modell_konfig(). So laesst sich der Dienst in Betrieb nehmen, ohne
# vorher eine Umgebungsvariable zu pflegen.
ANBIETER_START = os.environ.get("MODELL_ANBIETER", "claude").lower()
MODELL_START = os.environ.get("MODELL", "claude-sonnet-5")
API_SCHLUESSEL_START = os.environ.get("MODELL_API_SCHLUESSEL", "")
ARBEITSBEREICH_START = os.environ.get("ANTHROPIC_WORKSPACE_ID", "")
# Wie viele frueher bewertete Faelle als Beispiele mitgegeben werden.
BEISPIELE_MAX = int(os.environ.get("BEISPIELE_MAX", "6"))

# Der eigene Name -- der, in dessen Namen die KI Entwuerfe schreibt und der in
# der Nachrichtenansicht neben den eigenen Zeilen steht. Aus der Umgebung,
# damit der Dienst jedem gehoert, der ihn betreibt.
EIGENER_NAME = os.environ.get("EIGENER_NAME", "Ich")

# Domain fuer die UID der Kalender-Eintraege (.ics). Nur ein technischer,
# global eindeutiger Bezeichner -- kein Netzzugriff. Vorgabe generisch.
KALENDER_DOMAIN = os.environ.get("KALENDER_DOMAIN", "wa-gehilfe.local")

# Profil = Partition der Daten (Wissen, Entwuerfe) fuer den Fall mehrerer
# getrennter Instanzen auf einer Datenbank. Eine Instanz braucht nur eine.
PROFIL_STANDARD = os.environ.get("PROFIL", "standard")

def modell_konfig() -> tuple[str, str, str]:
    """Anbieter, Modell, Schluessel -- Einrichtung schlaegt Umgebung.

    Der Schluessel darf beim ersten Start fehlen. Dann fuehrt die Oberflaeche
    durch die Einrichtung, statt dass jemand erst eine Umgebungsvariable
    anfassen muss.
    """
    return (
        zugang.einstellung_lesen("anbieter", ANBIETER_START).lower(),
        zugang.einstellung_lesen("modell", MODELL_START),
        zugang.einstellung_lesen("api_schluessel", API_SCHLUESSEL_START),
    )


def arbeitsbereich() -> str:
    """Anthropic-Arbeitsbereich, falls der Schluessel keinem zugeordnet ist.

    Ein Schluessel ohne Zuordnung wird von der Schnittstelle abgewiesen:
    "This API key is not scoped to a workspace, so this request must include
    the anthropic-workspace-id header". Statt einen neuen Schluessel zu
    verlangen, wird der Kopf mitgeschickt, wenn hier etwas steht.
    """
    return zugang.einstellung_lesen("arbeitsbereich", ARBEITSBEREICH_START).strip()


def claude_kopf(schluessel: str) -> dict[str, str]:
    kopf = {"x-api-key": schluessel, "anthropic-version": "2023-06-01"}
    if arbeitsbereich():
        kopf["anthropic-workspace-id"] = arbeitsbereich()
    return kopf


def eingerichtet() -> bool:
    return zugang.gesetzt(modell_konfig()[2])


app = FastAPI(title="wa-gehilfe")

# Das fertige Stilblatt aus dem Bau. Pfad relativ zu DIESER Datei, nicht zum
# Arbeitsverzeichnis: der Dienst wird als Modul gestartet, und wo uvicorn
# dabei steht, ist nicht verlaesslich.
_STATISCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATISCH):
    app.mount("/static", StaticFiles(directory=_STATISCH), name="static")

# Die PWA-Dateien liegen im Quellbaum, nicht im Bau: Symbole, Manifest und
# Service Worker aendern sich nicht bei jedem Uebersetzen.
_PWA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "statisch")
if os.path.isdir(_PWA):
    app.mount("/statisch", StaticFiles(directory=_PWA), name="statisch")


def _stilkennung() -> str:
    """Kurzer Fingerabdruck des Stilblatts, fuer die Adresse.

    Ohne ihn behaelt der Browser die alte Datei. Genau so passiert: die
    Profilbilder kamen mit neuen Klassen, der Browser hatte aber noch das
    Stilblatt von davor -- in dem es w-9 nicht gab, und das Bild erschien in
    voller Groesse. Der Code war richtig, die Datei alt.
    """
    pfad = os.path.join(_STATISCH, "app.css")
    try:
        with open(pfad, "rb") as d:
            return hashlib.sha256(d.read()).hexdigest()[:10]
    except OSError:
        return "0"


STILKENNUNG = _stilkennung()

# Fuer /verwaltung/zustand: seit wann laeuft dieser Prozess.
START_ZEIT = time.time()


# --- Speicher ---------------------------------------------------------------


def db() -> sqlite3.Connection:
    verb = sqlite3.connect(DB_PFAD, timeout=10)
    verb.row_factory = sqlite3.Row
    return verb


def schema_anlegen() -> None:
    os.makedirs(os.path.dirname(DB_PFAD), exist_ok=True)
    with closing(db()) as v:
        v.execute("""
            CREATE TABLE IF NOT EXISTS entwuerfe (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                zeit        REAL    NOT NULL,
                profil      TEXT    NOT NULL,
                chat        TEXT    NOT NULL,
                kontext     TEXT    NOT NULL,   -- JSON der letzten Nachrichten
                entwurf     TEXT    NOT NULL,
                ergebnis    TEXT,               -- gesendet | geaendert | verworfen
                endfassung  TEXT,               -- was der Mensch wirklich schrieb
                bewertet_am REAL
            )
        """)
        v.execute("CREATE INDEX IF NOT EXISTS idx_chat ON entwuerfe(chat, bewertet_am)")
        # Vormerkungen: im Webinterface geschrieben, danach entweder sofort
        # ueber den Koppler gesetzt oder von der Erweiterung abgeholt.
        #
        # Beide Wege schreiben in ein echtes Eingabefeld einer echten
        # WhatsApp-Web-Sitzung -- der Koppler in seiner eigenen, die
        # Erweiterung in der offenen. Ein Protokollfeld fuer Entwuerfe gibt es
        # nicht; Entwuerfe entstehen im Client und werden von dort auf die
        # verbundenen Geraete synchronisiert.
        #
        # Die Tabelle bleibt auch mit Koppler noetig: faellt er aus oder ist
        # der Chat unter dem getippten Namen nicht auffindbar, liegt die
        # Vormerkung einfach da, bis die Erweiterung sie abholt.
        v.execute("""
            CREATE TABLE IF NOT EXISTS vormerkungen (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                zeit       REAL NOT NULL,
                profil     TEXT NOT NULL,
                chat       TEXT NOT NULL,
                text       TEXT NOT NULL,
                abgeholt_am REAL
            )
        """)
        v.execute("CREATE INDEX IF NOT EXISTS idx_vorm ON vormerkungen(profil, chat, abgeholt_am)")
        # Wissen: was der Dienst aus dem Verlauf ueber einen Kontakt gelernt
        # hat. Getrennt vom Gedaechtnis der Entwuerfe, weil es einen anderen
        # Zweck hat -- dort geht es um die Form einer Antwort, hier um den
        # Menschen dahinter.
        #
        # Jede Aussage steht einzeln, nicht als Textblock. Nur so laesst sich
        # etwas gezielt loeschen, was nicht stimmt oder nicht gespeichert
        # bleiben soll.
        v.execute("""
            CREATE TABLE IF NOT EXISTS wissen (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                profil     TEXT NOT NULL,
                chat       TEXT NOT NULL,
                bereich    TEXT NOT NULL,   -- Person | Beziehung | Vorlieben | Vorhaben | Thema | Ton
                aussage    TEXT NOT NULL,
                erstellt   REAL NOT NULL,
                UNIQUE(profil, chat, aussage)
            )
        """)
        # herkunft nachruesten: die Tabelle gibt es laenger als diese Spalte,
        # und CREATE TABLE IF NOT EXISTS ruehrt eine bestehende nicht an.
        # 'modell' als Vorgabe stimmt fuer alles, was vorher entstand -- von
        # Hand konnte man bis dahin nichts eintragen.
        spalten = {r[1] for r in v.execute("PRAGMA table_info(wissen)")}
        if "herkunft" not in spalten:
            v.execute("ALTER TABLE wissen ADD COLUMN "
                      "herkunft TEXT NOT NULL DEFAULT 'modell'")
        # person: ueber WEN die Aussage geht, wenn das nicht der Chat selbst
        # ist. Gefuellt wird sie nur bei Gruppen -- dort schreiben mehrere,
        # und eine Aussage ueber Alex gehoert zu Alex, nicht zur Gruppe.
        #
        # Ohne diese Spalte bliebe Gruppenwissen in der Gruppe liegen: die
        # Tabelle haengt am Chat, und Alex' Einzelchat saehe nie, was die
        # Gruppe ueber ihn hergibt.
        if "person" not in spalten:
            v.execute("ALTER TABLE wissen ADD COLUMN person TEXT")
            v.execute("CREATE INDEX IF NOT EXISTS idx_wissen_person "
                      "ON wissen(profil, person)")
        v.execute("CREATE INDEX IF NOT EXISTS idx_wissen ON wissen(profil, chat)")
        # Was wann ausgewertet wurde. Ohne dieses Gedaechtnis liefe die
        # automatische Runde immer wieder ueber dieselben Chats und kostete
        # jedes Mal aufs Neue.
        v.execute("""
            CREATE TABLE IF NOT EXISTS wissen_lauf (
                profil   TEXT NOT NULL,
                chat     TEXT NOT NULL,
                zuletzt  REAL NOT NULL,
                gelesen  INTEGER NOT NULL DEFAULT 0,
                fehler   TEXT,
                PRIMARY KEY (profil, chat)
            )
        """)
        # Abschriften von Sprachnachrichten.
        #
        # Der Schluessel ist die Kennung der Nachricht: eine Aufnahme aendert
        # sich nicht mehr, also wird sie genau einmal abgeschrieben. Ohne
        # diese Tabelle kostete jedes Oeffnen des Verlaufs die Rechenzeit
        # erneut.
        v.execute("""
            CREATE TABLE IF NOT EXISTS abschriften (
                wa_id    TEXT PRIMARY KEY,
                chat     TEXT NOT NULL,
                text     TEXT NOT NULL,
                sekunden REAL,
                erstellt REAL NOT NULL
            )
        """)
        # art unterscheidet, was da abgeschrieben wurde: eine Aufnahme oder
        # ein Bild. Dieselbe Tabelle, weil beides dasselbe ist -- Text zu
        # einer Nachricht, die keinen hat, einmal erzeugt und dann behalten.
        if "art" not in {r[1] for r in v.execute("PRAGMA table_info(abschriften)")}:
            v.execute("ALTER TABLE abschriften ADD COLUMN "
                      "art TEXT NOT NULL DEFAULT 'sprache'")
        # Was dieser Dienst gesendet hat.
        #
        # Nicht fuer die Anzeige -- der Verlauf kommt ohnehin von WhatsApp --,
        # sondern zum Nachvollziehen. Senden ueber einen Fremdclient ist der
        # Schritt, fuer den Nummern gesperrt werden; wenn es je Aerger gibt,
        # muss ohne Raten feststehen, was von hier ausging und was nicht.
        #
        # quelle unterscheidet "vorschlag" (aus einem Entwurf uebernommen,
        # ob geaendert oder nicht) von "selbst" (von Hand getippt).
        v.execute("""
            CREATE TABLE IF NOT EXISTS gesendet (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                zeit    REAL NOT NULL,
                profil  TEXT NOT NULL,
                chat    TEXT NOT NULL,
                text    TEXT NOT NULL,
                quelle  TEXT NOT NULL DEFAULT 'selbst',
                wa_id   TEXT
            )
        """)
        # Namen zu Chats, die nur als Nummer gefuehrt werden.
        #
        # Die Nummer bleibt der SCHLUESSEL -- ueberall sonst ist der Chatname
        # die Kennung, und ein wechselnder Name wuerde die Aussagen, Entwuerfe
        # und Vormerkungen dieses Chats verwaisen lassen. Hier steht nur die
        # Beschriftung daneben; sie darf sich aendern, ohne dass etwas
        # verlorengeht.
        #
        # herkunft haelt fest, woher der Name stammt: "modell", wenn er aus
        # dem Verlauf abgeleitet wurde, "hand", wenn ihn jemand gesetzt hat.
        # Von Hand Gesetztes wird nie ueberschrieben.
        # Was automatisch vorgetippt wurde. Der Schluessel ist der Chat, und
        # letzte_zeit haelt fest, auf WELCHE Nachricht der Entwurf antwortet.
        #
        # Ohne diese Zeit taete die Schleife bei jeder Runde dasselbe noch
        # einmal: derselbe Chat, dieselbe letzte Nachricht, derselbe Entwurf
        # -- nur wuerde er jedes Mal neu erzeugt und kostete jedes Mal einen
        # Modellaufruf. Mit ihr passiert nur dann etwas, wenn wirklich eine
        # neue Nachricht gekommen ist.
        v.execute("""
            CREATE TABLE IF NOT EXISTS entwurf_lauf (
                profil       TEXT NOT NULL,
                chat         TEXT NOT NULL,
                letzte_zeit  REAL NOT NULL,
                zuletzt      REAL NOT NULL,
                ergebnis     TEXT,
                PRIMARY KEY (profil, chat)
            )
        """)
        v.execute("""
            CREATE TABLE IF NOT EXISTS chat_namen (
                chat     TEXT PRIMARY KEY,
                name     TEXT NOT NULL,
                herkunft TEXT NOT NULL DEFAULT 'modell',
                erstellt REAL NOT NULL
            )
        """)
        v.commit()


# --- Modellanbindung (austauschbar) -----------------------------------------


def _http_json(url: str, kopf: dict[str, str], rumpf: dict[str, Any]) -> dict[str, Any]:
    daten = json.dumps(rumpf).encode()
    anfrage = urllib.request.Request(url, data=daten, method="POST",
                                     headers={"Content-Type": "application/json", **kopf})
    try:
        with urllib.request.urlopen(anfrage, timeout=60) as antwort:
            return json.loads(antwort.read())
    except urllib.error.HTTPError as e:
        # Sichtbar scheitern: ein Assistent, der still nichts mehr tut, ist
        # schlimmer als gar keiner.
        raise HTTPException(502, f"Modellanbieter: HTTP {e.code} {e.read()[:300]!r}") from e
    except Exception as e:
        raise HTTPException(502, f"Modellanbieter nicht erreichbar: {e}") from e


BILD_SYSTEM = """Du beschreibst ein Bild aus einem WhatsApp-Chat in EINEM
kurzen Satz, damit jemand, der es nicht sieht, den Zusammenhang versteht.

- Was ist zu sehen, und was ist daran wichtig.
- Steht Text im Bild -- Preis, Adresse, Uhrzeit, Name --, gib ihn wieder.
- Keine Mutmassungen ueber Personen, keine Namen erfinden.
- Kein "Das Bild zeigt", sondern gleich die Sache."""


def modell_bild(rohdaten: bytes, mimetyp: str, hoechstens: int = 200) -> str:
    """Ein Bild beschreiben lassen.

    Eigene Funktion statt eines Zusatzes an modell_fragen: die Anbieter
    bauen Bilder ganz verschieden ein, und modell_fragen mit zwei
    Betriebsarten waere unuebersichtlicher als zwei Funktionen.
    """
    import base64
    anbieter, modell, schluessel = modell_konfig()
    if not zugang.gesetzt(schluessel):
        raise HTTPException(500, "Kein Modellschluessel hinterlegt")
    kodiert = base64.b64encode(rohdaten).decode()
    typ = (mimetyp or "image/jpeg").split(";")[0]

    if anbieter == "claude":
        antwort = _http_json(
            "https://api.anthropic.com/v1/messages",
            claude_kopf(schluessel),
            {"model": modell, "max_tokens": hoechstens, "system": BILD_SYSTEM,
             "messages": [{"role": "user", "content": [
                 {"type": "image", "source": {"type": "base64",
                                              "media_type": typ,
                                              "data": kodiert}},
                 {"type": "text", "text": T("Beschreibe dieses Bild.","Describe this image.")}]}]},
        )
        teile = antwort.get("content") or []
        return "".join(x.get("text", "") for x in teile).strip()

    if anbieter == "openrouter":
        antwort = _http_json(
            "https://openrouter.ai/api/v1/chat/completions",
            {"Authorization": f"Bearer {schluessel}"},
            {"model": modell, "max_tokens": hoechstens,
             "messages": [
                 {"role": "system", "content": BILD_SYSTEM},
                 {"role": "user", "content": [
                     {"type": "text", "text": T("Beschreibe dieses Bild.","Describe this image.")},
                     {"type": "image_url",
                      "image_url": {"url": f"data:{typ};base64,{kodiert}"}}]}]},
        )
        wahl = (antwort.get("choices") or [{}])[0]
        return ((wahl.get("message") or {}).get("content") or "").strip()

    raise HTTPException(500, T(f"Unbekannter Anbieter: {anbieter}", f"Unknown provider: {anbieter}"))


def modell_fragen(system: str, nutzer: str, hoechstens: int = 800) -> str:
    """Eine Frage ans Modell. `hoechstens` begrenzt die Laenge der ANTWORT.

    800 reichen fuer einen Entwurf -- das ist eine Nachricht, kein Aufsatz.
    Fuer das Auswerten eines Verlaufs reichen sie nicht: bei einem langen
    Verlauf kam die Antwort nach gut 1600 Zeichen mitten im Satz zum Stehen,
    mit einer offenen und keiner schliessenden Klammer. Der Aufrufer sah nur
    "Antwort war kein JSON" und der ganze Chat blieb ohne Wissen.
    """
    anbieter, modell, schluessel = modell_konfig()
    if not zugang.gesetzt(schluessel):
        raise HTTPException(500, "Kein Modellschluessel hinterlegt — siehe /einrichtung")

    if anbieter == "claude":
        antwort = _http_json(
            "https://api.anthropic.com/v1/messages",
            claude_kopf(schluessel),
            {"model": modell, "max_tokens": hoechstens, "system": system,
             "messages": [{"role": "user", "content": nutzer}]},
        )
        teile = antwort.get("content") or []
        return "".join(t.get("text", "") for t in teile).strip()

    if anbieter == "openrouter":
        antwort = _http_json(
            "https://openrouter.ai/api/v1/chat/completions",
            {"Authorization": f"Bearer {schluessel}"},
            {"model": modell, "max_tokens": hoechstens,
             "messages": [{"role": "system", "content": system},
                          {"role": "user", "content": nutzer}]},
        )
        wahl = (antwort.get("choices") or [{}])[0]
        return ((wahl.get("message") or {}).get("content") or "").strip()

    raise HTTPException(500, T(f"Unbekannter Anbieter: {anbieter}", f"Unknown provider: {anbieter}"))


# --- Wissen ueber die Kontakte ----------------------------------------------
#
# Der Dienst liest aus dem vorhandenen Verlauf, was ueber den Menschen am
# anderen Ende bekannt ist, und haelt es je Chat fest. Das dient dem Entwurf:
# Wer weiss, dass jemand siezt, Schichtdienst hat und im Juli umzieht,
# formuliert anders.
#
# Bewusst als einzelne Aussagen und nicht als Fliesstext: nur so laesst sich
# gezielt streichen, was falsch ist oder nicht gespeichert bleiben soll. Und
# bewusst nur, was im Chat steht -- nichts Erschlossenes, nichts Geratenes.

# Die Analyse-Prompts bleiben bewusst deutsch, auch bei SPRACHE=en.
#
# Sie legen das Vokabular der Bereiche fest (siehe BEREICHE), und das sind
# Datenschluessel. Ein zweisprachiger Prompt erzeugte zwei Vokabulare in
# derselben Tabelle. Auf die Ausgabe wirkt es nicht: das Modell antwortet in
# der Sprache des jeweiligen Chats, nicht in der des Prompts.
WISSEN_SYSTEM = """Du liest einen WhatsApp-Verlauf und haeltst fest, was
daraus ueber den Gespraechspartner hervorgeht.

Gib ausschliesslich JSON zurueck, eine Liste von Objekten:
[{"bereich": "...", "aussage": "..."}]

Erlaubte Bereiche: Person, Beziehung, Vorlieben, Vorhaben, Ton.

Regeln:
- Nur was im Verlauf tatsaechlich steht oder unmittelbar daraus hervorgeht.
  Nichts erschliessen, nichts vermuten, nichts ausschmuecken.
- Jede Aussage ein knapper, fuer sich verstaendlicher Satz.
- Keine Nachrichten zitieren, keine Zeitstempel, keine Telefonnummern.
- "Ton" beschreibt, wie miteinander geschrieben wird (Anrede, Laenge,
  Umgangston, Sprache).
- Nichts Fluechtiges ("hat gestern gefragt, ob..."), sondern Bestaendiges.
- Findest du nichts Belastbares, gib eine leere Liste zurueck."""

# Fuer Gruppen dieselben Regeln, aber ein anderer Gegenstand.
#
# Der Einzelchat-Prompt fragt nach "dem Gespraechspartner" -- in einer Gruppe
# gibt es den nicht. Wer ihn dort unveraendert benutzt, bekommt entweder
# nichts oder eine Aussage ueber irgendeinen der Beteiligten, und beides ist
# wertlos. Gefragt ist das Gefuege: wer dazugehoert, wofuer die Gruppe da
# ist, was ansteht.
WISSEN_SYSTEM_GRUPPE = """Du liest einen WhatsApp-GRUPPENVERLAUF. Dort
schreiben mehrere, und es geht um ZWEIERLEI: um die einzelnen Beteiligten
und um die Gruppe als Ganzes. Halte beides fest.

Gib ausschliesslich JSON zurueck, eine Liste von Objekten:
[{"bereich": "...", "aussage": "..."}]

Erlaubte Bereiche: Person, Beziehung, Vorlieben, Vorhaben, Thema, Ton.

Zu den EINZELNEN Beteiligten:
- Zu jedem, der erkennbar hervortritt, eine eigene Aussage -- nicht nur zu
  einem.
- Immer mit Namen, sonst laesst sich die Aussage niemandem zuordnen:
  "Alex kuemmert sich um die Technik", nicht "jemand kuemmert sich um die
  Technik".
- Und setze bei solchen Aussagen zusaetzlich das Feld "person" auf genau
  diesen Namen, so wie er im Verlauf steht:
  {"bereich": "Person", "person": "Alex", "aussage": "Alex kuemmert
  sich um die Technik"}. Geht es um die Gruppe als Ganzes, lass "person"
  weg.
- "Beziehung" auch untereinander, nicht nur zu mir: wer mit wem zu tun hat,
  wer wem zuarbeitet.

Zur GRUPPE:
- "Thema": wofuer die Gruppe da ist und worum es darin immer wieder geht.
- "Vorhaben": was ansteht, wer es macht, bis wann.
- "Ton": wie in dieser Gruppe geschrieben wird (Anrede, Laenge, Umgangston,
  Sprache) -- der kann ein ganz anderer sein als im Einzelchat.

Allgemein:
- Nur was im Verlauf tatsaechlich steht oder unmittelbar daraus hervorgeht.
  Nichts erschliessen, nichts vermuten, nichts ausschmuecken.
- Jede Aussage ein knapper, fuer sich verstaendlicher Satz.
- Keine Nachrichten zitieren, keine Zeitstempel, keine Telefonnummern.
- Nichts Fluechtiges ("hat gestern gefragt, ob..."), sondern Bestaendiges.
- Findest du nichts Belastbares, gib eine leere Liste zurueck."""

# Wie lang die Antwort beim Auswerten eines Verlaufs werden darf. Deutlich
# mehr als die 800 fuer einen Entwurf: hier entstehen Dutzende Aussagen.
WISSEN_ANTWORT_TOKEN = int(os.environ.get("WISSEN_ANTWORT_TOKEN", "4000"))


def liste_aus_antwort(roh: str) -> list[Any] | None:
    """Die Liste aus der Antwort des Modells holen, so gut es geht.

    Drei Stufen, weil drei Dinge schiefgehen koennen:

    1. Das Modell packt sein JSON in einen Markdown-Block. Die Zaeune weg,
       dann steht die Liste da.
    2. Es schreibt noch einen Satz davor oder dahinter. Dann wird die Liste
       herausgeschnitten.
    3. Die Antwort bricht mitten im JSON ab, weil das Token-Limit erreicht
       ist -- eine offene Klammer, keine schliessende. Vorher ging dabei
       ALLES verloren, obwohl die ersten zwanzig Aussagen vollstaendig
       dastanden. Jetzt werden die vollstaendigen Objekte einzeln gerettet.

    Gibt None zurueck, wenn sich wirklich nichts lesen laesst.
    """
    text = roh.strip()

    # Stufe 1: Markdown-Zaeune entfernen, auch eine allein stehende oeffnende.
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    # Stufe 2: vollstaendige Liste, mit und ohne Beiwerk drumherum.
    kandidaten = [text]
    treffer = re.search(r"\[.*\]", text, re.S)
    if treffer:
        kandidaten.append(treffer.group(0))
    for kandidat in kandidaten:
        try:
            d = json.loads(kandidat)
            if isinstance(d, list):
                return d
        except Exception:
            pass

    # Stufe 3: abgeschnitten -- die vollstaendigen Objekte einzeln lesen.
    gerettet = []
    for m in re.finditer(r"\{[^{}]*\}", text, re.S):
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                gerettet.append(d)
        except Exception:
            pass
    return gerettet or None


# --- Namen zu Nummern -------------------------------------------------------
#
# Wer nicht im Adressbuch des gekoppelten Telefons steht, kommt ohne Namen an
# -- der Koppler setzt dann die Telefonnummer ein. Im Verlauf steht der Name
# aber oft trotzdem: als Unterschrift, als "hier ist X", in der Anrede.

NAME_SYSTEM = """Du bekommst einen WhatsApp-Verlauf. Nenne den Namen der
Person am anderen Ende -- nicht den eigenen.

Antworte mit NICHTS ausser dem Namen, oder mit einem einzelnen Bindestrich,
wenn im Verlauf kein Name vorkommt. Keine Anfuehrungszeichen, keine
Erklaerung, kein Satz.

Nur was dasteht. Rate nicht aus einer Anrede wie "Hallo zusammen", und
erfinde nichts aus dem Zusammenhang. Ein Firmenname ist auch ein Name."""


def ist_nummer(chat: str) -> bool:
    """Wird dieser Chat nur als Telefonnummer gefuehrt?"""
    return bool(re.fullmatch(r"\+[\d\s/().-]{6,}", (chat or "").strip()))


def namen_karte() -> dict[str, str]:
    with closing(db()) as v:
        return {z["chat"]: z["name"]
                for z in v.execute("SELECT chat, name FROM chat_namen")}


def anzeige(chat: str, karte: dict[str, str] | None = None) -> str:
    """Wie ein Chat beschriftet wird. Die Nummer bleibt sichtbar -- sie ist
    die Kennung, und ein abgeleiteter Name kann danebenliegen."""
    k = namen_karte() if karte is None else karte
    name = k.get(chat)
    return f"{name} ({chat})" if name else chat


def name_ermitteln(chat_name: str, anzahl: int = 60) -> str | None:
    """Den Namen aus dem Verlauf holen und ablegen. None, wenn keiner drinsteht.

    Von Hand gesetzte Namen bleiben unangetastet.
    """
    with closing(db()) as v:
        z = v.execute("SELECT name, herkunft FROM chat_namen WHERE chat=?",
                      (chat_name,)).fetchone()
    if z and z["herkunft"] == "hand":
        return z["name"]

    kid = koppler_chat_finden(chat_name)
    if not kid:
        return None
    nachrichten = koppler(f"/nachrichten?chat={urllib.parse.quote(kid)}&anzahl={anzahl}")
    if not isinstance(nachrichten, list) or not nachrichten:
        return None

    verlauf = "\n".join(
        f"{EIGENER_NAME if n.get('von_mir') else T('Gegenueber','Other side')}: {n.get('text','')}"
        for n in nachrichten if (n.get("text") or "").strip())[:12000]
    if not verlauf.strip():
        return None

    # 40 Token reichen fuer einen Namen. Mehr zu erlauben lockt nur Saetze an.
    roh = (modell_fragen(NAME_SYSTEM, verlauf, hoechstens=40) or "").strip()
    name = roh.strip().strip('"\'' ).splitlines()[0].strip() if roh else ""

    # Ein Bindestrich heisst "kein Name". Und was wie ein ganzer Satz aussieht,
    # ist keiner -- dann hat sich das Modell nicht an die Vorgabe gehalten.
    if not name or name in {"-", "--"} or len(name) > 60 or name.count(" ") > 3:
        return None

    with closing(db()) as v:
        v.execute("""INSERT INTO chat_namen (chat, name, herkunft, erstellt)
                     VALUES (?,?,'modell',?)
                     ON CONFLICT(chat) DO UPDATE SET
                       name=excluded.name, erstellt=excluded.erstellt
                     WHERE chat_namen.herkunft <> 'hand'""",
                  (chat_name, name, time.time()))
        v.commit()
    return name


def wissen_lesen(chat: str, profil: str) -> list[sqlite3.Row]:
    with closing(db()) as v:
        return v.execute("""SELECT id, bereich, aussage FROM wissen
                             WHERE profil=? AND chat=? ORDER BY bereich, id""",
                          (profil, chat)).fetchall()


def wissen_aus_gruppen(chat: str, profil: str) -> list[sqlite3.Row]:
    """Was ANDERE Chats -- Gruppen -- ueber diese Person hergeben.

    In einer Gruppe faellt Wissen ueber die einzelnen Beteiligten an, und das
    gehoert zu ihnen, nicht zur Gruppe. Ohne diesen Griff bliebe es liegen:
    die Tabelle haengt am Chat, und Alex' Einzelchat saehe nie, was die
    Technikgruppe ueber ihn hergibt.

    Zugeordnet wird ueber den Namen, und das ist die Schwachstelle: in der
    Gruppe heisst jemand "Alex", im Einzelchat "Alex Muster". Deshalb
    zaehlt auch, wenn der eine Name mit dem anderen beginnt -- aber nur auf
    Wortgrenze, sonst faende "Jan" auch "Janine".
    """
    ziel = (chat or "").strip().casefold()
    if not ziel:
        return []
    with closing(db()) as v:
        zeilen = v.execute("""SELECT id, bereich, aussage, person, chat AS quelle
                               FROM wissen
                              WHERE profil=? AND person IS NOT NULL AND chat<>?
                              ORDER BY bereich, id""", (profil, chat)).fetchall()
    treffer = []
    for z in zeilen:
        p = (z["person"] or "").strip().casefold()
        if not p:
            continue
        if p == ziel or ziel.startswith(p + " ") or p.startswith(ziel + " "):
            treffer.append(z)
    return treffer


def wissen_aufbauen(chat_name: str, profil: str, anzahl: int = 200) -> dict[str, Any]:
    """Verlauf holen, Aussagen ableiten, ablegen.

    Der Verlauf kommt vom Koppler -- nur er sieht mehr als den sichtbaren
    Ausschnitt. Ohne ihn geht das hier nicht, und das wird auch so gesagt,
    statt still nichts zu tun.
    """
    if not koppler_da():
        raise HTTPException(409, "Dafuer wird die gekoppelte Sitzung gebraucht.")
    kid = koppler_chat_finden(chat_name)
    if not kid:
        raise HTTPException(404, f"Chat {chat_name!r} nicht gefunden.")
    nachrichten = koppler(f"/nachrichten?chat={urllib.parse.quote(kid)}&anzahl={anzahl}")
    if not isinstance(nachrichten, list):
        raise HTTPException(409, "Kein Verlauf abrufbar.")
    if not nachrichten:
        # Leerer Verlauf ist kein Fehler, sondern nichts zu lernen. Vorher
        # flog hier eine 409, der Chat stand danach dauerhaft rot in der
        # Uebersicht und wurde jede Woche erneut vergeblich versucht.
        # Beim ersten grossen Lauf traf das einzelne Chats.
        return {"chat": chat_name, "gelesen": 0, "neu": 0,
                "hinweis": T("Kein Verlauf vorhanden.","No history available.")}

    # Auch hier ueber medien_als_text: eine Wissensrunde, die
    # Sprachnachrichten ueberspringt, uebersieht genau die Chats, in denen
    # ueberwiegend gesprochen wird -- die fielen als "nur Nachrichten ohne
    # Text" durch.
    # bilder=False: Abschriften laufen hier lokal und kosten nichts, eine
    # Bildbeschreibung dagegen einen Modellaufruf. Die Wissensrunde geht
    # ueber hunderte Chats -- zwei Bilder je Chat waeren schon dort
    # hunderte Aufrufe fuer etwas, das im Hintergrund laeuft.
    # Bilder werden dort beschrieben, wo jemand hinsieht: beim Entwurf.
    # Vorhandene Beschreibungen nimmt die Runde selbstverstaendlich mit.
    aufbereitet = medien_als_text(nachrichten, chat_name,
                                  hoechstens_neu=3, bilder=False)
    verlauf = "\n".join(f"{T('ich','me') if n.von_mir else chat_name}: {n.text}"
                        for n in aufbereitet)[:60000]

    # Gruppe oder Einzelchat? Die Kennung sagt es: WhatsApp haengt an
    # Gruppen @g.us. Das ist verlaesslicher als der Name und kostet nichts --
    # die Kennung liegt hier ohnehin schon vor.
    gruppe = str(kid).endswith("@g.us")
    system = WISSEN_SYSTEM_GRUPPE if gruppe else WISSEN_SYSTEM
    frage = (f"{T('Gruppenverlauf','Group history') if gruppe else T('Verlauf','History')} {T('mit','with')} {chat_name}:"
             f"\n\n{verlauf}")
    roh = modell_fragen(system, frage, hoechstens=WISSEN_ANTWORT_TOKEN)
    if not roh.strip():
        # Gelegentlich kommt gar nichts zurueck -- beim ersten grossen Lauf
        # in einzelnen Faellen, und derselbe Chat lieferte beim Nachstellen sauber
        # eine leere Liste. Also einmal nachfassen, statt den Chat als
        # fehlerhaft abzulegen und es eine Woche lang nicht mehr zu versuchen.
        roh = modell_fragen(system, frage, hoechstens=WISSEN_ANTWORT_TOKEN)
    if not roh.strip():
        return {"chat": chat_name, "gelesen": len(nachrichten), "neu": 0,
                "hinweis": T("Modell gab zweimal nichts zurueck.","Model returned nothing twice.")}

    eintraege = liste_aus_antwort(roh)
    if eintraege is None:
        raise HTTPException(502, T(f"Antwort war kein JSON: {roh[:200]}", f"Reply was not JSON: {roh[:200]}"))

    neu = 0
    with closing(db()) as v:
        for e in eintraege if isinstance(eintraege, list) else []:
            aussage = str(e.get("aussage", "")).strip()
            bereich = str(e.get("bereich", "Person")).strip() or "Person"
            if not aussage:
                continue
            # person nur aus Gruppen. Im Einzelchat waere sie bestenfalls
            # eine Wiederholung des Chatnamens und schlimmstenfalls falsch --
            # das Modell nennt dort gelegentlich mich selbst.
            person = (str(e.get("person", "")).strip() or None) if gruppe else None
            try:
                v.execute("""INSERT INTO wissen (profil, chat, bereich, aussage,
                                                 erstellt, person)
                             VALUES (?,?,?,?,?,?)""",
                          (profil, chat_name, bereich, aussage, time.time(), person))
                neu += 1
            except sqlite3.IntegrityError:
                pass  # kennen wir schon -- UNIQUE faengt das ab
        v.commit()
    return {"chat": chat_name, "gelesen": len(nachrichten), "neu": neu}


# --- Statusprobe ------------------------------------------------------------
#
# "Schluessel ist hinterlegt" sagt nichts darueber, ob das Modell antwortet.
# Ein abgelaufener, widerrufener oder falsch abgetippter Schluessel sieht
# genauso aus wie ein guter. Deshalb wird wirklich gefragt -- mit der
# kleinstmoeglichen Anfrage, und das Ergebnis eine Weile gemerkt, damit die
# Uebersicht nicht bei jedem Neuladen Geld kostet.

_PROBE: dict[str, Any] = {"zeit": 0.0, "ok": False, "text": T("noch nicht geprueft","not checked yet")}
PROBE_GUELTIG = 300.0


def modell_probe(erzwingen: bool = False) -> dict[str, Any]:
    anbieter, modell, schluessel = modell_konfig()
    if not zugang.gesetzt(schluessel):
        return {"ok": False, "stufe": "aus", "text": T("kein Schluessel hinterlegt","no key stored"), "zeit": time.time()}
    if not erzwingen and (time.time() - _PROBE["zeit"]) < PROBE_GUELTIG:
        return _PROBE

    try:
        if anbieter == "claude":
            _http_json("https://api.anthropic.com/v1/messages",
                       claude_kopf(schluessel),
                       {"model": modell, "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}]})
        elif anbieter == "openrouter":
            _http_json("https://openrouter.ai/api/v1/chat/completions",
                       {"Authorization": f"Bearer {schluessel}"},
                       {"model": modell, "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}]})
        else:
            raise HTTPException(500, T(f"Unbekannter Anbieter: {anbieter}", f"Unknown provider: {anbieter}"))
        _PROBE.update({"ok": True, "text": T(f"{anbieter} / {modell} antwortet", f"{anbieter} / {modell} responds"),
                       "zeit": time.time()})
    except HTTPException as e:
        # Die Meldung des Anbieters mitnehmen: "invalid x-api-key" ist eine
        # Auskunft, "Fehler" waere keine.
        _PROBE.update({"ok": False, "text": str(e.detail)[:200], "zeit": time.time()})
    except Exception as e:
        _PROBE.update({"ok": False, "text": str(e)[:200], "zeit": time.time()})
    return _PROBE


def whatsapp_probe() -> dict[str, Any]:
    """Zustand der gekoppelten Sitzung, in Worten der Oberflaeche."""
    if not koppler_da():
        return {"ok": False, "stufe": "aus", "text": T("kein Koppler eingerichtet","no connector configured")}
    z = koppler("/zustand")
    st = z.get("status", "unbekannt")
    if st == "bereit":
        return {"ok": True, "stufe": "gut",
                "text": T("verbunden","connected") + (f" {T('als','as')} {z['nummer']}" if z.get("nummer") else "")}
    if st == "qr":
        return {"ok": False, "stufe": "wartet", "text": T("wartet auf das Scannen des Codes","waiting for the code to be scanned")}
    if st == "nicht_erreichbar":
        return {"ok": False, "stufe": "schlecht", "text": T("Koppler antwortet nicht","connector not responding")}
    return {"ok": False, "stufe": "schlecht", "text": st}


# --- Prompt -----------------------------------------------------------------

# Der Kern der Regeln in beiden Sprachen; die letzte Regel unterscheidet
# Hand-Entwurf (darf KEIN_ENTWURF sagen) und Vortippen (muss liefern). Das
# Sentinel KEIN_ENTWURF bleibt in beiden Sprachen gleich -- der Code prueft
# darauf.
_SYSTEM_KERN = T(f"""Du formulierst Antwortentwuerfe fuer WhatsApp-Nachrichten, die
{EIGENER_NAME} anschliessend prueft, aendert oder verwirft. Du schreibst als
{EIGENER_NAME}, nicht ueber die Person.

Regeln:
- Schreibe so, wie {EIGENER_NAME} in diesem Chat schreibt: gleiche Sprache, gleiche
  Anrede, gleiche Laenge, gleicher Ton. Ein Chat mit einem Kumpel klingt anders
  als einer mit dem Vermieter.
- Nur der Nachrichtentext. Keine Anfuehrungszeichen, keine Einleitung, keine
  Erklaerung, keine Unterschrift.
- Im Zweifel kurz. Ein zu knapper Entwurf ist schnell ergaenzt; ein zu langer
  wird verworfen.
- Erfinde keine Zusagen, Termine, Preise oder Tatsachen. Fehlt etwas
  Entscheidendes, formuliere die Rueckfrage, die {EIGENER_NAME} stellen wuerde.
""", f"""You draft replies to WhatsApp messages that {EIGENER_NAME} then reviews,
edits or discards. You write as {EIGENER_NAME}, not about them.

Rules:
- Write the way {EIGENER_NAME} writes in this chat: same language, same form of
  address, same length, same tone. A chat with a buddy sounds different from
  one with the landlord.
- Only the message text. No quotation marks, no preamble, no explanation, no
  signature.
- When in doubt, keep it short. A draft that is too brief is quickly extended;
  one that is too long gets discarded.
- Do not invent commitments, appointments, prices or facts. If something
  essential is missing, phrase the follow-up question {EIGENER_NAME} would ask.
""")

SYSTEM = _SYSTEM_KERN + T(
    "- Wenn die letzte Nachricht keine Antwort braucht, antworte ausschliesslich mit:\n"
    "  KEIN_ENTWURF",
    "- If the last message needs no reply, answer with exactly:\n"
    "  KEIN_ENTWURF")

# Fuer das automatische Vortippen dieselben Regeln, aber ohne die letzte.
#
# Dort ist die Lage eine andere: die Schleife nimmt nur Chats, in denen die
# GEGENSEITE zuletzt geschrieben hat. Ob das eine Frage war oder ein "bis
# dann", entscheidet nicht der Dienst -- vorgetippt wird immer, und
# weggeworfen wird mit einem Tastendruck. Ein fehlender Entwurf dagegen faellt
# niemandem auf, und dann war die ganze Runde umsonst.
SYSTEM_AUTO = _SYSTEM_KERN + T(
    "- Schreibe IMMER einen Entwurf, auch wenn die letzte Nachricht keine Frage\n"
    "  war. Dann ist es das, was man ueblicherweise darauf antwortet -- eine\n"
    "  kurze Bestaetigung, ein Dank, ein Gruss. Niemals KEIN_ENTWURF.",
    "- ALWAYS write a draft, even if the last message was not a question. Then\n"
    "  it is what one usually replies to it -- a short acknowledgement, a\n"
    "  thanks, a greeting. Never KEIN_ENTWURF.")


def beispiele_holen(chat: str, profil: str) -> list[sqlite3.Row]:
    """Frueher bewertete Faelle aus demselben Chat.

    Geaenderte und verworfene zuerst -- daraus laesst sich am meisten lernen.
    """
    with closing(db()) as v:
        return v.execute("""
            SELECT kontext, entwurf, ergebnis, endfassung
              FROM entwuerfe
             WHERE chat = ? AND profil = ? AND ergebnis IS NOT NULL
             ORDER BY CASE ergebnis WHEN 'geaendert' THEN 0
                                    WHEN 'verworfen' THEN 1
                                    ELSE 2 END,
                      bewertet_am DESC
             LIMIT ?
        """, (chat, profil, BEISPIELE_MAX)).fetchall()


# Die Bereiche sind DATENSCHLUESSEL, keine Beschriftungen.
#
# Sie stehen so in der Datenbank, das Modell liefert sie so zurueck, und die
# Wissensseite gruppiert danach. Wuerde man sie je nach SPRACHE uebersetzen,
# entstuenden zwei Vokabulare in derselben Tabelle -- ein deutscher Eintrag
# "Person" und ein englischer "Person" waeren zufaellig gleich, "Vorlieben"
# und "Preferences" nicht. Deshalb: Schluessel bleiben deutsch, nur die
# ANZEIGE wird uebersetzt.
BEREICHE = ("Person", "Beziehung", "Vorlieben", "Vorhaben", "Thema", "Ton")

_BEREICH_EN = {
    "Person": "Person", "Beziehung": "Relationship", "Vorlieben": "Preferences",
    "Vorhaben": "Plans", "Thema": "Topic", "Ton": "Tone",
}


def bereich_anzeige(schluessel: str) -> str:
    """Den Bereich so zeigen, wie ihn der Mensch liest -- Schluessel bleibt."""
    return T(schluessel, _BEREICH_EN.get(schluessel, schluessel))


def prompt_bauen(nachrichten: list["Nachricht"], chat: str, profil: str,
                 hinweis: str | None) -> str:
    teile: list[str] = []

    # Was ueber den Menschen am anderen Ende bekannt ist, zuerst: Es faerbt den
    # ganzen Entwurf, waehrend die Beispiele nur die Form korrigieren.
    bekannt = wissen_lesen(chat, profil)
    if bekannt:
        # Mit dem abgeleiteten Namen, wenn es einen gibt: "Was ueber Alex
        # Muster (+41 79 ...) bekannt ist" sagt dem Modell, an wen es schreibt.
        # Eine nackte Nummer sagt ihm gar nichts.
        teile.append(T(f"Was ueber {anzeige(chat)} bekannt ist:", f"What is known about {anzeige(chat)}:"))
        teile.extend(f"- [{z['bereich']}] {z['aussage']}" for z in bekannt)
        teile.append(T("Nutze das, aber erwaehne es nicht ungefragt.\n","Use it, but do not bring it up unprompted.\n"))

    # Und was in Gruppen ueber diese Person angefallen ist. Getrennt
    # ausgewiesen, samt Herkunft: es stammt nicht aus diesem Gespraech, und
    # das Modell soll es nicht so behandeln, als haette man es hier erfahren.
    aus_gruppen = wissen_aus_gruppen(chat, profil)
    if aus_gruppen:
        teile.append(T("Aus gemeinsamen Gruppen ueber diese Person bekannt:","Known about this person from shared groups:"))
        teile.extend(f"- [{z['bereich']}] {z['aussage']} ({T('aus','from')}: {z['quelle']})"
                     for z in aus_gruppen)
        teile.append(T("Vorsicht damit: es stammt aus einer Gruppe, nicht aus diesem Gespraech. Nicht darauf anspielen.\n",
                       "Careful with this: it comes from a group, not from this conversation. Do not allude to it.\n"))

    beisp = beispiele_holen(chat, profil)
    if beisp:
        teile.append(T("Frueher in diesem Chat -- so wurden deine Entwuerfe aufgenommen:","Earlier in this chat -- how your drafts were received:"))
        for b in beisp:
            if b["ergebnis"] == "geaendert" and b["endfassung"]:
                teile.append(T(f"- Dein Entwurf: {b['entwurf']}\n  {EIGENER_NAME} schrieb stattdessen: {b['endfassung']}",
                               f"- Your draft: {b['entwurf']}\n  {EIGENER_NAME} wrote instead: {b['endfassung']}"))
            elif b["ergebnis"] == "verworfen":
                teile.append(T(f"- Verworfen (unpassend): {b['entwurf']}", f"- Discarded (unsuitable): {b['entwurf']}"))
            else:
                teile.append(T(f"- Unveraendert uebernommen: {b['entwurf']}", f"- Used unchanged: {b['entwurf']}"))
        teile.append(T("Richte dich danach.\n","Follow that.\n"))

    teile.append(T("Chatverlauf, aelteste zuerst:","Chat history, oldest first:"))
    for n in nachrichten:
        wer = EIGENER_NAME if n.von_mir else (n.von or T("Gegenueber","Other side"))
        teile.append(f"[{wer}] {n.text}")

    if hinweis:
        teile.append(T(f"\nHinweis von {EIGENER_NAME} fuer diese Antwort: {hinweis}", f"\nNote from {EIGENER_NAME} for this reply: {hinweis}"))

    teile.append(T("\nFormuliere jetzt den Antwortentwurf.","\nNow write the reply draft."))
    return "\n".join(teile)


# --- Schnittstelle ----------------------------------------------------------


class Nachricht(BaseModel):
    von: str | None = None
    text: str
    von_mir: bool = False


class EntwurfAnfrage(BaseModel):
    profil: str = Field(default_factory=lambda: PROFIL_STANDARD, description="fuer spaetere getrennte Instanzen")
    chat: str = Field(..., description="stabile Kennung des Chats")
    nachrichten: list[Nachricht]
    hinweis: str | None = None


class EntwurfAntwort(BaseModel):
    id: int
    entwurf: str


class Rueckmeldung(BaseModel):
    id: int
    ergebnis: Literal["gesendet", "geaendert", "verworfen"]
    endfassung: str | None = None


@app.on_event("startup")
async def start() -> None:
    schema_anlegen()
    # Laeuft mit, solange der Dienst laeuft. Ein eigener Zeitplaner waere fuer
    # eine Runde alle paar Stunden mehr Apparat als Nutzen.
    asyncio.create_task(wissen_schleife())
    # Und das automatische Vortippen. Getrennte Schleifen, weil sie in ganz
    # verschiedenen Takten laufen: Wissen alle paar Stunden, Entwuerfe alle
    # paar Minuten -- eine Antwort, die erst nach drei Stunden dasteht, hat
    # ihren Zweck verfehlt.
    asyncio.create_task(vortippen_schleife())


@app.get("/gesundheit")
def gesundheit() -> dict[str, str]:
    """Offen, weil Orchestrierung und Reverse-Proxy hier ohne Anmeldung
    hinschauen muessen.

    Deshalb steht hier auch nichts drin, was jemandem nuetzt: kein Zaehler, kein
    Modellname. Eine Lebendpruefung ist keine Auskunftsstelle.
    """
    return {"status": "ok"}


def zustand() -> dict[str, Any]:
    """Die eigentlichen Angaben -- nur hinter der Anmeldung."""
    with closing(db()) as v:
        anzahl = v.execute("SELECT COUNT(*) AS n FROM entwuerfe").fetchone()["n"]
        bewertet = v.execute(
            "SELECT COUNT(*) AS n FROM entwuerfe WHERE ergebnis IS NOT NULL").fetchone()["n"]
    anbieter, modell, schluessel = modell_konfig()
    return {"anbieter": anbieter, "modell": modell,
            "schluessel_gesetzt": zugang.gesetzt(schluessel),
            "entwuerfe": anzahl, "davon_bewertet": bewertet}


@app.post("/entwurf", response_model=EntwurfAntwort,
          dependencies=[Depends(zugang.api_zugang)])
def entwurf(a: EntwurfAnfrage) -> EntwurfAntwort:
    if not a.nachrichten:
        raise HTTPException(400, "keine Nachrichten uebergeben")

    text = modell_fragen(SYSTEM, prompt_bauen(a.nachrichten, a.chat, a.profil, a.hinweis))
    if not text:
        raise HTTPException(502, "Modell lieferte einen leeren Entwurf")

    with closing(db()) as v:
        cur = v.execute(
            "INSERT INTO entwuerfe (zeit, profil, chat, kontext, entwurf) VALUES (?,?,?,?,?)",
            (time.time(), a.profil, a.chat,
             json.dumps([n.model_dump() for n in a.nachrichten], ensure_ascii=False), text))
        v.commit()
        return EntwurfAntwort(id=int(cur.lastrowid), entwurf=text)


def kopf(titel: str = "wa-gehilfe", zurueck: str | None = None,
         aktiv: str = "") -> str:
    """Seitenanfang samt Navigation.

    Frueher war KOPF eine Zeichenkette mit handgeschriebenem CSS, und jede
    Seite baute ihre Navigation selbst -- mal ein Pfeil, mal keiner, mal ein
    Link mehr. Als Funktion ist die Leiste ueberall dieselbe, und man sieht
    auf jeder Seite, wo man ist.

    zurueck setzt einen Pfeil nach links; auf dem Telefon ist das der Weg,
    den man sucht. aktiv hebt den Punkt hervor, auf dem man steht.
    """
    def punkt(pfad: str, text: str) -> str:
        an = pfad == aktiv
        stil = ("bg-slate-900 text-white" if an
                else "text-slate-600 hover:bg-slate-100")
        return (f'<a href="{pfad}" class="px-3 py-1.5 rounded-lg '
                f'text-sm font-medium {stil}">{text}</a>')

    pfeil = (f'<a href="{zurueck}" class="text-slate-500 hover:text-slate-900 '
             f'text-2xl leading-none pr-1" aria-label="Zurueck">&larr;</a>'
             if zurueck else "")

    return f"""<!doctype html><html lang="{SPRACHE}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{esc_h(titel)}</title>
<link rel="stylesheet" href="/static/app.css?v={STILKENNUNG}">
<link rel="manifest" href="/statisch/manifest.webmanifest" crossorigin="use-credentials">
<meta name="theme-color" content="#25d366">
<link rel="icon" href="/statisch/symbol-192.png" type="image/png">
<link rel="apple-touch-icon" href="/statisch/symbol-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="wa-gehilfe">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<script>
 // Der Service Worker macht die Seite installierbar. Ohne ihn bietet kein
 // Browser "zum Startbildschirm hinzufuegen" an.
 //
 // Ausgeliefert wird er unter /sw.js, nicht unter /statisch/: ein Service
 // Worker darf nur den Pfad steuern, unter dem er liegt. Von /statisch/ aus
 // waere die App nicht in seinem Geltungsbereich.
 if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
</script>
</head><body class="bg-slate-50 text-slate-900 antialiased">
<header class="sticky top-0 z-10 bg-white/90 backdrop-blur border-b border-slate-200">
 <div class="mx-auto max-w-3xl px-4 py-2.5 flex items-center gap-2">
  {pfeil}
  <span class="font-semibold truncate flex-1">{esc_h(titel)}</span>
  <nav class="flex gap-1">
   {punkt("/", T("Chats", "Chats"))}
   {punkt("/wissen", T("Wissen", "Knowledge"))}
   {punkt("/einstellungen", T("Mehr", "More"))}
  </nav>
 </div>
</header>
<main class="mx-auto max-w-3xl px-4 py-4">"""


# Selbstaktualisierung.
#
# Ohne sie ist die Seite ein totes Fenster: neue Nachrichten erscheinen erst,
# wenn jemand neu laedt. Ein meta-refresh waere einfacher, wuerde aber einen
# halb getippten Text wegwerfen -- deshalb dieses Skript, das vorher
# nachsieht.
#
# Drei Bedingungen, alle noetig:
#   * im Antwortfeld steht nichts,
#   * der Mauszeiger ist in keinem Eingabefeld,
#   * das Fenster ist sichtbar (im Hintergrund waere es Verschwendung).
AKTUALISIERUNG = """<script>
setInterval(function () {
  if (document.hidden) return;
  var a = document.activeElement;
  if (a && (a.tagName === 'TEXTAREA' || a.tagName === 'INPUT')) return;
  var f = document.querySelector('textarea[name=text]');
  if (f && f.value.trim()) return;
  location.reload();
}, %d000);
</script>"""


def fuss(ans_ende: bool = False, aktualisieren: int = 0) -> str:
    """Seitenabschluss.

    ans_ende springt ans untere Ende -- fuer einen Verlauf, dessen juengste
    Nachricht unten steht. Ohne das oeffnet ein langer Chat bei der aeltesten
    Nachricht, und man scrollt erst einmal.

    Die einzige Zeile Javascript im ganzen Dienst, und zwar bewusst: die
    Alternative ohne Skript waere autofocus auf dem Antwortfeld -- das
    springt zwar auch, reisst auf dem Telefon aber die Tastatur hoch, sobald
    man einen Chat nur lesen will.
    """
    # Ans Ende springen -- und zwar mehrfach.
    #
    # Ein einzelner Aufruf beim Laden trifft daneben: zu dem Zeitpunkt sind
    # Bilder und Abspieler noch nicht da, die Seite waechst danach weiter,
    # und man landet irgendwo in der Mitte. Deshalb sofort, nach dem
    # vollstaendigen Laden, und noch einmal kurz danach fuer alles, was sich
    # verspaetet.
    sprung = ("""<script>
function ansEnde() { window.scrollTo(0, document.documentElement.scrollHeight); }
ansEnde();
window.addEventListener('load', ansEnde);
setTimeout(ansEnde, 300);
setTimeout(ansEnde, 1200);
</script>""" if ans_ende else "")
    neu_laden = (AKTUALISIERUNG % aktualisieren) if aktualisieren else ""
    return sprung + ZEITSKRIPT + neu_laden + "</main></body></html>"


def lampen(erzwingen: bool = False) -> str:
    """Zwei Anzeigen: WhatsApp und das Modell.

    Beide sagen, was gerade wirklich ist -- nicht, was eingetragen wurde. Eine
    Anzeige, die schon bei hinterlegtem Schluessel gruen leuchtet, beruhigt
    genau bis zum ersten Entwurf, der dann scheitert.
    """
    w = whatsapp_probe()
    m = modell_probe(erzwingen)
    mstufe = "gut" if m.get("ok") else (
        "aus" if m.get("stufe") == "aus" else "schlecht")
    alter = m.get("zeit") and time.time() - m["zeit"]
    wann = (T(f"vor {int(alter)} s geprueft", f"checked {int(alter)} s ago") if alter and alter > 2 else T("gerade geprueft","just checked"))

    farbe = {"gut": "bg-emerald-500", "wartet": "bg-amber-500",
             "schlecht": "bg-red-500", "aus": "bg-slate-300"}

    def kachel(stufe: str, titel: str, text: str, fuss_html: str) -> str:
        return f"""<div class="flex-1 min-w-56 rounded-xl border border-slate-200
     bg-white p-4">
 <div class="flex items-center gap-2">
  <span class="w-2.5 h-2.5 rounded-full {farbe.get(stufe, 'bg-slate-300')}"></span>
  <span class="font-medium">{titel}</span>
 </div>
 <p class="text-sm text-slate-700 mt-1">{esc_h(text)}</p>
 <p class="text-xs text-slate-500 mt-1">{fuss_html}</p>
</div>"""

    return f"""<div class="flex flex-wrap gap-3 mb-4">
{kachel(w['stufe'], "WhatsApp", w['text'],
        f'<a class="underline" href="/koppeln">{T("verwalten","manage")}</a>')}
{kachel(mstufe, T("KI-Modell","AI model"), m.get('text') or '',
        f'{esc_h(wann)} &middot; <a class="underline" '
        f'href="/einstellungen?trotzdem=1&amp;pruefen=1">{T("jetzt pruefen","check now")}</a> '
        f'&middot; <a class="underline" href="/einrichtung">{T("Schluessel","Key")}</a>')}
</div>"""

def anmeldeseite(hinweis: str = "") -> HTMLResponse:
    # Bewusst 401 statt 200: ein Suchdienst oder Skript soll die Anmeldemaske
    # nicht fuer die Seite selbst halten.
    return HTMLResponse(f"""{kopf(T("Anmeldung","Sign in"))}
<h1 class="text-xl font-semibold mb-3">{T("Anmeldung","Sign in")}</h1>
<p>{T("Diese Seite zeigt Nachrichtenausschnitte. Zugang nur mit Token.","This page shows message excerpts. Access with a token only.")}</p>
<form method="post" action="/anmeldung">
 <input type="password" name="token" autocomplete="current-password" autofocus
        placeholder="{T('Zugangstoken','Access token')}">
 <button type="submit">{T("Anmelden","Sign in")}</button>
</form>
<p class="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800">{hinweis}</p>{fuss()}""", status_code=401)


@app.post("/anmeldung")
def anmelden(anfrage: Request, token: str = Form("")) -> Response:
    if zugang.gesperrt(anfrage):
        return anmeldeseite(T("Zu viele Fehlversuche. In fuenf Minuten erneut versuchen.","Too many failed attempts. Try again in five minutes."))
    if not zugang.token_stimmt(token):
        zugang.fehlversuch(anfrage)
        return anmeldeseite(T("Token stimmt nicht.","Wrong token."))
    zugang.versuche_loeschen(anfrage)
    antwort = RedirectResponse("/", status_code=303)
    zugang.keks_setzen(antwort, sicher=zugang.ueber_tls(anfrage))
    return antwort


# --- Koppler ----------------------------------------------------------------
#
# Der Koppler (koppler/, eigener Container) haelt eine echte, dauerhaft
# gekoppelte WhatsApp-Web-Sitzung. Er kann den ganzen Verlauf lesen und einen
# Entwurf setzen, ohne dass irgendwo ein Fenster offen sein muss.
#
# Er laeuft nur auf der Schleife und wird von hier aus angesprochen; die
# Weboberflaeche ist der einzige Weg dorthin. Fehlt er, faellt alles auf die
# Erweiterung zurueck -- deshalb wird sein Ausfall nirgends zum Fehler des
# ganzen Dienstes.

KOPPLER_URL = os.environ.get("KOPPLER_URL", "").rstrip("/")
KOPPLER_TOKEN = os.environ.get("KOPPLER_TOKEN", "")


def koppler_da() -> bool:
    return bool(KOPPLER_URL and KOPPLER_TOKEN)


def koppler(pfad: str, rumpf: dict[str, Any] | None = None) -> dict[str, Any]:
    if not koppler_da():
        return {"status": "nicht_eingerichtet"}
    kopf = {"Authorization": f"Bearer {KOPPLER_TOKEN}"}
    try:
        if rumpf is None:
            anfrage = urllib.request.Request(f"{KOPPLER_URL}{pfad}", headers=kopf)
        else:
            anfrage = urllib.request.Request(
                f"{KOPPLER_URL}{pfad}", method="POST",
                data=json.dumps(rumpf).encode(),
                headers=kopf | {"Content-Type": "application/json"})
        with urllib.request.urlopen(anfrage, timeout=30) as a:
            return json.loads(a.read())
    except Exception as e:
        # Ein nicht erreichbarer Koppler ist ein Zustand, kein Absturz.
        return {"status": "nicht_erreichbar", "fehler": str(e)}


def qr_svg(text: str, groesse: int = 280) -> str:
    """QR-Code als SVG, hier erzeugt.

    Zuerst stand hier ein Skript, das die Zeichenbibliothek von einem CDN holte.
    Das lieferte 404 -- der Pfad im Paket stimmte nicht --, und die Seite zeigte
    stumm nichts an. Eine Kernfunktion an ein fremdes CDN zu haengen ist ohnehin
    bruechig: sie faellt dort aus, wo es am unguenstigsten ist, naemlich in einem
    Netz ohne freien Ausgang.
    """
    import qrcode
    from qrcode.image.svg import SvgPathImage

    code = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    code.add_data(text)
    code.make(fit=True)
    bild = code.make_image(image_factory=SvgPathImage)
    roh = bild.to_string(encoding="unicode")
    # Feste Groesse statt der Millimeterangaben, die die Bibliothek setzt.
    roh = re.sub(r'width="[^"]*"', f'width="{groesse}"', roh, count=1)
    roh = re.sub(r'height="[^"]*"', f'height="{groesse}"', roh, count=1)
    return roh


def chat_auswahl(feld: str = "chat") -> str:
    """Auswahlliste der Chats, aus der gekoppelten Sitzung.

    Einen Namen abzutippen war Unfug: Der Koppler kennt die Chats, und ein
    Tippfehler fuehrte stillschweigend ins Leere -- der Name wurde nicht
    gefunden, und die Vormerkung blieb liegen, ohne dass es jemand sah.
    Faellt der Koppler aus, bleibt das Textfeld als Rueckfall.
    """
    stil = ("rounded-lg border border-slate-300 px-3 py-2 text-sm "
            "focus:outline-none focus:ring-2 focus:ring-slate-400")
    chats = koppler("/chats") if koppler_da() else None
    if not isinstance(chats, list) or not chats:
        return (f'<input name="{feld}" placeholder="{T("Chatname","Chat name")}" required '
                f'class="{stil}">')

    karte = namen_karte()
    # Ungelesene nach oben: was gerade wartet, will man meist zuerst.
    geordnet = sorted(chats, key=lambda c: (-(c.get("ungelesen") or 0),
                                            (c.get("name") or "").casefold()))
    zeilen = []
    for c in geordnet:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        merk = f" ({T('Gruppe','group')})" if c.get("gruppe") else ""
        u = c.get("ungelesen") or 0
        merk += f" \u2014 {u} " + T("ungelesen", "unread") if u else ""
        # Der Wert bleibt der rohe Chatname -- er ist die Kennung. Nur die
        # Beschriftung traegt den abgeleiteten Namen dazu.
        zeilen.append(f'<option value="{esc_h(name)}">'
                      f'{esc_h(anzeige(name, karte) + merk)}</option>')
    return (f'<select name="{feld}" required class="{stil} max-w-full">'
            + "".join(zeilen) + "</select>")

def koppel_abschnitt() -> str:
    """Zustand der gekoppelten Sitzung, samt QR-Code wenn noetig."""
    z = koppler("/zustand")
    st = z.get("status", "unbekannt")

    if st == "qr" and z.get("qr"):
        return f"""
<p>{T("WhatsApp auf dem Telefon &rarr; Einstellungen &rarr; Verknuepfte Geraete &rarr; <b>Geraet verknuepfen</b>, dann diesen Code scannen:","WhatsApp on the phone &rarr; Settings &rarr; Linked devices &rarr; <b>Link a device</b>, then scan this code:")}</p>
<div style="margin:1rem 0;background:#fff;display:inline-block;padding:12px">
{qr_svg(z['qr'])}
</div>
<p class="text-sm text-slate-600">{T("Der Code wechselt regelmaessig; die Seite laedt sich selbst neu.","The code changes regularly; the page reloads itself.")}</p>
<script>setTimeout(() => location.reload(), 25000)</script>"""

    if st == "bereit":
        return (f"<p>&#10003; <b>{T('Verbunden','Connected')}</b>"
                + (f" {T('als','as')} {esc_h(z.get('nummer'))}" if z.get("nummer") else "")
                + f".</p><p>{T('Vorgemerkte Texte werden direkt gesetzt &mdash; ohne dass ein WhatsApp-Fenster offen sein muss.','Queued texts are set directly &mdash; no WhatsApp window needs to be open.')}</p>"
                  "<form method=post action='/koppeln-loesen'>"
                  f"<button>{T('Verbindung loesen','Unlink')}</button></form>")

    if st == "nicht_eingerichtet":
        return (f"<p>{T('Kein Koppler vorhanden. Ohne ihn bleibt es bei der Browser-Erweiterung: Entwuerfe entstehen nur fuer den Chat, der gerade offen ist. Das ist kein Fehler, nur weniger.','No connector present. Without it you are left with the browser extension: drafts only for the chat that is currently open. Not an error, just less.')}</p>")

    return (f"<p>{T('Zustand','State')}: <b>{esc_h(st)}</b>"
            + (f" &mdash; {esc_h(z.get('fehler'))}" if z.get("fehler") else "")
            + "</p><script>setTimeout(() => location.reload(), 8000)</script>")


@app.get("/einrichtung", response_class=HTMLResponse)
def einrichtung(anfrage: Request, hinweis: str = "") -> Response:
    """Fragt beim Betreten ab, was noch fehlt.

    Absicht: Der Dienst soll sich selbst in Betrieb nehmen lassen. Wer ihn
    aufsetzt, soll nicht erst eine Umgebungsvariable suchen muessen -- und wer
    ihn spaeter uebernimmt, sieht auf einen Blick, was fehlt.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    anbieter, modell, schluessel = modell_konfig()
    fertig = zugang.gesetzt(schluessel)

    modellteil = (
        f"<p>&#10003; {T('Schluessel hinterlegt','Key stored')} &mdash; {T('Anbieter','Provider')} <b>{esc_h(anbieter)}</b>, "
        f"{T('Modell','Model')} <b>{esc_h(modell)}</b>.</p>"
        if fertig else
        f"<p>{T('Noch kein Schluessel hinterlegt. Ohne ihn kann kein Entwurf entstehen.','No key stored yet. Without it no draft can be created.')}</p>")

    return HTMLResponse(f"""{kopf(T("Einrichtung","Setup"), zurueck="/einstellungen", aktiv="/einstellungen")}
<h1>{T("Einrichtung","Setup")}</h1>
<p class="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800">{esc_h(hinweis)}</p>

<h2>1. {T("Modell","Model")}</h2>
{modellteil}
<form method="post" action="/einrichtung">
 <label>{T("Anbieter","Provider")}
  <select name="anbieter">
   <option value="claude"{' selected' if anbieter == 'claude' else ''}>claude</option>
   <option value="openrouter"{' selected' if anbieter == 'openrouter' else ''}>openrouter</option>
  </select>
 </label>
 <label>{T("Modell","Model")} <input name="modell" value="{esc_h(modell)}" style="width:16rem"></label>
 <label>{T("API-Schluessel","API key")}
  <input name="schluessel" type="password" autocomplete="off"
         placeholder="{T('unveraendert lassen','leave unchanged') if fertig else 'sk-...'}" style="width:24rem">
 </label>
 <label>{T("Arbeitsbereich (nur Claude, nur falls noetig)","Workspace (Claude only, only if needed)")}
  <input name="arbeitsbereich" value="{esc_h(arbeitsbereich())}" style="width:24rem"
         placeholder="{T('leer lassen, solange es ohne geht','leave empty while it works without')}">
 </label>
 <button type="submit">{T("Speichern","Save")}</button>
</form>
<p class="text-sm text-slate-600">{T('Meldet die Schnittstelle <em>"This API key is not scoped to a workspace"</em>, gehoert hier die Workspace-ID aus der Anthropic-Konsole hinein &mdash; oder du legst dort einen Schluessel an, der einem Arbeitsbereich zugeordnet ist. Beides geht.','If the API reports <em>"This API key is not scoped to a workspace"</em>, put the workspace ID from the Anthropic console here &mdash; or create a key there that is bound to a workspace. Either works.')}</p>

<h2>2. {T("WhatsApp verbinden","Connect WhatsApp")}</h2>
{koppel_abschnitt()}

<h2>3. {T("Browser-Erweiterung","Browser extension")}</h2>
<p>{T('Ordner <code>erweiterung/</code> unter <code>chrome://extensions</code> laden (Entwicklermodus &rarr; „Entpackte Erweiterung laden") und dort eintragen:','Load the <code>erweiterung/</code> folder at <code>chrome://extensions</code> (developer mode &rarr; "Load unpacked") and enter there:')}</p>
<ul>
 <li>{T("Adresse","Address")}: <code>{esc_h(str(anfrage.base_url).rstrip('/'))}</code></li>
 <li>{T("Zugangstoken","Access token")}: <code>{esc_h(zugang.zugang_token())}</code></li>
</ul>
<p class="text-sm text-slate-600">{T("Der Token steht hier, weil diese Seite ohnehin nur erreicht, wer sich bereits ausgewiesen hat. Ihn woanders zu suchen, waere nur unbequemer, nicht sicherer.","The token is shown here because only someone already signed in reaches this page. Hiding it elsewhere would be less convenient, not more secure.")}</p>

<p><a href="/?trotzdem=1">{T("Zur Uebersicht","To the overview")} &rarr;</a> &mdash; {T("geht auch, solange noch etwas fehlt; es ist dann nur weniger zu sehen.","works even while something is still missing; there is just less to see.")}</p>{fuss()}""")


@app.post("/einrichtung")
def einrichtung_speichern(anfrage: Request,
                          anbieter: str = Form("claude"),
                          modell: str = Form(""),
                          schluessel: str = Form(""),
                          arbeitsbereich: str = Form("")) -> Response:
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    if anbieter not in ("claude", "openrouter"):
        return einrichtung(anfrage, T("Unbekannter Anbieter.","Unknown provider."))
    zugang.einstellung_schreiben("anbieter", anbieter)
    if modell.strip():
        zugang.einstellung_schreiben("modell", modell.strip())
    # Leeres Feld heisst "nicht anfassen" -- sonst loescht ein versehentliches
    # Speichern den Schluessel, nur weil das Feld aus gutem Grund leer angezeigt
    # wird.
    if schluessel.strip():
        zugang.einstellung_schreiben("api_schluessel", schluessel.strip())
    # Hier ist leer eine gueltige Angabe: wer den Arbeitsbereich wieder
    # herausnimmt, will ihn los sein. Anders als beim Schluessel steht der
    # Wert ja sichtbar im Feld, es geht also nichts unbemerkt verloren.
    zugang.einstellung_schreiben("arbeitsbereich", arbeitsbereich.strip())
    # Nach einer Aenderung soll die Lampe sofort den neuen Stand zeigen und
    # nicht noch fuenf Minuten die alte Auskunft.
    modell_probe(erzwingen=True)
    return RedirectResponse("/einrichtung", status_code=303)


@app.get("/koppeln", response_class=HTMLResponse)
def koppeln(anfrage: Request) -> Response:
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    return HTMLResponse(f"""{kopf(T("WhatsApp verbinden","Connect WhatsApp"), zurueck="/einstellungen", aktiv="/einstellungen")}
<p><a href="/">&larr; {T("Uebersicht","Overview")}</a></p>
<h1>{T("WhatsApp verbinden","Connect WhatsApp")}</h1>
{koppel_abschnitt()}
<p class="text-sm text-slate-600">{T("Hinweis: Eine verbundene Sitzung ist ein inoffizieller Client. Dafuer werden Nummern gesperrt, und sie belegt einen der vier Plaetze fuer verknuepfte Geraete.","Note: a linked session is an unofficial client. Numbers get banned for that, and it takes one of the four linked-device slots.")}</p>{fuss()}""")


@app.post("/koppeln-loesen")
def koppeln_loesen(anfrage: Request) -> Response:
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    koppler("/abmelden", {})
    return RedirectResponse("/koppeln", status_code=303)


def esc_h(s: Any) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --- Wissen automatisch aufbauen --------------------------------------------
#
# Der Dienst ist verbunden und sieht alle Chats -- dann soll er auch von selbst
# lernen, statt auf einen Knopfdruck je Kontakt zu warten.
#
# Begrenzt, nicht entfesselt: Jede Auswertung ist ein Modellaufruf ueber bis zu
# 200 Nachrichten. Deshalb laeuft eine Runde nur ueber wenige Chats, bevorzugt
# die noch nie angesehenen, und ein bereits ausgewerteter Chat kommt erst nach
# Tagen wieder dran. Wer alles sofort will, kann einzelne Chats weiterhin von
# Hand ausloesen.

AUTO_STANDARD = os.environ.get("WISSEN_AUTOMATISCH", "ja").lower() != "nein"
# 15 Minuten und 10 Chats statt drei Stunden und 4.
#
# Die alten Werte stammten aus einer Zeit, in der noch nichts ausgewertet war
# und jede Runde etwas kostete. Inzwischen ist der Bestand durch: naechste_chats
# liefert nur, was NEU ist oder laenger als WISSEN_ERNEUT_NACH_S zurueckliegt.
# Meistens ist die Liste also leer, die Runde kostet dann nichts, und ein neuer
# Chat ist nach Minuten erfasst statt nach Stunden.
AUTO_INTERVALL = float(os.environ.get("WISSEN_INTERVALL_S", "900"))
AUTO_JE_RUNDE = int(os.environ.get("WISSEN_JE_RUNDE", "10"))


def takt(sekunden: float) -> str:
    """Ein Abstand, wie man ihn sagt.

    Die Oberflaeche rechnete fest in Stunden. Bei 900 Sekunden stand dort
    "alle 0 h" -- richtig gerechnet und trotzdem falsch.
    """
    if sekunden < 3600:
        return f"{int(round(sekunden / 60))} min"
    stunden = sekunden / 3600
    return f"{int(stunden)} h" if abs(stunden - round(stunden)) < 0.05 \
        else f"{stunden:.1f} h".replace(".", ",")
AUTO_ERNEUT_NACH = float(os.environ.get("WISSEN_ERNEUT_NACH_S", str(7 * 86400)))


def auto_an() -> bool:
    return zugang.einstellung_lesen("wissen_automatisch",
                                    "ja" if AUTO_STANDARD else "nein") != "nein"


def naechste_chats(profil: str, anzahl: int) -> list[str]:
    """Welche Chats als naechstes drankommen.

    Reihenfolge: nie ausgewertete zuerst, danach die aeltesten. Gruppen sind
    dabei -- sie liefern Zusammenhaenge, die in keinem Einzelchat stehen.
    """
    chats = koppler("/chats")
    if not isinstance(chats, list):
        return []
    with closing(db()) as v:
        gesehen = {z["chat"]: z["zuletzt"] for z in
                   v.execute("SELECT chat, zuletzt FROM wissen_lauf WHERE profil=?", (profil,))}
    offen, alt = [], []
    for c in chats:
        name = (c.get("name") or "").strip()
        # Gruppen sind bewusst dabei. Der frueher hier stehende
        # Ausschluss stammte aus der Annahme, dort schrieben zu viele
        # durcheinander, als dass ueber eine einzelne Person etwas
        # Belastbares herauskaeme. Die Auswertungen des ersten grossen Laufs
        # widersprechen dem: gerade Gruppen liefern Zusammenhaenge, die in
        # keinem Einzelchat stehen -- wer zu wem gehoert, was gerade ansteht.
        #
        # Beim VORTIPPEN bleiben sie aussen vor, das ist etwas anderes: dort
        # landet ein Satz im Eingabefeld, und in einer Gruppe ist der im
        # falschen Moment peinlicher als keiner.
        if not name:
            continue
        wann = gesehen.get(name)
        if wann is None:
            offen.append(name)
        elif time.time() - wann > AUTO_ERNEUT_NACH:
            alt.append((wann, name))
    alt.sort()
    return (offen + [n for _, n in alt])[:anzahl]


def wissen_runde(profil: str = PROFIL_STANDARD) -> dict[str, Any]:
    """Eine Runde: wenige Chats auswerten, Ergebnis festhalten."""
    if not auto_an() or not koppler_da():
        return {"uebersprungen": T("abgeschaltet oder kein Koppler","switched off or no connector")}
    if whatsapp_probe().get("stufe") != "gut":
        return {"uebersprungen": T("WhatsApp nicht verbunden","WhatsApp not connected")}
    if not modell_probe().get("ok"):
        # Ohne funktionierendes Modell waere jede Runde nur eine Reihe von
        # Fehlschlaegen -- und beim naechsten Mal dieselbe.
        return {"uebersprungen": T("Modell antwortet nicht","model not responding")}

    erledigt = []
    for name in naechste_chats(profil, AUTO_JE_RUNDE):
        fehler = None
        gelesen = 0
        try:
            r = wissen_aufbauen(name, profil)
            gelesen = r.get("gelesen", 0)
            erledigt.append({"chat": name, "neu": r.get("neu", 0)})
            # Wird der Chat nur als Nummer gefuehrt, gleich nach dem Namen
            # sehen -- der Verlauf ist ohnehin schon gelesen worden. Ein
            # Fehlschlag hier darf die Runde nicht kippen; ohne Namen laeuft
            # alles weiter, es steht dann eben die Nummer da.
            if ist_nummer(name):
                try:
                    name_ermitteln(name)
                except Exception as e:
                    print(f"[name] {name}: {e}", flush=True)
        except HTTPException as e:
            fehler = str(e.detail)[:200]
            erledigt.append({"chat": name, "fehler": fehler})
        except Exception as e:
            fehler = str(e)[:200]
            erledigt.append({"chat": name, "fehler": fehler})
        # Auch ein Fehlschlag wird vermerkt. Sonst haengt die Runde beim
        # naechsten Mal am selben Chat fest und kommt nie weiter.
        with closing(db()) as v:
            v.execute("""INSERT INTO wissen_lauf (profil, chat, zuletzt, gelesen, fehler)
                         VALUES (?,?,?,?,?)
                         ON CONFLICT(profil, chat) DO UPDATE SET
                           zuletzt=excluded.zuletzt, gelesen=excluded.gelesen,
                           fehler=excluded.fehler""",
                      (profil, name, time.time(), gelesen, fehler))
            v.commit()
    return {"ausgewertet": erledigt}


# --- Automatisches Vortippen ------------------------------------------------
#
# Der Dienst sucht selbst nach Chats, in denen die Gegenseite zuletzt
# geschrieben hat, laesst einen Entwurf formulieren und schreibt ihn ueber den
# Koppler ins Eingabefeld. Von dort synchronisiert WhatsApp ihn aufs Telefon.
#
# Gesendet wird nach wie vor nichts. Der Entwurf steht da, bis ein Mensch ihn
# abschickt, aendert oder loescht.

VORTIPPEN_STANDARD = os.environ.get("VORTIPPEN", "ja").lower() != "nein"
VORTIPPEN_INTERVALL = float(os.environ.get("VORTIPPEN_INTERVALL_S", "900"))
VORTIPPEN_JE_RUNDE = int(os.environ.get("VORTIPPEN_JE_RUNDE", "5"))
# Wie viele Nachrichten als Zusammenhang mitgegeben werden.
VORTIPPEN_KONTEXT = int(os.environ.get("VORTIPPEN_KONTEXT", "15"))


def vortippen_an() -> bool:
    return zugang.einstellung_lesen(
        "vortippen", "ja" if VORTIPPEN_STANDARD else "nein") != "nein"


def vortippen_kandidaten(profil: str, anzahl: int) -> list[dict[str, Any]]:
    """Chats, in denen eine Antwort aussteht und noch keine vorgetippt ist.

    Bedingungen, alle drei notwendig:
      * kein Gruppenchat -- dort schreiben viele durcheinander, und ein
        vorgetippter Satz im falschen Moment ist peinlicher als keiner;
      * die GEGENSEITE hat zuletzt geschrieben;
      * seit dem letzten Vortippen ist eine neue Nachricht gekommen.

    Die zweite Bedingung ist hier nur ein VORFILTER. letzte_von_mir kommt aus
    chat.lastMessage, und das baut whatsapp-web.js aus lastReceivedKey --
    verlaesslich ist es nicht. Die Gegenprobe macht vortippen_runde am echten
    Verlauf, den sie ohnehin holt.

    Die juengsten zuerst: was gerade hereinkam, will man am ehesten
    beantworten.
    """
    chats = koppler("/chats")
    if not isinstance(chats, list):
        return []

    with closing(db()) as v:
        gesehen = {z["chat"]: z["letzte_zeit"] for z in
                   v.execute("SELECT chat, letzte_zeit FROM entwurf_lauf WHERE profil=?",
                             (profil,))}

    offen = []
    for c in chats:
        name = (c.get("name") or "").strip()
        if not name or c.get("gruppe"):
            continue
        if c.get("letzte_von_mir") is not False:
            # None heisst: der Koppler kennt die letzte Nachricht nicht.
            # Dann lieber nichts tun, als ins Blaue zu tippen.
            continue
        zeit = c.get("letzte_zeit")
        if not zeit:
            continue
        if gesehen.get(name) is not None and zeit <= gesehen[name]:
            continue
        offen.append({"name": name, "id": c.get("id"), "zeit": float(zeit)})

    offen.sort(key=lambda c: -c["zeit"])
    return offen[:anzahl]


def vortippen_runde(profil: str = PROFIL_STANDARD) -> dict[str, Any]:
    """Eine Runde: wenige Chats, je ein Entwurf, ins Feld geschrieben."""
    if not vortippen_an() or not koppler_da():
        return {"uebersprungen": T("abgeschaltet oder kein Koppler","switched off or no connector")}
    if whatsapp_probe().get("stufe") != "gut":
        return {"uebersprungen": T("WhatsApp nicht verbunden","WhatsApp not connected")}
    if not modell_probe().get("ok"):
        return {"uebersprungen": T("Modell antwortet nicht","model not responding")}

    erledigt = []
    for k in vortippen_kandidaten(profil, VORTIPPEN_JE_RUNDE):
        name, kid = k["name"], k["id"]
        ergebnis = None
        try:
            roh = koppler(f"/nachrichten?chat={urllib.parse.quote(kid)}"
                          f"&anzahl={VORTIPPEN_KONTEXT}")
            if not isinstance(roh, list) or not roh:
                ergebnis = T("kein Verlauf","no history")
                raise RuntimeError(ergebnis)

            # Ueber medien_als_text: Sprachnachrichten und Bilder kommen
            # als Abschrift beziehungsweise Beschreibung mit hinein, statt
            # ersatzlos zu fehlen.
            nachrichten = medien_als_text(roh, name)
            if not nachrichten:
                ergebnis = T("nur Nachrichten ohne Text","only messages without text")
                raise RuntimeError(ergebnis)

            # Gegenprobe am echten Verlauf, VOR dem Modellaufruf.
            #
            # letzte_von_mir aus /chats taugt nur als Vorfilter: das Feld
            # stammt aus chat.lastMessage, und das baut whatsapp-web.js aus
            # lastReceivedKey. Im Trockenlauf aufgefallen -- ein Chat wurde
            # als "Gegenseite zuletzt" ausgewaehlt, obwohl die letzten zwei
            # Nachrichten die eigenen waren.
            #
            # Hier kostet die Pruefung nichts: die Nachrichten liegen schon
            # vor, und ein gesparter Modellaufruf ist ein gesparter
            # Modellaufruf.
            if nachrichten[-1].von_mir:
                ergebnis = T("zuletzt von mir -- nichts zu beantworten","last one was mine -- nothing to answer")
                raise RuntimeError(ergebnis)

            text = modell_fragen(
                SYSTEM_AUTO, prompt_bauen(nachrichten, name, profil, None)).strip()
            if not text or text == "KEIN_ENTWURF":
                ergebnis = T("Modell gab keinen Entwurf","model gave no draft")
                raise RuntimeError(ergebnis)

            # Erst ablegen, dann setzen: geht das Setzen schief, ist der
            # Entwurf trotzdem festgehalten und die Erweiterung kann ihn beim
            # naechsten Oeffnen des Chats holen.
            with closing(db()) as v:
                v.execute(
                    "INSERT INTO entwuerfe (zeit, profil, chat, kontext, entwurf) "
                    "VALUES (?,?,?,?,?)",
                    (time.time(), profil, name,
                     json.dumps([n.model_dump() for n in nachrichten],
                                ensure_ascii=False), text))
                v.commit()

            r = koppler("/entwurf", {"chat": kid, "text": text,
                                     "nur_wenn_leer": True})
            if isinstance(r, dict) and r.get("ok"):
                ergebnis = "getippt"
            else:
                # Auch das belegte Feld landet hier. Kein Fehler -- der
                # Entwurf liegt in der Tabelle und wartet.
                ergebnis = str((r or {}).get("grund") or r)[:120]
        except Exception as e:
            ergebnis = ergebnis or str(getattr(e, "detail", e))[:120]

        with closing(db()) as v:
            v.execute("""INSERT INTO entwurf_lauf
                           (profil, chat, letzte_zeit, zuletzt, ergebnis)
                         VALUES (?,?,?,?,?)
                         ON CONFLICT(profil, chat) DO UPDATE SET
                           letzte_zeit=excluded.letzte_zeit,
                           zuletzt=excluded.zuletzt,
                           ergebnis=excluded.ergebnis""",
                      (profil, name, k["zeit"], time.time(), ergebnis))
            v.commit()
        erledigt.append({"chat": name, "ergebnis": ergebnis})

    return {"vorgetippt": erledigt}


async def vortippen_schleife() -> None:
    # Derselbe Vorlauf wie bei der Wissensrunde: der Koppler braucht nach dem
    # Start eine Weile, bis Chromium steht und die Sitzung verbunden ist.
    await asyncio.sleep(120)
    while True:
        try:
            await asyncio.to_thread(vortippen_runde)
        except Exception as e:
            print(f"[vortippen] Runde fehlgeschlagen: {e}", flush=True)
        await asyncio.sleep(VORTIPPEN_INTERVALL)


async def wissen_schleife() -> None:
    # Etwas Vorlauf: der Koppler braucht nach dem Start eine Weile, bis
    # Chromium steht und die Sitzung verbunden ist.
    await asyncio.sleep(90)
    while True:
        try:
            # In einem Thread, weil die Modellaufrufe blockierend sind und
            # sonst die ganze Oberflaeche stillstuende.
            await asyncio.to_thread(wissen_runde)
        except Exception as e:
            print(f"[wissen] Runde fehlgeschlagen: {e}", flush=True)
        await asyncio.sleep(AUTO_INTERVALL)


# --- Sprachnachrichten abschreiben ------------------------------------------
#
# WhatsApp liefert die Aufnahme, abgeschrieben wird hier -- mit Whisper, das
# quelloffen ist und lokal laeuft. Ein Dienst eines Dritten haette bedeutet,
# private Sprachnachrichten dorthin zu schicken; das waere der falsche Preis
# fuer eine Bequemlichkeit.
#
# Auf Anforderung, nie im Vorbeigehen: eine Minute Aufnahme braucht auf
# dieser Maschine einige Sekunden Rechenzeit, und beim Durchblaettern eines
# Verlaufs will die niemand fuer jede Nachricht aufwenden.

WHISPER_MODELL = os.environ.get("WHISPER_MODELL", "base")
WHISPER_SPRACHE = os.environ.get("WHISPER_SPRACHE", "") or None
_whisper = None


def whisper_modell():
    """Das Modell, einmal geladen und dann behalten.

    Erst beim ersten Gebrauch: sonst kostete jeder Neustart des Dienstes die
    Ladezeit und den Speicher, auch wenn nie jemand eine Abschrift anfordert.
    Die Gewichte liegen unter /daten, damit ein neuer Container sie nicht
    erneut herunterlaedt.
    """
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(
            WHISPER_MODELL, device="cpu", compute_type="int8",
            download_root=os.path.join(os.path.dirname(DB_PFAD), "whisper"))
    return _whisper


def abschrift_lesen(wa_id: str) -> sqlite3.Row | None:
    with closing(db()) as v:
        return v.execute("SELECT text, sekunden, art FROM abschriften WHERE wa_id=?",
                         (wa_id,)).fetchone()


def bild_beschreiben(wa_id: str, chat: str) -> str:
    """Ein Bild beschreiben und die Beschreibung behalten."""
    vorhanden = abschrift_lesen(wa_id)
    if vorhanden:
        return vorhanden["text"]

    rohdaten, typ = medien_holen(wa_id)
    if not typ.startswith("image/"):
        raise HTTPException(409, T("kein Bild","not an image"))
    begonnen = time.time()
    text = (modell_bild(rohdaten, typ) or "").strip() or T("(nichts erkennbar)","(nothing recognisable)")
    with closing(db()) as v:
        v.execute("""INSERT INTO abschriften (wa_id, chat, text, sekunden,
                                              erstellt, art)
                     VALUES (?,?,?,?,?,'bild')
                     ON CONFLICT(wa_id) DO UPDATE SET text=excluded.text""",
                  (wa_id, chat, text, time.time() - begonnen, time.time()))
        v.commit()
    return text


# Wie viele Medien beim Bauen eines Entwurfs hoechstens NEU ausgewertet
# werden. Vorhandenes kostet nichts, Neues kostet Rechenzeit und einen
# Modellaufruf -- ohne Grenze koennte ein Chat mit dreissig Bildern einen
# einzelnen Entwurf unerwartet teuer machen.
MEDIEN_JE_ENTWURF = int(os.environ.get("MEDIEN_JE_ENTWURF", "4"))


def medien_als_text(roh: list[dict[str, Any]], chat: str,
                    hoechstens_neu: int = MEDIEN_JE_ENTWURF,
                    bilder: bool = True) -> list["Nachricht"]:
    """Den Verlauf fuer das Modell aufbereiten.

    Sprachnachrichten und Bilder haben keinen Text und fielen bisher ganz
    heraus -- das Modell sah einen Verlauf mit Luecken und entwarf darauf.
    Wer ein Bild schickt und dann "und?" schreibt, bekam eine Antwort, die
    das "und?" nicht einordnen konnte.

    Vorhandene Abschriften und Beschreibungen kosten nichts. Neue werden nur
    fuer die juengsten Medien erzeugt und nur bis zur Obergrenze: der
    Zusammenhang der letzten Nachrichten traegt den Entwurf, ein Bild von
    vorgestern nicht.
    """
    vorhanden = {}
    with closing(db()) as v:
        for z in v.execute("SELECT wa_id, text, art FROM abschriften WHERE chat=?",
                           (chat,)):
            vorhanden[z["wa_id"]] = (z["text"], z["art"])

    # Von hinten nach vorn neu auswerten: das Juengste ist das Wichtigste.
    neu_erzeugt = 0
    for n in reversed(roh):
        wa = n.get("id")
        typ = n.get("typ")
        if not wa or wa in vorhanden or (n.get("text") or "").strip():
            continue
        if neu_erzeugt >= hoechstens_neu:
            continue
        try:
            if typ in ("ptt", "audio"):
                vorhanden[wa] = (abschrift_erzeugen(wa, chat), "sprache")
                neu_erzeugt += 1
            elif typ == "image" and bilder:
                vorhanden[wa] = (bild_beschreiben(wa, chat), "bild")
                neu_erzeugt += 1
        except Exception as e:
            # Ein Medium, das sich nicht auswerten laesst, darf den Entwurf
            # nicht verhindern -- dann fehlt eben dieses eine.
            print(f"[medien] {wa}: {getattr(e, 'detail', e)}", flush=True)

    nachrichten = []
    for n in roh:
        text = (n.get("text") or "").strip()
        wa = n.get("id")
        if wa in vorhanden:
            beschreibung, art = vorhanden[wa]
            marke = T("Sprachnachricht","Voice message") if art == "sprache" else T("Bild","Image")
            # Die Herkunft bleibt sichtbar: das Modell soll wissen, dass der
            # Satz nicht getippt, sondern gesprochen oder gezeigt wurde.
            text = f"[{marke}: {beschreibung}]" + (f" {text}" if text else "")
        if not text:
            continue
        nachrichten.append(Nachricht(von_mir=bool(n.get("von_mir")), text=text))
    return nachrichten


def abschrift_erzeugen(wa_id: str, chat: str) -> str:
    """Aufnahme holen, abschreiben, ablegen. Gibt den Text zurueck."""
    vorhanden = abschrift_lesen(wa_id)
    if vorhanden:
        return vorhanden["text"]

    m = koppler(f"/medien?id={urllib.parse.quote(wa_id)}")
    if not isinstance(m, dict) or not m.get("daten"):
        raise HTTPException(502, str((m or {}).get("fehler") or
                                     T("Aufnahme nicht abrufbar","recording not retrievable")))

    # Ueber eine Datei und nicht ueber den Speicher: faster-whisper liest mit
    # PyAV, und das will einen Pfad oder einen Datenstrom. Eine temporaere
    # Datei ist der kuerzeste Weg, der beides zuverlaesslich kann.
    import base64
    import tempfile
    rohdaten = base64.b64decode(m["daten"])
    begonnen = time.time()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=True) as datei:
        datei.write(rohdaten)
        datei.flush()
        segmente, _ = whisper_modell().transcribe(
            datei.name, language=WHISPER_SPRACHE, vad_filter=True)
        text = " ".join(s.text.strip() for s in segmente).strip()
    dauer = time.time() - begonnen

    if not text:
        text = T("(nichts Verstaendliches erkannt)","(nothing intelligible recognised)")
    with closing(db()) as v:
        v.execute("""INSERT INTO abschriften (wa_id, chat, text, sekunden, erstellt)
                     VALUES (?,?,?,?,?)
                     ON CONFLICT(wa_id) DO UPDATE SET text=excluded.text""",
                  (wa_id, chat, text, dauer, time.time()))
        v.commit()
    print(f"[abschrift] {wa_id} in {dauer:.1f}s, {len(text)} Zeichen", flush=True)
    return text


# --- Gespraeche lesen -------------------------------------------------------
#
# Warum der Dienst eine eigene Chatansicht bekommt: Der Entwurf sollte im
# WhatsApp-Eingabefeld landen. Das geht nicht -- WhatsApps Entwurfsfunktion
# arbeitet nicht mit Begleitgeraeten, und was der Koppler in seiner Sitzung
# setzt, sieht niemand (siehe Kopf von koppler/server.js).
#
# Also andersherum: wenn der Vorschlag nicht zum Menschen kommt, kommt der
# Mensch zum Vorschlag. Verlauf, Wissen und Entwurf an einer Stelle, und die
# Seite ist ohnehin fuers Telefon gebaut.

# Was nicht Text ist, hat keinen body -- ohne diese Uebersetzung bliebe eine
# Sprachnachricht eine leere Zeile, und der Verlauf saehe lueckenhaft aus,
# obwohl nichts fehlt.
TYP_TEXT = {
    "ptt": "🎤 " + T("Sprachnachricht","Voice message"),
    "audio": "🎵 Audio",
    "image": "📷 " + T("Bild","Image"),
    "video": "🎬 Video",
    "document": "📄 " + T("Dokument","Document"),
    "sticker": "🏷 Sticker",
    "location": "📍 " + T("Standort","Location"),
    "vcard": "👤 " + T("Kontakt","Contact"),
    "revoked": "🚫 " + T("geloescht","deleted"),
    "e2e_notification": "",
    "notification_template": "",
}


def nachricht_text(n: dict[str, Any]) -> str:
    """Was von einer Nachricht anzuzeigen ist."""
    t = (n.get("text") or "").strip()
    if t:
        return t
    return TYP_TEXT.get(n.get("typ") or "", f"[{n.get('typ') or T('ohne Text','no text')}]")


def zeit_kurz(ts: Any) -> str:
    """Ein Zeitstempel, den der BROWSER formatiert.

    Der Server steht auf Europe/Zurich, das Telefon aber nicht immer: in
    Thailand waere jede Uhrzeit fuenf Stunden daneben, und genau dort gibt es
    Standorte. Die Sekunde seit 1970 ist eindeutig, die Darstellung nicht --
    also liefert der Server die Zahl und der Browser macht daraus, was bei
    ihm gilt.

    Der Text im Element bleibt als Rueckfall stehen, fuer den Fall, dass
    kein Skript laeuft. Er zeigt dann die Zeit des Servers.
    """
    try:
        sekunden = float(ts)
    except (TypeError, ValueError):
        return ""
    lesbar = time.strftime("%d.%m. %H:%M", time.localtime(sekunden))
    return f'<time data-ts="{int(sekunden)}">{lesbar}</time>'


# Formatiert alle <time data-ts> nach den Einstellungen des Browsers.
# Steht in jeder Seite, weil Zeitstempel ueberall vorkommen.
ZEITSKRIPT = """<script>
(function () {
  var heute = new Date().toDateString();
  document.querySelectorAll('time[data-ts]').forEach(function (e) {
    var d = new Date(Number(e.dataset.ts) * 1000);
    if (isNaN(d)) return;
    var uhr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    // Heutiges nur mit Uhrzeit -- das Datum sagt dann nichts.
    e.textContent = d.toDateString() === heute
      ? uhr
      : d.toLocaleDateString([], { day: '2-digit', month: '2-digit' }) + ' ' + uhr;
    e.title = d.toLocaleString();
  });
})();
</script>"""


@app.get("/", response_class=HTMLResponse)
def chats_seite(anfrage: Request, archiv: int = 0) -> Response:
    """Die Startseite IST die Chatliste.

    Vorher lag hier die Verwaltung -- Modell, Kopplung, Token, eine Tabelle
    aller Entwuerfe. Das ist, was man einmal einrichtet, nicht was man
    tut. Wer den Dienst oeffnet, will sehen, wo eine Antwort aussteht.
    Die Verwaltung liegt jetzt unter /einstellungen.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()

    chats = koppler("/chats") if koppler_da() else None
    if not isinstance(chats, list):
        return HTMLResponse(kopf(T("Chats","Chats"), aktiv="/") + f"""
<div class="rounded-xl border border-amber-300 bg-amber-50 p-4">
 <p class="font-medium">{T("Keine gekoppelte Sitzung.","No linked session.")}</p>
 <p class="text-sm text-slate-600 mt-1">{T("Ohne sie gibt es keine Chats zu zeigen.","Without one there are no chats to show.")}</p>
 <a href="/koppeln" class="inline-block mt-3 px-4 py-2 rounded-lg
    bg-slate-900 text-white text-sm font-medium">{T("Jetzt verbinden","Connect now")}</a>
</div>""" + fuss())

    karte = namen_karte()
    # Archivierte sind weggeraeumt, nicht geloescht -- sie erscheinen nur,
    # wenn man sie sehen will.
    sichtbar = [c for c in chats if bool(c.get("archiviert")) == bool(archiv)]
    anzahl_archiv = sum(1 for c in chats if c.get("archiviert"))
    geordnet = sorted(sichtbar, key=lambda c: (-(c.get("ungelesen") or 0),
                                               -(c.get("letzte_zeit") or 0)))
    zeilen = []
    for c in geordnet[:120]:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        u = c.get("ungelesen") or 0
        kennzeichen = (f'<span class="ml-2 shrink-0 rounded-full bg-emerald-600 '
                       f'text-white text-xs font-semibold px-2 py-0.5">{u}</span>'
                       if u else "")
        gruppe = (f'<span class="ml-2 shrink-0 rounded-md bg-slate-100 '
                  f'text-slate-600 text-xs px-1.5 py-0.5">{T("Gruppe","Group")}</span>'
                  if c.get("gruppe") else "")
        wartet = c.get("letzte_von_mir") is False
        # Ein Punkt statt eines Worts: "wartet auf Antwort" in jeder Zeile
        # waere Laerm, aber genau das will man auf einen Blick sehen.
        punkt = (f'<span class="shrink-0 w-2 h-2 rounded-full bg-emerald-500"'
                 f' title="{T("wartet auf deine Antwort","waiting for your reply")}"></span>' if wartet
                 else '<span class="shrink-0 w-2 h-2"></span>')
        # loading="lazy": nur was sichtbar wird, wird geholt. Bei 120 Chats
        # waeren es sonst 120 Aufrufe in die WhatsApp-Seite hinein, jedes Mal.
        # width und height als Attribute, nicht nur als Klasse: faellt das
        # Stilblatt aus oder ist es veraltet, steht hier trotzdem ein Bild
        # von 36 Pixeln statt eines bildschirmfuellenden Katzenfotos.
        bild = (f'<img src="/profilbild?chat={esc_h(c.get("id") or "")}"'
                f' loading="lazy" alt="" width="36" height="36"'
                f' class="shrink-0 w-9 h-9 rounded-full object-cover'
                f' bg-slate-200"'
                f' onerror="this.style.visibility=&quot;hidden&quot;">')
        zeilen.append(f"""<li>
 <a href="/gespraech?id={esc_h(c.get('id') or '')}"
    class="flex items-center gap-3 px-3 py-3 hover:bg-slate-50 active:bg-slate-100">
  {punkt}{bild}
  <span class="min-w-0 flex-1">
   <span class="block font-medium truncate">{esc_h(anzeige(name, karte))}</span>
   <span class="block text-xs text-slate-500">{zeit_kurz(c.get('letzte_zeit'))}</span>
  </span>{gruppe}{kennzeichen}
 </a>
 <form method="post" action="/archiv" class="px-3 pb-2 -mt-1">
  <input type="hidden" name="id" value="{esc_h(c.get('id') or '')}">
  <input type="hidden" name="zurueck" value="{'ja' if archiv else ''}">
  <button class="text-[11px] text-slate-400 hover:text-slate-700"
     >{T("zurueckholen","unarchive") if archiv else T("archivieren","archive")}</button>
 </form></li>""")

    # Kein Ausgangsbuch auf der Hauptseite.
    #
    # Hier stand einmal ein Block "Von hier gesendet" mit den
    # letzten zehn Nachrichten. Gedacht war er als Nachweis: wer eine
    # Maschine in seinem Namen schreiben laesst, soll sehen, was sie gesagt
    # hat. Nur geht ohne einen Druck auf Senden nichts raus, und was raus
    # ist, steht im Chat. Der Block hat also nichts gezeigt, was nicht
    # ohnehin zu sehen war -- und stand dafuer ueber allem anderen.
    #
    # Die Tabelle "gesendet" bleibt. Sie kostet keine Aufmerksamkeit und
    # traegt die Herkunft (Vorschlag, Termin, selbst getippt), die WhatsApp
    # selbst nicht kennt.

    offen = sum(1 for c in chats
                if not c.get("gruppe") and c.get("letzte_von_mir") is False)
    hinweis = (f'<p class="text-sm text-slate-600 mb-3">{offen} '
               + T("Chats warten auf eine Antwort.","chats awaiting a reply.")
               + '</p>' if offen else "")

    kopfzeile = f"""<form method="get" action="/suche" class="mb-3">
 <input name="q" placeholder="{T('Chat oder Nachricht suchen','Search chat or message')}&hellip;"
    class="w-full rounded-lg border border-slate-300 px-3 py-2
           focus:outline-none focus:ring-2 focus:ring-slate-400">
</form>
<p class="text-sm mb-3">
 <a class="underline text-slate-600" href="/{'' if archiv else '?archiv=1'}"
   >{T("zu den aktiven Chats","back to active chats") if archiv else f'{T("Archiv","Archive")} ({anzahl_archiv})'}</a>
</p>"""

    return HTMLResponse(kopf(T("Archiv","Archive") if archiv else T("Chats","Chats"), aktiv="/")
                        + kopfzeile + hinweis + f"""
<ul class="rounded-xl border border-slate-200 bg-white divide-y divide-slate-100
           overflow-hidden">{''.join(zeilen) or
 f'<li class="p-4 text-slate-500">{T("Keine Chats.","No chats.")}</li>'}</ul>"""
                        + fuss(aktualisieren=20))


@app.get("/gespraech", response_class=HTMLResponse)
def gespraech(anfrage: Request, id: str, anzahl: int = 40,
              entwurf: str = "", fehler: str = "",
              t_titel: str = "", t_beginn: str = "", t_ende: str = "",
              t_ort: str = "", t_text: str = "", zitat: str = "") -> Response:
    """Ein Verlauf, das Wissen dazu, und ein Feld zum Antworten.

    entwurf und fehler kommen von /vorschlag zurueck und stehen in der
    Adresse, nicht in einer Sitzung: eine Seite, die sich neu laden laesst
    und dabei dasselbe zeigt, ist leichter zu verstehen.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    if not koppler_da():
        return HTMLResponse(kopf(T("Gespraech","Conversation"), zurueck="/") +
                            f'<p class="text-red-700">{T("Kein Koppler.","No connector.")}</p>' + fuss())

    roh = koppler(f"/nachrichten?chat={urllib.parse.quote(id)}&anzahl={anzahl}")
    chats = koppler("/chats")
    dieser = next((c for c in chats if c.get("id") == id), {}) \
        if isinstance(chats, list) else {}
    name = (dieser.get("name") or id).strip()
    karte = namen_karte()
    titel = anzeige(name, karte)

    if not isinstance(roh, list):
        return HTMLResponse(
            kopf(titel, zurueck="/") +
            f'<p class="text-red-700">{esc_h(str(roh))}</p>' + fuss())

    # Alle Abschriften dieses Chats auf einmal, nicht je Nachricht einzeln.
    with closing(db()) as v:
        abschriften = {z["wa_id"]: z["text"] for z in
                       v.execute("SELECT wa_id, text FROM abschriften WHERE chat=?",
                                 (name,))}

    blasen = []
    for n in roh:
        eigen = n.get("von_mir")
        stil = ("ml-auto bg-eigen border-eigen-rand" if eigen
                else "mr-auto bg-white border-slate-200")

        # Medien. Bilder holt der Browser selbst und erst beim Hinscrollen;
        # alles andere bleibt ein Verweis, damit ein Verlauf nicht unbemerkt
        # dreissig Megabyte nachlaedt.
        zusatz = ""
        wa = n.get("id")
        typ = n.get("typ")
        if wa and typ in ("image", "sticker"):
            gross = "max-h-40" if typ == "sticker" else "max-h-80"
            zusatz += (f'<a href="/medien?wa_id={esc_h(wa)}" target="_blank">'
                       f'<img src="/medien?wa_id={esc_h(wa)}" loading="lazy"'
                       f' alt="{T("Bild","Image")}" class="mt-1 rounded-lg {gross} w-auto">'
                       f'</a>')
        elif wa and typ in ("video", "document"):
            zusatz += (f'<p class="mt-1"><a class="text-sm underline" '
                       f'href="/medien?wa_id={esc_h(wa)}" target="_blank">'
                       f'{T("Video","Video") if typ == "video" else T("Dokument","Document")} '
                       f'{T("oeffnen","open")}</a></p>')

        # Sprachnachricht: abspielbar, dazu die Abschrift oder ein Knopf dafuer.
        if n.get("typ") in ("ptt", "audio") and n.get("id"):
            # preload="none": erst laden, wenn jemand auf Abspielen drueckt.
            # Sonst zoege ein Verlauf mit zwanzig Sprachnachrichten beim
            # Oeffnen alle zwanzig durch den Koppler.
            zusatz += (f'<audio controls preload="none" class="mt-1 w-full '
                       f'max-w-72 h-9" src="/medien?wa_id={esc_h(n["id"])}">'
                       f'</audio>')
            gesprochen = abschriften.get(n["id"])
            if gesprochen:
                zusatz += (f'<p class="mt-1.5 pt-1.5 border-t border-black/10 '
                          f'text-sm italic whitespace-pre-wrap break-words">'
                          f'{esc_h(gesprochen)}</p>')
            else:
                zusatz += f"""<form method="post" action="/transkribieren" class="mt-1">
 <input type="hidden" name="id" value="{esc_h(id)}">
 <input type="hidden" name="wa_id" value="{esc_h(n['id'])}">
 <button class="text-xs underline text-slate-600 hover:text-slate-900"
    >{T("abschreiben","transcribe")}</button></form>"""

        # Bei einem Bild ist m.body die Bildunterschrift. Fehlt sie, waere
        # "Bild" ueber dem Bild nur Wiederholung -- dann bleibt die Zeile weg.
        beschriftung = (n.get("text") or "").strip()
        zeige_text = beschriftung or not zusatz
        textzeile = (f'<p class="whitespace-pre-wrap break-words">'
                     f'{esc_h(nachricht_text(n))}</p>' if zeige_text else "")

        # Vorhandene Reaktionen, klein unter der Blase.
        reaktionen = "".join(
            f'<span class="text-sm">{esc_h(r)}</span>'
            for r in (n.get("reaktionen") or []))
        reaktion_html = (f'<div class="-mt-1 mb-1 {"text-right" if eigen else ""}">'
                         f'{reaktionen}</div>' if reaktionen else "")

        # Handgriffe je Nachricht. Bewusst klein und grau: sie sollen da
        # sein, wenn man sie sucht, und sonst nicht stoeren.
        griffe = ""
        if wa:
            knopf_stil = "text-[11px] text-slate-400 hover:text-slate-700"
            teile_g = [
                f'<a href="/gespraech?id={esc_h(id)}&zitat={esc_h(wa)}"'
                f' class="{knopf_stil}">{T("antworten","reply")}</a>']
            for e in ("👍", "❤️", "😂"):
                teile_g.append(
                    f'<form method="post" action="/reaktion" class="inline">'
                    f'<input type="hidden" name="id" value="{esc_h(id)}">'
                    f'<input type="hidden" name="wa_id" value="{esc_h(wa)}">'
                    f'<input type="hidden" name="emoji" value="{e}">'
                    f'<button class="{knopf_stil}">{e}</button></form>')
            if eigen:
                teile_g.append(
                    f'<form method="post" action="/nachricht" class="inline">'
                    f'<input type="hidden" name="id" value="{esc_h(id)}">'
                    f'<input type="hidden" name="wa_id" value="{esc_h(wa)}">'
                    f'<input type="hidden" name="was" value="loeschen">'
                    f'<button class="{knopf_stil}">{T("loeschen","delete")}</button></form>')
            griffe = (f'<div class="flex gap-2 items-center mt-1 '
                      f'{"justify-end" if eigen else ""}">'
                      + "".join(teile_g) + "</div>")

        zitat_hinweis = (f'<p class="text-[11px] text-slate-500 border-l-2 '
                         f'border-slate-300 pl-2 mb-1">{T("Antwort auf eine Nachricht","Reply to a message")}</p>'
                         if n.get("hat_zitat") else "")

        blasen.append(f"""<div class="max-w-[85%] rounded-2xl border {stil}
     px-3.5 py-2 shadow-sm">
 {zitat_hinweis}
 {textzeile}
 {zusatz}
 <p class="text-[11px] text-slate-500 mt-0.5">{zeit_kurz(n.get('zeit'))}</p>
 {griffe}
</div>{reaktion_html}""")

    bekannt = wissen_lesen(name, PROFIL_STANDARD)
    aus_gruppen = wissen_aus_gruppen(name, PROFIL_STANDARD)
    wissen_html = ""
    if bekannt or aus_gruppen:
        punkte = "".join(
            f'<li class="py-1"><span class="rounded bg-slate-100 text-xs '
            f'px-1.5 py-0.5 mr-1.5">{esc_h(bereich_anzeige(z["bereich"]))}</span>'
            f'{esc_h(z["aussage"])}</li>' for z in bekannt)
        punkte += "".join(
            f'<li class="py-1"><span class="rounded bg-slate-100 text-xs '
            f'px-1.5 py-0.5 mr-1.5">{esc_h(bereich_anzeige(z["bereich"]))}</span>'
            f'{esc_h(z["aussage"])} <span class="text-xs text-slate-500">'
            f'{T("aus","from")} {esc_h(z["quelle"])}</span></li>' for z in aus_gruppen)
        wissen_html = f"""<details class="mb-4 rounded-xl border border-slate-200
     bg-white px-4 py-3">
 <summary class="cursor-pointer font-medium text-sm">{T("Bekannt ueber diesen Chat","Known about this chat")}
  ({len(bekannt) + len(aus_gruppen)})</summary>
 <ul class="mt-2 text-sm divide-y divide-slate-100">{punkte}</ul>
</details>"""

    # Zitat: zeigt, worauf geantwortet wird, mit einem Weg zurueck.
    zitat_karte = ""
    if zitat:
        zitierter = next((x for x in roh if x.get("id") == zitat), None)
        auszug = (nachricht_text(zitierter)[:160] if zitierter else T("Nachricht","Message"))
        zitat_karte = f"""<div class="mt-3 rounded-lg border-l-4 border-slate-400
     bg-white px-3 py-2 flex items-start gap-2">
 <span class="flex-1 text-sm text-slate-700">{T("Antwort auf:","Replying to:")}
  <em>{esc_h(auszug)}</em></span>
 <a href="/gespraech?id={esc_h(id)}" class="text-slate-400 hover:text-slate-700"
    aria-label="{T('Zitat entfernen','Remove quote')}">&times;</a>
</div>"""

    # Ein erkannter Termin: aenderbar, und erst dann eine der beiden Aktionen.
    # Das Modell kann sich im Datum irren, und ein falscher Termin im Chat
    # laesst sich nicht zurueckholen.
    termin_html = ""
    if t_beginn:
        felder = urllib.parse.urlencode({
            "t_titel": t_titel, "t_beginn": t_beginn, "t_ende": t_ende,
            "t_ort": t_ort, "t_text": t_text})
        f_stil = ("rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm "
                  "focus:outline-none focus:ring-2 focus:ring-slate-400")
        termin_html = f"""<div class="mt-4 rounded-xl border border-sky-300
     bg-sky-50 p-4">
 <p class="font-medium mb-2">{T("Termin erkannt","Appointment detected")}</p>
 <form method="post" action="/termin-whatsapp" class="grid gap-2">
  <input type="hidden" name="id" value="{esc_h(id)}">
  <input name="t_titel" value="{esc_h(t_titel)}" required class="{f_stil}">
  <div class="flex flex-wrap gap-2">
   <input type="datetime-local" name="t_beginn" value="{esc_h(t_beginn)}"
      required class="{f_stil}">
   <input type="datetime-local" name="t_ende" value="{esc_h(t_ende)}"
      class="{f_stil}">
  </div>
  <input name="t_ort" value="{esc_h(t_ort)}" placeholder="{T('Ort','Place')}"
     class="{f_stil}">
  <input name="t_text" value="{esc_h(t_text)}" placeholder="{T('Notiz','Note')}"
     class="{f_stil}">
  <div class="flex flex-wrap items-center gap-2 mt-1">
   <button class="px-4 py-2 rounded-lg bg-sky-700 text-white text-sm
      font-medium hover:bg-sky-800">{T("In WhatsApp anlegen","Create in WhatsApp")}</button>
   <a href="/termin.ics?{felder}" class="px-4 py-2 rounded-lg border
      border-slate-300 bg-white text-sm hover:bg-slate-50">{T(".ics laden","Download .ics")}</a>
   <span class="text-xs text-slate-600 ml-auto">{T("Anlegen ist eine Nachricht an den Chat.","Creating it sends a message to the chat.")}</span>
  </div>
 </form>
</div>"""

    warnung = (f"""<div class="mb-3 rounded-lg border border-red-300 bg-red-50
     px-3 py-2 text-sm text-red-800">{esc_h(fehler)}</div>""" if fehler else "")

    return HTMLResponse(kopf(titel, zurueck="/") + wissen_html + f"""
<p class="text-center mb-2"><a class="text-sm underline text-slate-600"
 href="/gespraech?id={urllib.parse.quote(id)}&anzahl={anzahl + 60}"
 >{T("aeltere Nachrichten laden","load older messages")}</a></p>
<div class="flex flex-col gap-2 mb-5">{''.join(blasen) or
 f'<p class="text-slate-500">{T("Keine Nachrichten.","No messages.")}</p>'}</div>
{warnung}
{zitat_karte}
<form method="post" action="/senden"
      class="sticky bottom-0 bg-slate-50 pt-2 pb-3 border-t border-slate-200">
 <input type="hidden" name="id" value="{esc_h(id)}">
 <input type="hidden" name="zitat_id" value="{esc_h(zitat)}">
 <input type="hidden" name="quelle" value="{'vorschlag' if entwurf else 'selbst'}">
 <textarea name="text" rows="3" required placeholder="{T('Antwort schreiben','Write a reply')}&hellip;"
    class="w-full rounded-xl border border-slate-300 px-3 py-2
           focus:outline-none focus:ring-2 focus:ring-slate-400"
 >{esc_h(entwurf or '')}</textarea>
 <div class="flex items-center gap-2 mt-2">
  <button type="submit" class="px-4 py-2 rounded-lg bg-emerald-600 text-white
     font-medium hover:bg-emerald-700">{T("Senden","Send")}</button>
  <button type="submit" form="vorschlag-formular"
     class="px-4 py-2 rounded-lg border border-slate-300 bg-white
            hover:bg-slate-50">{T("Vorschlag holen","Get suggestion")}</button>
  <span class="text-xs text-slate-500 ml-auto">{T("Eigener Text geht direkt raus, ein Vorschlag wird vorher gezeigt.","Your own text goes straight out; a suggestion is shown first.")}</span>
 </div>
</form>
<form method="post" action="/vorschlag" id="vorschlag-formular">
 <input type="hidden" name="id" value="{esc_h(id)}">
</form>

<form method="post" action="/senden-medien" enctype="multipart/form-data"
      class="flex flex-wrap items-center gap-2 mt-3">
 <input type="hidden" name="id" value="{esc_h(id)}">
 <input type="file" name="datei" required
    class="text-sm file:mr-2 file:px-3 file:py-1.5 file:rounded-lg
           file:border file:border-slate-300 file:bg-white file:text-sm">
 <input name="text" placeholder="{T('Bildunterschrift','Caption')}"
    class="rounded-lg border border-slate-300 px-3 py-1.5 text-sm flex-1
           min-w-40">
 <button class="px-3 py-1.5 rounded-lg border border-slate-300 bg-white
    text-sm hover:bg-slate-50">{T("Datei senden","Send file")}</button>
</form>
{termin_html}
<div class="flex flex-wrap items-center gap-4 mt-3">
 <form method="post" action="/termin-erkennen">
  <input type="hidden" name="id" value="{esc_h(id)}">
  <button class="text-sm underline text-slate-600 hover:text-slate-900"
     >{T("Termin im Verlauf suchen","Find appointment in history")}</button>
 </form>
 <form method="post" action="/gelesen">
  <input type="hidden" name="id" value="{esc_h(id)}">
  <button class="text-sm underline text-slate-600 hover:text-slate-900"
     >{T("als gelesen markieren","mark as read")}</button>
 </form>
</div>""" + fuss(ans_ende=True, aktualisieren=15))


# --- Termine ----------------------------------------------------------------
#
# Wird im Gespraech etwas abgemacht, soll daraus ein Termin werden koennen --
# als WhatsApp-Ereignis im Chat und als .ics zum Einlesen in einen Kalender.
#
# Beides auf Anforderung. Ein Ereignis ist eine NACHRICHT: alle im Chat sehen
# sie. Das darf nichts automatisch tun.

TERMIN_SYSTEM = """Du liest die letzten Nachrichten eines Chats und pruefst,
ob darin ein konkreter Termin VEREINBART wurde.

Gib ausschliesslich JSON zurueck:
{"gefunden": true|false, "titel": "...", "beginn": "YYYY-MM-DDTHH:MM",
 "ende": "YYYY-MM-DDTHH:MM"|null, "ort": "..."|null, "beschreibung": "..."|null}

Regeln:
- "gefunden": false, wenn nichts Konkretes abgemacht wurde. Ein Vorschlag
  ohne Zustimmung, ein "muessen wir mal" oder eine offene Frage ist KEIN
  Termin.
- Datum und Uhrzeit muessen aus dem Verlauf hervorgehen. Relative Angaben
  ("morgen", "naechsten Dienstag") rechnest du anhand des genannten
  Bezugsdatums aus.
- Fehlt die Uhrzeit, nimm 09:00 und schreibe das in die Beschreibung.
- "ende" nur, wenn es dasteht. Sonst null.
- Der Titel ist kurz und sagt, worum es geht -- kein ganzer Satz."""


def termin_erkennen(chat_name: str, nachrichten: list[Nachricht]) -> dict[str, Any]:
    """Das Modell nach einem vereinbarten Termin fragen.

    Mit dem heutigen Datum im Prompt: ohne Bezugspunkt kann es "naechsten
    Dienstag" nicht ausrechnen, und ein Termin im falschen Jahr ist
    schlimmer als keiner.
    """
    heute = time.strftime("%A, %d.%m.%Y", time.localtime())
    verlauf = "\n".join(f"{T('ich','me') if n.von_mir else chat_name}: {n.text}"
                         for n in nachrichten)[:20000]
    roh = modell_fragen(TERMIN_SYSTEM,
                        T(f"Heute ist {heute}.\n\nVerlauf mit {chat_name}:\n\n{verlauf}", f"Today is {heute}.\n\nHistory with {chat_name}:\n\n{verlauf}"),
                        hoechstens=500)
    text = re.sub(r"^```[a-zA-Z]*\s*", "", (roh or "").strip())
    text = re.sub(r"\s*```$", "", text).strip()
    treffer = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(treffer.group(0) if treffer else text)
    except Exception:
        raise HTTPException(502, f"Antwort war kein JSON: {roh[:160]}")


def ics_bauen(titel: str, beginn: str, ende: str = "", ort: str = "",
              beschreibung: str = "") -> str:
    """Ein Kalendereintrag nach RFC 5545.

    Von Hand zusammengesetzt statt mit einer Bibliothek: es sind zwoelf
    Zeilen, und eine Abhaengigkeit mehr fuer zwoelf Zeilen lohnt nicht.

    Die Zeiten stehen OHNE Zeitzone (Ortszeit). Ein Termin, der im Chat
    "19:30" heisst, soll im Kalender 19:30 heissen -- wer ihn mitnimmt, ist
    ohnehin am selben Ort.
    """
    def zeit(s: str) -> str:
        return (s or "").replace("-", "").replace(":", "")[:15].ljust(15, "0")

    def entschaerfen(s: str) -> str:
        # Komma, Semikolon und Backslash haben in .ics Bedeutung.
        return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))

    kennung = hashlib.sha256(f"{titel}{beginn}".encode()).hexdigest()[:24]
    zeilen = [
        "BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//wa-gehilfe//wa-gehilfe//{SPRACHE.upper()}",
        "BEGIN:VEVENT",
        f"UID:{kennung}@{KALENDER_DOMAIN}",
        f"DTSTAMP:{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}",
        f"DTSTART:{zeit(beginn)}",
    ]
    if ende:
        zeilen.append(f"DTEND:{zeit(ende)}")
    zeilen.append(f"SUMMARY:{entschaerfen(titel)}")
    if ort:
        zeilen.append(f"LOCATION:{entschaerfen(ort)}")
    if beschreibung:
        zeilen.append(f"DESCRIPTION:{entschaerfen(beschreibung)}")
    zeilen += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(zeilen) + "\r\n"


# --- Medien ausliefern ------------------------------------------------------
#
# Bilder holt der Browser selbst, ueber ein <img src>. Das ist der Grund,
# warum das hier ein GET ist und kein Knopf: so laedt jedes Bild genau dann,
# wenn es gebraucht wird, und mit loading="lazy" erst beim Hinscrollen.
#
# Zwischengespeichert wird auf der Platte. Eine Aufnahme oder ein Bild
# aendert sich nicht mehr, und jedes Neuladen des Verlaufs erneut durch den
# Koppler und die WhatsApp-Server zu gehen waere Verschwendung -- und
# langsam.

MEDIEN_VERZEICHNIS = os.path.join(os.path.dirname(DB_PFAD), "medien")

ENDUNGEN = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "video/mp4": ".mp4", "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
}


def medien_holen(wa_id: str) -> tuple[bytes, str]:
    """Die Datei einer Nachricht, aus dem Zwischenspeicher oder frisch."""
    os.makedirs(MEDIEN_VERZEICHNIS, exist_ok=True)
    # Die Kennung enthaelt Zeichen, die in Dateinamen nichts verloren haben.
    sicher = hashlib.sha256(wa_id.encode()).hexdigest()[:32]
    for vorhanden in os.listdir(MEDIEN_VERZEICHNIS):
        if vorhanden.startswith(sicher):
            pfad = os.path.join(MEDIEN_VERZEICHNIS, vorhanden)
            typ = next((m for m, e in ENDUNGEN.items()
                        if vorhanden.endswith(e)), "application/octet-stream")
            with open(pfad, "rb") as d:
                return d.read(), typ

    m = koppler(f"/medien?id={urllib.parse.quote(wa_id)}")
    if not isinstance(m, dict) or not m.get("daten"):
        raise HTTPException(502, str((m or {}).get("fehler") or
                                     T("Medien nicht abrufbar","media not retrievable")))
    import base64
    rohdaten = base64.b64decode(m["daten"])
    typ = (m.get("mimetyp") or "application/octet-stream").split(";")[0]
    with open(os.path.join(MEDIEN_VERZEICHNIS,
                           sicher + ENDUNGEN.get(typ, ".bin")), "wb") as d:
        d.write(rohdaten)
    return rohdaten, typ


PROFIL_VERZEICHNIS = os.path.join(os.path.dirname(DB_PFAD), "profilbilder")


@app.get("/sw.js")
def service_worker() -> Response:
    """Der Service Worker, von der Wurzel aus.

    Ein Service Worker darf nur den Pfad steuern, unter dem er ausgeliefert
    wird. Von /statisch/ aus waere die App nicht in seinem Geltungsbereich,
    und der Browser boete "zum Startbildschirm hinzufuegen" nicht an.

    Ohne Anmeldung: er enthaelt nichts Privates, und der Browser holt ihn,
    bevor eine Sitzung besteht.
    """
    pfad = os.path.join(_PWA, "sw.js")
    if not os.path.exists(pfad):
        return Response(status_code=404)
    with open(pfad, "rb") as d:
        return Response(d.read(), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/profilbild")
def profilbild(anfrage: Request, chat: str) -> Response:
    """Das Profilbild eines Chats, zwischengespeichert.

    WhatsApp gibt nur eine Adresse heraus, die nach kurzer Zeit verfaellt.
    Der Dienst laedt das Bild deshalb selbst und legt es ab -- sonst
    muesste der Browser die Adresse holen duerfen, und der Aufruf ginge
    an fremde Server vorbei an uns.

    Sieben Tage alt ist die Grenze: Profilbilder aendern sich selten, aber
    nicht nie.
    """
    if not zugang.angemeldet(anfrage):
        return Response(status_code=401)

    os.makedirs(PROFIL_VERZEICHNIS, exist_ok=True)
    sicher = hashlib.sha256(chat.encode()).hexdigest()[:32] + ".jpg"
    pfad = os.path.join(PROFIL_VERZEICHNIS, sicher)
    if os.path.exists(pfad) and time.time() - os.path.getmtime(pfad) < 7 * 86400:
        with open(pfad, "rb") as d:
            return Response(d.read(), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

    antwort_k = koppler(f"/profilbild?chat={urllib.parse.quote(chat)}")
    adresse = (antwort_k or {}).get("url") if isinstance(antwort_k, dict) else None
    if not adresse:
        return Response(status_code=404)
    try:
        with urllib.request.urlopen(adresse, timeout=15) as a:
            rohdaten = a.read()
    except Exception:
        return Response(status_code=404)
    with open(pfad, "wb") as d:
        d.write(rohdaten)
    return Response(rohdaten, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/medien")
def medien(anfrage: Request, wa_id: str) -> Response:
    """Eine Datei aus einem Chat ausliefern."""
    if not zugang.angemeldet(anfrage):
        return Response(status_code=401)
    try:
        rohdaten, typ = medien_holen(wa_id)
    except HTTPException as e:
        return Response(str(e.detail), status_code=e.status_code,
                        media_type="text/plain")
    # Der Browser darf es behalten: die Datei aendert sich nicht mehr.
    return Response(rohdaten, media_type=typ,
                    headers={"Cache-Control": "private, max-age=604800"})


@app.post("/reaktion")
def reaktion(anfrage: Request, id: str = Form(...), wa_id: str = Form(...),
             emoji: str = Form("")) -> Response:
    """Auf eine Nachricht reagieren. Leerer Text nimmt die Reaktion weg."""
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    r = koppler("/reaktion", {"id": wa_id, "emoji": emoji})
    ziel = f"/gespraech?id={urllib.parse.quote(id)}"
    if not (isinstance(r, dict) and r.get("ok")):
        grund = str((r or {}).get("fehler") or r)[:160]
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(grund)}",
                                status_code=303)
    return RedirectResponse(ziel, status_code=303)


@app.post("/nachricht")
def nachricht_aendern(anfrage: Request, id: str = Form(...), wa_id: str = Form(...),
                      was: str = Form(...), text: str = Form(""),
                      bestaetigt: str = Form("")) -> Response:
    """Eigene Nachricht loeschen oder bearbeiten.

    Loeschen wird bestaetigt, Bearbeiten nicht: das eine ist endgueltig, das
    andere laesst sich wiederholen.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    ziel = f"/gespraech?id={urllib.parse.quote(id)}"

    if was == "loeschen" and bestaetigt != "ja":
        return bestaetigungsseite(
            T("Nachricht loeschen","Delete message"), "/nachricht",
            {"id": id, "wa_id": wa_id, "was": was},
            T("Diese Nachricht wird fuer alle geloescht. Das laesst sich nicht rueckgaengig machen.",
              "This message will be deleted for everyone. This cannot be undone."), ziel, knopf=T("Ja, loeschen","Yes, delete"))

    r = koppler("/nachricht", {"id": wa_id, "was": was, "text": text})
    if not (isinstance(r, dict) and r.get("ok")):
        grund = str((r or {}).get("fehler") or r)[:160]
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(grund)}",
                                status_code=303)
    return RedirectResponse(ziel, status_code=303)


@app.post("/archiv")
def archiv(anfrage: Request, id: str = Form(...), zurueck: str = Form("")) -> Response:
    """Chat archivieren oder zurueckholen."""
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    koppler("/archiv", {"chat": id, "zurueck": bool(zurueck)})
    return RedirectResponse("/", status_code=303)


@app.post("/senden-medien")
async def senden_medien(anfrage: Request) -> Response:
    """Eine Datei in den Chat schicken.

    Mehrteilig statt JSON, weil ein Datei-Auswahlfeld nun einmal so sendet.
    Die Datei geht nie auf die Platte: sie wird gelesen, kodiert und
    weitergereicht.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    import base64
    formular = await anfrage.form()
    id = str(formular.get("id") or "")
    text = str(formular.get("text") or "")
    datei = formular.get("datei")
    ziel = f"/gespraech?id={urllib.parse.quote(id)}"
    if not id or datei is None or not getattr(datei, "filename", ""):
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(T('keine Datei','no file'))}", status_code=303)

    rohdaten = await datei.read()
    if len(rohdaten) > 32 * 1024 * 1024:
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(T('Datei zu gross (max. 32 MB)','file too large (max. 32 MB)'))}",
                                status_code=303)

    r = koppler("/senden-medien", {
        "chat": id, "daten": base64.b64encode(rohdaten).decode(),
        "mimetyp": datei.content_type or "application/octet-stream",
        "dateiname": datei.filename, "text": text})
    if not (isinstance(r, dict) and r.get("ok")):
        grund = str((r or {}).get("fehler") or r)[:160]
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(grund)}",
                                status_code=303)

    chats = koppler("/chats")
    name = next((c.get("name") for c in chats if c.get("id") == id), id) \
        if isinstance(chats, list) else id
    with closing(db()) as v:
        v.execute("""INSERT INTO gesendet (zeit, profil, chat, text, quelle, wa_id)
                     VALUES (?,?,?,?,'datei',?)""",
                  (time.time(), PROFIL_STANDARD, name,
                   f"[{T('Datei','File')}: {datei.filename}] {text}".strip(), r.get("id")))
        v.commit()
    return RedirectResponse(ziel, status_code=303)


def _ziffern(wert: str) -> str:
    """Nur die Ziffern -- damit +41 79, 0041-79 und 4179 dasselbe finden."""
    return re.sub(r"\D", "", wert or "")


def chat_passt(c: dict, frage: str, karte: dict[str, str]) -> bool:
    """Passt dieser Chat zur Suchfrage?

    Zwei getrennte Wege, und das ist kein Zierrat. Eine Nummernsuche darf
    nicht als Textsuche ueber die Kennung laufen: "12" steckt in fast jeder
    Telefonnummer und haette die halbe Liste zurueckgegeben.

    Bei Nummern zaehlen nur Ziffern, damit +41 79, 0041 79 und 079 dasselbe
    finden. Die fuehrende 0 ist dabei die Verkehrsausscheidungsziffer: im
    Chat steht 41790000000, getippt wird 079 000 00 00 -- ohne die 0 passt
    der Rest als Teilstueck.
    """
    n = frage.strip().lower()
    if not n:
        return False

    namen = [anzeige(c.get("name") or "", karte), c.get("name") or ""]
    kennung = c.get("id") or ""

    if re.fullmatch(r"[\d\s+()/.-]+", n):
        z = _ziffern(n)
        if len(z) < 3:          # sonst passt jede Nummer auf jede
            return False
        varianten = {z}
        if z.startswith("00"):
            varianten.add(z[2:])
        elif z.startswith("0"):
            varianten.add(z[1:])
        ziele = [_ziffern(x) for x in namen + [kennung]]
        return any(v in ziel for v in varianten for ziel in ziele if ziel)

    return any(n in f.lower() for f in namen + [kennung])


@app.get("/suche", response_class=HTMLResponse)
def suche(anfrage: Request, q: str = "", chat: str = "") -> Response:
    """Suche ueber Chats und Verlaeufe.

    Zwei Fragen in einem Feld, und die haeufigere zuerst: "wo ist der Chat
    mit X" wird oefter gestellt als "wer hat wann Y geschrieben". Die
    Chat-Liste zeigt ausserdem nur die 120 juengsten -- wer laenger nicht
    geschrieben hat, ist ueberhaupt nur hier zu finden.

    Mit gesetztem chat ist es eine Suche IM Gespraech; dann waere eine
    Chat-Liste daneben nur Ablenkung und bleibt weg.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()

    feld = ("w-full rounded-lg border border-slate-300 px-3 py-2 "
            "focus:outline-none focus:ring-2 focus:ring-slate-400")
    im_chat = bool(chat)
    formular = f"""<form method="get" action="/suche" class="mb-4">
 <input type="hidden" name="chat" value="{esc_h(chat)}">
 <input name="q" value="{esc_h(q)}" autofocus
    placeholder="{T('In diesem Chat suchen','Search in this chat') if im_chat
                  else T('Name, Nummer oder Nachricht','Name, number or message')}&hellip;"
    class="{feld}">
</form>"""

    if not q.strip():
        return HTMLResponse(kopf(T("Suche","Search"), zurueck="/") + formular + fuss())

    karte = namen_karte()
    chats = koppler("/chats")
    if not isinstance(chats, list):
        chats = []
    namen = {c.get("id"): c.get("name") for c in chats}

    # --- Chats ---------------------------------------------------------
    chat_html = ""
    if not im_chat:
        passende = [c for c in chats if chat_passt(c, q, karte)]
        passende.sort(key=lambda c: -(c.get("letzte_zeit") or 0))
        if passende:
            zs = "".join(f"""<li>
 <a href="/gespraech?id={esc_h(c.get('id') or '')}"
    class="flex items-center gap-3 px-3 py-2.5 hover:bg-slate-50
           active:bg-slate-100">
  <img src="/profilbild?chat={esc_h(c.get('id') or '')}" loading="lazy" alt=""
     width="36" height="36"
     class="shrink-0 w-9 h-9 rounded-full object-cover bg-slate-200"
     onerror="this.style.visibility=&quot;hidden&quot;">
  <span class="min-w-0 flex-1">
   <span class="block font-medium truncate"
      >{esc_h(anzeige(c.get('name') or '', karte))}</span>
   <span class="block text-xs text-slate-500"
      >{zeit_kurz(c.get('letzte_zeit'))}</span>
  </span>
  {f'<span class="ml-2 shrink-0 rounded-md bg-slate-100 text-slate-600 text-xs px-1.5 py-0.5">{T("Gruppe","Group")}</span>' if c.get('gruppe') else ''}
  {f'<span class="ml-2 shrink-0 rounded-md bg-slate-100 text-slate-500 text-xs px-1.5 py-0.5">{T("Archiv","Archive")}</span>' if c.get('archiviert') else ''}
 </a></li>""" for c in passende[:25])
            mehr = (f'<li class="px-3 py-2 text-xs text-slate-500">{T("und","and")} '
                    f'{len(passende) - 25} {T("weitere","more")}</li>'
                    if len(passende) > 25 else "")
            chat_html = (
                f'<p class="text-xs font-medium uppercase tracking-wide '
                f'text-slate-500 mb-1">{T("Chats","Chats")} ({len(passende)})</p>'
                f'<ul class="mb-5 rounded-xl border border-slate-200 bg-white '
                f'divide-y divide-slate-100 overflow-hidden">{zs}{mehr}</ul>')

    # --- Nachrichten ---------------------------------------------------
    treffer = koppler(f"/suche?q={urllib.parse.quote(q)}"
                      + (f"&chat={urllib.parse.quote(chat)}" if chat else ""))
    if not isinstance(treffer, list):
        nachrichten_html = (f'<p class="text-red-700">{esc_h(str(treffer))}</p>')
        anzahl = 0
    else:
        anzahl = len(treffer)
        zeilen = "".join(f"""<li class="py-2">
 <a href="/gespraech?id={esc_h(m.get('chat') or '')}"
    class="block hover:bg-slate-50 rounded-lg px-2 py-1">
  <span class="text-xs text-slate-500">
   {esc_h(anzeige(namen.get(m.get('chat')) or m.get('chat') or '', karte))}
   &middot; {zeit_kurz(m.get('zeit'))}</span>
  <span class="block">{esc_h((m.get('text') or '')[:200])}</span>
 </a></li>""" for m in treffer)
        if not zeilen:
            zeilen = (f'<li class="py-3 text-slate-500">{T("keine Nachricht mit diesem Wort","no message with that word")}</li>')
        nachrichten_html = (
            f'<p class="text-xs font-medium uppercase tracking-wide '
            f'text-slate-500 mb-1">{T("Nachrichten","Messages")} ({anzahl})</p>'
            f'<ul class="rounded-xl border border-slate-200 bg-white '
            f'divide-y divide-slate-100 px-2">{zeilen}</ul>')

    leer = ("" if chat_html or anzahl else
            f'<p class="text-sm text-slate-600 mb-3">{T("Kein Chat und keine Nachricht dazu gefunden.","No chat and no message found for that.")}</p>')

    return HTMLResponse(kopf(f'{T("Suche","Search")}: {q}', zurueck="/") + formular
                        + leer + chat_html + nachrichten_html + fuss())


@app.post("/gelesen")
def gelesen(anfrage: Request, id: str = Form(...)) -> Response:
    """Den Chat als gelesen markieren.

    Schickt nichts an den Gespraechspartner: sendSeen setzt den eigenen
    Zaehler zurueck. Ohne das bleibt ein Chat in der Liste als ungelesen
    stehen, auch wenn man ihn hier gerade durchgelesen hat.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    koppler("/gelesen", {"chat": id})
    return RedirectResponse(f"/gespraech?id={urllib.parse.quote(id)}",
                            status_code=303)


@app.post("/termin-erkennen")
def termin_erkennen_formular(anfrage: Request, id: str = Form(...)) -> Response:
    """Im Verlauf nach einem vereinbarten Termin suchen."""
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()

    ziel = f"/gespraech?id={urllib.parse.quote(id)}"
    try:
        roh = koppler(f"/nachrichten?chat={urllib.parse.quote(id)}&anzahl=30")
        if not isinstance(roh, list) or not roh:
            raise RuntimeError(T("kein Verlauf","no history"))
        nachrichten = medien_als_text(roh, name)
        chats = koppler("/chats")
        name = next((c.get("name") for c in chats if c.get("id") == id), id) \
            if isinstance(chats, list) else id

        d = termin_erkennen(name, nachrichten)
        if not d.get("gefunden") or not d.get("beginn"):
            return RedirectResponse(
                f"{ziel}&fehler={urllib.parse.quote(T('Kein konkreter Termin gefunden.','No concrete appointment found.'))}",
                status_code=303)

        teile = urllib.parse.urlencode({
            "t_titel": d.get("titel") or T("Termin","Appointment"),
            "t_beginn": d.get("beginn") or "",
            "t_ende": d.get("ende") or "",
            "t_ort": d.get("ort") or "",
            "t_text": d.get("beschreibung") or "",
        })
        return RedirectResponse(f"{ziel}&{teile}", status_code=303)
    except Exception as e:
        grund = str(getattr(e, "detail", e))[:200]
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(grund)}",
                                status_code=303)


def _epoche(s: str) -> int | None:
    """"2026-09-25T19:30" als Sekunden seit 1970, in Ortszeit."""
    try:
        return int(time.mktime(time.strptime(s[:16], "%Y-%m-%dT%H:%M")))
    except (ValueError, TypeError):
        return None


@app.post("/termin-whatsapp")
def termin_whatsapp(anfrage: Request, id: str = Form(...),
                    t_titel: str = Form(...), t_beginn: str = Form(...),
                    t_ende: str = Form(""), t_ort: str = Form(""),
                    t_text: str = Form(""), bestaetigt: str = Form("")) -> Response:
    """Den Termin als WhatsApp-Ereignis in den Chat stellen.

    Das ist eine Nachricht -- alle im Chat sehen sie und koennen zusagen.
    Deshalb dieselbe Bestaetigung wie beim Senden, und protokolliert wird es
    ebenso.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()

    ziel = f"/gespraech?id={urllib.parse.quote(id)}"
    beginn = _epoche(t_beginn)
    if not beginn:
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(T('Beginn unverstaendlich','start time not understood'))}",
                                status_code=303)

    if bestaetigt != "ja":
        chats_v = koppler("/chats")
        name_v = next((c.get("name") for c in chats_v if c.get("id") == id), id) \
            if isinstance(chats_v, list) else id
        wann = time.strftime("%A, %d.%m.%Y um %H:%M", time.localtime(beginn))
        vorschau = f"{T('Termin','Appointment')}: {t_titel}\n{wann}"
        if t_ort:
            vorschau += f"\n{T('Ort','Place')}: {t_ort}"
        if t_text:
            vorschau += f"\n{t_text}"
        vorschau += T("\n\nAlle im Chat sehen diesen Termin und koennen zusagen.","\n\nEveryone in the chat sees this appointment and can accept.")
        return bestaetigungsseite(
            T(f"Termin anlegen bei {anzeige(name_v)}", f"Create appointment with {anzeige(name_v)}"), "/termin-whatsapp",
            {"id": id, "t_titel": t_titel, "t_beginn": t_beginn,
             "t_ende": t_ende, "t_ort": t_ort, "t_text": t_text},
            vorschau, ziel, knopf=T("Ja, anlegen","Yes, create"))

    r = koppler("/termin", {"chat": id, "name": t_titel, "beginn": beginn,
                            "ende": _epoche(t_ende), "ort": t_ort,
                            "beschreibung": t_text})
    if not (isinstance(r, dict) and r.get("ok")):
        grund = str((r or {}).get("fehler") or r)[:200]
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(grund)}",
                                status_code=303)

    chats = koppler("/chats")
    name = next((c.get("name") for c in chats if c.get("id") == id), id) \
        if isinstance(chats, list) else id
    with closing(db()) as v:
        v.execute("""INSERT INTO gesendet (zeit, profil, chat, text, quelle, wa_id)
                     VALUES (?,?,?,?,'termin',?)""",
                  (time.time(), PROFIL_STANDARD, name,
                   T(f"Termin: {t_titel} am {t_beginn}", f"Appointment: {t_titel} on {t_beginn}"), r.get("id")))
        v.commit()
    return RedirectResponse(ziel, status_code=303)


@app.get("/termin.ics")
def termin_ics(anfrage: Request, t_titel: str = "", t_beginn: str = "",
               t_ende: str = "", t_ort: str = "", t_text: str = "") -> Response:
    """Denselben Termin zum Einlesen in einen beliebigen Kalender."""
    if not zugang.angemeldet(anfrage):
        return Response(status_code=401)
    if not t_beginn:
        return Response(T("Beginn fehlt","start missing"), status_code=400, media_type="text/plain")
    t_titel = t_titel or T("Termin","Appointment")
    datei = re.sub(r"[^\w.-]", "_", t_titel)[:40] or "termin"
    return Response(
        ics_bauen(t_titel, t_beginn, t_ende, t_ort, t_text),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{datei}.ics"'})


@app.post("/transkribieren")
def transkribieren(anfrage: Request, id: str = Form(...),
                   wa_id: str = Form(...)) -> Response:
    """Eine einzelne Sprachnachricht abschreiben.

    Auf Anforderung, weil es Rechenzeit kostet. Einmal abgeschrieben, bleibt
    es stehen -- die Aufnahme aendert sich nicht mehr.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()

    ziel = f"/gespraech?id={urllib.parse.quote(id)}"
    chats = koppler("/chats")
    name = next((c.get("name") for c in chats if c.get("id") == id), id) \
        if isinstance(chats, list) else id
    try:
        abschrift_erzeugen(wa_id, name)
        return RedirectResponse(ziel, status_code=303)
    except Exception as e:
        grund = str(getattr(e, "detail", e))[:200]
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(grund)}",
                                status_code=303)


@app.post("/vorschlag")
def vorschlag(anfrage: Request, id: str = Form(...)) -> Response:
    """Einen Entwurf fuer diesen Chat erzeugen und ins Formular legen.

    Derselbe Weg wie beim automatischen Vortippen, nur von Hand ausgeloest --
    und ohne die Bedingung, dass die Gegenseite zuletzt geschrieben haben
    muss. Wer hier drueckt, will eine Formulierung, egal wer zuletzt dran war.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()

    ziel = f"/gespraech?id={urllib.parse.quote(id)}"
    try:
        roh = koppler(f"/nachrichten?chat={urllib.parse.quote(id)}"
                      f"&anzahl={VORTIPPEN_KONTEXT}")
        if not isinstance(roh, list) or not roh:
            raise RuntimeError(T("kein Verlauf","no history"))
        nachrichten = medien_als_text(roh, name)
        if not nachrichten:
            raise RuntimeError(T("keine Textnachrichten als Zusammenhang","no text messages for context"))

        chats = koppler("/chats")
        name = next((c.get("name") for c in chats if c.get("id") == id), id) \
            if isinstance(chats, list) else id

        text = modell_fragen(
            SYSTEM_AUTO, prompt_bauen(nachrichten, name, PROFIL_STANDARD, None)).strip()
        if not text or text == "KEIN_ENTWURF":
            raise RuntimeError(T("Modell gab keinen Entwurf","model gave no draft"))

        with closing(db()) as v:
            v.execute("INSERT INTO entwuerfe (zeit, profil, chat, kontext, entwurf) "
                      "VALUES (?,?,?,?,?)",
                      (time.time(), PROFIL_STANDARD, name,
                       json.dumps([n.model_dump() for n in nachrichten],
                                  ensure_ascii=False), text))
            v.commit()
        return RedirectResponse(f"{ziel}&entwurf={urllib.parse.quote(text)}",
                                status_code=303)
    except Exception as e:
        grund = str(getattr(e, "detail", e))[:200]
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(grund)}",
                                status_code=303)


def bestaetigungsseite(titel: str, ziel_pfad: str, felder: dict[str, str],
                       vorschau: str, zurueck: str,
                       knopf: str | None = None) -> HTMLResponse:
    """Zwischenschritt vor allem, was den Chat verlaesst.

    Ein Klick auf "Senden" ging vorher sofort raus. Bei etwas, das sich nicht
    zurueckholen laesst -- eine Nachricht an einen Menschen, ein Ereignis,
    das alle im Chat sehen -- ist das ein Klick zu wenig. Hier steht
    schwarz auf weiss, WAS an WEN geht, bevor es geht.
    """
    if knopf is None:
        knopf = T("Ja, senden", "Yes, send")
    versteckt = "".join(
        f'<input type="hidden" name="{esc_h(k)}" value="{esc_h(v)}">'
        for k, v in felder.items())
    return HTMLResponse(kopf(titel, zurueck=zurueck) + f"""
<div class="rounded-xl border border-amber-300 bg-amber-50 p-4">
 <p class="font-medium">{T("Das geht gleich raus:","This is about to go out:")}</p>
 <div class="mt-2 rounded-lg bg-white border border-slate-200 p-3
      whitespace-pre-wrap break-words">{esc_h(vorschau)}</div>
 <form method="post" action="{esc_h(ziel_pfad)}" class="flex gap-2 mt-4">
  {versteckt}
  <input type="hidden" name="bestaetigt" value="ja">
  <button class="px-4 py-2 rounded-lg bg-emerald-600 text-white font-medium
     hover:bg-emerald-700">{esc_h(knopf)}</button>
  <a href="{esc_h(zurueck)}" class="px-4 py-2 rounded-lg border
     border-slate-300 bg-white hover:bg-slate-50">{T("Abbrechen","Cancel")}</a>
 </form>
</div>""" + fuss())


@app.post("/senden")
def senden(anfrage: Request, id: str = Form(...), text: str = Form(...),
           quelle: str = Form("selbst"), bestaetigt: str = Form(""),
           zitat_id: str = Form("")) -> Response:
    """Senden -- aber erst nach Bestaetigung.

    Zwei Sicherungen, die zusammengehoeren: Es gibt im ganzen Dienst keinen
    zweiten Aufrufer (kein Zeitplan, kein Automatismus), und selbst der eine
    fragt vorher nach. Senden ueber einen Fremdclient ist der Schritt, fuer
    den Nummern gesperrt werden -- und eine Nachricht an einen Menschen
    laesst sich nicht zurueckholen.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()

    ziel = f"/gespraech?id={urllib.parse.quote(id)}"
    text = (text or "").strip()
    if not text:
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(T('leerer Text','empty text'))}", status_code=303)

    # Nachfragen nur bei dem, was die Maschine geschrieben hat.
    #
    # Zuerst galt es fuer jedes Senden -- das war eine Bestaetigung zu viel:
    # wer einen Satz selbst tippt und auf Senden drueckt, hat ihn gerade
    # gelesen und will ihn abschicken. Ein zweiter Klick schuetzt dort vor
    # nichts und gewoehnt einen nur daran, wegzuklicken.
    #
    # Bei einem Vorschlag ist es anders: den hat jemand anders formuliert,
    # und der geht unter deinem Namen raus.
    if quelle == "vorschlag" and bestaetigt != "ja":
        chats_v = koppler("/chats")
        name_v = next((c.get("name") for c in chats_v if c.get("id") == id), id) \
            if isinstance(chats_v, list) else id
        return bestaetigungsseite(
            T(f"Senden an {anzeige(name_v)}", f"Send to {anzeige(name_v)}"), "/senden",
            {"id": id, "text": text, "quelle": quelle,
             "zitat_id": zitat_id}, text, ziel)

    chats = koppler("/chats")
    name = next((c.get("name") for c in chats if c.get("id") == id), id) \
        if isinstance(chats, list) else id

    r = koppler("/senden", {"chat": id, "text": text,
                            "zitat_id": zitat_id or None})
    if not (isinstance(r, dict) and r.get("ok")):
        grund = str((r or {}).get("fehler") or r)[:200]
        return RedirectResponse(f"{ziel}&fehler={urllib.parse.quote(grund)}",
                                status_code=303)

    # Erst nach dem Erfolg protokollieren: was hier steht, ist wirklich raus.
    with closing(db()) as v:
        v.execute("""INSERT INTO gesendet (zeit, profil, chat, text, quelle, wa_id)
                     VALUES (?,?,?,?,?,?)""",
                  (time.time(), PROFIL_STANDARD, name, text,
                   "vorschlag" if quelle == "vorschlag" else "selbst",
                   r.get("id")))
        v.commit()
    return RedirectResponse(ziel, status_code=303)


@app.get("/wissen", response_class=HTMLResponse)
def wissen_seite(anfrage: Request, profil: str = PROFIL_STANDARD) -> Response:
    """Was der Dienst ueber wen weiss -- nachlesbar und streichbar."""
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()

    with closing(db()) as v:
        alle = v.execute("""SELECT id, chat, bereich, aussage, herkunft FROM wissen
                             WHERE profil=? ORDER BY chat, bereich, id""",
                          (profil,)).fetchall()

    nach_chat: dict[str, list[Any]] = {}
    for z in alle:
        nach_chat.setdefault(z["chat"], []).append(z)

    karte = namen_karte()
    # Nach Menge sortiert: wo viel bekannt ist, schaut man am ehesten nach.
    geordnet = sorted(nach_chat.items(), key=lambda p: -len(p[1]))

    teile = []
    for c, zeilen in geordnet:
        punkte = "".join(f"""<li class="flex items-start gap-2 py-1.5">
 <span class="shrink-0 rounded bg-slate-100 text-slate-600 text-xs px-1.5
    py-0.5 mt-0.5">{esc_h(bereich_anzeige(z['bereich']))}</span>
 {'<span class="shrink-0 rounded bg-sky-100 text-sky-800 text-xs px-1.5 py-0.5 mt-0.5">{T("eigen","own")}</span>'
  if z['herkunft'] == 'hand' else ''}
 <span class="flex-1 text-sm">{esc_h(z['aussage'])}</span>
 <form method="post" action="/wissen-loeschen" class="shrink-0">
  <input type="hidden" name="id" value="{z['id']}">
  <button class="text-slate-400 hover:text-red-600 px-1" title="{T('streichen','remove')}"
     aria-label="{T('streichen','remove')}">&times;</button>
 </form></li>""" for z in zeilen)
        teile.append(f"""<details class="rounded-xl border border-slate-200
     bg-white px-4 py-3">
 <summary class="cursor-pointer font-medium flex items-center gap-2">
  <span class="flex-1 truncate">{esc_h(anzeige(c, karte))}</span>
  <span class="shrink-0 rounded-full bg-slate-100 text-slate-600 text-xs
     px-2 py-0.5">{len(zeilen)}</span>
 </summary>
 <ul class="mt-2 divide-y divide-slate-100">{punkte}</ul>
</details>""")

    verbunden = whatsapp_probe().get("ok")

    with closing(db()) as v:
        laeufe = v.execute("""SELECT chat, zuletzt, fehler FROM wissen_lauf
                               WHERE profil=? ORDER BY zuletzt DESC LIMIT 5""",
                            (profil,)).fetchall()
        offen_n = v.execute("SELECT COUNT(*) AS n FROM wissen_lauf WHERE profil=?",
                            (profil,)).fetchone()["n"]

    an = auto_an()
    if an:
        letzte = (T(f"zuletzt {zeit_kurz(laeufe[0]['zuletzt'])}", f"last {zeit_kurz(laeufe[0]['zuletzt'])}")
                  if laeufe else T("noch keine Runde gelaufen","no round yet"))
        auto_text = T(f"Alle {takt(AUTO_INTERVALL)} bis zu {AUTO_JE_RUNDE} Chats, {offen_n} bereits angesehen. {letzte}.",
                      f"Every {takt(AUTO_INTERVALL)}, up to {AUTO_JE_RUNDE} chats, {offen_n} reviewed so far. {letzte}.")
        auto_klein = T("Einzelchats und Gruppen. Bei Gruppen geht es um das Gefuege: wer dazugehoert, was ansteht, welcher Ton dort herrscht.",
                       "Direct chats and groups. For groups it is about the fabric: who belongs, what is coming up, what tone prevails.")
    else:
        auto_text = T("Es wird nur ausgewertet, was du von Hand anstoesst.","Only what you trigger by hand gets analysed.")
        auto_klein = ""

    fehler_html = ""
    if an and laeufe and laeufe[0]["fehler"]:
        fehler_html = (f'<p class="mt-2 text-sm text-red-700">{T("Zuletzt","Last")}: '
                       f'{esc_h(laeufe[0]["fehler"])}</p>')

    knopf = ("px-4 py-2 rounded-lg bg-slate-900 text-white text-sm font-medium "
             "hover:bg-slate-800 disabled:opacity-40")
    feld = ("rounded-lg border border-slate-300 px-3 py-2 text-sm "
            "focus:outline-none focus:ring-2 focus:ring-slate-400")

    fehlt = ("" if verbunden else f"""<p class="text-sm text-red-700 mt-2">
 {T("Dafuer wird die gekoppelte Sitzung gebraucht &mdash; nur sie sieht mehr als den offenen Ausschnitt.","This needs the linked session &mdash; only it sees more than the open excerpt.")} <a class="underline" href="/koppeln">{T("Verbinden","Connect")}</a></p>""")

    return HTMLResponse(kopf(T("Wissen","Knowledge"), aktiv="/wissen") + f"""
<p class="text-sm text-slate-600 mb-4">{T("Abgeleitet aus dem Verlauf und beim Entwerfen mitverwendet. Was nicht stimmt, streichst du einzeln.","Derived from the history and used when drafting. Remove anything that is wrong, item by item.")}</p>

<div class="rounded-xl border border-slate-200 bg-white p-4 mb-4">
 <div class="flex items-center gap-2">
  <span class="w-2.5 h-2.5 rounded-full {'bg-emerald-500' if an else 'bg-slate-300'}"></span>
  <span class="font-medium">{T("Automatisch sammeln","Collect automatically")}</span>
  <form method="post" action="/wissen-automatisch" class="ml-auto">
   <button class="{knopf}">{T("Abschalten","Turn off") if an else T("Einschalten","Turn on")}</button>
  </form>
 </div>
 <p class="text-sm text-slate-600 mt-2">{auto_text}</p>
 {f'<p class="text-xs text-slate-500 mt-1">{auto_klein}</p>' if auto_klein else ''}
 {fehler_html}
</div>

<div class="rounded-xl border border-slate-200 bg-white p-4 mb-4">
 <p class="font-medium mb-2">{T("Einzelnen Chat jetzt auswerten","Analyse one chat now")}</p>
 <form method="post" action="/wissen-aufbauen"
       class="flex flex-wrap items-center gap-2">
  {chat_auswahl("chat") if verbunden else ''}
  <button class="{knopf}" {'' if verbunden else 'disabled'}>{T("Auswerten","Analyse")}</button>
 </form>{fehlt}
</div>

<div class="rounded-xl border border-slate-200 bg-white p-4 mb-6">
 <p class="font-medium mb-1">{T("Selbst etwas eintragen","Add something yourself")}</p>
 <p class="text-sm text-slate-600 mb-3">{T("Was im Verlauf nie stand, kann das Modell nicht daraus lesen: &bdquo;arbeitet Schicht&ldquo;, &bdquo;nie vor 10 Uhr anrufen&ldquo;. Eigene Eintraege bleiben stehen, auch wenn der Verlauf erneut ausgewertet wird.","What never appeared in the history the model cannot infer: &ldquo;works shifts&rdquo;, &ldquo;never call before 10&rdquo;. Your own entries stay, even when the history is analysed again.")}</p>
 <form method="post" action="/wissen-ergaenzen"
       class="flex flex-wrap items-center gap-2">
  {chat_auswahl("chat") if verbunden else
   f'<input name="chat" placeholder="{T("Chatname","Chat name")}" required class="{feld}">'}
  <select name="bereich" class="{feld}">
   {''.join(f'<option value="{esc_h(b)}">{esc_h(bereich_anzeige(b))}</option>'
            for b in BEREICHE)}
  </select>
  <input name="aussage" required placeholder="{T('Was gilt fuer diesen Chat?','What applies to this chat?')}"
     class="{feld} flex-1 min-w-48">
  <button type="submit" class="{knopf}">{T("Eintragen","Add")}</button>
 </form>
</div>

<h2 class="font-semibold mb-2">{T("Bekannt zu","Known for")} {len(geordnet)} {T("Chats","chats")}</h2>
<div class="flex flex-col gap-2">{''.join(teile) or
 f'<p class="text-slate-500">{T("Noch nichts gelernt.","Nothing learned yet.")}</p>'}</div>""" + fuss())


@app.post("/wissen-aufbauen")
def wissen_aufbauen_formular(anfrage: Request, chat: str = Form(...),
                             profil: str = Form(PROFIL_STANDARD)) -> Response:
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    wissen_aufbauen(chat.strip(), profil)
    return RedirectResponse("/wissen", status_code=303)


@app.post("/wissen-automatisch")
def wissen_automatisch(anfrage: Request) -> Response:
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    zugang.einstellung_schreiben("wissen_automatisch", "nein" if auto_an() else "ja")
    return RedirectResponse("/wissen", status_code=303)


@app.post("/wissen-ergaenzen")
def wissen_ergaenzen(anfrage: Request, chat: str = Form(...),
                     bereich: str = Form("Person"),
                     aussage: str = Form(...)) -> Response:
    """Eine eigene Aussage zu einem Chat ablegen.

    Von Hand Eingetragenes ist oft das wertvollste: was das Modell aus dem
    Verlauf nicht lesen kann, weil es nie geschrieben wurde. "Siezt sich mit
    ihrem Mann", "arbeitet Schicht", "nie vor 10 Uhr anrufen".

    herkunft='hand' unterscheidet es vom Abgeleiteten. Geloescht wird es
    ohnehin nur einzeln und von Hand; die UNIQUE-Bedingung sorgt dafuer, dass
    ein spaeterer Durchgang es nicht verdoppelt.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    aussage = (aussage or "").strip()
    bereich = (bereich or "Person").strip() or "Person"
    if aussage:
        with closing(db()) as v:
            v.execute("""INSERT INTO wissen (profil, chat, bereich, aussage,
                                             erstellt, herkunft)
                         VALUES (?,?,?,?,?,'hand')
                         ON CONFLICT(profil, chat, aussage) DO UPDATE SET
                           bereich=excluded.bereich, herkunft='hand'""",
                      (PROFIL_STANDARD, chat.strip(), bereich, aussage, time.time()))
            v.commit()
    return RedirectResponse("/wissen", status_code=303)


@app.post("/vortippen-schalter")
def vortippen_schalter(anfrage: Request) -> Response:
    """Das automatische Vortippen an- oder abschalten.

    Eigener Schalter und nicht an den der Wissensrunde gehaengt: der eine
    liest nur, der andere schreibt in echte Chats. Wer das eine abstellen
    will, meint selten auch das andere.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    zugang.einstellung_schreiben("vortippen", "nein" if vortippen_an() else "ja")
    return RedirectResponse("/", status_code=303)


@app.post("/wissen-loeschen")
def wissen_loeschen(anfrage: Request, id: int = Form(...)) -> Response:
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    with closing(db()) as v:
        v.execute("DELETE FROM wissen WHERE id=?", (id,))
        v.commit()
    return RedirectResponse("/wissen", status_code=303)


@app.post("/vormerken-formular")
def vormerken_formular(anfrage: Request, chat: str = Form(...), text: str = Form(...)) -> Response:
    """Gegenstueck zum Formular in der Oberflaeche.

    Eigener Endpunkt statt JSON, weil ein HTML-Formular nun einmal Felder
    schickt und keinen JSON-Rumpf -- und weil die Antwort eine Weiterleitung
    sein soll, kein JSON im Browserfenster.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()
    vormerken(Vormerkung(chat=chat.strip(), text=text))
    return RedirectResponse("/", status_code=303)


@app.post("/abmeldung")
def abmelden() -> Response:
    antwort = RedirectResponse("/", status_code=303)
    antwort.delete_cookie(zugang.KEKS, path="/")
    return antwort


@app.get("/einstellungen", response_class=HTMLResponse)
def oberflaeche(anfrage: Request, trotzdem: int = 0, pruefen: int = 0) -> Response:
    """Schlichte Ansicht: Zustand und was der Dienst bisher gelernt hat.

    Absicht ist Nachvollziehbarkeit -- man soll sehen koennen, warum der Agent
    etwas vorschlaegt, statt es glauben zu muessen. Deshalb stehen hier Entwurf
    UND Endfassung nebeneinander.
    """
    if not zugang.angemeldet(anfrage):
        return anmeldeseite()

    # Solange etwas Notwendiges fehlt, fuehrt der Weg zuerst durch die
    # Einrichtung. Eine Uebersicht, die leer bleibt, weil kein Schluessel
    # hinterlegt ist, erklaert niemandem, was zu tun waere -- sie sieht nur
    # kaputt aus.
    if not trotzdem and (
            not eingerichtet()
            or (koppler_da() and koppler("/zustand").get("status") != "bereit")):
        return RedirectResponse("/einrichtung", status_code=303)

    lampen_html = lampen(erzwingen=bool(pruefen))
    chatliste = chat_auswahl("chat")
    z = zustand()
    with closing(db()) as v:
        offen = v.execute("""
            SELECT chat, text FROM vormerkungen
             WHERE abgeholt_am IS NULL ORDER BY id DESC LIMIT 20
        """).fetchall()

    def esc(s: Any) -> str:
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    # Einmal fuer die ganze Seite, nicht je Zeile: sonst waere es je Eintrag
    # eine Abfrage.
    karte = namen_karte()

    with closing(db()) as v:
        vt = v.execute("""SELECT chat, zuletzt, ergebnis FROM entwurf_lauf
                           ORDER BY zuletzt DESC LIMIT 1""").fetchone()
        vt_n = v.execute("SELECT COUNT(*) AS n FROM entwurf_lauf").fetchone()["n"]
    if vortippen_an():
        _vt_zeit = time.strftime('%d.%m. %H:%M', time.localtime(vt['zuletzt'])) if vt else ""
        letzte_vt = (T(f"zuletzt {_vt_zeit} fuer {esc(anzeige(vt['chat'], karte))} &mdash; {esc(vt['ergebnis'])}",
                       f"last {_vt_zeit} for {esc(anzeige(vt['chat'], karte))} &mdash; {esc(vt['ergebnis'])}")
                     if vt else T("noch nichts vorgetippt","nothing pre-typed yet"))
        vortippen_text = (
            T(f"An &mdash; alle {takt(VORTIPPEN_INTERVALL)} bis zu {VORTIPPEN_JE_RUNDE} Chats, {vt_n} bisher bedient. {letzte_vt}.",
              f"On &mdash; every {takt(VORTIPPEN_INTERVALL)}, up to {VORTIPPEN_JE_RUNDE} chats, {vt_n} handled so far. {letzte_vt}.")
            + "<br><small>"
            + T("Nur Einzelchats, in denen die Gegenseite zuletzt geschrieben hat. Ein Feld, in dem schon etwas steht, wird nicht angetastet. Gesendet wird nichts.",
                "Only direct chats where the other side wrote last. A field that already has text is left untouched. Nothing is sent.")
            + "</small>")
    else:
        vortippen_text = T("Aus &mdash; Entwuerfe entstehen nur, wenn die Erweiterung danach fragt.",
                           "Off &mdash; drafts are only created when the extension asks.")

    offene_vormerkungen = ""
    if offen:
        eintraege = "".join(
            f"<li><b>{esc(anzeige(o['chat'], karte))}</b>: {esc(o['text'])}</li>"
            for o in offen)
        offene_vormerkungen = (
            f"<p>{T('Wartet auf das Oeffnen des Chats:','Waiting for the chat to be opened:')}</p><ul>{eintraege}</ul>")

    knopf = ("px-4 py-2 rounded-lg bg-slate-900 text-white text-sm font-medium "
             "hover:bg-slate-800")
    leise = ("px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-sm "
             "hover:bg-slate-50")
    feld = ("rounded-lg border border-slate-300 px-3 py-2 text-sm "
            "focus:outline-none focus:ring-2 focus:ring-slate-400")

    def marke(beschriftung: str, wert: str) -> str:
        return (f'<span class="inline-flex items-center gap-1.5 rounded-lg '
                f'border border-slate-200 bg-white px-2.5 py-1 text-xs">'
                f'<span class="text-slate-500">{beschriftung}</span>'
                f'<span class="font-medium">{esc(wert)}</span></span>')

    return HTMLResponse(kopf(T("Einstellungen","Settings"), aktiv="/einstellungen") + f"""
{lampen_html}

<div class="flex flex-wrap gap-2 mb-4">
 {marke(T("Anbieter","Provider"), z['anbieter'])}
 {marke(T("Modell","Model"), z['modell'])}
 {marke(T("Schluessel","Key"), T("gesetzt","set") if z['schluessel_gesetzt'] else T("FEHLT","MISSING"))}
 {marke(T("Entwuerfe","Drafts"), z['entwuerfe'])}
 {marke(T("bewertet","rated"), z['davon_bewertet'])}
</div>

<div class="rounded-xl border border-amber-300 bg-amber-50 p-4 mb-4">
 <p class="font-medium">{T("Vortippen erreicht das Telefon nicht.","Pre-typing does not reach the phone.")}</p>
 <p class="text-sm text-slate-700 mt-1">{T("WhatsApp gleicht Entwuerfe nicht zwischen Geraeten ab. Was der Koppler in seiner Sitzung setzt, sieht niemand. Antworten gehen ueber","WhatsApp does not sync drafts between devices. What the connector sets in its session, nobody sees. Reply via")} <a class="underline" href="/">{T("Chats","Chats")}</a>.</p>
 <div class="flex items-center gap-2 mt-3">
  <form method="post" action="/vortippen-schalter">
   <button class="{leise}">{T("Abschalten","Turn off") if vortippen_an() else T("Einschalten","Turn on")}</button>
  </form>
  <span class="text-xs text-slate-600">{vortippen_text}</span>
 </div>
</div>

<div class="rounded-xl border border-slate-200 bg-white p-4 mb-4">
 <p class="font-medium mb-1">{T("Fuer einen Chat vormerken","Queue for a chat")}</p>
 <p class="text-sm text-slate-600 mb-3">{T("Legt einen Text bereit. Erreicht aus demselben Grund nur die Browser-Erweiterung, nicht das Telefon.","Prepares a text. For the same reason it only reaches the browser extension, not the phone.")}</p>
 <form method="post" action="/vormerken-formular"
       class="flex flex-wrap items-center gap-2">
  {chatliste}
  <input name="text" required placeholder="{T('Text, der im Feld stehen soll','Text to place in the field')}"
     class="{feld} flex-1 min-w-48">
  <button type="submit" class="{knopf}">{T("Vormerken","Queue")}</button>
 </form>
 {offene_vormerkungen}
</div>

<form method="post" action="/abmeldung">
 <button class="{leise}">{T("Abmelden","Sign out")}</button>
</form>{fuss()}""")


class Vormerkung(BaseModel):
    chat: str
    text: str
    profil: str = PROFIL_STANDARD


@app.post("/vormerken", dependencies=[Depends(zugang.mensch_oder_erweiterung)])
def vormerken(v: Vormerkung) -> dict[str, Any]:
    """Text fuer einen Chat hinterlegen. Die Erweiterung tippt ihn spaeter ein.

    Pro Chat gilt die juengste Vormerkung; aeltere unabgeholte werden dabei
    verworfen. Zwei offene Entwuerfe fuer denselben Chat waeren nur eine Frage
    danach, welcher zuerst kommt -- und das ist keine Eigenschaft, auf die man
    sich verlassen moechte.
    """
    with closing(db()) as x:
        x.execute("DELETE FROM vormerkungen WHERE profil=? AND chat=? AND abgeholt_am IS NULL",
                  (v.profil, v.chat))
        cur = x.execute(
            "INSERT INTO vormerkungen (zeit, profil, chat, text) VALUES (?,?,?,?)",
            (time.time(), v.profil, v.chat, v.text))
        x.commit()
        kennung = int(cur.lastrowid)

    # Wenn eine gekoppelte Sitzung da ist, den Entwurf sofort setzen -- dann
    # muss kein Fenster offen sein und niemand auf die Erweiterung warten.
    # Gelingt es nicht, bleibt die Vormerkung schlicht liegen und die
    # Erweiterung holt sie beim naechsten Oeffnen des Chats ab. Zwei Wege zum
    # selben Ziel, und der zweite faengt den ersten auf.
    weg = "vorgemerkt"
    if koppler_da():
        kid = koppler_chat_finden(v.chat)
        if kid:
            r = koppler("/entwurf", {"chat": kid, "text": v.text})
            if r.get("ok"):
                with closing(db()) as x:
                    x.execute("UPDATE vormerkungen SET abgeholt_am=? WHERE id=?",
                              (time.time(), kennung))
                    x.commit()
                weg = "direkt gesetzt"
    return {"id": kennung, "chat": v.chat, "weg": weg}


def koppler_chat_finden(name: str) -> str | None:
    """Chatname aus der Oberflaeche -> Kennung beim Koppler.

    Der Mensch tippt einen Namen, der Koppler braucht eine Kennung. Verglichen
    wird ohne Ruecksicht auf Gross- und Kleinschreibung; exakte Treffer haben
    Vorrang vor Teiltreffern, damit "Anna" nicht in "Anna und Bernd" landet.
    """
    chats = koppler("/chats")
    if not isinstance(chats, list):
        return None
    gesucht = name.strip().casefold()
    for c in chats:
        if (c.get("name") or "").strip().casefold() == gesucht:
            return c.get("id")
    for c in chats:
        if gesucht and gesucht in (c.get("name") or "").casefold():
            return c.get("id")
    return None


@app.get("/abholen", dependencies=[Depends(zugang.mensch_oder_erweiterung)])
def abholen(chat: str, profil: str = PROFIL_STANDARD) -> dict[str, Any]:
    """Die Erweiterung fragt: liegt fuer den offenen Chat etwas bereit?

    Wird beim Abholen als erledigt markiert statt geloescht -- so bleibt
    nachvollziehbar, was wann eingetippt wurde.
    """
    with closing(db()) as x:
        z = x.execute("""SELECT id, text FROM vormerkungen
                          WHERE profil=? AND chat=? AND abgeholt_am IS NULL
                          ORDER BY id DESC LIMIT 1""", (profil, chat)).fetchone()
        if not z:
            return {"text": None}
        x.execute("UPDATE vormerkungen SET abgeholt_am=? WHERE id=?", (time.time(), z["id"]))
        x.commit()
    return {"id": z["id"], "text": z["text"]}


@app.post("/rueckmeldung", dependencies=[Depends(zugang.api_zugang)])
def rueckmeldung(r: Rueckmeldung) -> dict[str, str]:
    """Ausgang festhalten. Ohne diesen Schritt lernt der Dienst nichts."""
    with closing(db()) as v:
        treffer = v.execute(
            "UPDATE entwuerfe SET ergebnis=?, endfassung=?, bewertet_am=? WHERE id=?",
            (r.ergebnis, r.endfassung, time.time(), r.id)).rowcount
        v.commit()
    if not treffer:
        raise HTTPException(404, f"kein Entwurf mit id={r.id}")
    return {"status": "gespeichert"}


# --- Verwaltung: fuer ein Betriebswerkzeug ----------------------------------
#
# Diese Ebene ist fuer den Betrieb da, nicht fuer den Inhalt. Sie gibt Zahlen
# heraus -- wie viele Entwuerfe, wie oft uebernommen, wann zuletzt etwas
# passiert ist -- und laesst Token erneuern. Sie gibt **keinen** Nachrichtentext
# und **keinen** Entwurf heraus, und zwar nicht aus Versehen nicht, sondern
# weil keine der Abfragen hier die Spalten kontext, entwurf oder endfassung
# auch nur liest.
#
# Ehrlich dazugesagt: Wer den Verwaltungstoken hat, kann einen neuen
# Zugangstoken setzen und sich damit anschliessend doch Inhalte ansehen. Das
# laesst sich nicht vermeiden, wenn Erneuern moeglich sein soll. Es bleibt
# aber nicht verborgen -- jede Erneuerung wird mit Zeitpunkt festgehalten,
# taucht im Zustand auf und wirft nebenbei alle offenen Sitzungen hinaus.
# Die Trennung schuetzt also verlaesslich gegen Mitlesen, nicht gegen einen
# entschlossenen Missbrauch des Verwaltungstokens.

class TokenWunsch(BaseModel):
    neu: str | None = Field(default=None, description="Vorgabe; sonst wird einer erzeugt")


class Aufraeumen(BaseModel):
    aelter_als_tage: int = Field(ge=1, description="Entwuerfe aelter als dies loeschen")
    profil: str | None = None


@app.get("/verwaltung/zustand", dependencies=[Depends(zugang.verwaltung_zugang)])
def verwaltung_zustand() -> dict[str, Any]:
    _anbieter, _modell, _schluessel = modell_konfig()
    with closing(db()) as v:
        gesamt = v.execute("SELECT COUNT(*) AS n FROM entwuerfe").fetchone()["n"]
        nach_ergebnis = {z["ergebnis"] or "offen": z["n"] for z in v.execute(
            "SELECT ergebnis, COUNT(*) AS n FROM entwuerfe GROUP BY ergebnis")}
        profile = [{"profil": z["profil"], "entwuerfe": z["n"]} for z in v.execute(
            "SELECT profil, COUNT(*) AS n FROM entwuerfe GROUP BY profil ORDER BY n DESC")]
        letzte = v.execute("SELECT MAX(zeit) AS t FROM entwuerfe").fetchone()["t"]

    def herkunft(name: str) -> dict[str, Any]:
        wann = zugang.geaendert_am(name)
        return {"erneuert_am": wann, "quelle": "datenbank" if wann else "umgebung"}

    return {
        "modell": {"anbieter": _anbieter, "modell": _modell,
                   "schluessel_gesetzt": zugang.gesetzt(_schluessel)},
        "entwuerfe": {"gesamt": gesamt, "nach_ergebnis": nach_ergebnis,
                      "letzte_taetigkeit": letzte},
        "profile": profile,
        "token": {"zugang": herkunft("zugang"), "verwaltung": herkunft("verwaltung")},
        # Damit ein Betriebswerkzeug nicht nur sieht, dass der Dienst laeuft, sondern auch,
        # ob er arbeiten kann. Beides ohne einen Nachrichtentext.
        "verbindungen": {"whatsapp": whatsapp_probe(), "modell": modell_probe()},
        "laeuft_seit": START_ZEIT,
    }


@app.post("/verwaltung/token/{welcher}", dependencies=[Depends(zugang.verwaltung_zugang)])
def verwaltung_token_erneuern(welcher: str, wunsch: TokenWunsch | None = None) -> dict[str, Any]:
    """Token erneuern. Die Antwort enthaelt ihn im Klartext -- sonst koennte ihn
    niemand verteilen, und das ist der Zweck des Aufrufs.

    ``zugang``     wirft alle offenen Weboberflaechen-Sitzungen hinaus und legt
                   die Erweiterung still, bis der neue Wert dort eingetragen ist.
    ``verwaltung`` betrifft nur das Betriebswerkzeug selbst.
    """
    if welcher not in ("zugang", "verwaltung"):
        raise HTTPException(404, T("unbekannter Token; erlaubt sind zugang und verwaltung","unknown token; allowed are zugang and verwaltung"))
    neu = zugang.erneuern(welcher, (wunsch.neu if wunsch else None))
    return {"token_art": welcher, "token": neu, "gueltig_ab": time.time(),
            "hinweis": (T("Alle offenen Sitzungen sind jetzt ungueltig; der neue Wert muss in der Erweiterung und in der Umgebung nachgezogen werden.",
                          "All open sessions are now invalid; the new value must be updated in the extension and in the environment.")
                        if welcher == "zugang" else
                        T("Nur die Verwaltungsschnittstelle ist betroffen.","Only the admin interface is affected."))}


@app.post("/verwaltung/aufraeumen", dependencies=[Depends(zugang.verwaltung_zugang)])
def verwaltung_aufraeumen(a: Aufraeumen) -> dict[str, int]:
    """Altes Gedaechtnis loeschen -- ohne es dafuer ansehen zu muessen.

    Loeschen ist kein Lesen: die Abfrage waehlt nur nach Zeit und Profil aus.
    Das ist der eine Eingriff am Inhalt, der von hier aus Sinn ergibt, wenn
    etwa ein Profil stillgelegt wird.
    """
    grenze = time.time() - a.aelter_als_tage * 86400
    with closing(db()) as v:
        if a.profil:
            n = v.execute("DELETE FROM entwuerfe WHERE zeit < ? AND profil = ?",
                          (grenze, a.profil)).rowcount
        else:
            n = v.execute("DELETE FROM entwuerfe WHERE zeit < ?", (grenze,)).rowcount
        v.commit()
    return {"geloescht": n}
