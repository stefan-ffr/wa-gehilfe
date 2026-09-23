/* Koppler -- eine echte WhatsApp-Web-Sitzung, ferngesteuert.
 *
 * Warum ueberhaupt: Die Erweiterung kann nur, was im Browserfenster gerade
 * offen ist. Dieser Dienst haelt eine eigene, dauerhaft gekoppelte Sitzung.
 * Damit laesst sich der ganze Verlauf lesen -- nicht nur der sichtbare
 * Ausschnitt -- und ein Entwurf setzen, ohne dass irgendwo ein Fenster offen
 * sein muss.
 *
 * ACHTUNG -- Entwuerfe kommen NICHT auf dem Telefon an.
 *
 * Hier stand, der Entwurf wandere "ueber die Geraetesynchronisierung aufs
 * Telefon". Das war von Anfang an eine unbelegte Annahme und ist falsch:
 * WhatsApps Entwurfsfunktion arbeitet nicht mit Begleitgeraeten, Entwuerfe
 * werden zwischen Telefon, Web und Desktop nicht abgeglichen.
 *
 * Am 22.09.2026 bis zum Ende durchgemessen, in drei Anlaeufen:
 *
 *   1. ins sichtbare Eingabefeld tippen -- in einer kopfgesteuerten Sitzung
 *      wird kein Chat aktiv, das Feld gehoert irgendeinem anderen. Der
 *      Entwurf waere beim Falschen gelandet.
 *   2. chat.draftMessage setzen -- steht danach im Modell, aendert aber nur
 *      den Speicher.
 *   3. WAWebUpdateDraftMessageChatAction.updateDraftMessageChat aufrufen,
 *      also WhatsApps eigene Aktion. Laeuft fehlerfrei durch, der Wert steht
 *      danach im Modell -- und auf dem Telefon bleibt das Feld leer.
 *
 * Der Entwurf existiert also wirklich, nur in DIESER Sitzung, wo ihn
 * niemand sieht. Wer hier weitermacht, spart sich die drei Anlaeufe: das
 * Problem liegt nicht am Setzen.
 *
 * Was bleibt: den Vorschlag dorthin bringen, wo er gelesen wird -- in die
 * Oberflaeche des Dienstes, oder als Nachricht an den eigenen Chat.
 *
 * Preis, offen benannt: Das ist ein inoffizieller Client. Dafuer werden
 * Nummern gesperrt. Ausserdem belegt die Sitzung einen der vier Plaetze fuer
 * verbundene Geraete.
 */
'use strict';

const http = require('http');
const { Client, LocalAuth } = require('whatsapp-web.js');

const PORT = Number(process.env.KOPPLER_PORT || 8098);
const TOKEN = (process.env.KOPPLER_TOKEN || '').trim();
const DATEN = process.env.KOPPLER_DATEN || '/daten/koppler';
// Bindeadresse: Vorgabe nur die Schleife. In docker-compose steht der
// Koppler in einem internen Netz und setzt KOPPLER_BIND=0.0.0.0, damit der
// App-Container ihn erreicht -- der Token schuetzt ihn, nicht die Adresse.
const BIND = process.env.KOPPLER_BIND || '127.0.0.1';

// Wie viele Chats /chats hoechstens zurueckgibt. Standen fest auf 100, und
// genau 100 kamen am 21.09.2026 zurueck -- bei einer harten Grenze heisst das
// meist, dass hinten etwas fehlt. Die Antwort nennt seither die Gesamtzahl in
// der Kopfzeile X-Chats-Gesamt, damit sich das nachsehen laesst.
const CHAT_GRENZE = Number(process.env.KOPPLER_CHAT_GRENZE || 300);

if (!TOKEN || TOKEN.length < 16) {
  console.error('KOPPLER_TOKEN fehlt oder ist zu kurz (mindestens 16 Zeichen).');
  process.exit(1);
}

// Zustand nach aussen. `qr` ist der rohe Kopplungstext; das Bild daraus baut
// die Weboberflaeche, damit hier keine Bildbibliothek noetig ist.
let zustand = { status: 'startet', qr: null, seit: Date.now(), nummer: null };

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: DATEN }),
  puppeteer: {
    executablePath: process.env.CHROMIUM || '/usr/bin/chromium',
    // --no-sandbox ist im Container noetig; ohne eigenen Benutzernamensraum
    // startet Chromium sonst gar nicht.
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
           '--disable-gpu'],
  },
});

