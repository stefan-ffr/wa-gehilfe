/* WhatsApp-Entwurfshilfe -- Inhaltsskript
 *
 * Liest den offenen Chat aus dem DOM, holt einen Entwurf vom Dienst und tippt
 * ihn ins Eingabefeld. Gesendet wird NICHTS -- das machst du selbst.
 *
 * Auslösen mit Strg+Umschalt+E (siehe manifest.json).
 */

// --- Die Sollbruchstelle, bewusst an EINER Stelle --------------------------
//
// WhatsApp ändert sein Markup ohne Ankündigung. Wenn etwas nicht mehr geht,
// ist es fast immer hier. Mehrere Kandidaten je Zweck, erster Treffer gewinnt.
const WAHL = {
  eingabefeld: [
    'div[contenteditable="true"][data-tab="10"]',
    'footer div[contenteditable="true"]',
    'div[contenteditable="true"][role="textbox"]',
  ],
  nachrichtenliste: [
    'div[data-testid="conversation-panel-messages"]',
    '#main div[role="application"]',
    '#main .copyable-area',
  ],
  nachricht: ['div.message-in, div.message-out', '[data-id]'],
  textInNachricht: ['span.selectable-text span', 'span.selectable-text', '.copyable-text'],
  chatTitel: ['header span[title]', '#main header span[dir="auto"]'],
};

function finde(kandidaten, wurzel = document) {
  for (const s of kandidaten) {
    const t = wurzel.querySelector(s);
    if (t) return t;
  }
  return null;
}

function findeAlle(kandidaten, wurzel = document) {
  for (const s of kandidaten) {
    const t = wurzel.querySelectorAll(s);
    if (t.length) return Array.from(t);
  }
  return [];
}

// --- Rückmeldung an den Benutzer -------------------------------------------
// Ein Helfer, der heimlich nichts mehr tut, ist schlimmer als keiner.
function melde(text, art = 'info') {
  document.getElementById('wa-entwurf-hinweis')?.remove();
  const farbe = { info: '#0969da', fehler: '#b42318', gut: '#1a7f37' }[art] || '#0969da';
  const el = document.createElement('div');
  el.id = 'wa-entwurf-hinweis';
  el.textContent = text;
  el.style.cssText = `position:fixed;bottom:16px;right:16px;z-index:99999;
    background:#fff;border:1px solid ${farbe};border-left:4px solid ${farbe};
    color:#1f2328;font:13px system-ui,sans-serif;padding:.6rem .8rem;
    border-radius:6px;box-shadow:0 2px 10px rgba(0,0,0,.15);max-width:340px`;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), art === 'fehler' ? 12000 : 5000);
}

// --- Chat auslesen ----------------------------------------------------------

function chatKennung() {
  const t = finde(WAHL.chatTitel);
  return (t?.getAttribute('title') || t?.textContent || 'unbekannt').trim();
}

function letzteNachrichten(anzahl = 25) {
  const liste = finde(WAHL.nachrichtenliste);
  if (!liste) return null; // Unterscheidung wichtig: null = DOM nicht gefunden
  return findeAlle(WAHL.nachricht, liste)
    .slice(-anzahl)
    .map((el) => {
      const text = findeAlle(WAHL.textInNachricht, el).map((s) => s.textContent).join(' ').trim();
      if (!text) return null;
      return { text, von_mir: el.classList.contains('message-out') };
    })
    .filter(Boolean);
}

// --- Entwurf ins Eingabefeld ------------------------------------------------
//
// Das Feld ist ein contenteditable unter React. textContent zu setzen reicht
// NICHT -- React bekommt davon nichts mit, und beim Senden wäre das Feld leer.
// execCommand('insertText') erzeugt echte Eingabe-Ereignisse, die React sieht.
function entwurfEinfuegen(text) {
  const feld = finde(WAHL.eingabefeld);
  if (!feld) return false;
  feld.focus();
  const bereich = document.createRange();
  bereich.selectNodeContents(feld);
  const auswahl = window.getSelection();
  auswahl.removeAllRanges();
  auswahl.addRange(bereich);
  document.execCommand('insertText', false, text);
  return true;
}

// --- Ablauf -----------------------------------------------------------------

let laufendeId = null;      // fuer die Rueckmeldung
let laufenderEntwurf = null;

// Alle Netzanfragen laufen ueber den Hintergrunddienst: aus dem Inhaltsskript
// heraus gelten die CORS-Regeln von web.whatsapp.com, und der Zugangstoken
// soll hier gar nicht erst auftauchen. Siehe hintergrund.js.
function beimDienst(art, daten) {
  return chrome.runtime.sendMessage({ art, daten });
}

