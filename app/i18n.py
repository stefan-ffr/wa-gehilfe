"""Zweisprachigkeit -- de|en, umschaltbar ueber die Umgebung.

Die Quellsprache ist Deutsch. Englisch kommt als zweiter Zweig aus demselben
Aufruf: `T("Chats", "Chats")` liefert je nach SPRACHE den einen oder den
anderen Text. Das haelt Uebersetzung und Verwendung an einer Stelle -- kein
getrennter Katalog, der auseinanderlaeuft.

    from .i18n import T, SPRACHE
    titel = T("Einstellungen", "Settings")

Gesetzt wird die Sprache mit SPRACHE=de oder SPRACHE=en. Unbekanntes faellt
auf Deutsch zurueck.

Stand: das Geruest steht und wird schrittweise durch den Code gezogen. Noch
nicht jeder Text ist zweisprachig; was nicht in T(...) steht, erscheint auf
Deutsch, unabhaengig von SPRACHE.
"""
from __future__ import annotations

import os

SPRACHE = (os.environ.get("SPRACHE", "de") or "de").strip().lower()
if SPRACHE not in ("de", "en"):
    SPRACHE = "de"


def T(de: str, en: str) -> str:
    """Den Text in der aktiven Sprache. Vorgabe und Rueckfall: Deutsch."""
    return en if SPRACHE == "en" else de
