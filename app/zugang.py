"""Zugangsschutz fuer den Entwurfsdienst.

Dieser Dienst zeigt Ausschnitte fremder WhatsApp-Nachrichten. Er darf deshalb
nicht offen stehen -- auch nicht "nur intern", denn intern haengt er ueber
Traefik an einem Namen, den jedes Geraet im Netz aufloesen kann.

Es gibt **zwei** Geheimnisse mit verschiedenen Rechten:

  ``ZUGANG_TOKEN``       Inhalt. Erweiterung (Bearer) und Weboberflaeche.
                         Sieht Nachrichten, Entwuerfe, Endfassungen.

  ``VERWALTUNG_TOKEN``   Betrieb. Nur ``/verwaltung/*`` -- Zustand, Zaehler,
                         Token erneuern. Kommt an **keinen** Nachrichtentext
                         und an keinen Entwurf heran, auch nicht indirekt.

Warum getrennt: Das NOC soll nach dem Dienst sehen und eingreifen koennen,
ohne dabei mitzulesen. Ein einziges Geheimnis fuer beides waere bequem und
genau deshalb falsch -- wer den Zustand abfragen darf, haette dann auch den
Chatverlauf.

Warum der Dienst die Cloudflare-Access-Kennung NICHT selbst prueft: von aussen
kommt man ueber den Tunnel, und davor steht Access. Von innen kommt man ueber
Traefik, und dort gibt es kein Access-Merkmal. Wuerde der Dienst eines
verlangen, waere der innere Weg tot; wuerde er einem Kopffeld glauben, waere
der Schutz wertlos, weil Kopffelder faelschbar sind. Access sichert also die
Aussenkante, dieses Modul sichert den Dienst selbst -- unabhaengig voneinander.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import closing

import jwt
from fastapi import HTTPException, Request

DB_PFAD = os.environ.get("ENTWURF_DB", "/daten/entwuerfe.sqlite3")

# Aus der Umgebung kommt der *Startwert*. Wird ein Token zur Laufzeit erneuert,
# liegt der gueltige Wert danach in der Datenbank und gewinnt.
ZUGANG_START = os.environ.get("ZUGANG_TOKEN", "").strip()
VERWALTUNG_START = os.environ.get("VERWALTUNG_TOKEN", "").strip()

from .i18n import T

MINDESTLAENGE = 16

# Kein stiller Notlauf ohne Schutz. Ein Dienst, der im Zweifel offen startet,
# steht irgendwann offen, ohne dass es jemand merkt -- lieber gar nicht starten.
if not ZUGANG_START:
    raise RuntimeError(
        T("ZUGANG_TOKEN ist nicht gesetzt. Der Dienst zeigt Chat-Inhalte und "
          "startet deshalb nicht ohne Zugangsschutz. Token erzeugen mit: "
          "openssl rand -base64 32",
          "ZUGANG_TOKEN is not set. The service shows chat content and therefore "
          "refuses to start without access protection. Create a token with: "
          "openssl rand -base64 32"))
if len(ZUGANG_START) < MINDESTLAENGE:
    raise RuntimeError(T(f"ZUGANG_TOKEN ist zu kurz (mindestens {MINDESTLAENGE} Zeichen).", f"ZUGANG_TOKEN is too short (at least {MINDESTLAENGE} characters)."))

# VERWALTUNG_TOKEN ist freiwillig: fehlt es, gibt es die Schnittstelle nicht.
# Abwesenheit schaltet ab, nicht auf. Ein Vertipper im Namen faellt sofort auf,
# weil /verwaltung/zustand dann mit 503 antwortet statt still offen zu stehen.
if VERWALTUNG_START:
    if len(VERWALTUNG_START) < MINDESTLAENGE:
        raise RuntimeError(T(f"VERWALTUNG_TOKEN ist zu kurz (mindestens {MINDESTLAENGE} Zeichen).", f"VERWALTUNG_TOKEN is too short (at least {MINDESTLAENGE} characters)."))
    if hmac.compare_digest(VERWALTUNG_START, ZUGANG_START):
        raise RuntimeError(
            T("VERWALTUNG_TOKEN und ZUGANG_TOKEN sind gleich. Damit waere die "
              "Trennung zwischen Betrieb und Inhalt nur behauptet.",
              "VERWALTUNG_TOKEN and ZUGANG_TOKEN are identical. The separation "
              "between operations and content would be merely claimed."))

KEKS = "entwurf_sitzung"
SITZUNGSDAUER = int(os.environ.get("SITZUNGSDAUER", str(12 * 3600)))


# --- Ablage der erneuerten Token --------------------------------------------
#
# Eigene Verbindung statt Import aus main: sonst haengen die beiden Module im
# Kreis aneinander. Die Datei ist dieselbe -- sie liegt im Datentraeger und
# ueberlebt damit den Neubau des Containers, was der ganze Sinn der Uebung ist.


def _speicher() -> sqlite3.Connection:
    v = sqlite3.connect(DB_PFAD, timeout=10)
    v.row_factory = sqlite3.Row
    v.execute("""CREATE TABLE IF NOT EXISTS geheimnisse (
                   name      TEXT PRIMARY KEY,
                   wert      TEXT NOT NULL,
                   geaendert REAL NOT NULL
                 )""")
    return v


def _gelesen(name: str) -> tuple[str, float] | None:
    try:
        with closing(_speicher()) as v:
            z = v.execute("SELECT wert, geaendert FROM geheimnisse WHERE name=?",
                          (name,)).fetchone()
    except sqlite3.Error:
        return None  # Datenbank noch nicht da -> Startwert gilt
    return (z["wert"], z["geaendert"]) if z else None


def _geschrieben(name: str, wert: str) -> None:
    with closing(_speicher()) as v:
        v.execute("INSERT INTO geheimnisse (name, wert, geaendert) VALUES (?,?,?) "
                  "ON CONFLICT(name) DO UPDATE SET wert=excluded.wert, "
                  "geaendert=excluded.geaendert", (name, wert, time.time()))
        v.commit()


def einstellung_lesen(name: str, standard: str = "") -> str:
    """Wert aus der Ablage, sonst der Startwert.

    Dieselbe Tabelle wie fuer die Token: Was zur Laufzeit gesetzt wird, soll an
    einer Stelle liegen und den Neubau des Containers ueberleben.
    """
    z = _gelesen(name)
    return z[0] if z else standard


def einstellung_schreiben(name: str, wert: str) -> None:
    _geschrieben(name, wert)


def gesetzt(wert: str) -> bool:
    """Ist das ein echter Wert -- oder nur ein sichtbarer Platzhalter?

    Nomad verwirft leere Werte, deshalb tragen unfertige Eintraege in der
    Nomad-Variablen ein `BITTE-SETZEN-...`. Ohne diese Pruefung meldete der
    Zustand `schluessel_gesetzt: true`, obwohl jeder Entwurf scheitern musste.
    """
    w = (wert or "").strip()
    return bool(w) and not w.startswith("BITTE-SETZEN")


def zugang_token() -> str:
    z = _gelesen("zugang")
    return z[0] if z else ZUGANG_START


def verwaltung_token() -> str:
    z = _gelesen("verwaltung")
    return z[0] if z else VERWALTUNG_START


def geaendert_am(name: str) -> float | None:
    z = _gelesen(name)
    return z[1] if z else None


def erneuern(name: str, neu: str | None = None) -> str:
    """Token ersetzen. Ohne Vorgabe wird einer erzeugt."""
    wert = (neu or "").strip() or neuer_token()
    if len(wert) < MINDESTLAENGE:
        raise HTTPException(400, T(f"Token zu kurz (mindestens {MINDESTLAENGE} Zeichen).", f"Token too short (at least {MINDESTLAENGE} characters)."))
    gegenstueck = verwaltung_token() if name == "zugang" else zugang_token()
    if gegenstueck and hmac.compare_digest(wert, gegenstueck):
        raise HTTPException(400, T("Die beiden Token duerfen nicht gleich sein.","The two tokens must not be identical."))
    _geschrieben(name, wert)
    return wert


def neuer_token() -> str:
    return secrets.token_urlsafe(32)


# --- Pruefungen -------------------------------------------------------------


def _stimmt(vorgelegt: str, erwartet: str) -> bool:
    """Zeitkonstanter Vergleich -- ein naives == verraet den Token zeichenweise."""
    if not erwartet:
        return False
    return hmac.compare_digest(vorgelegt.strip(), erwartet)


def token_stimmt(vorgelegt: str) -> bool:
    return _stimmt(vorgelegt, zugang_token())


# --- Sitzungen fuer die Weboberflaeche --------------------------------------
#
# Der Signierschluessel haengt am jeweils gueltigen Zugangstoken. Das hat einen
# erwuenschten Nebeneffekt: Wird der Token erneuert, sind alle offenen
# Sitzungen sofort ungueltig. Genau das will man beim Erneuern.


def _schluessel() -> bytes:
    # Die Kennzeichnung verhindert, dass eine gueltige Signatur je als Token
    # durchgehen koennte.
    return hashlib.sha256(b"sitzung\x00" + zugang_token().encode()).digest()


def _signieren(ablauf: int) -> str:
    return hmac.new(_schluessel(), str(ablauf).encode(), hashlib.sha256).hexdigest()


def sitzung_bauen() -> str:
    ablauf = int(time.time()) + SITZUNGSDAUER
    return f"{ablauf}.{_signieren(ablauf)}"


def sitzung_gueltig(wert: str | None) -> bool:
    if not wert or "." not in wert:
        return False
    ablauf_text, signatur = wert.rsplit(".", 1)
    try:
        ablauf = int(ablauf_text)
    except ValueError:
        return False
    # Erst die Signatur, dann die Frist: sonst koennte man an der Laufzeit
    # ablesen, ob ein geratener Ablaufzeitpunkt "schon mal richtig" war.
    if not hmac.compare_digest(signatur, _signieren(ablauf)):
        return False
    return ablauf > time.time()


# --- Fehlversuche bremsen ---------------------------------------------------
#
# Absichtlich im Arbeitsspeicher und absichtlich schlicht: eine Sperre, die
# einen Neustart ueberlebt, sperrt irgendwann den Besitzer aus. Gegen geduldiges
# Durchprobieren reicht die Verzoegerung, gegen einen entschlossenen Angreifer
# hilft ohnehin nur die Laenge des Tokens.
_FEHLVERSUCHE: dict[str, list[float]] = {}
GRENZE = 5
FENSTER = 300.0


def _absender(anfrage: Request) -> str:
    return anfrage.client.host if anfrage.client else "unbekannt"


def gesperrt(anfrage: Request) -> bool:
    jetzt = time.time()
    versuche = [t for t in _FEHLVERSUCHE.get(_absender(anfrage), []) if jetzt - t < FENSTER]
    _FEHLVERSUCHE[_absender(anfrage)] = versuche
    return len(versuche) >= GRENZE


def fehlversuch(anfrage: Request) -> None:
    _FEHLVERSUCHE.setdefault(_absender(anfrage), []).append(time.time())


def versuche_loeschen(anfrage: Request) -> None:
    _FEHLVERSUCHE.pop(_absender(anfrage), None)


# --- Cloudflare Access ------------------------------------------------------
#
# Kommt die Anfrage durch den Tunnel, hat Access den Benutzer schon geprueft
# und legt den Nachweis als signiertes Merkmal bei. Den nehmen wir an -- dann
# entfaellt die zweite Anmeldemaske fuer jemanden, der gerade eben bei
# Cloudflare seine Kennung gezeigt hat.
#
# Der Unterschied zum frueheren Stand ist wichtig: Der Dienst *verlangt* kein
# Access-Merkmal, er *akzeptiert* eines. Verlangen wuerde jeden anderen Weg
# toeten; blind glauben waere wertlos, weil Kopffelder faelschbar sind.
# Deshalb wird die Signatur gegen Cloudflares oeffentliche Schluessel geprueft,
# dazu Empfaenger (aud) und Aussteller. Ein Kopffeld allein oeffnet nichts.

CF_TEAM = os.environ.get("CF_ACCESS_TEAM", "").strip().rstrip("/")
CF_AUD = os.environ.get("CF_ACCESS_AUD", "").strip()
_jwks: "jwt.PyJWKClient | None" = None


def access_aktiv() -> bool:
    return bool(CF_TEAM and CF_AUD)


def _schluesselsatz() -> "jwt.PyJWKClient":
    global _jwks
    if _jwks is None:
        # PyJWKClient haelt die Schluessel selbst vor; Cloudflare wechselt sie
        # regelmaessig, ein fest eingebauter Schluessel waere also eine Zeitbombe.
        _jwks = jwt.PyJWKClient(f"https://{CF_TEAM}/cdn-cgi/access/certs",
                                cache_keys=True, lifespan=3600)
    return _jwks


def access_benutzer(anfrage: Request) -> str | None:
    """Gibt die von Access bestaetigte Kennung zurueck, sonst None."""
    if not access_aktiv():
        return None
    roh = (anfrage.headers.get("cf-access-jwt-assertion")
           or anfrage.cookies.get("CF_Authorization") or "")
    if not roh:
        return None
    try:
        schluessel = _schluesselsatz().get_signing_key_from_jwt(roh).key
        nutz = jwt.decode(roh, schluessel, algorithms=["RS256"],
                          audience=CF_AUD, issuer=f"https://{CF_TEAM}")
    except Exception:
        # Ein ungueltiges Merkmal ist kein Fehler, sondern einfach kein Nachweis
        # -- der Aufrufer faellt dann auf Token oder Anmeldemaske zurueck.
        return None
    # Dienst-Token (fuer die Erweiterung) tragen keine Mailadresse, sondern nur
    # die Kennung des Tokens. Beides zaehlt als bestaetigt.
    return nutz.get("email") or nutz.get("common_name") or nutz.get("sub") or "access"


# --- Abhaengigkeiten fuer die Endpunkte -------------------------------------


def _bearer(anfrage: Request) -> str:
    schema, _, wert = anfrage.headers.get("authorization", "").partition(" ")
    return wert if schema.lower() == "bearer" else ""


def api_zugang(anfrage: Request) -> None:
    """Inhaltsebene: ZUGANG_TOKEN oder ein gueltiger Access-Nachweis.

    Als FastAPI-Abhaengigkeit eingehaengt, damit kein Endpunkt vergessen werden
    kann, ohne dass es auffaellt.
    """
    if token_stimmt(_bearer(anfrage)) or access_benutzer(anfrage):
        return
    raise HTTPException(401, T("Zugang verweigert","Access denied"), headers={"WWW-Authenticate": "Bearer"})


def verwaltung_zugang(anfrage: Request) -> None:
    """Betriebsebene: NUR VERWALTUNG_TOKEN.

    Der Zugangstoken wird hier bewusst **nicht** akzeptiert. Die Trennung soll
    in beide Richtungen gelten: das NOC sieht keine Nachrichten, und der
    Browser-Token darf nicht am Betrieb drehen.
    """
    if not VERWALTUNG_START and not verwaltung_token():
        raise HTTPException(
            503, T("Verwaltungsschnittstelle nicht eingerichtet (VERWALTUNG_TOKEN fehlt).",
                   "Admin interface not configured (VERWALTUNG_TOKEN missing)."))
    if not _stimmt(_bearer(anfrage), verwaltung_token()):
        raise HTTPException(401, T("Zugang verweigert","Access denied"), headers={"WWW-Authenticate": "Bearer"})


def mensch_oder_erweiterung(anfrage: Request) -> None:
    """Fuer Endpunkte, die beide Seiten bedienen.

    Die Vormerkung wird im Browser geschrieben (Sitzung oder Access) und von
    der Erweiterung abgeholt (Bearer-Token). Beide Wege sind derselbe
    Berechtigungsgrad -- Inhalt.
    """
    if (token_stimmt(_bearer(anfrage)) or access_benutzer(anfrage)
            or sitzung_gueltig(anfrage.cookies.get(KEKS))):
        return
    raise HTTPException(401, T("Zugang verweigert","Access denied"), headers={"WWW-Authenticate": "Bearer"})


def angemeldet(anfrage: Request) -> bool:
    """Weboberflaeche: von Access bestaetigt oder gueltiges Sitzungs-Keks.

    Access zuerst: wer gerade bei Cloudflare seine Kennung gezeigt hat, soll
    nicht gleich noch eine Maske sehen.
    """
    return bool(access_benutzer(anfrage)) or sitzung_gueltig(anfrage.cookies.get(KEKS))


def keks_setzen(antwort, sicher: bool) -> None:
    antwort.set_cookie(
        KEKS, sitzung_bauen(),
        max_age=SITZUNGSDAUER, httponly=True, samesite="lax", secure=sicher, path="/")


def ueber_tls(anfrage: Request) -> bool:
    """Kam die Anfrage ueber HTTPS?

    Der Dienst selbst spricht HTTP; TLS endet bei Traefik bzw. am Tunnel.
    Deshalb zaehlt das weitergereichte Kopffeld. Das ist faelschbar -- es
    entscheidet hier aber nur ueber das ``Secure``-Merkmal des Keks, nicht
    ueber den Zugang.
    """
    return (anfrage.headers.get("x-forwarded-proto", anfrage.url.scheme).split(",")[0].strip()
            == "https")
