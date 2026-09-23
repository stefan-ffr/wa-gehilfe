"""Die Entscheidungen, die man nicht ansieht.

Suche, Bereichs-Anzeige und der Prompt-Bau tragen Regeln, die sich leicht
still verschieben. Genau die stehen hier.
"""
from __future__ import annotations

import pytest

KARTE: dict[str, str] = {}

CHATS = [
    {"id": "41791234567@c.us", "name": "Alex", "letzte_zeit": 100},
    {"id": "41790000002@c.us", "name": "+41 79 000 00 02", "letzte_zeit": 90},
    {"id": "120363@g.us", "name": "Familie Mueller", "gruppe": True,
     "letzte_zeit": 80},
    {"id": "66812345678@c.us", "name": "Niko", "letzte_zeit": 70,
     "archiviert": True},
]


def treffer(m, frage):
    return [c["name"] for c in CHATS if m.chat_passt(c, frage, KARTE)]


@pytest.mark.parametrize("frage,erwartet", [
    ("alex", ["Alex"]),
    ("ALEX", ["Alex"]),                       # Gross/Klein egal
    ("mueller", ["Familie Mueller"]),
    ("fami", ["Familie Mueller"]),            # Teiltreffer im Namen
    ("079 000 00 02", ["+41 79 000 00 02"]),  # nationale Schreibweise
    ("079 000", ["+41 79 000 00 02"]),
    ("0041 79 000", ["+41 79 000 00 02"]),    # internationale mit 00
    ("+41 79 000 00 02", ["+41 79 000 00 02"]),
    ("0000002", ["+41 79 000 00 02"]),        # blanke Ziffern
    ("668", ["Niko"]),
    ("12", []),                               # zu kurz -- sonst passt alles
    ("1", []),
    ("xyz", []),
    ("   ", []),
])
def test_chat_passt(m, frage, erwartet):
    assert treffer(m, frage) == erwartet


def test_nummernsuche_laeuft_nicht_als_textsuche(m):
    """"12" steckt in fast jeder Nummer. Wuerde die Ziffernsuche als
    Textsuche ueber die Kennung laufen, kaeme die halbe Liste zurueck."""
    assert treffer(m, "12") == []
    # ... waehrend eine Textsuche weiterhin Teiltreffer findet
    assert treffer(m, "ale") == ["Alex"]


def test_bereiche_sind_schluessel_und_bleiben_deutsch(m):
    """Die Bereiche stehen so in der Datenbank. Uebersetzt wird die Anzeige,
    nicht der Schluessel -- sonst entstehen zwei Vokabulare in einer Tabelle."""
    assert m.BEREICHE == ("Person", "Beziehung", "Vorlieben", "Vorhaben",
                          "Thema", "Ton")
    for schluessel in m.BEREICHE:
        assert m.bereich_anzeige(schluessel)          # nie leer
    if m.SPRACHE == "en":
        assert m.bereich_anzeige("Vorlieben") == "Preferences"
    else:
        assert m.bereich_anzeige("Vorlieben") == "Vorlieben"


def test_unbekannter_bereich_faellt_auf_sich_selbst_zurueck(m):
    assert m.bereich_anzeige("Erfundenes") == "Erfundenes"


def test_prompt_traegt_namen_und_verlauf(m):
    n = [m.Nachricht(von_mir=False, text="Kommst du?"),
         m.Nachricht(von_mir=True, text="Ja")]
    p = m.prompt_bauen(n, "Alex", m.PROFIL_STANDARD, None)
    assert "Kommst du?" in p and "Ja" in p
    assert m.EIGENER_NAME in p          # eigene Zeilen sind zugeordnet


def test_prompt_nimmt_hinweis_auf(m):
    n = [m.Nachricht(von_mir=False, text="Wann?")]
    p = m.prompt_bauen(n, "Alex", m.PROFIL_STANDARD, "kurz halten")
    assert "kurz halten" in p


def test_systemprompts_unterscheiden_sich_nur_in_der_letzten_regel(m):
    """Der Hand-Entwurf darf KEIN_ENTWURF sagen, das Vortippen nicht."""
    assert "KEIN_ENTWURF" in m.SYSTEM
    assert "KEIN_ENTWURF" in m.SYSTEM_AUTO      # als Verbot
    assert m.SYSTEM != m.SYSTEM_AUTO
    assert m.EIGENER_NAME in m.SYSTEM


def test_ics_ist_gueltig_und_ohne_fremde_domain(m):
    ics = m.ics_bauen("Kaffee", "2026-10-01T10:00", ort="Basel")
    assert ics.startswith("BEGIN:VCALENDAR")
    assert "END:VCALENDAR" in ics
    assert "SUMMARY:Kaffee" in ics
    assert m.KALENDER_DOMAIN in ics


def test_ziffern_normalisiert(m):
    assert m._ziffern("+41 79 000 00 02") == "41790000002"
    assert m._ziffern("keine") == ""