client.on('qr', (qr) => { zustand = { status: 'qr', qr, seit: Date.now(), nummer: null }; });
client.on('authenticated', () => { zustand = { ...zustand, status: 'angemeldet', qr: null }; });
client.on('ready', async () => {
  let nummer = null;
  try { nummer = client.info?.wid?.user || null; } catch { /* egal */ }
  zustand = { status: 'bereit', qr: null, seit: Date.now(), nummer };
  console.log('bereit', nummer ? `als ${nummer}` : '');
});
client.on('disconnected', (grund) => {
  zustand = { status: 'getrennt', qr: null, seit: Date.now(), nummer: null };
  console.log('getrennt:', grund);
});
client.on('auth_failure', (m) => {
  zustand = { status: 'anmeldung_fehlgeschlagen', qr: null, seit: Date.now(), nummer: null };
  console.error('Anmeldung fehlgeschlagen:', m);
});

client.initialize().catch((e) => {
  zustand = { status: 'fehler', qr: null, seit: Date.now(), nummer: null };
  console.error('Start fehlgeschlagen:', e.message);
});

// --- Anzeigename eines Chats ------------------------------------------------
//
// Wer weder im Adressbuch steht noch einen selbstgesetzten Namen hat, kommt
// als Nummer an. Der Dienst ueberspringt Eintraege ohne Namen, also fielen
// solche Chats frueher ganz hinten runter -- weder in der Auswahlliste noch
// in der Wissensrunde tauchten sie auf.
//
// Der Kontakt wird nur fuer Chats geholt, deren Name nach einer blossen
// Nummer aussieht: getContact() ist ein Aufruf in die Seite hinein, und den
// fuer alle dreihundert zu machen waere Verschwendung.

// Sieht dieser Name nach einer blossen Telefonnummer aus?
//
// Der Grund, warum es diese Pruefung braucht: chat.name ist bei unbekannten
// Nummern NICHT leer. WhatsApp setzt dort die formatierte Nummer ein, etwa
// "+41 77 521 02 41". Wer nur auf einen leeren Namen prueft, hoert genau
// dort auf zu suchen, wo es interessant wird.
function nurNummer(s) {
  return /^\+?[\d\s/().-]{6,}$/.test((s || '').trim());
}

async function anzeigename(chat) {
  const name = (chat.name || '').trim();
  if (name && !nurNummer(name)) return name;

  if (!chat.isGroup) {
    try {
      const k = await chat.getContact();
      // Reihenfolge: jeder Name schlaegt die Nummer.
      //
      // Hier stand die Nummer zuerst, und contact.name wurde ueberhaupt nicht
      // abgefragt -- ein Fehler, der am 21.09.2026 so aussah, als koenne
      // WhatsApp die Telefonbuchkontakte nicht liefern. Kann es doch: was das
      // gekoppelte Telefon synchronisiert hat, steht in contact.name. Dass
      // chat.name leer ist, heisst seit der LID-Umstellung nicht mehr, dass
      // kein Adressbucheintrag existiert.
      //
      //   name         Adressbuch des Telefons
      //   pushname     was die Person selbst gesetzt hat
      //   verifiedName Geschaeftskonten
      for (const k2 of [k?.name, k?.pushname, k?.verifiedName]) {
        const t = (k2 || '').trim();
        if (t) return t;
      }
      // contact.number NICHT vor chat.name: bei LID-Kontakten liefert es die
      // LID, nicht die Telefonnummer. Am 21.09.2026 gemessen --
      //
      //   chat_name '+41 77 521 02 41'   contact.number 57076256624815
      //
      // -- und genau dieser Griff hat die Anzeige kurzzeitig von lesbaren
      // Nummern auf "+2023063838786" umgestellt.
      if (name) return name;

      const nummer = (k?.number || '').trim();
      if (nummer && !/^\d{15,}$/.test(nummer)) {
        return '+' + nummer.replace(/^\+/, '');
      }
    } catch { /* dann eben die Kennung */ }
  }

  if (name) return name;

  // Letzter Rueckfall, auch fuer namenlose Gruppen: der Teil vor dem @.
  // Haesslich, aber eindeutig -- und besser, als den Chat verschwinden zu
  // lassen.
  return chat.id?.user || chat.id?._serialized || 'ohne Namen';
}

