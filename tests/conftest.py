"""Gemeinsame Vorrichtung fuer die Tests.

Der Dienst wird NICHT gegen ein echtes WhatsApp getestet -- das waere weder
wiederholbar noch erlaubt. Stattdessen wird der Koppler vorgetaeuscht: er ist
ohnehin die einzige Stelle, an der Fremddaten hereinkommen.

Die Sprache steht beim Import fest (i18n liest die Umgebung einmal). Deshalb
setzt conftest sie, BEVOR app.main importiert wird, und die englische Fassung
laeuft als eigener pytest-Aufruf -- siehe .github/workflows/test.yml.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

import pytest

WURZEL = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL))

# Muss vor dem Import von app.main stehen.
os.environ.setdefault("ZUGANG_TOKEN", "testtoken-ohne-belang-aaaaaa")
os.environ.setdefault("VERWALTUNG_TOKEN", "testtoken-ohne-belang-bbbbbb")
os.environ.setdefault("SPRACHE", "de")
os.environ.setdefault(
    "ENTWURF_DB", str(pathlib.Path(tempfile.mkdtemp()) / "test.sqlite3"))

CHATS = [
    {"id": "4179000001@c.us", "name": "Alex", "letzte_zeit": 1758500000,
     "ungelesen": 2, "letzte_von_mir": False, "archiviert": False},
    {"id": "120363@g.us", "name": "Gruppe Technik", "gruppe": True,
     "letzte_zeit": 1758400000, "letzte_von_mir": True, "archiviert": False},
    {"id": "4179000002@c.us", "name": "+41 79 000 00 02",
     "letzte_zeit": 1758300000, "letzte_von_mir": False, "archiviert": True},
]

NACHRICHTEN = [
    {"id": "A1", "chat": "4179000001@c.us", "text": "Treffen wir uns Freitag?",
     "zeit": 1758500000, "von_mir": False, "typ": "chat", "reaktionen": ["X"]},
    {"id": "A2", "chat": "4179000001@c.us", "text": "", "zeit": 1758500100,
     "von_mir": False, "typ": "ptt"},
    {"id": "A3", "chat": "4179000001@c.us", "text": "", "zeit": 1758500200,
     "von_mir": False, "typ": "image"},
    {"id": "A4", "chat": "4179000001@c.us", "text": "Klar", "zeit": 1758500300,
     "von_mir": True, "typ": "chat", "hat_zitat": True},
]


def _koppler(pfad, rumpf=None, **kw):
    if pfad.startswith("/chats"):
        return CHATS
    if pfad.startswith("/nachrichten"):
        return NACHRICHTEN
    if pfad.startswith("/suche"):
        return NACHRICHTEN[:1]
    if pfad.startswith("/zustand"):
        return {"status": "bereit", "nummer": "41790000000"}
    return {"ok": True}


@pytest.fixture(scope="session")
def m():
    """Das Modul selbst -- mit vorgetaeuschtem Koppler und Modell."""
    from app import main as modul

    modul.schema_anlegen()
    modul.koppler = _koppler
    modul.koppler_da = lambda: True
    modul.eingerichtet = lambda: True
    modul.modell_probe = lambda erzwingen=False: {
        "ok": True, "stufe": "gut", "text": "test", "zeit": 0}
    modul.zugang.angemeldet = lambda anfrage: True

    with modul.closing(modul.db()) as v:
        v.execute("DELETE FROM wissen")
        for bereich, aussage in (("Person", "arbeitet Schicht"),
                                 ("Ton", "kurz und direkt"),
                                 ("Vorlieben", "mag Kaffee")):
            v.execute("INSERT INTO wissen (profil, chat, bereich, aussage,"
                      " erstellt) VALUES (?,?,?,?,?)",
                      (modul.PROFIL_STANDARD, "Alex", bereich, aussage, 0))
        v.commit()
    return modul


@pytest.fixture(scope="session")
def klient(m):
    from fastapi.testclient import TestClient
    return TestClient(m.app)


@pytest.fixture(scope="session")
def sprache():
    return os.environ["SPRACHE"]
