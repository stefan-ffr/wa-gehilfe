"""Jede Seite muss sich rendern lassen -- in jeder Sprache.

Das ist der Test, der beim Umbau am meisten gefunden hat: eine Oberflaeche,
die aus f-Strings gebaut wird, faellt bei einem Tippfehler erst beim Aufruf
um, nicht beim Import. Deshalb wird hier wirklich jede Seite geholt.
"""
from __future__ import annotations

import re

import pytest

SEITEN = [
    ("chats", "/"),
    ("archiv", "/?archiv=1"),
    ("gespraech", "/gespraech?id=4179000001%40c.us"),
    ("gespraech_zitat", "/gespraech?id=4179000001%40c.us&zitat=A1"),
    ("gespraech_termin", "/gespraech?id=4179000001%40c.us&t_titel=Kaffee"
                         "&t_beginn=2026-10-01T10:00&t_ort=Basel"),
    ("gespraech_fehler", "/gespraech?id=4179000001%40c.us&fehler=Testfehler"),
    ("wissen", "/wissen"),
    ("einstellungen", "/einstellungen"),
    ("einrichtung", "/einrichtung"),
    ("koppeln", "/koppeln"),
    ("suche_leer", "/suche"),
    ("suche_treffer", "/suche?q=Alex"),
    ("suche_im_chat", "/suche?q=Freitag&chat=4179000001%40c.us"),
]


@pytest.mark.parametrize("name,pfad", SEITEN, ids=[s[0] for s in SEITEN])
def test_seite_rendert(klient, name, pfad):
    a = klient.get(pfad)
    assert a.status_code == 200, f"{name}: HTTP {a.status_code}"
    assert len(a.text) > 500, f"{name}: verdaechtig kurz"
    assert "</html>" in a.text, f"{name}: unvollstaendiges Markup"


def test_gesundheit_ist_offen_und_karg(klient):
    """Eine Lebendpruefung ist keine Auskunftsstelle."""
    a = klient.get("/gesundheit")
    assert a.status_code == 200
    assert a.json() == {"status": "ok"}


def test_ohne_identitaet_kein_inhalt(klient, m):
    """Ohne Anmeldung darf keine Seite Inhalt zeigen -- und zwar mit 401,
    damit ein Skript die Maske nicht fuer die Seite haelt."""
    echt = m.zugang.angemeldet
    m.zugang.angemeldet = lambda anfrage: False
    try:
        for _, pfad in SEITEN:
            a = klient.get(pfad)
            assert a.status_code == 401, f"{pfad} gab {a.status_code} statt 401"
            assert "4179000001" not in a.text
    finally:
        m.zugang.angemeldet = echt


# --- Sprache ---------------------------------------------------------------

# Eindeutig deutsche Beschriftungen. "Chats" oder "Archiv" waeren mehrdeutig
# und taugen deshalb nicht als Nachweis.
NUR_DEUTSCH = [
    "Anmelden", "Abmelden", "Senden", "Einschalten", "Abschalten", "Auswerten",
    "Eintragen", "Speichern", "Abbrechen", "Vormerken", "Verbinden",
    "Keine Nachrichten", "Keine Chats", "Noch nichts gelernt",
    "Vorschlag holen", "Datei senden", "abschreiben", "antworten", "loeschen",
    "archivieren", "zurueckholen", "aeltere Nachrichten laden",
    "als gelesen markieren", "Termin erkannt", "Termin im Verlauf suchen",
    "Bekannt zu", "Automatisch sammeln", "Selbst etwas eintragen",
    "Zugangstoken", "verwalten", "jetzt pruefen",
]

# Je Seite ein paar Texte, die im englischen Modus WIRKLICH dastehen muessen.
# Ohne diese Gegenprobe wuerde eine leere Seite als "kein Deutsch" durchgehen.
ENGLISCH_ERWARTET = {
    "/": ["Knowledge", "More", "Search chat or message"],
    "/gespraech?id=4179000001%40c.us": ["Send", "Get suggestion", "reply",
                                        "mark as read"],
    "/wissen": ["Collect automatically", "Analyse", "Known for"],
    "/einstellungen": ["Provider", "Model", "Sign out"],
    "/einrichtung": ["Setup", "API key", "Save"],
    "/koppeln": ["Connect WhatsApp", "Unlink"],
}


def _sichtbar(html: str) -> str:
    """Nur der Text, ohne Tags und Attribute.

    Attributwerte sind Daten: value="loeschen" und <option value="Vorlieben">
    MUESSEN deutsch bleiben, sonst zerfaellt das Vokabular in der Datenbank.
    Ein Sprachtest, der sie mitliest, schlaegt zu Recht nie fehl -- er misst
    nur das Falsche.
    """
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html))


@pytest.mark.parametrize("name,pfad", SEITEN, ids=[s[0] for s in SEITEN])
def test_kein_deutsch_im_englischen_modus(klient, sprache, name, pfad):
    if sprache != "en":
        pytest.skip("nur im englischen Lauf")
    text = _sichtbar(klient.get(pfad).text)
    reste = sorted({w for w in NUR_DEUTSCH if w in text})
    assert not reste, f"{name}: deutscher Rest {reste}"


@pytest.mark.parametrize("pfad", list(ENGLISCH_ERWARTET))
def test_englisch_steht_wirklich_da(klient, sprache, pfad):
    if sprache != "en":
        pytest.skip("nur im englischen Lauf")
    text = klient.get(pfad).text
    fehlt = [w for w in ENGLISCH_ERWARTET[pfad] if w not in text]
    assert not fehlt, f"{pfad}: englische Texte fehlen: {fehlt}"


def test_deutsch_steht_im_deutschen_modus(klient, sprache):
    if sprache != "de":
        pytest.skip("nur im deutschen Lauf")
    text = klient.get("/").text
    assert "Wissen" in text and "Mehr" in text
