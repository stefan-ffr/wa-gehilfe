"""Zweisprachigkeit -- de|en, umschaltbar ueber die Umgebung.

Die Quellsprache ist Deutsch. Englisch kommt als zweiter Zweig aus demselben
Aufruf: `T("Chats", "Chats")` liefert je nach SPRACHE den einen oder den
anderen Text. Das haelt Uebersetzung und Verwendung an einer Stelle -- kein
getrennter Katalog, der auseinanderlaeuft.

    from .i18n import T, SPRACHE
    titel = T("Einstellungen", "Settings")

Gesetzt wird die Sprache mit SPRACHE=de oder SPRACHE=en. Unbekanntes faellt
auf Deutsch zurueck.

Was NICHT uebersetzt wird, und warum:

* **Datenschluessel.** Die Wissensbereiche (Person, Beziehung, Vorlieben, ...)
  stehen so in der Datenbank und kommen so vom Modell zurueck. Zwei
  Vokabulare in derselben Tabelle waeren nicht mehr zusammenzufuehren.
  Uebersetzt wird nur die ANZEIGE, siehe bereich_anzeige() in main.py.
* **Formularwerte.** value="loeschen" und dergleichen sind Protokoll
  zwischen Seite und Endpunkt, kein Text fuer Menschen.
* **Die Analyse-Prompts** (WISSEN_SYSTEM, BILD_SYSTEM, TERMIN_SYSTEM). Sie
  legen ebenjenes Vokabular fest. Auf die Ausgabe wirkt es nicht: das Modell
  antwortet in der Sprache des jeweiligen Chats, nicht in der des Prompts.
"""
from __future__ import annotations

import os

SPRACHE = (os.environ.get("SPRACHE", "de") or "de").strip().lower()
if SPRACHE not in ("de", "en"):
    SPRACHE = "de"


def T(de: str, en: str) -> str:
    """Den Text in der aktiven Sprache. Vorgabe und Rueckfall: Deutsch."""
    return en if SPRACHE == "en" else de