async function entwurfHolen() {
  const nachrichten = letzteNachrichten();
  if (nachrichten === null) {
    melde('Nachrichtenliste im DOM nicht gefunden — WhatsApp hat vermutlich sein '
        + 'Markup geändert. Selektoren in inhalt.js (WAHL) prüfen.', 'fehler');
    return;
  }
  if (!nachrichten.length) {
    melde('Keine Nachrichten im offenen Chat gefunden.', 'fehler');
    return;
  }

  melde('Entwurf wird geholt …');
  const { daten, fehler } = await beimDienst('entwurf', { chat: chatKennung(), nachrichten });
  if (fehler) {
    melde(fehler, 'fehler');
    return;
  }

  const { id, entwurf } = daten;
  if (entwurf.trim() === 'KEIN_ENTWURF') {
    melde('Hier ist keine Antwort nötig — kein Entwurf eingefügt.', 'info');
    return;
  }
  if (!entwurfEinfuegen(entwurf)) {
    melde('Eingabefeld nicht gefunden — Selektor in inhalt.js (WAHL.eingabefeld) prüfen.', 'fehler');
    return;
  }
  laufendeId = id;
  laufenderEntwurf = entwurf;
  melde('Entwurf eingefügt. Prüfen, ändern, senden — abgeschickt wird nichts von allein.', 'gut');
}

// --- Lernen: was ist aus dem Entwurf geworden? ------------------------------
//
// Das wertvollste Signal ist nicht "abgelehnt", sondern was stattdessen
// geschrieben wurde. Deshalb wird beim Senden der Feldinhalt mit dem Entwurf
// verglichen.
async function rueckmelden(ergebnis, endfassung) {
  if (laufendeId === null) return;
  const id = laufendeId;
  laufendeId = null;
  laufenderEntwurf = null;
  try {
    await beimDienst('rueckmeldung', { id, ergebnis, endfassung });
  } catch { /* Rueckmeldung ist Beiwerk -- sie darf das Senden nie stoeren */ }
}

document.addEventListener('keydown', (e) => {
  // Entwurf anfordern
  if (e.ctrlKey && e.shiftKey && (e.key === 'E' || e.key === 'e')) {
    e.preventDefault();
    entwurfHolen();
    return;
  }
  // Enter im Eingabefeld = gesendet. Feldinhalt vorher lesen.
  if (e.key === 'Enter' && !e.shiftKey && laufendeId !== null) {
    const feld = finde(WAHL.eingabefeld);
    const jetzt = (feld?.textContent || '').trim();
    if (!jetzt) return;
    rueckmelden(jetzt === laufenderEntwurf.trim() ? 'gesendet' : 'geaendert', jetzt);
  }
}, true);

// --- Vormerkungen abholen ---------------------------------------------------
//
// Im Webinterface kann Text fuer einen Chat hinterlegt werden. Sobald dieser
// Chat hier geoeffnet wird, landet er im Eingabefeld. Der Umweg ist noetig,
// weil nur an dieser Stelle Text ungesendet stehen kann -- ein Dienst kann
// nicht von aussen in ein Eingabefeld schreiben.
async function vormerkungPruefen(chat) {
  const { daten, fehler } = await beimDienst('abholen', { chat });
  if (fehler || !daten || !daten.text) return;
  // Nichts ueberschreiben, was gerade getippt wird -- der eigene Text hat
  // immer Vorrang vor einer Vormerkung.
  const feld = finde(WAHL.eingabefeld);
  if (feld && (feld.textContent || '').trim()) {
    melde('Vormerkung liegt bereit, das Feld ist aber nicht leer.', 'info');
    return;
  }
  if (entwurfEinfuegen(daten.text)) {
    melde('Vorgemerkter Text eingefügt. Prüfen und senden.', 'gut');
  }
}

// Chatwechsel: offener Entwurf gilt als verworfen, und fuer den neuen Chat
// wird nachgesehen, ob etwas bereitliegt.
let letzterChat = chatKennung();
setInterval(() => {
  const jetzt = chatKennung();
  if (jetzt !== letzterChat) {
    if (laufendeId !== null) rueckmelden('verworfen', null);
    letzterChat = jetzt;
    if (jetzt && jetzt !== 'unbekannt') vormerkungPruefen(jetzt);
  }
}, 2000);

// Auch beim ersten Laden einmal nachsehen -- sonst muesste man erst den Chat
// wechseln, damit eine Vormerkung ankommt.
setTimeout(() => {
  const c = chatKennung();
  if (c && c !== 'unbekannt') vormerkungPruefen(c);
}, 3000);

console.log('[WhatsApp-Entwurfshilfe] bereit — Strg+Umschalt+E für einen Entwurf');