// --- Entwurf setzen ---------------------------------------------------------
//
// Absichtlich nicht ueber sendMessage: gesendet wird nichts. Der Entwurf
// wird am Chatmodell gesetzt, nicht in der Oberflaeche.
//
// Der Weg ueber das Eingabefeld war ein Irrweg, und zwar ein gefaehrlicher.
// Am 22.09.2026 gemessen (/diagnose auf Paola Meixueiro):
//
//   VOR dem Oeffnen:  aktiver Chat None, Feld None
//   nach 200 ms:      aktiv=None, Feld='Du bist unmoeglich'
//   nach 7700 ms:     aktiv=None, Feld='Du bist unmoeglich'
//
// Der gewuenschte Chat wird nie aktiv, und das sichtbare Feld gehoert einem
// anderen. Laenger warten half nicht -- der Wert stand schon bei der ersten
// Probe da und aenderte sich nie. In einer kopfgesteuerten Sitzung gibt es
// schlicht keine Oberflaeche, die man umschalten koennte.
//
// Dieselbe Diagnose nannte den richtigen Weg: das Chatmodell fuehrt
// __x_draftMessage und __x_draftMessageSortTs. Der Entwurf gehoert dorthin.
// Er haengt dann am Chat statt an einem Fenster, kann nicht beim Falschen
// landen, und WhatsApp synchronisiert ihn von dort aufs Telefon.
async function entwurfAmModell(chatId, text, nurWennLeer) {
  return client.pupPage.evaluate(async (id, t, nurLeer) => {
    const s = window.require('WAWebCollections');
    const chat = s.Chat.get(id) || (await s.Chat.find(id));
    if (!chat) return { ok: false, grund: 'Chat nicht gefunden' };

    const vorhanden = String(chat.draftMessage ?? chat.__x_draftMessage ?? '').trim();
    if (nurLeer && vorhanden) {
      return { ok: false, grund: 'Feld nicht leer -- nichts angetastet',
               vorhanden };
    }
    // Das Modell zu setzen genuegt NICHT. Am 22.09.2026 gemessen: der Wert
    // stand danach im Modell und liess sich zurueckzulesen, auf dem Telefon
    // kam aber nichts an. Es aendert nur den Speicher, nicht den Zustand.
    //
    // Die Aktion, die WhatsApp beim Tippen selbst aufruft, heisst
    // WAWebUpdateDraftMessageChatAction -- gefunden ueber __debug.modulesMap.
    // Sie schreibt den Entwurf fort und loest die Synchronisierung aus.
    //
    // Welche Funktion das Modul anbietet, wird hier nachgesehen statt
    // geraten: beim Thema Entwurf lag ich schon zweimal daneben. Die
    // Antwort nennt Fundstelle und Ausgang, damit ein Fehlschlag erklaerbar
    // bleibt, statt nur "ging nicht" zu heissen.
    const bericht = { versucht: null, exporte: [], fehler: null };
    try {
      const modul = window.require('WAWebUpdateDraftMessageChatAction');
      bericht.exporte = Object.keys(modul);
      const treffer = Object.entries(modul)
        .find(([k, v]) => typeof v === 'function' && /draft/i.test(k));
      if (treffer) {
        bericht.versucht = treffer[0];
        await treffer[1](chat, t);
      } else {
        bericht.fehler = 'keine passende Funktion im Modul';
      }
    } catch (e) {
      bericht.fehler = e.message;
    }

    // Unabhaengig davon auch am Modell setzen: schadet nicht, und wenn die
    // Aktion den Wert schon gesetzt hat, ist es dieselbe Zuweisung.
    if (String(chat.draftMessage ?? '') !== t) {
      chat.draftMessage = t;
      chat.draftMessageSortTs = Math.floor(Date.now() / 1000);
    }

    const danach = String(chat.draftMessage ?? chat.__x_draftMessage ?? '');
    return { ok: danach === t, gesetzt: danach, vorher: vorhanden, aktion: bericht };
  }, chatId, text, Boolean(nurWennLeer));
}

// --- HTTP -------------------------------------------------------------------
//
// Nur auf der Schleife, nur fuer den Entwurfsdienst. Der Token ist trotzdem da:
// auf demselben Rechner laufen andere Container, und "nur lokal" ist keine
// Berechtigung.

