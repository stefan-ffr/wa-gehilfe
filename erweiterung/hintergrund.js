/* Hintergrunddienst -- hier laeuft der gesamte Netzverkehr.
 *
 * Zwei Gruende, warum nicht das Inhaltsskript selbst anfragt:
 *
 *  1. **Es duerfte gar nicht.** Seit Chrome 85 gelten fuer fetch() aus einem
 *     Inhaltsskript die CORS-Regeln der *Seite* (web.whatsapp.com). Eine
 *     Anfrage an den eigenen Dienst waere dort fremder Ursprung und wuerde
 *     abgewiesen. Der Hintergrunddienst faellt unter host_permissions und darf.
 *  2. **Der Token hat auf der Seite nichts zu suchen.** Inhaltsskripte laufen
 *     zwar in einer eigenen Welt, aber je weniger Stellen das Geheimnis kennen,
 *     desto besser. Hier bleibt es im Hintergrund.
 */

const STANDARD = {
  dienst: 'http://localhost:8099',
  profil: 'standard',
  token: '',
  cfKennung: '',
  cfGeheimnis: '',
};

// Sprache nach dem Browser: Deutsch bleibt Vorgabe, alles andere Englisch.
const SPRACHE = (navigator.language || 'de').toLowerCase().startsWith('de') ? 'de' : 'en';
const T = (de, en) => (SPRACHE === 'en' ? en : de);

async function koepfe() {
  const w = await chrome.storage.sync.get(STANDARD);
  const k = { 'Content-Type': 'application/json' };
  if (w.token) k.Authorization = `Bearer ${w.token}`;
  // Cloudflare Access laesst Dienst-Token ohne Anmeldemaske durch. Noetig nur,
  // wenn der Dienst von aussen ueber den Tunnel angesprochen wird -- im eigenen
  // Netz bleiben die Felder leer.
  if (w.cfKennung && w.cfGeheimnis) {
    k['CF-Access-Client-Id'] = w.cfKennung;
    k['CF-Access-Client-Secret'] = w.cfGeheimnis;
  }
  return { w, k };
}

async function anfragen(pfad, rumpf) {
  const { w, k } = await koepfe();
  if (!w.token) {
    return { fehler: T('Kein Zugangstoken hinterlegt — in den Einstellungen der Erweiterung eintragen.',
                       'No access token stored — set it in the extension settings.') };
  }
  let antwort;
  try {
    antwort = await fetch(`${w.dienst}${pfad}`, {
      method: 'POST',
      headers: k,
      body: JSON.stringify(rumpf),
    });
  } catch (e) {
    return { fehler: `${T('Dienst nicht erreichbar', 'Service unreachable')}: ${e.message}` };
  }
  if (antwort.status === 401 || antwort.status === 403) {
    return { fehler: T('Zugang verweigert — Token in den Einstellungen pruefen.',
                       'Access denied — check the token in the settings.') };
  }
  if (!antwort.ok) {
    return { fehler: `${T('Dienst antwortet', 'Service responds')} ${antwort.status}: ${(await antwort.text()).slice(0, 200)}` };
  }
  try {
    return { daten: await antwort.json() };
  } catch {
    return { fehler: T('Antwort des Dienstes war kein JSON.', 'The service reply was not JSON.') };
  }
}

async function abholen(chat) {
  const { w, k } = await koepfe();
  if (!w.token) return { fehler: T('Kein Zugangstoken hinterlegt.', 'No access token stored.') };
  try {
    const u = `${w.dienst}/abholen?chat=${encodeURIComponent(chat)}`
            + `&profil=${encodeURIComponent(w.profil)}`;
    const a = await fetch(u, { headers: k });
    if (!a.ok) return { fehler: `Dienst antwortet ${a.status}` };
    return { daten: await a.json() };
  } catch (e) {
    return { fehler: `${T('Dienst nicht erreichbar', 'Service unreachable')}: ${e.message}` };
  }
}

chrome.runtime.onMessage.addListener((nachricht, _absender, antworten) => {
  (async () => {
    if (nachricht.art === 'abholen') {
      antworten(await abholen(nachricht.daten.chat));
    } else if (nachricht.art === 'entwurf') {
      const { profil } = await chrome.storage.sync.get(STANDARD);
      antworten(await anfragen('/entwurf', { profil, ...nachricht.daten }));
    } else if (nachricht.art === 'rueckmeldung') {
      antworten(await anfragen('/rueckmeldung', nachricht.daten));
    } else {
      antworten({ fehler: `unbekannte Anfrage: ${nachricht.art}` });
    }
  })();
  return true; // Kanal offen halten -- sonst kommt die Antwort zu spaet.
});