function rumpfLesen(req) {
  return new Promise((loese, fehler) => {
    let d = '';
    req.on('data', (s) => { d += s; if (d.length > 1e6) req.destroy(); });
    req.on('end', () => { try { loese(d ? JSON.parse(d) : {}); } catch (e) { fehler(e); } });
  });
}

function antwort(res, code, daten) {
  res.writeHead(code, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(daten));
}

const server = http.createServer(async (req, res) => {
  const kopf = req.headers.authorization || '';
  if (kopf !== `Bearer ${TOKEN}`) return antwort(res, 401, { fehler: 'Zugang verweigert' });

  const u = new URL(req.url, 'http://localhost');
  try {
    if (u.pathname === '/zustand') return antwort(res, 200, zustand);

    if (u.pathname === '/chats') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const chats = await client.getChats();
      const ausschnitt = chats.slice(0, CHAT_GRENZE);
      const liste = [];
      for (const c of ausschnitt) {
        // letzte_von_mir und letzte_zeit: ohne sie muesste jeder Aufrufer,
        // der wissen will, ob eine Antwort aussteht, den Verlauf JE CHAT
        // einzeln holen. Beides steht hier ohnehin schon bereit.
        const l = c.lastMessage;
        liste.push({
          id: c.id._serialized,
          name: await anzeigename(c),
          gruppe: c.isGroup,
          ungelesen: c.unreadCount,
          letzte_von_mir: l ? Boolean(l.fromMe) : null,
          letzte_zeit: l ? l.timestamp : null,
          archiviert: Boolean(c.archived),
        });
      }
      // gesamt neben geliefert: sonst sieht eine abgeschnittene Liste genauso
      // aus wie eine vollstaendige, und niemand merkt, dass hinten etwas fehlt.
      res.setHeader('X-Chats-Gesamt', String(chats.length));
      return antwort(res, 200, liste);
    }

    // Diagnose: woher kennt WhatsApp Web einen Namen, und welches Feld
    // traegt ihn? Seit der LID-Umstellung ist die Chat-Kennung <lid>@lid,
    // und getContact() liefert den LID-Kontakt. Der Adressbuchname koennte
    // am Nummern-Kontakt <nummer>@c.us haengen -- diese Auskunft zeigt
    // beide nebeneinander, statt es zu vermuten.
    if (u.pathname === '/kontakt') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const chat = await client.getChatById(u.searchParams.get('chat'));
      // Auch _data: die Bibliothek reicht nur eine Auswahl durch. Namentlich
      // formattedName fehlt -- und genau damit beschriftet WhatsApp Web seine
      // Oberflaeche. Statt weiter Felder zu raten, kommen hier alle mit, die
      // wie ein Name aussehen.
      const felder = (k) => {
        if (!k) return null;
        const roh = k._data || {};
        const namensfelder = {};
        for (const [s, w] of Object.entries(roh)) {
          if (/name/i.test(s) && typeof w === 'string' && w) namensfelder[s] = w;
        }
        return {
          id: k.id?._serialized, name: k.name, pushname: k.pushname,
          verifiedName: k.verifiedName, shortName: k.shortName,
          number: k.number, isMyContact: k.isMyContact,
          roh_namensfelder: namensfelder,
          roh_schluessel: Object.keys(roh).sort(),
        };
      };

      const ueberLid = await chat.getContact().catch((e) => ({ fehler: e.message }));
      let ueberNummer = null;
      const nummer = (ueberLid && ueberLid.number || '').replace(/\D/g, '');
      if (nummer) {
        ueberNummer = await client.getContactById(`${nummer}@c.us`)
          .catch((e) => ({ fehler: e.message }));
      }
      // Laesst sich aus der LID ueberhaupt eine Telefonnummer gewinnen?
      // WhatsApp hat dafuer WAWebLidMigrationUtils.toPn. Wenn das etwas
      // liefert, waere die Nummer als Schluessel moeglich; wenn nicht,
      // bleibt die LID der einzige stabile Bezug.
      const perToPn = await client.pupPage.evaluate((id) => {
        try {
          const wid = window.require('WAWebWidFactory').createWidFromWidLike(id);
          const pn = window.require('WAWebLidMigrationUtils').toPn(wid);
          return pn ? (pn._serialized || String(pn)) : null;
        } catch (e) { return 'FEHLER: ' + e.message; }
      }, chat.id?._serialized).catch((e) => 'FEHLER: ' + e.message);

      return antwort(res, 200, {
        chat_name: chat.name,
        chat_id: chat.id?._serialized,
        ueber_lid: felder(ueberLid) || ueberLid,
        nummer,
        ueber_nummer: felder(ueberNummer) || ueberNummer,
        per_to_pn: perToPn,
      });
    }

    // Diagnose: WELCHER Chat ist nach dem Oeffnen tatsaechlich aktiv, und
    // was steht in seinem Eingabefeld?
    //
    // Anlass am 22.09.2026: der Koppler meldete "Feld nicht leer" fuer einen
    // Chat, in dem nachweislich nichts stand. Dann las er das Feld eines
    // anderen -- und haette bei leerem Feld auch dorthin geschrieben. Ein
    // Entwurf im falschen Chat ist schlimmer als gar keiner.
    if (u.pathname === '/diagnose') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const ziel = u.searchParams.get('chat');
      const d = await client.pupPage.evaluate(async (id) => {
        const feldText = () => {
          for (const s of ['div[contenteditable="true"][data-tab="10"]',
                           'footer div[contenteditable="true"]',
                           'div[contenteditable="true"][role="textbox"]']) {
            const f = document.querySelector(s);
            if (f) return { wahl: s, text: (f.textContent || '') };
          }
          return { wahl: null, text: null };
        };
        const aktiv = () => {
          try {
            const c = window.require('WAWebCmd').Cmd.activeChat;
            return c ? (c.id?._serialized || String(c.id)) : null;
          } catch (e) { return 'FEHLER: ' + e.message; }
        };

        const vorher = { aktiv: aktiv(), feld: feldText() };
        const sammlungen = window.require('WAWebCollections');
        const chat = sammlungen.Chat.get(id) || (await sammlungen.Chat.find(id));
        if (!chat) return { vorher, fehler: 'Chat nicht gefunden' };

        await window.require('WAWebCmd').Cmd.openChatAt({ chat });
        const proben = [];
        for (const ms of [200, 500, 1000, 2000, 4000]) {
          await new Promise((r) => setTimeout(r, ms));
          proben.push({ nach_ms: ms, aktiv: aktiv(), feld: feldText() });
        }
        // Gibt es im Chatmodell ein Entwurfsfeld? Dann liesse sich der
        // Entwurf setzen, ohne die Oberflaeche zu bemuehen.
        const entwurfsfelder = Object.keys(chat).filter((s) => /draft/i.test(s));
        return { vorher, ziel: id, proben, entwurfsfelder };
      }, ziel).catch((e) => ({ fehler: e.message }));
      return antwort(res, 200, d);
    }

    // Diagnose: welche Module und Funktionen gibt es zum Thema Entwurf?
    //
    // Am 22.09.2026: chat.draftMessage laesst sich setzen und zurueckzulesen,
    // auf dem Telefon kommt aber nichts an. Das Setzen allein loest also
    // keine Speicherung aus -- es braucht die Aktion, die WhatsApp selbst
    // dafuer benutzt. window.require ist WhatsApps eigenes, nicht das der
    // Bibliothek; die Modulliste haengt bei Metro-Bundles an __debug.
    if (u.pathname === '/module') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const suche = (u.searchParams.get('suche') || 'draft').toLowerCase();
      const d = await client.pupPage.evaluate((s) => {
        const raus = { suche: s, module: [], cmd: [], chat: [], quelle: null };
        try {
          const karte = window.require('__debug').modulesMap;
          raus.quelle = '__debug.modulesMap';
          raus.module = Object.keys(karte).filter((n) => n.toLowerCase().includes(s)).slice(0, 60);
        } catch (e) { raus.quelle = 'FEHLER: ' + e.message; }
        try {
          const cmd = window.require('WAWebCmd').Cmd;
          const namen = new Set();
          for (let o = cmd; o && o !== Object.prototype; o = Object.getPrototypeOf(o)) {
            Object.getOwnPropertyNames(o).forEach((n) => namen.add(n));
          }
          raus.cmd = [...namen].filter((n) => n.toLowerCase().includes(s));
        } catch (e) { raus.cmd = ['FEHLER: ' + e.message]; }
        try {
          const c = window.require('WAWebCollections').Chat.getModelsArray()[0];
          const namen = new Set();
          for (let o = c; o && o !== Object.prototype; o = Object.getPrototypeOf(o)) {
            Object.getOwnPropertyNames(o).forEach((n) => namen.add(n));
          }
          raus.chat = [...namen].filter((n) => n.toLowerCase().includes(s));
        } catch (e) { raus.chat = ['FEHLER: ' + e.message]; }
        return raus;
      }, suche).catch((e) => ({ fehler: e.message }));
      return antwort(res, 200, d);
    }

    if (u.pathname === '/nachrichten') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const chat = await client.getChatById(u.searchParams.get('chat'));
      const n = await chat.fetchMessages({ limit: Number(u.searchParams.get('anzahl') || 25) });
      // von: wer die Nachricht geschrieben hat. In Gruppen steht das in
      // author, im Einzelchat in from. Ohne dieses Feld laesst sich eine
      // Aussage aus einer Gruppe nur ueber den NAMEN einer Person zuordnen
      // -- und zwei Oliver in zwei Gruppen verschmelzen dabei.
      // typ und hat_medien: ohne sie sieht eine Sprachnachricht genauso aus
      // wie eine leere Zeile. Der Dienst ueberspringt alles ohne Text, und
      // 15 Chats meldeten deshalb "nur Nachrichten ohne Text" -- dort wurde
      // gesprochen statt geschrieben. Erst mit dem Typ laesst sich sagen,
      // wie gross diese Luecke ueberhaupt ist.
      return antwort(res, 200, n.map((m) => ({
        // id: ohne sie laesst sich eine einzelne Nachricht spaeter nicht
        // wieder ansprechen -- etwa um ihre Sprachaufnahme zu holen.
        id: m.id?._serialized || null,
        text: m.body,
        von_mir: m.fromMe,
        zeit: m.timestamp,
        von: m.author || m.from || null,
        typ: m.type || null,
        hat_medien: Boolean(m.hasMedia),
        hat_zitat: Boolean(m.hasQuotedMsg),
        // Reaktionen kommen als Rohfeld mit: getReactions() waere ein
        // eigener Aufruf je Nachricht, und bei vierzig Nachrichten sind das
        // vierzig Aufrufe in die Seite hinein.
        reaktionen: (m._data?.reactions || [])
          .map((r) => r.aggregateEmoji || r.text).filter(Boolean),
      })));
    }

    if (u.pathname === '/entwurf' && req.method === 'POST') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const { chat, text, nur_wenn_leer } = await rumpfLesen(req);
      if (!chat || !text) return antwort(res, 400, { fehler: 'chat und text noetig' });
      // Am Modell, nicht in der Oberflaeche -- Begruendung bei entwurfAmModell.
      const r = await entwurfAmModell(chat, text, Boolean(nur_wenn_leer));
      // 409 und nicht 502, wenn das Feld belegt war: das ist kein Fehlschlag,
      // sondern die Weigerung, etwas zu ueberschreiben. Der Aufrufer soll das
      // unterscheiden koennen, ohne im Text zu suchen.
      const code = r.ok ? 200 : (r.grund || '').startsWith('Feld nicht leer') ? 409 : 502;
      return antwort(res, code, r);
    }

    // Die Medien einer einzelnen Nachricht holen -- fuer die Abschrift von
    // Sprachnachrichten.
    //
    // Nur auf Anforderung, nie im Vorbeigehen: eine Sprachnachricht von einer
    // Minute sind einige hundert Kilobyte, und beim Durchblaettern eines
    // Verlaufs will die niemand alle herunterladen.
    if (u.pathname === '/medien') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const kennung = u.searchParams.get('id');
      if (!kennung) return antwort(res, 400, { fehler: 'id noetig' });

      const treffer = await client.getMessageById(kennung);
      if (!treffer) return antwort(res, 404, { fehler: 'Nachricht nicht gefunden' });
      if (!treffer.hasMedia) return antwort(res, 409, { fehler: 'keine Medien' });

      const medien = await treffer.downloadMedia();
      if (!medien || !medien.data) {
        // Kommt vor: fuer @lid-Kontakte ist der Medienabruf beim Projekt als
        // offener Fehler gemeldet (#201856, #201830). Lieber ehrlich 502 als
        // eine leere Datei, die den Aufrufer raten laesst.
        return antwort(res, 502, { fehler: 'Medien nicht abrufbar' });
      }
      return antwort(res, 200, {
        typ: treffer.type,
        mimetyp: medien.mimetype,
        dauer: treffer.duration || null,
        daten: medien.data,
      });
    }

    // Profilbild eines Chats. Eigener Endpunkt statt eines Feldes in
    // /chats: getProfilePicUrl ist ein Aufruf in die Seite hinein, und den
    // fuer dreihundert Chats bei jedem Listenaufruf zu machen waere
    // Verschwendung. So holt der Browser nur, was er gerade zeigt.
    if (u.pathname === '/profilbild') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const kennung = u.searchParams.get('chat');
      if (!kennung) return antwort(res, 400, { fehler: 'chat noetig' });
      const adresse = await client.getProfilePicUrl(kennung).catch(() => null);
      if (!adresse) return antwort(res, 404, { fehler: 'kein Bild' });
      return antwort(res, 200, { url: adresse });
    }

    // Als gelesen markieren. Schickt nichts an den Gespraechspartner --
    // sendSeen setzt nur den eigenen Zaehler zurueck und die blauen Haken,
    // falls der andere sie eingeschaltet hat.
    if (u.pathname === '/gelesen' && req.method === 'POST') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const { chat } = await rumpfLesen(req);
      if (!chat) return antwort(res, 400, { fehler: 'chat noetig' });
      const c = await client.getChatById(chat);
      await c.sendSeen();
      return antwort(res, 200, { ok: true });
    }

    // Senden. Der einzige Endpunkt, der etwas nach draussen schickt.
    //
    // Bis zum 22.09.2026 gab es ihn bewusst nicht: Lesen und Entwerfen sieht
    // von aussen aus wie ein gekoppeltes Geraet, Senden ueber einen
    // Fremdclient ist das, wofuer Nummern gesperrt werden. Er kam erst dazu,
    // als klar war, dass Entwuerfe das Telefon nicht erreichen und der Dienst
    // deshalb selbst zum Client werden muss.
    //
    // Er sendet NIE von selbst. Aufgerufen wird er ausschliesslich, wenn ein
    // Mensch in der Oberflaeche auf Senden drueckt -- keine Schleife, kein
    // Zeitplan, kein Automatismus ruehrt ihn an.
    if (u.pathname === '/senden' && req.method === 'POST') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const { chat, text, zitat_id: rumpfZitat } = await rumpfLesen(req);
      if (!chat || !text || !String(text).trim()) {
        return antwort(res, 400, { fehler: 'chat und text noetig' });
      }
      if (String(text).length > 4000) {
        return antwort(res, 400, { fehler: 'Text zu lang (max. 4000 Zeichen)' });
      }
      const m = await client.sendMessage(chat, String(text),
        rumpfZitat ? { quotedMessageId: rumpfZitat } : {});
      console.log('gesendet an', chat, '-', String(text).length, 'Zeichen',
        rumpfZitat ? '(mit Zitat)' : '');
      return antwort(res, 200, {
        ok: true,
        id: m?.id?._serialized || null,
        zeit: m?.timestamp || null,
      });
    }

    // Medien senden. Wie /senden ein Weg nach draussen -- dieselbe Regel:
    // nur auf ausdruecklichen Knopfdruck.
    if (u.pathname === '/senden-medien' && req.method === 'POST') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const { chat, daten, mimetyp, dateiname, text } = await rumpfLesen(req);
      if (!chat || !daten) return antwort(res, 400, { fehler: 'chat und daten noetig' });
      const { MessageMedia } = require('whatsapp-web.js');
      const anhang = new MessageMedia(mimetyp || 'application/octet-stream',
                                      daten, dateiname || 'datei');
      const m = await client.sendMessage(chat, anhang,
        text ? { caption: String(text) } : {});
      console.log('Anhang gesendet an', chat, '-', dateiname);
      return antwort(res, 200, { ok: true, id: m?.id?._serialized || null });
    }

    // Auf eine Nachricht reagieren. Ein leerer Text nimmt die Reaktion weg
    // -- so sieht es die Bibliothek vor, und so kommt man ohne zweiten
    // Endpunkt aus.
    if (u.pathname === '/reaktion' && req.method === 'POST') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const { id, emoji } = await rumpfLesen(req);
      if (!id) return antwort(res, 400, { fehler: 'id noetig' });
      const m = await client.getMessageById(id);
      if (!m) return antwort(res, 404, { fehler: 'Nachricht nicht gefunden' });
      await m.react(String(emoji || ''));
      return antwort(res, 200, { ok: true });
    }

    // Eigene Nachricht loeschen oder aendern.
    if (u.pathname === '/nachricht' && req.method === 'POST') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const { id, was, text } = await rumpfLesen(req);
      if (!id || !was) return antwort(res, 400, { fehler: 'id und was noetig' });
      const m = await client.getMessageById(id);
      if (!m) return antwort(res, 404, { fehler: 'Nachricht nicht gefunden' });
      if (!m.fromMe) return antwort(res, 403, { fehler: 'nur eigene Nachrichten' });
      if (was === 'loeschen') {
        // true = fuer alle. Eine Nachricht nur bei sich zu loeschen waere
        // aus der Ferne sinnlos: das Telefon zeigt sie weiter.
        await m.delete(true);
      } else if (was === 'bearbeiten') {
        if (!text || !String(text).trim()) {
          return antwort(res, 400, { fehler: 'text noetig' });
        }
        await m.edit(String(text));
      } else {
        return antwort(res, 400, { fehler: 'was: loeschen oder bearbeiten' });
      }
      return antwort(res, 200, { ok: true });
    }

    // Chat archivieren oder zurueckholen.
    if (u.pathname === '/archiv' && req.method === 'POST') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const { chat, zurueck } = await rumpfLesen(req);
      if (!chat) return antwort(res, 400, { fehler: 'chat noetig' });
      if (zurueck) await client.unarchiveChat(chat);
      else await client.archiveChat(chat);
      return antwort(res, 200, { ok: true });
    }

    // Suche ueber alle Verlaeufe, wahlweise auf einen Chat begrenzt.
    if (u.pathname === '/suche') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const frage = u.searchParams.get('q');
      if (!frage) return antwort(res, 400, { fehler: 'q noetig' });
      const nur = u.searchParams.get('chat');
      const treffer = await client.searchMessages(frage, {
        limit: 40, ...(nur ? { chatId: nur } : {}),
      });
      return antwort(res, 200, treffer.map((m) => ({
        id: m.id?._serialized || null,
        chat: m.id?.remote || null,
        text: m.body,
        von_mir: m.fromMe,
        zeit: m.timestamp,
        typ: m.type || null,
      })));
    }

    // Einen Termin als WhatsApp-Ereignis in den Chat stellen.
    //
    // Das ist eine NACHRICHT, keine stille Notiz -- alle im Chat sehen sie
    // und koennen zu- oder absagen. Deshalb gilt hier dasselbe wie fuer
    // /senden: nur auf ausdruecklichen Knopfdruck, nie aus einer Schleife.
    //
    // WhatsApp-Ereignisse gibt es nicht in jedem Chat; scheitert es, kommt
    // die Meldung der Bibliothek unveraendert zurueck, statt sie zu
    // verschlucken.
    if (u.pathname === '/termin' && req.method === 'POST') {
      if (zustand.status !== 'bereit') return antwort(res, 409, { fehler: 'nicht gekoppelt' });
      const { chat, name, beginn, ende, ort, beschreibung } = await rumpfLesen(req);
      if (!chat || !name || !beginn) {
        return antwort(res, 400, { fehler: 'chat, name und beginn noetig' });
      }
      const { ScheduledEvent } = require('whatsapp-web.js');
      const ereignis = new ScheduledEvent(String(name), new Date(Number(beginn) * 1000), {
        description: beschreibung || undefined,
        endTime: ende ? new Date(Number(ende) * 1000) : undefined,
        location: ort || undefined,
        callType: 'none',
      });
      const m = await client.sendMessage(chat, ereignis);
      console.log('Termin gestellt in', chat, '-', name);
      return antwort(res, 200, { ok: true, id: m?.id?._serialized || null });
    }

    if (u.pathname === '/abmelden' && req.method === 'POST') {
      await client.logout().catch(() => {});
      zustand = { status: 'getrennt', qr: null, seit: Date.now(), nummer: null };
      return antwort(res, 200, { status: 'abgemeldet' });
    }

    return antwort(res, 404, { fehler: 'unbekannt' });
  } catch (e) {
    return antwort(res, 500, { fehler: e.message });
  }
});

server.listen(PORT, BIND, () => console.log(`Koppler auf ${BIND}:${PORT}`));
